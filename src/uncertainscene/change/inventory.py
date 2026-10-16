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
                "moved": (255, 127, 14), "changed in place": (148, 103, 189), "shifted": (23, 190, 207), "deformed": (227, 119, 194),
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


def _det_named(o: SceneObject) -> bool:
    return o.extra.get("label_source") == "detector"


def _det_labels_differ(a: SceneObject, b: SceneObject) -> bool:
    return _det_named(a) and _det_named(b) and a.label != b.label


def consistent_partners(objs_a: list[SceneObject], objs_b: list[SceneObject], same_sim: float = 0.8,
                        min_iou: float = 0.5) -> int:
    """Make same-object links agree in both directions. Run 6: the neck pillow in A was
    "removed" while the neck pillow in B was "unchanged" with A's pillow as its partner. When an
    object is unchanged and points at an object of the other recording that lies in the same
    place (voxel IoU >= min_iou), looks alike (>= same_sim) and is not named differently by the
    detector, that object is still there: both are unchanged partners. Otherwise the one-sided
    link is dropped. Returns how many statuses were corrected."""
    fixed = 0
    for objs, others in ((objs_a, objs_b), (objs_b, objs_a)):
        for o in objs:
            if o.status != "unchanged" or o.partner is None:
                continue
            p = others[o.partner]
            if p.status == "unchanged" and p.partner == o.id:
                continue
            strong = (o.stats.get("counterpart_iou", 0) >= min_iou and (o.stats.get("counterpart_sim") or 0) >= same_sim
                      and not _det_labels_differ(o, p))
            if strong and p.status in ("removed", "added", "unchanged") and (p.partner is None or p.partner == o.id):
                if p.status != "unchanged":
                    fixed += 1
                p.status, p.partner = "unchanged", o.id
            else:
                o.partner = None
    return fixed


def _set_moved(a: SceneObject, b: SceneObject, sim: float) -> None:
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


def _sure_label(o, min_prob: float) -> bool:
    return o.label is not None and (o.extra.get("label_source") == "detector" or (o.label_prob or 0) >= min_prob)


