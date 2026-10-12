"""Object-level change detection on SAM 2 objects, with label-free thresholds.

For every object of one recording, its 3D points are tested against all cameras of the other
recording (same visibility test as detect.py: same surface / seen through / occluded) and
the per-point results are pooled over the object:
- observed: share of points the other recording saw well enough to judge;
- gone: share of judgements where the other recording looks straight through the object;
- appearance: mean feature distance where the surface agrees;
- shift: median signed surface shift (deformation, pressed-in cushions).
Pooling over an object averages out per-point noise, which is what lets thin objects (a bat
on a cushion, a coaster) be found. Thresholds come from the same test against the object's
own recording (other views, nothing changed there): the 97.5th percentile of that object-level
null, so no labels are used.

Status per object: removed / added (gone, or a different-looking object in the same place),
moved (a removed and an added object that look the same; displacement with a bootstrap
interval), changed in place (same object, different look: flipped, deformed, swapped for a
look-alike), shifted (surface moved, e.g. squashed), unchanged, or not seen by the other
recording (never reported as a change).
"""

from __future__ import annotations

import logging
import numpy as np

from ..recon.frames import Recon
from .detect import ChangeResult, DetectConfig, _sample_feat, _test
from .segment import SceneObject, _extent

log = logging.getLogger(__name__)

STATUS_COLOR = {"unchanged": (150, 150, 150), "removed": (214, 39, 40), "added": (44, 160, 44),
                "moved": (255, 127, 14), "changed in place": (148, 103, 189), "shifted": (23, 190, 207),
                "not seen in other recording": (90, 90, 200), "unknown": (90, 90, 90)}


def _pool(objs: list[SceneObject], rec: Recon, feats, targets_of, tau: float, conf_thr: np.ndarray,
          cfg: DetectConfig) -> list[dict]:
    """Run the visibility test for all objects at once, then pool per object."""
    if not objs:
        return []
    X = np.concatenate([o.X for o in objs])
    fr = np.concatenate([o.frame for o in objs])
    pix = np.concatenate([o.pix for o in objs])
    oid = np.concatenate([np.full(len(o.X), i) for i, o in enumerate(objs)])
    H, W = rec.depth.shape[1:]
    sf = _sample_feat(feats, fr, pix[:, 0], pix[:, 1], H, W).astype(np.float32) if feats is not None else None
    ns, ng, app, _, ex = _test(rec, X, fr, sf, targets_of, feats, cfg, tau, conf_thr, return_extra=True)
    out = []
    for i, o in enumerate(objs):
        m = oid == i
        n_obs = ns[m] + ng[m]
        observed = n_obs >= cfg.min_obs
        a = app[m][observed & ~np.isnan(app[m])]
        d = ex["disp"][m][observed & np.isfinite(ex["disp"][m])]
        out.append({"observed": float(observed.mean()),
                    "gone": float(ng[m][observed].sum() / max(n_obs[observed].sum(), 1)),
                    "appearance": float(a.mean()) if len(a) else float("nan"),
                    "shift_rel": float(np.median(d)) if len(d) else float("nan"),   # scene units
                    "n_points": int(m.sum())})
    return out


def descriptors(objs: list[SceneObject], rec: Recon, feats) -> None:
    if feats is None:
        return
    H, W = rec.depth.shape[1:]
    for o in objs:
        f = _sample_feat(feats, o.frame, o.pix[:, 0], o.pix[:, 1], H, W).astype(np.float32).mean(0)
        o.desc = f / (np.linalg.norm(f) + 1e-9)


def _sim(a: SceneObject, b: SceneObject) -> float:
    s = []
    if a.desc is not None and b.desc is not None:
        s.append(float(a.desc @ b.desc))
    if a.extra.get("clip") is not None and b.extra.get("clip") is not None:
        s.append(float(a.extra["clip"] @ b.extra["clip"]))
    return float(np.mean(s)) if s else 0.0


def _iou(a: SceneObject, b: SceneObject) -> float:
    n = len(a.voxels & b.voxels)
    return n / max(len(a.voxels | b.voxels), 1)


def counterparts(objs: list[SceneObject], others: list[SceneObject]) -> None:
    """Best overlapping object of the other recording (3D voxel IoU) and its similarity."""
    owner: dict = {}
    for j, p in enumerate(others):
        for k in p.voxels:
            owner.setdefault(k, []).append(j)
    for o in objs:
        cnt: dict = {}
        for k in o.voxels:
            for j in owner.get(k, ()):
                cnt[j] = cnt.get(j, 0) + 1
        best, best_iou = None, 0.0
        for j, n in cnt.items():
            iou = n / max(len(o.voxels) + len(others[j].voxels) - n, 1)
            if iou > best_iou:
                best, best_iou = j, iou
        o.stats["counterpart"] = best
        o.stats["counterpart_iou"] = round(best_iou, 3)
        o.stats["counterpart_sim"] = round(_sim(o, others[best]), 3) if best is not None else None


