"""Every object in the scene: SAM 2 masks on key frames, lifted to 3D and merged across frames.

1. SAM 2 (automatic mask generator, frozen, pretrained) cuts each key frame of a recording
   into object masks at full frame resolution.
2. Each mask is lifted into the shared 3D frame with that frame's depth (eroded by one pixel
   and depth-trimmed, so boundary pixels that bleed onto the background are dropped).
3. Masks of the same physical object in different frames overlap in 3D: fragments whose
   voxel sets overlap (intersection over the smaller > 0.5 and over the larger > 0.25) are
   merged with union-find. This is the tracking step: it works across viewpoints because the
   geometry is shared, so no frame-to-frame prompt propagation is needed.
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


def load_sam(model_id: str = "facebook/sam2.1-hiera-small", points_per_side: int = 24):
    """SAM 2 automatic mask generator (pip install git+https://github.com/facebookresearch/sam2)."""
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

    key = (model_id, points_per_side)
    if key not in _SAM:
        _SAM[key] = SAM2AutomaticMaskGenerator.from_pretrained(
            model_id, points_per_side=points_per_side, points_per_batch=64, pred_iou_thresh=0.75,
            stability_score_thresh=0.88, min_mask_region_area=150, multimask_output=True)
    return _SAM[key]


def sam_masks(files: list[str], frames: list[int], size_hw: tuple[int, int], model_id: str = "facebook/sam2.1-hiera-small",
              points_per_side: int = 24, min_area: float = 0.001, max_area: float = 0.5) -> dict:
    """{frame: [dict(mask=bool[H, W] at reconstruction size, full_bbox=(x0, y0, x1, y1), score)]}."""
    import torch
    from PIL import Image

    gen = load_sam(model_id, points_per_side)
    H, W = size_hw
    out = {}
    for s in frames:
        img = np.asarray(Image.open(files[s]).convert("RGB"))
        with torch.inference_mode():
            ms = gen.generate(img)
        fh, fw = img.shape[:2]
        keep = []
        for m in ms:
            a = m["area"] / (fh * fw)
            if not (min_area <= a <= max_area):
                continue
            seg = Image.fromarray(m["segmentation"].astype(np.uint8) * 255).resize((W, H), Image.Resampling.NEAREST)
            x, y, w, h = m["bbox"]
            keep.append({"mask": np.asarray(seg) > 127, "full_bbox": (int(x), int(y), int(x + w), int(y + h)),
                         "score": float(m["predicted_iou"]) * float(m["stability_score"]),
                         "full_hw": (fh, fw)})
        out[s] = keep
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


def lift(rec: Recon, masks: dict, voxel: float, conf_quantile: float = 0.3, max_points: int = 1500,
         seed: int = 0) -> list[dict]:
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
            if len(X) > max_points:
                sel = rng.choice(len(X), max_points, replace=False)
                X, v, u = X[sel], v[sel], u[sel]
            x0, y0, x1, y1 = m["full_bbox"]
            fh, fw = m["full_hw"]
            border = x0 <= 2 or y0 <= 2 or x1 >= fw - 3 or y1 >= fh - 3
            frags.append({"frame": s, "X": X, "pix": np.stack([v, u], 1), "mask": m["mask"], "full_bbox": m["full_bbox"],
                          "area": int(m["mask"].sum()), "border": border, "score": m["score"],
                          "vox": _keys(X, voxel)})
    return frags


def merge(frags: list[dict], min_over_small: float = 0.5, min_over_large: float = 0.25) -> list[list[int]]:
    """Union-find groups of fragments that overlap in 3D (never two from the same frame)."""
    parent = list(range(len(frags)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict = {}
    for i, f in enumerate(frags):
        for k in f["vox"]:
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
        ni, nj = len(frags[i]["vox"]), len(frags[j]["vox"])
        if n / min(ni, nj) > min_over_small and n / max(ni, nj) > min_over_large:
            parent[find(i)] = find(j)
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


def build_objects(rec: Recon, masks: dict, recording: str, voxel: float, structure_units: float,
                  max_points: int = 3000, min_frames: int = 1, min_voxels: int = 8, seed: int = 0) -> list[SceneObject]:
    rng = np.random.default_rng(seed)
    # voxels must be coarser than the pixel footprint, or views of one surface share no voxel
    fp = [np.median(rec.depth[s][rec.depth[s] > 0]) / rec.K[s][0, 0] for s in masks if (rec.depth[s] > 0).any()]
    if fp:
        voxel = max(voxel, 2.5 * float(np.median(fp)))
    frags = lift(rec, masks, voxel, seed=seed)
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
        o = SceneObject(len(objs), recording, X, frame, pix, vox, mk, best_frame=int(best["frame"]),
                        complete=not best["border"], centroid=c, extent=ext, structure=bool(ext[0] > structure_units))
        o.extra["full_bbox"] = best["full_bbox"]
        o.extra["voxel"] = voxel
        o.extra["n_views"] = len(mk)
        objs.append(o)
    # drop parts: an object almost entirely inside a larger non-structure object
    objs.sort(key=lambda o: -len(o.voxels))
    keep = []
    for o in objs:
        part = any(not p.structure and len(o.voxels & p.voxels) > 0.8 * len(o.voxels) for p in keep)
        if not part:
            keep.append(o)
    for i, o in enumerate(keep):
        o.id = i
    log.info("recording %s: %d masks on %d frames -> %d fragments -> %d objects (%d structures)", recording,
             sum(len(m) for m in masks.values()), len(masks), len(frags), len(keep), sum(o.structure for o in keep))
    return keep


def key_frames(rec: Recon, vid: int, n: int) -> list[int]:
    idx = np.nonzero(rec.video == vid)[0]
    return [int(idx[int(round(x))]) for x in np.linspace(0, len(idx) - 1, min(n, len(idx)))]