def pair_by_identity(objs_a: list[SceneObject], objs_b: list[SceneObject], min_prob: float = 0.5,
                     min_sim: float = 0.6, same_frac: float = 0.5) -> int:
    """One object, one verdict. Run 10: the neck pillow was "removed" in A (its points were not
    found in B) and a neck pillow in B, in the same place, was "unchanged" with no partner: two
    point clouds, two opposite answers, because the recordings sit ~12 cm apart and the
    point-overlap match missed it. Here an unpaired object with a sure name (detector, or CLIP
    >= min_prob) that one side calls removed/added is paired with the unpaired object of the
    same sure name in the other recording (similar size, looks alike >= min_sim), nearest first:
    within `same_frac` of its size they are the same object, unchanged; further apart, moved.
    Returns how many pairs were made."""
    def free(o, gone_word):
        return (not o.structure and not o.extra.get("background") and o.partner is None
                and o.status in (gone_word, "unchanged", "shifted"))

    A = [o for o in objs_a if free(o, "removed") and _sure_label(o, min_prob)]
    B = [o for o in objs_b if free(o, "added") and _sure_label(o, min_prob)]
    cand = []
    for a in A:
        for b in B:
            if a.label != b.label or (a.status != "removed" and b.status != "added"):
                continue
            size = max(float(np.max(a.extent)), float(np.max(b.extent)))
            ratio = (float(np.max(a.extent)) + 1e-9) / (float(np.max(b.extent)) + 1e-9)
            sim = _sim(a, b)
            if not 1 / 1.6 < ratio < 1.6 or sim < min_sim:
                continue
            d = float(np.linalg.norm(np.asarray(b.centroid) - np.asarray(a.centroid)))
            cand.append((d / size, sim, a, b))
    used, n = set(), 0
    for rel, sim, a, b in sorted(cand, key=lambda t: t[0]):
        if id(a) in used or id(b) in used:
            continue
        used |= {id(a), id(b)}
        n += 1
        if rel < same_frac:
            a.status = b.status = "unchanged"
            a.partner, b.partner = b.id, a.id
        else:
            _set_moved(a, b, sim)
        for o in (a, b):
            o.stats["paired_by_identity"] = True
    if n:
        log.info("objects: %d one-sided objects paired with the same named object in the other recording", n)
    return n


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
    consistent_partners(objs_a, objs_b, same_sim)
    pair_by_identity(objs_a, objs_b)
    # moved = removed in A + added in B that look alike
    rem = [o for o in objs_a if o.status == "removed" and not o.structure]
    add = [o for o in objs_b if o.status == "added" and not o.structure]
    cand = []
    for a in rem:
        for b in add:
            sim = _sim(a, b)
            ratio = (a.extent[0] + 1e-9) / (b.extent[0] + 1e-9)
            same_label = a.label is not None and a.label == b.label
            if _det_labels_differ(a, b):
                continue                        # both named by the detector, and differently: not one object
            bonus = 0.1 if same_label and _det_named(a) and _det_named(b) else 0.05 * same_label
            if sim >= move_sim and (same_label or 1 / 1.6 < ratio < 1.6):
                cand.append((sim + bonus, a, b))
    used = set()
    for sim, a, b in sorted(cand, key=lambda t: -t[0]):
        if a.id in used or ("B", b.id) in used:
            continue
        used |= {a.id, ("B", b.id)}
        _set_moved(a, b, sim)
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
        if o.status in ("moved", "deformed") and o.recording == "B":
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
            "deformed": f"The {name} is in the same place but its shape changed (deformed).",
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
                     "label_source": o.extra.get("label_source"), "detector_votes": o.extra.get("det_votes"),
                     "continuity": {k: v for k, v in (o.extra.get("continuity") or {}).items()
                                    if k in ("tracks", "runs", "frames_seen", "frames_total", "span_share")} or None,
                     "affordance": o.extra.get("affordance"),
                     "context": {k: v for k, v in (o.extra.get("context") or {}).items() if k != "sentence"} or None,
                     "change": o.extra.get("change"), "change_sentence": o.extra.get("change_sentence"),
                     "pose": (o.extra.get("pose") or {}).get("words"), "coverage": o.extra.get("coverage"),
                     "rigidity": (o.extra.get("rigidity") or {}).get("rigidity"),
                     "tracks": o.extra.get("tracks"), "pose_copy_removed": o.extra.get("pose_copy_removed"),
                     "name_size_warning": o.extra.get("name_size_warning"),
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


def detector_label(votes: dict, min_votes: float = 0.5, min_share: float = 0.6):
    """(label, share of votes, top 3) from detector votes {label: summed confidence x IoU over
    views}, or None when they are too few (about two confident views) or split."""
    tot = sum(votes.values())
    if tot < min_votes:
        return None
    top = sorted(votes.items(), key=lambda kv: -kv[1])[:3]
    if top[0][1] < min_share * tot:
        return None
    return top[0][0], round(top[0][1] / tot, 3), [(lab, round(v / tot, 3)) for lab, v in top]


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
    from .detect2d import SUPPORT

    for o, top, e, img in zip(objs, tops, embs, best):
        o.label, o.label_prob = canonical(top[0][0]), top[0][1]
        o.label_top3 = [(lab, round(p, 3)) for lab, p in top]
        o.extra["clip_top3"] = o.label_top3
        o.extra["label_source"] = "clip"
        det = detector_label(o.extra.get("det_votes") or {})
        if det is not None:             # whole-object detector votes over many views beat one CLIP crop
            o.label, o.label_prob, o.label_top3 = det
            o.extra["label_source"] = "detector"
        o.extra["clip"] = e
        o.extra["background"] = o.label in BACKGROUND or o.label in SUPPORT or o.structure
        if crop_dir is not None:
            crop_dir.mkdir(parents=True, exist_ok=True)
            img.save(crop_dir / f"{o.recording}{o.id:03d}_{(o.label or 'object').replace(' ', '_')}.jpg", quality=82)