def classify(objs_a: list[SceneObject], objs_b: list[SceneObject], rec: Recon, feats, res: ChangeResult,
             cfg: DetectConfig | None = None, null_q: float = 0.975, min_observed: float = 0.25,
             same_sim: float = 0.8, move_sim: float = 0.75) -> dict:
    cfg = cfg or DetectConfig()
    tau = res.thresholds["tau_rel_depth"]
    conf_thr = np.array([np.quantile(c, cfg.conf_quantile) for c in rec.conf])
    if not cfg.use_appearance:
        feats = None
    descriptors(objs_a + objs_b, rec, feats)
    own = {0: np.nonzero(rec.video == 0)[0], 1: np.nonzero(rec.video == 1)[0]}
    cross, null = {}, {}
    for vid, objs in ((0, objs_a), (1, objs_b)):
        cross[vid] = _pool(objs, rec, feats, own[1 - vid], tau, conf_thr, cfg)
        null[vid] = _pool(objs, rec, feats, own[vid], tau, conf_thr, cfg)   # own frames: nothing changed
    nl = [s for v in (0, 1) for s in null[v] if s["observed"] >= min_observed]

    def q(key, lo):
        v = np.array([abs(s[key]) for s in nl if np.isfinite(s[key])])
        return float(max(lo, np.quantile(v, null_q))) if len(v) >= 5 else lo

    scale = res.thresholds.get("scene_scale_median_depth", 1.0)
    thr = {"gone": q("gone", 0.3), "appearance": q("appearance", 0.05),
           "shift": q("shift_rel", 0.01 * scale), "null_objects": len(nl), "null_quantile": null_q}
    counterparts(objs_a, objs_b)
    counterparts(objs_b, objs_a)
    for vid, objs, others in ((0, objs_a, objs_b), (1, objs_b, objs_a)):
        gone_word, other_word = ("removed", "added") if vid == 0 else ("added", "removed")
        for o, s, n0 in zip(objs, cross[vid], null[vid]):
            o.stats.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()})
            o.stats["null_gone"], o.stats["null_appearance"] = round(n0["gone"], 4), round(n0["appearance"], 4) \
                if np.isfinite(n0["appearance"]) else None
            cp = o.stats["counterpart"]
            same_obj = cp is not None and o.stats["counterpart_iou"] >= 0.3 and (o.stats["counterpart_sim"] or 0) >= same_sim
            if s["observed"] < min_observed:
                o.status = "not seen in other recording"
            elif s["gone"] >= thr["gone"] and not (same_obj and o.stats["counterpart_iou"] >= 0.5):
                o.status = gone_word
            elif np.isfinite(s["appearance"]) and s["appearance"] >= thr["appearance"]:
                o.status = "changed in place" if same_obj else gone_word
            elif np.isfinite(s["shift_rel"]) and abs(s["shift_rel"]) >= thr["shift"] and not o.structure:
                o.status = "shifted"
            else:
                o.status = "unchanged"
            if o.status == "unchanged" and cp is not None:
                o.partner = cp
    # moved = removed in A + added in B that look alike
    rem = [o for o in objs_a if o.status == "removed" and not o.structure]
    add = [o for o in objs_b if o.status == "added" and not o.structure]
    cand = []
    for a in rem:
        for b in add:
            sim = _sim(a, b)
            ratio = (a.extent[0] + 1e-9) / (b.extent[0] + 1e-9)
            same_label = a.label is not None and a.label == b.label
            if sim >= move_sim and (same_label or 1 / 1.6 < ratio < 1.6):
                cand.append((sim + 0.05 * same_label, a, b))
    used = set()
    for sim, a, b in sorted(cand, key=lambda t: -t[0]):
        if a.id in used or ("B", b.id) in used:
            continue
        used |= {a.id, ("B", b.id)}
        a.status = b.status = "moved"
        a.partner, b.partner = b.id, a.id
        rng = np.random.default_rng(a.id)
        boots = [np.median(b.X[rng.integers(0, len(b.X), len(b.X))], 0) - np.median(a.X[rng.integers(0, len(a.X), len(a.X))], 0)
                 for _ in range(200)]
        dist = np.linalg.norm(boots, axis=1)
        disp = b.centroid - a.centroid
        for o in (a, b):
            o.stats.update({"displacement": disp.tolist(), "distance": float(np.linalg.norm(disp)),
                            "distance_ci": [float(np.quantile(dist, 0.05)), float(np.quantile(dist, 0.95))],
                            "match_similarity": round(float(sim), 3)})
    counts = {}
    for o in objs_a + objs_b:
        counts[f"{o.recording}:{o.status}"] = counts.get(f"{o.recording}:{o.status}", 0) + 1
    log.info("object changes: %s; thresholds gone %.3f appearance %.3f shift %.4f (from %d null objects)",
             counts, thr["gone"], thr["appearance"], thr["shift"], len(nl))
    return thr


