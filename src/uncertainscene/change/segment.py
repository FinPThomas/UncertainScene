"""Every object in the scene: SAM 2 masks tracked through each recording, lifted to 3D.

1. SAM 2 cuts frames into object masks at full frame resolution. Main path (run 3 showed
   that matching single-frame masks in 3D splits one object into 2-9 pieces): `sam_tracks`
   seeds objects with the automatic mask generator on a few discovery frames and follows
   each one through the recording with SAM 2's video predictor, so every mask of one object
   carries the same track id. Objects first seen later are added at the discovery frame
   where they appear. Fallback: `sam_masks`, independent masks per key frame.
2. Each mask is lifted into the shared 3D frame with that frame's depth (eroded by one pixel
   and depth-trimmed, so boundary pixels that bleed onto the background are dropped).
3. Fragments with the same track id are one object. Tracks (or, in the fallback, single
   masks) that overlap in 3D (intersection over the smaller > 0.5 and over the larger
   > 0.25) are also merged with union-find, but never two that are visible in the same frame:
   this rejoins an object that left the view and was found again as a new track.
4. Parts (an object almost entirely inside a larger, non-structure object) are dropped;
   large structures (sofa, wall, floor, rug; largest extent over `structure_m`) are kept
   and flagged.

The result is a list of SceneObject per recording, with 3D points, the source pixel of each
point (for appearance features), its best view for crops and per-frame masks for overlays.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ..recon.frames import Recon
from .geometry import unproject

log = logging.getLogger(__name__)


@dataclass
class SceneObject:
    id: int
    recording: str                    # "A" or "B"
    X: np.ndarray                     # [N, 3] points (subsampled)
    frame: np.ndarray                 # [N] source frame of each point
    pix: np.ndarray                   # [N, 2] (v, u) at reconstruction resolution
    voxels: set
    masks: dict                       # frame -> bool mask at reconstruction resolution
    best_frame: int = -1
    complete: bool = True             # best view not cut by the image border
    centroid: np.ndarray | None = None
    extent: np.ndarray | None = None  # 5-95% extent along principal axes (scene units), largest first
    structure: bool = False
    label: str | None = None
    label_prob: float = 0.0
    label_top3: list = field(default_factory=list)
    status: str = "unknown"
    stats: dict = field(default_factory=dict)
    desc: np.ndarray | None = None    # appearance descriptor (L2-normalised)
    partner: int | None = None        # same object in the other recording (unchanged / moved)
    extra: dict = field(default_factory=dict)


# ---- SAM 2 (GPU) --------------------------------------------------------------------------
_SAM = {}


def load_sam(model_id: str = "facebook/sam2.1-hiera-small", points_per_side: int = 32):
    """SAM 2 automatic mask generator (pip install git+https://github.com/facebookresearch/sam2)."""
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

    key = (model_id, points_per_side)
    if key not in _SAM:
        _SAM[key] = SAM2AutomaticMaskGenerator.from_pretrained(
            model_id, points_per_side=points_per_side, points_per_batch=64, pred_iou_thresh=0.7,
            stability_score_thresh=0.85, min_mask_region_area=80, multimask_output=True)
    return _SAM[key]


WOBBLE_OFFSET = 1.0     # logit offset; SAM's own stability score uses the same +-1 thresholds


def mask_wobble(predictor, img: np.ndarray, ms: list[dict]) -> list:
    """How much each mask's centre moves within SAM 2's own boundary uncertainty: re-decode
    the mask from its prompt point with logits, threshold at -1 (loose) and +1 (tight), and
    return the centroid shift (du, dv) in full-resolution pixels (None if it fails)."""
    import torch

    out = []
    with torch.inference_mode():
        predictor.set_image(img)
        for m in ms:
            try:
                pt = np.asarray(m["point_coords"], float).reshape(-1, 2)[:1]
                logits, _, _ = predictor.predict(point_coords=pt, point_labels=np.ones(1), multimask_output=True,
                                                 return_logits=True)
                seg = m["segmentation"]
                best = max(range(len(logits)), key=lambda k: np.logical_and(logits[k] > 0, seg).sum()
                           / max(np.logical_or(logits[k] > 0, seg).sum(), 1))
                lg = logits[best]
                c = []
                for thr in (-WOBBLE_OFFSET, WOBBLE_OFFSET):
                    v, u = np.nonzero(lg > thr)
                    c.append((u.mean(), v.mean()) if len(u) else (np.nan, np.nan))
                d = np.subtract(c[0], c[1])
                out.append(d.tolist() if np.isfinite(d).all() else None)
            except Exception:  # noqa: BLE001 - wobble is optional
                out.append(None)
    return out


def sam_masks(files: list[str], frames: list[int], size_hw: tuple[int, int], model_id: str = "facebook/sam2.1-hiera-small",
              points_per_side: int = 32, min_area: float = 0.0005, max_area: float = 0.5, wobble: bool = True) -> dict:
    """{frame: [dict(mask=bool[H, W] at reconstruction size, full_bbox=(x0, y0, x1, y1), score,
    wobble_px=(du, dv) or None)]}."""
    import torch
    from PIL import Image

    gen = load_sam(model_id, points_per_side)
    H, W = size_hw
    out = {}
    wobble_failed = False
    for s in frames:
        img = np.asarray(Image.open(files[s]).convert("RGB"))
        with torch.inference_mode():
            ms = gen.generate(img)
        fh, fw = img.shape[:2]
        ms = [m for m in ms if min_area <= m["area"] / (fh * fw) <= max_area]
        wob = [None] * len(ms)
        if wobble and not wobble_failed:
            try:
                wob = mask_wobble(gen.predictor, img, ms)
            except Exception as e:  # noqa: BLE001
                wobble_failed = True
                log.warning("mask wobble unavailable (%s: %s); continuing without it", type(e).__name__, e)
        keep = []
        for m, w in zip(ms, wob):
            seg = Image.fromarray(m["segmentation"].astype(np.uint8) * 255).resize((W, H), Image.Resampling.NEAREST)
            x, y, w_, h = m["bbox"]
            keep.append({"mask": np.asarray(seg) > 127, "full_bbox": (int(x), int(y), int(x + w_), int(y + h)),
                         "score": float(m["predicted_iou"]) * float(m["stability_score"]),
                         "full_hw": (fh, fw), "wobble_px": w})
        out[s] = keep
    torch.cuda.empty_cache()
    return out


# ---- SAM 2 video tracking (GPU) -----------------------------------------------------------
def load_video_predictor(model_id: str = "facebook/sam2.1-hiera-small"):
    from sam2.sam2_video_predictor import SAM2VideoPredictor

    key = ("video", model_id)
    if key not in _SAM:
        _SAM[key] = SAM2VideoPredictor.from_pretrained(model_id)
    return _SAM[key]


def _is_new(m: np.ndarray, existing: list[np.ndarray], max_iou: float = 0.4, max_inside: float = 0.7) -> bool:
    """A discovered mask is a new object unless it matches a tracked mask (IoU) or lies mostly
    inside a tracked mask of similar size (a part). Much larger masks (sofa, wall) never hide
    the smaller objects on them."""
    a = m.sum()
    if a == 0:
        return False
    for t in existing:
        inter = np.logical_and(m, t).sum()
        if not inter:
            continue
        b = t.sum()
        if inter / (a + b - inter) > max_iou or (b <= 4 * a and inter / a > max_inside):
            return False
    return True


def _logit_wobble(lg: np.ndarray) -> list | None:
    """Centroid shift between the loose (logit > -1) and tight (> +1) mask, full-res pixels:
    the same mask-boundary uncertainty as mask_wobble, read from the tracker's own logits."""
    c = []
    for thr in (-WOBBLE_OFFSET, WOBBLE_OFFSET):
        v, u = np.nonzero(lg > thr)
        if not len(u):
            return None
        c.append((u.mean(), v.mean()))
    return np.subtract(c[0], c[1]).tolist()


def sam_tracks(files: list[str], frames: list[int], size_hw: tuple[int, int], model_id: str = "facebook/sam2.1-hiera-small",
               points_per_side: int = 32, min_area: float = 0.0005, max_area: float = 0.5, discover_every: int = 8,
               batch: int = 30, keep_frames=None, gen=None, predictor=None, workdir=None) -> dict:
    """Objects tracked through `frames` (one recording, in time order) with SAM 2's video
    predictor. Every `discover_every`-th frame the automatic mask generator proposes masks;
    those not already tracked there (`_is_new`) become new objects, followed forwards and
    backwards through all frames in batches of `batch` objects. Returns {frame: [dict(mask,
    full_bbox, score, full_hw, wobble_px, track)]} for `keep_frames` (default all)."""
    import tempfile

    import torch
    from PIL import Image

    gen = gen or load_sam(model_id, points_per_side).generate
    predictor = predictor or load_video_predictor(model_id)
    H, W = size_hw
    keep_frames = set(frames if keep_frames is None else keep_frames)
    n = len(frames)
    tracked: list[dict] = [dict() for _ in range(n)]      # per frame index: track -> (full-res mask, logits wobble)
    seeds: list[tuple[int, int, np.ndarray, float]] = []  # (track, frame index, mask, score)
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        for i, s in enumerate(frames):                     # the predictor reads a folder of <index>.jpg
            dst = f"{tmp}/{i:05d}.jpg"
            if str(files[s]).lower().endswith((".jpg", ".jpeg")):
                import os

                os.symlink(os.path.abspath(files[s]), dst)
            else:
                Image.open(files[s]).convert("RGB").save(dst, quality=95)
        fh, fw = np.asarray(Image.open(files[frames[0]])).shape[:2]
        state = None
        for d in list(range(0, n, discover_every)):
            img = np.asarray(Image.open(files[frames[d]]).convert("RGB"))
            with torch.inference_mode():
                ms = gen(img)
            ms = sorted([m for m in ms if min_area <= m["area"] / (fh * fw) <= max_area], key=lambda m: -m["area"])
            have = [t[0] for t in tracked[d].values()]
            new = []
            for m in ms:
                seg = np.asarray(m["segmentation"], bool)
                if _is_new(seg, have):
                    have.append(seg)
                    new.append((len(seeds) + len(new), d, seg, float(m["predicted_iou"]) * float(m["stability_score"])))
            for b0 in range(0, len(new), batch):
                chunk = new[b0:b0 + batch]
                if state is None:
                    state = predictor.init_state(video_path=tmp, offload_video_to_cpu=True, offload_state_to_cpu=True)
                else:
                    predictor.reset_state(state)
                with torch.inference_mode():
                    for tid, fi, seg, _ in chunk:
                        predictor.add_new_mask(state, frame_idx=fi, obj_id=tid, mask=seg)
                    for rev in (False, True):
                        for fi, ids, logits in predictor.propagate_in_video(state, start_frame_idx=d, reverse=rev):
                            lg = logits[:, 0].float().cpu().numpy()
                            for tid, l in zip(ids, lg):
                                mk = l > 0
                                if mk.sum() >= min_area * fh * fw:
                                    tracked[fi][int(tid)] = (mk, l)
            seeds += new
            if new:
                log.info("SAM 2 tracking: frame %d/%d: %d new objects (%d tracked so far)", d + 1, n, len(new), len(seeds))
        if state is not None:
            predictor.reset_state(state)
    score = {t: sc for t, _, _, sc in seeds}
    out: dict = {}
    for i, s in enumerate(frames):
        if s not in keep_frames:
            continue
        keep = []
        for tid, (mk, lg) in tracked[i].items():
            v, u = np.nonzero(mk)
            seg = Image.fromarray(mk.astype(np.uint8) * 255).resize((W, H), Image.Resampling.NEAREST)
            keep.append({"mask": np.asarray(seg) > 127, "full_bbox": (int(u.min()), int(v.min()), int(u.max()) + 1, int(v.max()) + 1),
                         "score": score[tid], "full_hw": (fh, fw), "wobble_px": _logit_wobble(lg), "track": int(tid)})
        out[s] = keep
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return out


# ---- lifting and merging (numpy) ----------------------------------------------------------
def _erode(m: np.ndarray) -> np.ndarray:
    e = m.copy()
    e[1:] &= m[:-1]
    e[:-1] &= m[1:]
    e[:, 1:] &= m[:, :-1]
    e[:, :-1] &= m[:, 1:]
    return e


def _keys(X: np.ndarray, voxel: float) -> set:
    return set(map(tuple, np.floor(X / voxel).astype(np.int64)))


VGGT_CONF_ALPHA = 0.2   # VGGT's confidence loss: c * |error| - alpha * log c, so |error| ~ alpha / c


def view_measurement(rec: Recon, s: int, X: np.ndarray, v: np.ndarray, u: np.ndarray, m: dict) -> dict:
    """One view's measurement of the object's centre and its covariance, from the models'
    own uncertainty (no tuning):
    - depth: VGGT's per-pixel confidence c gives an expected depth error alpha / c (in VGGT's
      units, times the chunk scale). Depth errors are shared by the whole object (a depth map
      is off by a smooth amount), so the median over the mask is used, along the viewing ray;
    - sideways: SAM 2 mask wobble, the centroid shift between its loose and tight masks
      (logit -1 / +1), as a uniform range (sd = shift / sqrt(12)), converted with depth / focal."""
    c = np.median(X, 0)
    R, t = rec.E[s][:, :3], rec.E[s][:, 3]
    cam = -R.T @ t
    ray = c - cam
    dist = float(np.linalg.norm(ray)) + 1e-12
    ray = ray / dist
    sc = 1.0 if rec.scale is None else float(rec.scale[s])
    conf = rec.conf[s][v, u]
    sd_depth = float(np.median(VGGT_CONF_ALPHA / np.maximum(conf, 1e-3))) * sc
    z = float(np.median(rec.depth[s][v, u]))
    f = float(rec.K[s][0, 0])
    wob = m.get("wobble_px")
    if wob is not None:
        fh, fw = m["full_hw"]
        H, W = rec.depth.shape[1:]
        px = float(np.hypot(wob[0] * W / fw, wob[1] * H / fh))
        sd_side = px / np.sqrt(12) * z / f
    else:
        sd_side = None
    return {"c": c, "ray": ray, "sd_depth": sd_depth, "sd_side": sd_side, "pixel": z / f}


def lift(rec: Recon, masks: dict, voxel: float, conf_quantile: float = 0.3, max_points: int = 4000,
         seed: int = 0, merge_vox: float | None = None) -> list[dict]:
    rng = np.random.default_rng(seed)
    frags = []
    H, W = rec.depth.shape[1:]
    for s, ms in masks.items():
        cthr = np.quantile(rec.conf[s], conf_quantile)
        good = (rec.depth[s] > 0) & (rec.conf[s] >= cthr)
        for m in ms:
            mk = _erode(m["mask"]) & good
            if mk.sum() < 15:
                continue
            X, v, u = unproject(rec.depth[s], rec.K[s], rec.E[s], mk)
            z = rec.depth[s][v, u]
            med = np.median(z)
            mad = np.median(np.abs(z - med)) + 1e-6
            ok = np.abs(z - med) < max(4 * mad, 0.02 * med)
            X, v, u = X[ok], v[ok], u[ok]
            if len(X) < 15:
                continue
            vox = _keys(X, voxel)                       # from every point, before thinning
            voxc = _keys(X, merge_vox or voxel)
            rgb = rec.images[s][v, u].reshape(-1, 3).mean(0)
            if len(X) > max_points:
                sel = rng.choice(len(X), max_points, replace=False)
                X, v, u = X[sel], v[sel], u[sel]
            x0, y0, x1, y1 = m["full_bbox"]
            fh, fw = m["full_hw"]
            border = x0 <= 2 or y0 <= 2 or x1 >= fw - 3 or y1 >= fh - 3
            frags.append({"frame": s, "track": m.get("track"), "X": X, "pix": np.stack([v, u], 1), "mask": m["mask"], "full_bbox": m["full_bbox"],
                          "area": int(m["mask"].sum()), "border": border, "score": m["score"],
                          "vox": vox, "voxc": voxc, "rgb": rgb, "meas": view_measurement(rec, s, X, v, u, m)})
    return frags


def merge(frags: list[dict], min_over_small: float = 0.5, min_over_large: float = 0.25,
          max_colour: float = 60.0) -> list[list[int]]:
    """Union-find groups of fragments: the same SAM 2 track (when tracked), or overlapping in
    3D on the merge grid ("voxc", sized to the depth uncertainty so two views of one object
    share cells) and alike in colour (mean within `max_colour` of 255). Two groups that are
    both visible in one frame are never merged (they are two objects)."""
    key = "voxc" if frags and "voxc" in frags[0] else "vox"
    parent = list(range(len(frags)))
    frames_of = {i: {f["frame"]} for i, f in enumerate(frags)}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b
            frames_of[b] |= frames_of.pop(a)

    first: dict = {}
    for i, f in enumerate(frags):
        if f.get("track") is not None:
            if f["track"] in first:
                union(i, first[f["track"]])
            else:
                first[f["track"]] = i

    owner: dict = {}
    for i, f in enumerate(frags):
        for k in f[key]:
            owner.setdefault(k, []).append(i)
    pairs: dict = {}
    for ids in owner.values():
        if len(ids) < 2 or len(ids) > 60:
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                i, j = ids[a], ids[b]
                if frags[i]["frame"] != frags[j]["frame"]:
                    pairs[(i, j)] = pairs.get((i, j), 0) + 1
    for (i, j), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        ni, nj = len(frags[i][key]), len(frags[j][key])
        if "rgb" in frags[i] and np.linalg.norm(frags[i]["rgb"] - frags[j]["rgb"]) > max_colour:
            continue
        if find(i) == find(j) or frames_of[find(i)] & frames_of[find(j)]:
            continue
        if n / min(ni, nj) > min_over_small and n / max(ni, nj) > min_over_large:
            union(i, j)
    groups: dict = {}
    for i in range(len(frags)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _extent(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    c = X.mean(0)
    if len(X) < 4:
        return c, np.zeros(3)
    _, _, vt = np.linalg.svd(X - c, full_matrices=False)
    P = (X - c) @ vt.T
    ext = np.quantile(P, 0.95, axis=0) - np.quantile(P, 0.05, axis=0)
    return np.median(X, 0), np.sort(ext)[::-1]


def effective_voxel(rec: Recon, masks: dict, voxel: float) -> float:
    """Voxels must be coarser than the pixel footprint, or views of one surface share no voxel.
    Compute this once for all recordings of a reconstruction, so their voxel keys line up."""
    fp = [np.median(rec.depth[s][rec.depth[s] > 0]) / rec.K[s][0, 0] for s in masks if (rec.depth[s] > 0).any()]
    return max(voxel, 2.5 * float(np.median(fp))) if fp else voxel


def merge_voxel(rec: Recon, masks: dict, voxel: float, k: float = 1.5) -> float:
    """Grid for merging views and matching objects: coarser than the typical depth error, so
    two views of the same surface (each off by about that much along its ray) share cells.
    The error comes from VGGT's own confidence (alpha / conf, in reconstruction units)."""
    sd = []
    for s in masks:
        ok = rec.depth[s] > 0
        if ok.any():
            sc = float(rec.scale[s]) if rec.scale is not None else 1.0
            sd.append(np.median(VGGT_CONF_ALPHA / np.maximum(rec.conf[s][ok], 1e-3)) * sc)
    return max(3 * voxel, k * float(np.median(sd))) if sd else 3 * voxel


def build_objects(rec: Recon, masks: dict, recording: str, voxel: float, structure_units: float,
                  max_points: int = 15000, min_frames: int = 1, min_voxels: int = 6, seed: int = 0,
                  adapt_voxel: bool = True, merge_vox: float | None = None) -> list[SceneObject]:
    """`merge_vox`: grid for merging views and for matching across recordings (pass the same
    value for every recording of a reconstruction; default from merge_voxel)."""
    rng = np.random.default_rng(seed)
    if adapt_voxel:
        voxel = effective_voxel(rec, masks, voxel)
    merge_vox = merge_vox or merge_voxel(rec, masks, voxel)
    frags = lift(rec, masks, voxel, seed=seed, merge_vox=merge_vox)
    objs = []
    for g in merge(frags):
        fs = [frags[i] for i in g]
        vox = set().union(*(f["vox"] for f in fs))
        if len(vox) < min_voxels or len({f["frame"] for f in fs}) < min_frames:
            continue
        X = np.concatenate([f["X"] for f in fs])
        frame = np.concatenate([np.full(len(f["X"]), f["frame"]) for f in fs])
        pix = np.concatenate([f["pix"] for f in fs])
        if len(X) > max_points:
            sel = rng.choice(len(X), max_points, replace=False)
            X, frame, pix = X[sel], frame[sel], pix[sel]
        inner = [f for f in fs if not f["border"]] or fs
        best = max(inner, key=lambda f: f["area"] * f["score"])
        c, ext = _extent(X)
        mk: dict = {}
        for f in fs:                                   # largest mask per frame
            if f["frame"] not in mk or f["area"] > mk[f["frame"]].sum():
                mk[f["frame"]] = f["mask"]
        voxc = set().union(*(f["voxc"] for f in fs))
        o = SceneObject(len(objs), recording, X, frame, pix, voxc, mk, best_frame=int(best["frame"]),
                        complete=not best["border"], centroid=c, extent=ext, structure=bool(ext[0] > structure_units))
        views: dict = {}
        for f_ in fs:
            if f_["frame"] not in views or f_["area"] > views[f_["frame"]][0]:
                views[f_["frame"]] = (f_["area"], f_["meas"])
        o.extra["views"] = [dict(frame=int(k), **views[k][1]) for k in sorted(views)]
        o.extra["full_bbox"] = best["full_bbox"]
        o.extra["tracks"] = sorted({int(f["track"]) for f in fs if f.get("track") is not None})
        o.extra["voxel"] = merge_vox            # size of the cells in o.voxels
        o.extra["fine_voxel"] = voxel
        o.extra["fine_vox"] = vox
        o.extra["n_views"] = len(mk)
        objs.append(o)
    # drop parts: an object almost entirely inside a larger non-structure object
    objs.sort(key=lambda o: -len(o.extra["fine_vox"]))
    keep = []
    for o in objs:
        fo = o.extra["fine_vox"]
        part = any(not p.structure and len(fo & p.extra["fine_vox"]) > 0.8 * len(fo) for p in keep)
        if not part:
            keep.append(o)
    for i, o in enumerate(keep):
        o.id = i
    log.info("recording %s: %d masks on %d frames -> %d fragments -> %d objects (%d structures), median %s views "
             "per object, merge grid %.4f units", recording, sum(len(m) for m in masks.values()), len(masks), len(frags),
             len(keep), sum(o.structure for o in keep),
             float(np.median([o.extra["n_views"] for o in keep])) if keep else 0, merge_vox)
    return keep


def key_frames(rec: Recon, vid: int, n: int) -> list[int]:
    idx = np.nonzero(rec.video == vid)[0]
    return [int(idx[int(round(x))]) for x in np.linspace(0, len(idx) - 1, min(n, len(idx)))]