def prune_unsure(objs: list[SceneObject], min_views: int = 3, min_prob: float = 0.5, sure_views: int = 20) -> list[str]:
    """Drop objects the pipeline is not sure are objects (Fin, 2026-10-15: "ball" in run 10 was
    a CLIP guess at 0.10 from 10 views; "want to be sure on the object to act on it"):
    non-structure objects seen in fewer than `min_views` frames, or named only by CLIP below
    `min_prob` and seen in fewer than `sure_views` frames. A well-tracked object with a weak name
    stays (it is still one thing, and dropping it would leave a hole) but is marked unsure and
    gets no robot actions. Detector-named objects stay. Updates objs in place (ids renumbered
    per recording); returns what was dropped."""
    gone, names = set(), []
    for o in objs:
        if o.structure:
            continue
        views = o.extra.get("n_views", len(getattr(o, "masks", {}) or {}))
        sure_name = o.extra.get("label_source") == "detector" or (o.label_prob or 0) >= min_prob
        o.extra["unsure"] = not sure_name
        if views < min_views or (not sure_name and views < sure_views):
            gone.add(id(o))
            names.append(f"{o.recording}{o.id} {o.label} ({o.extra.get('label_source')} {o.label_prob or 0:.2f}, "
                         f"{views} views)")
    if gone:
        for rec in sorted({o.recording for o in objs}):
            for i, o in enumerate([o for o in objs if o.recording == rec and id(o) not in gone]):
                o.id = i
        objs[:] = [o for o in objs if id(o) not in gone]
    return names


def check_label_sizes(objs: list[SceneObject], scale_m: float, slack: float = 1.25) -> int:
    """Drop names that cannot fit the object's size: when the largest size is over `slack` times
    the class's largest plausible size (detect2d.MAX_SIZE_M), take the first CLIP guess that
    fits, else call it a large surface (background). Returns how many names changed."""
    from .detect2d import MAX_SIZE_M, SUPPORT
    from .features import BACKGROUND

    changed = 0
    for o in objs:
        size = float(np.max(o.extent)) * scale_m
        lim = MAX_SIZE_M.get(o.label)
        if lim is None or size <= slack * lim:
            continue
        fits = [(lab, p) for lab, p in (o.extra.get("clip_top3") or [])
                if MAX_SIZE_M.get(canonical(lab), np.inf) * slack >= size]
        old = o.label
        o.label, o.label_prob = (canonical(fits[0][0]), fits[0][1]) if fits else ("large surface", 0.0)
        o.extra["label_source"] = "size check"
        o.extra["label_before_size_check"] = old
        o.extra["background"] = o.label in BACKGROUND or o.label in SUPPORT or o.structure or o.label == "large surface"
        changed += 1
    return changed


def _diag(o) -> float:
    X = np.asarray(o.X, float)
    return float(np.linalg.norm(np.quantile(X, 0.95, axis=0) - np.quantile(X, 0.05, axis=0))) if len(X) else 0.0