def to_change_objects(objs_a: list[SceneObject], objs_b: list[SceneObject], res: ChangeResult) -> list:
    """Changed SAM objects as objects.ChangeObject, with masks over res.A / res.B points (by
    voxel membership), so the existing metric, report and viewer code applies."""
    from .objects import ChangeObject

    out = []
    vox = None
    for o in objs_a + objs_b:
        if o.status in ("unchanged", "not seen in other recording", "unknown") or o.structure:
            continue
        if o.status == "moved" and o.recording == "B":
            continue                      # reported once, from A, with its partner
        ps = res.A if o.recording == "A" else res.B
        vox = vox or {}
        key = o.recording
        if key not in vox:
            vsize = o.extra["voxel"]
            vox[key] = [tuple(k) for k in np.floor(ps.X / vsize).astype(np.int64)]
        mask = np.array([k in o.voxels for k in vox[key]])
        X = o.X
        rng = np.random.default_rng(o.id)
        boots = np.array([np.median(X[rng.integers(0, len(X), len(X))], 0) for _ in range(100)])
        ext_b = np.array([_extent(X[rng.integers(0, len(X), len(X))])[1] for _ in range(60)])
        frames, counts = np.unique(o.frame, return_counts=True)
        co = ChangeObject(
            id=len(out), kind="appearance" if o.status in ("changed in place", "shifted") else o.status,
            n_points=len(X), centroid=o.centroid.tolist(), centroid_std=float(np.linalg.norm(boots.std(0))),
            extent=o.extent.tolist(), extent_ci=np.quantile(ext_b, [0.05, 0.95], axis=0).T.tolist(),
            frame_votes={int(f): int(c) for f, c in zip(frames, counts)}, recording=o.recording,
            label=o.label, label_prob=round(o.label_prob, 3))
        co.extra.update({"mask": mask, "status": o.status, "sam_id": o.id, "is_background": False,
                         "crop_frame": o.best_frame, "crop_box": o.extra.get("full_bbox"), "stats": o.stats})
        if o.status == "moved":
            co.displacement, co.distance = o.stats["displacement"], o.stats["distance"]
            co.distance_ci, co.match_similarity, co.partner = o.stats["distance_ci"], o.stats["match_similarity"], o.partner
        out.append(co)
    return out


def describe(o: SceneObject, E: np.ndarray, scale: float, unit: str = "cm", scale_range=None) -> str:
    from .describe import direction_words

    name = o.label or "object"
    k = 100 if unit == "cm" else 1
    if o.status == "moved":
        d = o.stats["distance"] * scale * k
        s_lo, s_hi = scale_range or (scale, scale)
        lo, hi = o.stats["distance_ci"][0] * s_lo * k, o.stats["distance_ci"][1] * s_hi * k
        return (f"The {name} moved {direction_words(np.array(o.stats['displacement']), E)} by about {d:.0f} {unit} "
                f"(90% interval {lo:.0f} to {hi:.0f}).")
    return {"removed": f"A {name} that was there before is no longer there.",
            "added": f"A {name} is there now that was not there before.",
            "changed in place": f"The {name} is in the same place but looks different (turned over, deformed or swapped).",
            "shifted": f"The {name}'s surface moved by about {abs(o.stats.get('shift_rel', 0)) * scale * k:.0f} {unit} "
                       "(squashed, pushed or deformed).",
            "unchanged": f"The {name} is unchanged.",
            "not seen in other recording": f"The {name} was only seen in one recording, so it cannot be compared."}.get(
        o.status, "")


def object_rows(objs: list[SceneObject], scale: float, floor_up: np.ndarray, floor_h: float) -> list[dict]:
    rows = []
    for o in objs:
        rows.append({"id": f"{o.recording}{o.id}", "recording": o.recording, "label": o.label,
                     "label_prob": round(o.label_prob, 3), "label_top3": o.label_top3, "status": o.status,
                     "structure": o.structure, "size_cm": (o.extent * scale * 100).round(1).tolist(),
                     "height_m": round(float((o.centroid @ floor_up - floor_h) * scale), 2),
                     "views": o.extra.get("n_views"), "complete": o.complete,
                     "partner": None if o.partner is None else f"{'B' if o.recording == 'A' else 'A'}{o.partner}",
                     "description": o.extra.get("description"),
                     "position_cm": o.extra.get("position_cm"),
                     "stats": {k: v for k, v in o.stats.items() if not isinstance(v, np.ndarray)}})
    return rows


