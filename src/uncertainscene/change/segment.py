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
def release_sam() -> None:
    """Free SAM 2 (mask generator and video predictor) from the GPU once masks are made."""
    if _SAM:
        import gc

        _SAM.clear()
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


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
               batch: int = 15, keep_frames=None, gen=None, predictor=None, workdir=None, det=None,
               part_inside: float = 0.7, seed_conf: float = 0.3) -> dict:
    """Objects tracked through `frames` (one recording, in time order) with SAM 2's video
    predictor. Every `discover_every`-th frame the automatic mask generator proposes masks;
    those not already tracked there (`_is_new`) become new objects, followed forwards and
    backwards through all frames in batches of `batch` objects. Returns {frame: [dict(mask,
    full_bbox, score, full_hw, wobble_px, track)]} for `keep_frames` (default all).

    `det` (optional, detect2d.Detector): whole-object instances. They seed tracks before the
    automatic masks; automatic masks more than `part_inside` inside an object instance are parts
    and seed nothing (detections below `seed_conf` only vote); on kept frames every track
    collects label votes ("det_votes", summed confidence x IoU of instances it matches) and a
    track that keeps lying inside the instance of another track is joined to it (its masks are
    added to that track's, "parts")."""
    import tempfile

    import torch
    from PIL import Image

    gen = gen or load_sam(model_id, points_per_side).generate
    predictor = predictor or load_video_predictor(model_id)
    H, W = size_hw
    keep_frames = set(frames if keep_frames is None else keep_frames)
    n = len(frames)
    # memory (run 4 hit Colab's RAM limit keeping float logits of every object in every frame,
    # ~2.4 MB each): keep only what is used later, compactly. Discovery frames: packed full-res
    # masks (to test what is new); kept frames: the mask at reconstruction size, its full-res
    # box and its wobble, computed as each frame arrives.
    disc = set(range(0, n, discover_every))
    keep_idx = {i for i, s in enumerate(frames) if s in keep_frames}
    tracked: list[dict] = [dict() for _ in range(n)]      # per frame index: track -> packed full-res mask
    kept: list[dict] = [dict() for _ in range(n)]         # per frame index: track -> (mask at rec size, bbox, wobble)
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
        dets: dict = {}                                    # frame index -> instances (masks at rec size)
        n_parts = 0
        for d in list(range(0, n, discover_every)):
            img = np.asarray(Image.open(files[frames[d]]).convert("RGB"))
            with torch.inference_mode():
                ms = gen(img)
            ms = sorted([m for m in ms if min_area <= m["area"] / (fh * fw) <= max_area], key=lambda m: -m["area"])
            cands = [(np.asarray(m["segmentation"], bool), float(m["predicted_iou"]) * float(m["stability_score"]), False)
                     for m in ms]
            things = []
            if det is not None:
                found = det(img)
                if d in keep_idx:
                    dets[d] = _small_dets(found, (W, H))
                sure = [x for x in found if x["conf"] >= seed_conf]       # weaker detections only vote
                things = [x["mask"][::4, ::4] for x in sure if x["object"]]
                # whole objects first (largest first), then the automatic masks
                cands = [(x["mask"], x["conf"], True) for x in sorted(sure, key=lambda x: -x["mask"].sum())] + cands
                del sure
                del found
            # what is new is decided at quarter resolution (16x fewer pixels per comparison;
            # ~150 candidates x ~150 tracks per discovery frame was minutes at full resolution)
            have = [np.unpackbits(t, count=fh * fw).reshape(fh, fw)[::4, ::4].astype(bool)
                    for t in tracked[d].values()]
            new = []
            for seg, sc, whole in cands:
                small = seg[::4, ::4]
                a = small.sum()
                if not whole and any(t.sum() > 1.5 * a and np.logical_and(small, t).sum() > part_inside * a
                                     for t in things):
                    n_parts += 1                           # a part of a detected object
                    continue
                if _is_new(small, have):
                    have.append(small)
                    new.append((len(seeds) + len(new), d, seg, sc))
            del cands
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
                                if mk.sum() < min_area * fh * fw:
                                    continue
                                if fi in disc:
                                    tracked[fi][int(tid)] = np.packbits(mk)
                                if fi in keep_idx:
                                    v, u = np.nonzero(mk)
                                    seg = Image.fromarray(mk.astype(np.uint8) * 255).resize((W, H), Image.Resampling.NEAREST)
                                    kept[fi][int(tid)] = (np.asarray(seg) > 127,
                                                          (int(u.min()), int(v.min()), int(u.max()) + 1, int(v.max()) + 1),
                                                          _logit_wobble(l))
                            del logits, lg
            if new:
                log.info("SAM 2 tracking: frame %d/%d: %d new objects (%d tracked so far)", d + 1, n, len(new),
                         len(seeds) + len(new))
            seeds += [(t, fi, None, sc) for t, fi, _, sc in new]     # seed masks are no longer needed
            del new
        if state is not None:
            predictor.reset_state(state)
    score = {t: sc for t, _, _, sc in seeds}
    votes: dict = {}
    root = {t: t for t in score}
    if det is not None:
        for i in sorted(keep_idx):
            if i not in dets:
                with torch.inference_mode():
                    dets[i] = _small_dets(det(np.asarray(Image.open(files[frames[i]]).convert("RGB"))), (W, H))
        votes, parents = track_votes(kept, dets)
        root = join_parts(parents, score)
        log.info("SAM 2 tracking + detector: %d part masks not seeded, %d tracks joined to the object they lie in, "
                 "%d of %d tracks labelled by the detector", n_parts, sum(root[t] != t for t in root),
                 sum(1 for t in score if votes.get(t)), len(score))
    tot: dict = {}                                         # votes of each joined object
    for t, r in root.items():
        for lab, v in votes.get(t, {}).items():
            tot.setdefault(r, {})[lab] = tot.setdefault(r, {}).get(lab, 0.0) + v
    parts = {}
    for t, r in root.items():
        if r != t:
            parts.setdefault(r, []).append(int(t))
    out: dict = {}
    for i, s in enumerate(frames):
        if i not in keep_idx:
            continue
        by: dict = {}
        for tid, (mk, bb, wb) in kept[i].items():
            r = root.get(tid, tid)
            if r not in by:
                by[r] = [mk.copy(), list(bb), wb if r == tid else None]
            else:
                g = by[r]
                g[0] |= mk
                g[1] = [min(g[1][0], bb[0]), min(g[1][1], bb[1]), max(g[1][2], bb[2]), max(g[1][3], bb[3])]
                g[2] = wb if r == tid else g[2]
        out[s] = [{"mask": mk, "full_bbox": tuple(bb), "score": score[r], "full_hw": (fh, fw), "wobble_px": wb,
                   "track": int(r), "det_votes": {k: round(v, 3) for k, v in tot.get(r, {}).items()},
                   "parts": parts.get(r, [])} for r, (mk, bb, wb) in by.items()]
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return out


