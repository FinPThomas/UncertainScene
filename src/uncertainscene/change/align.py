"""Line the after recording up with the before recording on the objects both agree did not move.

Run 10: the joint bundle adjustment ties the recordings together only through points seen in
both (1018), and the before and after clouds still sat ~12 cm, 8.6 deg and 14% in scale apart
(motion3d's correction). At that offset an object that stayed put no longer overlaps itself, so
the object matches fail and the same neck pillow is "removed" in one recording and "unchanged"
in the other.

Here every object pair classified unchanged (furniture and walls included: they are the
largest, most reliable anchors) gives point correspondences by nearest neighbour; a trimmed
similarity fit (ICP with scale) of B onto A over all of them, iterated, gives the correction.
It is applied to B's cameras and depth maps, so everything rebuilt afterwards (points, objects,
matches) lines up. Moved objects are not used, and only the trimmed best 80% of
correspondences enter each fit, so one wrong "unchanged" cannot pull the result.
"""
from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

log = logging.getLogger(__name__)


def static_pairs(objs_a: list, objs_b: list) -> list[tuple]:
    by_b = {o.id: o for o in objs_b}
    out = []
    for a in objs_a:
        b = by_b.get(a.partner) if a.partner is not None else None
        if a.status == "unchanged" and b is not None and b.status == "unchanged" and b.partner == a.id:
            out.append((a, b))
    return out


def fit_b_to_a(pairs: list[tuple], max_points: int = 3000, iters: int = 80, keep: float = 0.8,
               seed: int = 0) -> dict | None:
    """Similarity (s, R, t) with A ~ s R B + t from the unchanged object pairs, by trimmed ICP.
    Returns None with fewer than 3 pairs."""
    from scipy.spatial import cKDTree

    from ..recon.chunked import umeyama

    if len(pairs) < 3:
        return None
    rng = np.random.default_rng(seed)
    src, trees, dsts = [], [], []
    for a, b in pairs:
        XB = np.asarray(b.X, float)
        XA = np.asarray(a.X, float)
        if len(XB) > max_points:
            XB = XB[rng.choice(len(XB), max_points, replace=False)]
        src.append(XB)
        trees.append(cKDTree(XA))
        dsts.append(XA)
    s, R, t = 1.0, np.eye(3), np.zeros(3)
    resid0 = None
    for _ in range(iters):
        P, Q = [], []
        for XB, tree, XA in zip(src, trees, dsts):
            Y = s * XB @ R.T + t
            _, j = tree.query(Y, k=1)
            P.append(XB)
            Q.append(XA[j])
        P, Q = np.concatenate(P), np.concatenate(Q)
        if resid0 is None:
            resid0 = float(np.median(np.linalg.norm(Q - P, axis=1)))
        s2, R2, t2, res = umeyama(P, Q, iters=4, keep=keep)
        step = abs(s2 - s) + np.linalg.norm(R2 - R) + np.linalg.norm(t2 - t)
        s, R, t = s2, R2, t2
        if step < 1e-6:
            break
    return {"s": float(s), "R": R, "t": t, "pairs": len(pairs), "points": int(len(P)),
            "median_gap_before": resid0, "median_gap_after": float(res)}


def apply_to_b(rec, fit: dict):
    """Recon with recording B's cameras and depth maps moved by the fit (A stays the frame)."""
    from ..recon.chunked import transform_cameras

    b = rec.video == 1
    E, D = rec.E.copy(), rec.depth.copy()
    E[b], D[b] = transform_cameras(rec.E[b].astype(np.float64), rec.depth[b], fit["s"], fit["R"], fit["t"])
    scale = None
    if rec.scale is not None:
        scale = np.asarray(rec.scale, float).copy()
        scale[b] *= fit["s"]
    return replace(rec, E=E.astype(rec.E.dtype), depth=D.astype(rec.depth.dtype), scale=scale)


def describe(fit: dict, centre: np.ndarray, scale_m: float) -> dict:
    """Size of the correction in metres and degrees (at the scene centre)."""
    ang = float(np.degrees(np.arccos(np.clip((np.trace(fit["R"]) - 1) / 2, -1, 1))))
    shift = (fit["s"] * fit["R"] @ centre + fit["t"]) - centre
    return {"shift_cm": round(float(np.linalg.norm(shift)) * scale_m * 100, 2), "rotation_deg": round(ang, 3),
            "scale_change_pct": round(100 * (fit["s"] - 1), 2), "pairs": fit["pairs"], "points": fit["points"],
            "median_gap_cm_before": round(fit["median_gap_before"] * scale_m * 100, 2),
            "median_gap_cm_after": round(fit["median_gap_after"] * scale_m * 100, 2)}


def worth_applying(d: dict, min_shift_cm: float = 1.0, min_rot_deg: float = 0.3, min_scale_pct: float = 0.5) -> bool:
    return (d["shift_cm"] >= min_shift_cm or d["rotation_deg"] >= min_rot_deg
            or abs(d["scale_change_pct"]) >= min_scale_pct) and d["median_gap_cm_after"] <= d["median_gap_cm_before"]
