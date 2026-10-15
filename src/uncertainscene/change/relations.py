"""Where each object is relative to the others: "on top of" and "next to", from the point clouds.

For every pair of objects in one recording (scene units, scaled to metres with `scale`):

- **on top of**: the lowest points of A sit on B. Of A's bottom 20% of points (along the floor's
  up vector), at least `min_share` have B points within `foot_m` horizontally whose height is
  just below them (0 to `gap_m`), and A's centre is above B's.
- **next to**: the closest points of A and B are within `near_m` and neither is on the other.

Each object keeps the object it rests on (most supporting points) and its nearest neighbours.
The relations are written to `o.extra["context"]` and as one sentence for the description.
"""
from __future__ import annotations

import numpy as np


def _basis(up: np.ndarray) -> np.ndarray:
    up = up / np.linalg.norm(up)
    a = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, a)
    e1 /= np.linalg.norm(e1)
    return np.stack([e1, np.cross(up, e1), up])          # rows: two horizontal axes, then up


def name(o) -> str:
    return f"{o.label or 'object'} {o.recording}{o.id}"


def relations(objs: list, scale: float, up: np.ndarray, foot_m: float = 0.03, gap_m: float = 0.05,
              min_share: float = 0.3, near_m: float = 0.15, max_neighbours: int = 3, max_points: int = 8000,
              seed: int = 0) -> list[dict]:
    """Relations between objects of the same recording; also fills o.extra["context"]."""
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(seed)
    B = _basis(np.asarray(up, float))
    P = []
    for o in objs:
        X = np.asarray(o.X, float)
        if len(X) > max_points:
            X = X[rng.choice(len(X), max_points, replace=False)]
        P.append((X @ B.T) * scale)                       # metres: (h1, h2, height)
    trees3 = [cKDTree(p) for p in P]
    trees2 = [cKDTree(p[:, :2]) for p in P]
    lo = [np.quantile(p[:, 2], 0.2) for p in P]
    out = []
    for o in objs:
        o.extra["context"] = {"on_top_of": None, "next_to": [], "supports": []}
    for i, a in enumerate(objs):
        for j, b in enumerate(objs):
            if j <= i or a.recording != b.recording or (getattr(a, "structure", False) and getattr(b, "structure", False)):
                continue                                  # (wall-sized structures only as supports)
            if np.any(P[i].min(0) > P[j].max(0) + near_m) or np.any(P[j].min(0) > P[i].max(0) + near_m):
                continue
            d, _ = trees3[j].query(P[i], k=1, distance_upper_bound=near_m)
            gap = float(d.min()) if np.isfinite(d).any() else np.inf
            if not np.isfinite(gap):
                continue
            rel = None
            for top, bot in ((i, j), (j, i)):
                share = _support(P[top], lo[top], trees2[bot], P[bot], foot_m, gap_m)
                if share >= min_share and np.median(P[top][:, 2]) > np.median(P[bot][:, 2]):
                    rel = {"a": name(objs[top]), "b": name(objs[bot]), "relation": "on top of",
                           "share": round(share, 2), "gap_cm": round(100 * gap, 1), "top": top, "bottom": bot}
                    break
            if rel is None:
                rel = {"a": name(a), "b": name(b), "relation": "next to", "gap_cm": round(100 * gap, 1)}
            out.append(rel)
    # per object: what it rests on (best support), what rests on it, nearest neighbours
    for r in out:
        if r["relation"] == "on top of":
            t, s = objs[r["top"]], objs[r["bottom"]]
            cur = t.extra["context"]["on_top_of"]
            if cur is None or r["share"] > cur["share"]:
                t.extra["context"]["on_top_of"] = {"object": r["b"], "share": r["share"]}
            s.extra["context"]["supports"].append(r["a"])
    for o in objs:
        me = name(o)
        near = sorted([(r["gap_cm"], r["b"] if r["a"] == me else r["a"]) for r in out
                       if r["relation"] == "next to" and me in (r["a"], r["b"])])
        o.extra["context"]["next_to"] = [{"object": n, "gap_cm": g} for g, n in near[:max_neighbours]]
        o.extra["context"]["sentence"] = sentence(o)
    for r in out:
        r.pop("top", None)
        r.pop("bottom", None)
    return out


def _support(Pt: np.ndarray, lo_t: float, tree2_b, Pb: np.ndarray, foot_m: float, gap_m: float) -> float:
    """Share of the top object's lowest points that have the other object just beneath them."""
    low = Pt[Pt[:, 2] <= lo_t]
    if not len(low):
        return 0.0
    hits = 0
    for p, nb in zip(low, tree2_b.query_ball_point(low[:, :2], foot_m)):
        if nb:
            dz = p[2] - Pb[nb, 2]
            hits += bool(np.any((dz >= -0.01) & (dz <= gap_m)))
    return hits / len(low)


def _short(n: str) -> str:
    return "the " + n.rsplit(" ", 1)[0]


def sentence(o) -> str:
    c = o.extra.get("context") or {}
    parts = []
    if c.get("on_top_of"):
        parts.append(f"on top of {_short(c['on_top_of']['object'])}")
    if c.get("next_to"):
        parts.append("next to " + ", ".join(_short(n["object"]) for n in c["next_to"]))
    if not parts:
        return ""
    s = "It is " + " and ".join(parts) + "."
    if c.get("supports"):
        s += " Resting on it: " + ", ".join(_short(n) for n in c["supports"][:4]) + "."
    return s