def _small_dets(found: list[dict], size_wh: tuple[int, int]) -> list[dict]:
    """Detector instances with masks at reconstruction size (to compare with kept track masks)."""
    from PIL import Image

    return [{**x, "mask": np.asarray(Image.fromarray(x["mask"].astype(np.uint8) * 255).resize(size_wh, Image.Resampling.NEAREST))
             > 127} for x in found]


def track_votes(kept: list[dict], dets: dict, min_iou: float = 0.5, part_inside: float = 0.6) -> tuple[dict, dict]:
    """Per frame index, match tracked masks (kept[i]: track -> (mask, bbox, wobble)) with detector
    instances (dets[i]). Returns label votes {track: {label: summed confidence}} from instances a
    track matches (IoU > min_iou), and part evidence {(part, parent): [frames inside, frames both
    seen]}: the part lies more than `part_inside` inside an object instance that the parent track
    matches, and the instance is clearly larger than the part. When no track matches the
    instance (it was seeded in pieces, e.g. guitar body and neck), the tracks inside it count as
    parts of the largest of them."""
    from .detect2d import inside, iou

    votes: dict = {}
    parents: dict = {}
    for i, ds in dets.items():
        ts = {t: v[0] for t, v in kept[i].items()}
        if not ts:
            continue
        match = {}                                         # instance index -> best matching track
        for j, x in enumerate(ds):
            best = max(ts, key=lambda t: iou(ts[t], x["mask"]))
            q = iou(ts[best], x["mask"])
            if q > min_iou:
                match[j] = best
                votes.setdefault(best, {})
                votes[best][x["label"]] = votes[best].get(x["label"], 0.0) + x["conf"] * q
        for j, x in enumerate(ds):
            if not x["object"]:
                continue
            p = match.get(j)
            inner = [t for t, mk in ts.items() if t != p and x["mask"].sum() >= 1.5 * mk.sum()
                     and inside(mk, x["mask"]) > part_inside]
            if p is not None:
                for t in inner:
                    parents.setdefault((t, p), [0, 0])[0] += 1
            else:
                # no track covers the whole instance (it was seeded in pieces, e.g. guitar body and
                # neck): the pieces inside it are parts of one object, joined to the largest
                inner.sort(key=lambda t: -ts[t].sum())
                for t in inner[1:]:
                    parents.setdefault((t, inner[0]), [0, 0])[0] += 1
    seen: dict = {}
    for i in dets:
        for t in kept[i]:
            seen.setdefault(t, set()).add(i)
    for (t, p), c in parents.items():
        c[1] = len(seen[t] & seen[p])
    return votes, parents