CANON = {"sofa cushion": "cushion", "seat cushion": "cushion", "woven cushion": "cushion",
         "patterned cushion": "cushion", "grey cushion": "cushion", "cushion cover": "cushion",
         "cap": "baseball cap", "acoustic guitar": "guitar", "throw blanket": "blanket", "folded blanket": "blanket",
         "throw": "blanket", "tv remote": "remote control", "houseplant": "plant", "framed print": "picture frame",
         "picture": "picture frame", "stack of books": "book", "leather sofa": "sofa", "sofa arm": "sofa",
         "sofa back": "sofa", "carpet": "rug", "wooden floor": "floor", "tiled floor": "floor"}


def canonical(label: str | None) -> str | None:
    return CANON.get(label, label)


def masked_crop(o: SceneObject, rec: Recon, margin: float = 0.15, max_side: int = 336, frame: int | None = None):
    """Crop of one view of the object (default its best view) at full resolution, background
    faded to grey."""
    from PIL import Image

    frame = o.best_frame if frame is None else frame
    full = Image.open(rec.files[frame]).convert("RGB")
    m = Image.fromarray(o.masks[frame].astype(np.uint8) * 255).resize(full.size, Image.Resampling.NEAREST)
    x0, y0, x1, y1 = (o.extra.get("full_bbox") if frame == o.best_frame else None) or m.getbbox()
    mw, mh = (x1 - x0) * margin + 6, (y1 - y0) * margin + 6
    box = (int(max(0, x0 - mw)), int(max(0, y0 - mh)), int(min(full.width, x1 + mw)), int(min(full.height, y1 + mh)))
    img = np.asarray(full.crop(box)).astype(np.float32)
    mk = np.asarray(m.crop(box))[..., None] > 127
    img = np.where(mk, img, 0.35 * img + 0.65 * 128).astype(np.uint8)
    out = Image.fromarray(img)
    out.thumbnail((max_side, max_side))
    return out, box


def label_views(o: SceneObject, n: int = 4) -> list[int]:
    """Up to `n` views of the object for labelling: its best view plus the largest other masks,
    spread over the recording (whole object, several sides)."""
    others = sorted((f for f in o.masks if f != o.best_frame), key=lambda f: -int(o.masks[f].sum()))
    others = sorted(others[: 3 * n])                       # big views, then spread in time
    pick = [others[int(round(i))] for i in np.linspace(0, len(others) - 1, min(n - 1, len(others)))] if others else []
    return [o.best_frame] + sorted(set(pick))


def label_all(objs: list[SceneObject], rec: Recon, crop_dir=None, n_views: int = 4) -> None:
    """CLIP label (top 3) and image embedding for every object, from the mean embedding of up to
    `n_views` masked views of it (the Rigidity Copilot reads the same whole-object embedding)."""
    from .features import BACKGROUND, clip_inventory

    crops, groups, best = [], [], []
    for o in objs:
        g = []
        for f in label_views(o, n_views):
            try:
                img, box = masked_crop(o, rec, frame=f)
            except Exception:  # noqa: BLE001 - an empty or odd mask in one view
                continue
            if f == o.best_frame:
                o.extra["crop_box"] = box
                best.append(img)
            g.append(len(crops))
            crops.append(img)
        if not g or len(best) < len(groups) + 1:           # best view must exist
            img, box = masked_crop(o, rec)
            o.extra["crop_box"] = box
            best.append(img)
            g = g + [len(crops)]
            crops.append(img)
        groups.append(g)
        o.extra["label_views"] = len(g)
    tops, embs = clip_inventory(crops, groups=groups)
    for o, top, e, img in zip(objs, tops, embs, best):
        o.label, o.label_prob = canonical(top[0][0]), top[0][1]
        o.label_top3 = [(lab, round(p, 3)) for lab, p in top]
        o.extra["clip"] = e
        o.extra["background"] = o.label in BACKGROUND or o.structure
        if crop_dir is not None:
            crop_dir.mkdir(parents=True, exist_ok=True)
            img.save(crop_dir / f"{o.recording}{o.id:03d}_{(o.label or 'object').replace(' ', '_')}.jpg", quality=82)