def inside_in_views(p, q, min_frames: int = 2, min_share: float = 0.6, min_inside: float = 0.7) -> bool:
    """Whether p's mask lies inside q's box (at least `min_inside` of it) in at least
    `min_share` of the frames where both were segmented (and in at least `min_frames`): a strap,
    a handle or a panel that the tracker kept apart from the object it belongs to."""
    shared = sorted(set(p.masks) & set(q.masks))
    if len(shared) < min_frames:
        return False
    hits = 0
    for s in shared:
        mp, mq = p.masks[s], q.masks[s]
        if mp.shape != mq.shape or not mq.any() or not mp.any():
            continue
        ys, xs = np.nonzero(mq)
        box = np.zeros_like(mq)
        box[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = True
        hits += np.logical_and(mp, box).sum() / mp.sum() >= min_inside
    return hits >= max(min_frames, min_share * len(shared))


def group_pieces(objs: list[SceneObject], scale_m: float, gap_m: float = 0.03) -> int:
    """Join pieces of one object that tracking left apart (run 10: the guitar in after was six
    objects, one labelled guitar by the detector, four more labelled guitar by CLIP, all touching).
    A piece is a non-structure object labelled by CLIP only; it joins a touching (closest points
    within `gap_m`) larger non-structure object of the same recording that the detector named,
    either with the same label or, when the piece is under 0.6 of its size, when the piece lies
    inside that object's box in most images where both appear (inside_in_views). Two detector-named objects are never joined (the detector saw
    them as separate instances, e.g. two cushions). Updates objs in place (ids renumbered per
    recording); returns how many pieces were joined."""
    from scipy.spatial import cKDTree

    from .segment import _extent

    joined = 0
    for rec in sorted({o.recording for o in objs}):
        mine = [o for o in objs if o.recording == rec]
        named = [o for o in mine if not o.structure and o.extra.get("label_source") == "detector"]
        trees = {id(q): cKDTree(np.asarray(q.X, float)) for q in named}
        gone = set()
        for p in sorted(mine, key=_diag):
            if p.structure or p.extra.get("label_source") == "detector" or p.label is None:
                continue
            best, best_d = None, np.inf
            for q in named:
                if _diag(q) <= _diag(p) or id(q) in gone:
                    continue
                # same name, or (any CLIP name) a smaller piece that keeps lying inside q in the images
                if q.label != p.label and not (_diag(p) < 0.6 * _diag(q) and inside_in_views(p, q)):
                    continue
                d = float(trees[id(q)].query(np.asarray(p.X, float), k=1)[0].min()) * scale_m
                if d < gap_m and d < best_d:
                    best, best_d = q, d
            if best is None:
                continue
            q = best
            q.X = np.concatenate([q.X, p.X])
            q.frame = np.concatenate([q.frame, p.frame])
            q.pix = np.concatenate([q.pix, p.pix])
            q.voxels = q.voxels | p.voxels
            for s, m in p.masks.items():
                q.masks[s] = (q.masks[s] | m) if s in q.masks else m
            for k in ("fine_vox",):
                if k in q.extra and k in p.extra:
                    q.extra[k] = q.extra[k] | p.extra[k]
            q.extra["tracks"] = sorted(set(q.extra.get("tracks", [])) | set(p.extra.get("tracks", [])))
            q.extra["pieces_joined"] = q.extra.get("pieces_joined", []) + [f"{p.recording}{p.id} ({p.label}, CLIP)"]
            q.extra["n_views"] = len(q.masks)
            q.centroid, q.extent = _extent(q.X)
            trees[id(q)] = cKDTree(np.asarray(q.X, float))
            gone.add(id(p))
            joined += 1
        if gone:
            keep = [o for o in mine if id(o) not in gone]
            for i, o in enumerate(keep):
                o.id = i
            objs[:] = [o for o in objs if o.recording != rec or id(o) not in gone]
    return joined


def _size_z(label: str | None, size_m: float, scale_sd_log: float) -> tuple[float | None, float]:
    """How far a measured largest size (m) is from a label's typical one, in sds (None: no size)."""
    from .sizes import size_prior

    pr = size_prior(canonical(label)) if label else None
    if not pr or size_m <= 0:
        return None, 1.0
    sd = float(np.sqrt(pr[2] ** 2 + 0.15 ** 2 + scale_sd_log ** 2))
    return float(np.log(size_m / pr[0]) / sd), sd


def _name_candidates(o) -> dict:
    """Name -> weight from the detector / CLIP name and CLIP's top 3."""
    c: dict = {}
    if o.label:
        c[o.label] = 0.75 if o.extra.get("label_source") == "detector" else float(o.label_prob or 0)
    for lab, p in (o.extra.get("clip_top3") or o.label_top3 or []):
        lab = canonical(lab)
        c[lab] = max(c.get(lab, 0.0), float(p))
    return c


def names_by_size(objs_a: list[SceneObject], objs_b: list[SceneObject], scale_m: float, scale_sd_log: float = 0.1,
                  bad_z: float = 2.5) -> list[str]:
    """One name per physical object that also fits its measured size (Run 11: a 30 cm glass vase
    was a detector "cup" that moved 146 cm; a table tennis bat was a detector "lamp" in one
    recording and "table tennis bat" by CLIP in the other). Partners (the same object in both
    recordings) pool their candidate names; each candidate is scored by its weight times how
    well the typical size (sizes.py) fits the measured core size of both clouds. A name is only
    changed when the current one fits badly (beyond `bad_z` sds) or the partners disagree, and
    then to the best-scoring candidate. Returns what changed."""
    from .floor import core_extent

    by_b = {o.id: o for o in objs_b}
    groups = []
    for a in objs_a:
        b = by_b.get(a.partner) if a.partner is not None else None
        groups.append([a] + ([b] if b is not None else []))
    paired_b = {id(g[1]) for g in groups if len(g) > 1}
    groups += [[b] for b in objs_b if id(b) not in paired_b]
    out = []
    for g in groups:
        if any(o.structure or o.extra.get("background") for o in g):
            continue
        sizes = [core_extent(o.X)[0] * scale_m if len(o.X) >= 30 else float(np.max(o.extent)) * scale_m for o in g]
        cand: dict = {}
        for o in g:
            for lab, w in _name_candidates(o).items():
                cand[lab] = cand.get(lab, 0.0) + w / len(g)
        scores = {}
        for lab, w in cand.items():
            zs = [_size_z(lab, s, scale_sd_log) for s in sizes]
            if zs[0][0] is None:
                scores[lab] = (w * 0.3, None)                # a name without a typical size: weak, kept as an option
            else:
                scores[lab] = (w * float(np.prod([np.exp(-0.5 * z * z) / sd for z, sd in zs])) ** (1 / len(zs)),
                               max(abs(z) for z, _ in zs))
        if not scores:
            continue
        best = max(scores, key=lambda k: scores[k][0])
        cur = [o.label for o in g]
        cur_z = [scores.get(lab, (0, None))[1] for lab in cur]
        disagree = len(set(cur)) > 1
        bad = any(z is not None and z > bad_z for z in cur_z)
        if not (disagree or bad) or best in cur and not disagree:
            for o, z in zip(g, cur_z):
                o.extra["name_size_z"] = None if z is None else round(z, 2)
            continue
        if bad and not disagree and (scores[best][1] is None or scores[best][1] > 1.5):
            for o, z in zip(g, cur_z):      # nothing better fits: keep the name, flag it
                o.extra["name_size_z"] = None if z is None else round(z, 2)
                o.extra["name_size_warning"] = f"a typical {o.label} is not this size ({z:.1f} sd)"
            continue
        for o in g:
            if o.label != best:
                out.append(f"{o.recording}{o.id} {o.label} -> {best} ({sizes[g.index(o)] * 100:.0f} cm)")
                o.extra["label_before_size_names"] = o.label
                o.label = best
                o.label_prob = round(float(min(0.95, cand[best])), 3)
                o.extra["label_source"] = "size + names"
            o.extra["name_size_z"] = None if scores[best][1] is None else round(scores[best][1], 2)
    if out:
        log.info("names by size: %d changed: %s", len(out), "; ".join(out))
    return out


def scale_check(objs: list[SceneObject], scale_m: float, scale_range=None, n: int = 8) -> dict:
    """Is the metric scale right? For up to `n` objects whose names are sure (detector, or CLIP >=
    0.5) and have a typical size, the measured size at the chosen scale against the typical
    size: ratio and sds. Most should be within 2 sds; a scale that is off shows as most ratios
    on one side."""
    from .floor import core_extent
    from .sizes import size_prior

    sd_scale = float(np.log(scale_range[1] / scale_range[0]) / (2 * 1.645)) if scale_range else 0.1
    rows = []
    for o in sorted(objs, key=lambda o: -(0.75 if o.extra.get("label_source") == "detector" else o.label_prob or 0)):
        pr = size_prior(canonical(o.label)) if o.label else None
        sure = o.extra.get("label_source") in ("detector", "size + names") or (o.label_prob or 0) >= 0.5
        if o.structure or not pr or not sure or o.extra.get("unsure") or not o.complete:
            continue
        size = (core_extent(o.X)[0] if len(o.X) >= 30 else float(np.max(o.extent))) * scale_m
        z, _ = _size_z(o.label, size, sd_scale)
        rows.append({"object": f"{o.recording}{o.id} {o.label}", "typical_cm": round(pr[0] * 100, 1),
                     "measured_cm": round(size * 100, 1), "ratio": round(size / pr[0], 2), "sd": round(z, 2),
                     "fits": bool(abs(z) <= 2)})
        if len(rows) >= n:
            break
    if not rows:
        return {"objects": [], "verdict": "no sure object with a typical size to check the scale against"}
    r = np.array([x["ratio"] for x in rows])
    fit = sum(x["fits"] for x in rows)
    med = float(np.median(r))
    verdict = (f"{fit} of {len(rows)} objects fit their typical size at {scale_m:.3f} m/unit (median measured/typical "
               f"{med:.2f})" + ("; the scale looks right" if fit >= 0.6 * len(rows) and 0.75 <= med <= 1.33 else
                                 "; objects come out " + ("too big: scale may be too large" if med > 1 else
                                                         "too small: scale may be too small")))
    return {"objects": rows, "median_ratio": round(med, 3), "fit": fit, "verdict": verdict}