def join_parts(parents: dict, tracks, min_frames: int = 2, min_share: float = 0.5) -> dict:
    """{track: track it belongs to}: a part is joined to the parent it lies in on at least
    `min_frames` frames and on at least `min_share` of the frames where both are seen (the
    parent with most such frames, if several). Chains resolve to the outermost object."""
    best: dict = {}
    for (t, p), (k, both) in parents.items():
        if k >= min_frames and k >= min_share * max(both, 1) and k > best.get(t, (None, 0))[1]:
            best[t] = (p, k)
    root = {}
    for t in tracks:
        r, seen = t, {t}
        while r in best and best[r][0] not in seen:
            r = best[r][0]
            seen.add(r)
        root[t] = r
    return root


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
      (logit -1 / +1), as a uniform range (sd = shift / sqrt(12)), converted with depth / focal;
    - pose: the frame's camera-pose uncertainty from bundle adjustment (rec.pose_px, pixels)
      moves the whole view sideways by pose_px * depth / focal, so a view from a shaky camera
      counts for less in the fused position."""
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
    sd_pose = 0.0 if rec.pose_px is None else float(rec.pose_px[s]) * z / f
    return {"c": c, "ray": ray, "sd_depth": sd_depth, "sd_side": sd_side, "sd_pose": sd_pose, "pixel": z / f}


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
                          "vox": vox, "voxc": voxc, "rgb": rgb, "meas": view_measurement(rec, s, X, v, u, m),
                          "det_votes": m.get("det_votes") or {}, "parts": m.get("parts") or []})
    return frags


def _det_label(f: dict):
    from .inventory import detector_label

    d = detector_label(f.get("det_votes") or {})
    return None if d is None else d[0]


def merge(frags: list[dict], min_over_small: float = 0.5, min_over_large: float = 0.25,
          max_colour: float = 60.0, same_label_relax: float = 0.5) -> list[list[int]]:
    """Union-find groups of fragments: the same SAM 2 track (when tracked), or overlapping in
    3D on the merge grid ("voxc", sized to the depth uncertainty so two views of one object
    share cells) and alike in colour (mean within `max_colour` of 255). Two groups that are
    both visible in one frame are never merged (they are two objects).

    Continuity: when an object leaves the view and comes back, SAM 2 starts a new track. Two
    tracks the detector names the same (detector_label) are joined with overlap thresholds
    relaxed by `same_label_relax` and a looser colour test (lighting changes along the video);
    two tracks it names differently are never joined."""
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
    lab = [_det_label(f) for f in frags]
    for (i, j), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        ni, nj = len(frags[i][key]), len(frags[j][key])
        same = lab[i] is not None and lab[i] == lab[j]
        if lab[i] is not None and lab[j] is not None and lab[i] != lab[j]:
            continue
        k = same_label_relax if same else 1.0
        if "rgb" in frags[i] and np.linalg.norm(frags[i]["rgb"] - frags[j]["rgb"]) > max_colour * (1.5 if same else 1.0):
            continue
        if find(i) == find(j) or frames_of[find(i)] & frames_of[find(j)]:
            continue
        if n / min(ni, nj) > k * min_over_small and n / max(ni, nj) > k * min_over_large:
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


def clean_points(X: np.ndarray, frame: np.ndarray, voxel: float, pose_px: np.ndarray | None = None,
                 min_views: int = 2, k: int = 8, far: float = 3.0, min_keep: float = 0.3,
                 min_piece: float = 0.15) -> np.ndarray:
    """Mask of an object's points worth keeping. Its cloud is the union of every view's depth
    points, so one weak view (a pose that sits off, depth bleeding past the mask edge) adds a
    shell or a spike that distorts the hull. Kept:
    - points from frames whose pose is not among the object's most uncertain (pose_px above
      twice the median of its frames), when it has other views;
    - points in cells (`voxel`, the merge grid) that at least `min_views` frames put a point in,
      when the object was seen in that many frames: a surface is where the views agree;
    - points whose k-th neighbour is within `far` times the typical distance (no stray points);
    - the largest connected piece and pieces at least `min_piece` of its size (no small
      detached clusters).
    Falls back to the previous stage when a stage would keep under `min_keep` of the points."""
    from scipy.spatial import cKDTree

    keep = np.ones(len(X), bool)
    fr = np.unique(frame)
    if pose_px is not None and len(fr) >= 3:
        pp = np.asarray(pose_px, float)[frame]
        m = keep & (pp <= 2 * np.median(np.asarray(pose_px, float)[fr]) + 1e-9)
        if m.sum() >= min_keep * len(X):
            keep = m
    if len(np.unique(frame[keep])) >= min_views:
        cell = np.floor(X / voxel).astype(np.int64)
        _, inv = np.unique(cell, axis=0, return_inverse=True)
        inv = inv.ravel()
        pairs_ = np.unique(np.c_[inv[keep], frame[keep]], axis=0)
        views = np.bincount(pairs_[:, 0], minlength=int(inv.max()) + 1)
        m = keep & (views[inv] >= min_views)
        if m.sum() >= min_keep * len(X):
            keep = m
    if keep.sum() > k + 1:
        idx = np.nonzero(keep)[0]
        ref = X[idx] if len(idx) <= 200_000 else X[np.random.default_rng(0).choice(idx, 200_000, replace=False)]
        d = cKDTree(ref).query(X[idx], k=k + 1)[0][:, -1]
        m = d <= far * np.median(d) + 1e-12
        if m.sum() >= min_keep * len(X):
            keep[idx[~m]] = False
    # small detached clusters (a bit of the wall or sofa caught at a mask edge) bend the hull:
    # keep the largest connected piece and any piece at least `min_piece` of its size
    idx = np.nonzero(keep)[0]
    if len(idx) > 50:
        from scipy.sparse.csgraph import connected_components

        sub = idx if len(idx) <= 60_000 else np.sort(np.random.default_rng(0).choice(idx, 60_000, replace=False))
        tree = cKDTree(X[sub])
        r = 2.5 * float(np.median(tree.query(X[sub], k=2)[0][:, 1])) + 1e-12
        _, lab = connected_components(tree.sparse_distance_matrix(tree, r, output_type="coo_matrix"), directed=False)
        size = np.bincount(lab)
        ok_lab = size >= min_piece * size.max()
        good_sub = sub[ok_lab[lab]]
        if len(good_sub) >= min_keep * len(sub):
            # points not in the subsample follow their nearest subsampled neighbour
            _, j = tree.query(X[idx], k=1)
            keep[idx[~ok_lab[lab[j]]]] = False
    return keep


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
        good = clean_points(X, frame, merge_vox, getattr(rec, "pose_px", None))
        n_raw = len(X)
        X, frame, pix = X[good], frame[good], pix[good]
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
        dv: dict = {}                                  # detector label votes, once per track
        for t, f in {f.get("track"): f for f in fs}.items():
            for lab, v in f.get("det_votes", {}).items():
                dv[lab] = dv.get(lab, 0.0) + v
        o.extra["det_votes"] = dv
        o.extra["parts"] = sorted({p for f in fs for p in f.get("parts", [])})
        o.extra["continuity"] = continuity(fs, sorted(masks))
        o.extra["voxel"] = merge_vox            # size of the cells in o.voxels
        o.extra["fine_voxel"] = voxel
        o.extra["fine_vox"] = vox
        o.extra["n_views"] = len(mk)
        o.extra["points_kept_share"] = round(float(good.sum() / max(n_raw, 1)), 3)
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


def continuity(fs: list[dict], frames: list[int]) -> dict:
    """How continuously one object is followed through its recording: on which of the
    recording's mask frames it has a mask, by which tracks, and how many separate stretches
    ("runs") that makes. One track over one run = followed without a break."""
    pos = {s: i for i, s in enumerate(frames)}
    by: dict = {}
    for f in fs:
        by.setdefault(f["frame"], set()).add(f.get("track"))
    seen = sorted(pos[s] for s in by if s in pos)
    runs = 1 + sum(1 for a, b in zip(seen, seen[1:]) if b > a + 1) if seen else 0
    tracks = sorted({t for v in by.values() for t in v if t is not None})
    return {"frames": [int(frames[i]) for i in seen], "positions": seen, "track_of_frame": {int(s): sorted(int(t) for t in v if t is not None)
                                                                          for s, v in by.items()},
            "tracks": len(tracks), "runs": runs, "first": seen[0] if seen else None, "last": seen[-1] if seen else None,
            "frames_seen": len(seen), "frames_total": len(frames),
            "span_share": round(len(seen) / max(seen[-1] - seen[0] + 1, 1), 3) if seen else 0.0}


def key_frames(rec: Recon, vid: int, n: int) -> list[int]:
    idx = np.nonzero(rec.video == vid)[0]
    return [int(idx[int(round(x))]) for x in np.linspace(0, len(idx) - 1, min(n, len(idx)))]
