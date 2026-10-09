"""From changed points to change objects: cluster, match (re-identify), measure, with uncertainty.

- Removed candidates = clusters of A points that B sees through ('gone' in A->B).
- Added candidates = clusters of B points that A sees through ('gone' in B->A).
- Appearance changes = clusters of points whose geometry agrees but look differs.
- A removed and an added cluster that look alike (cosine similarity of mean appearance
  features) and have similar size are one object that **moved**; its displacement is the
  difference of centroids.

Uncertainty, all label-free:
- centroid / displacement: bootstrap over the cluster's points (sampling spread), combined
  with the reconstruction noise floor (tau x local depth) from the within-recording check;
- size: bootstrap of the 5th-95th percentile extent along the cluster's principal axes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .detect import APPEARANCE, GONE, ChangeResult, PointSet


@dataclass
class ChangeObject:
    id: int
    kind: str                         # removed | added | moved | appearance
    n_points: int
    centroid: list                    # world, scene units
    centroid_std: float               # 1-sigma, scene units
    extent: list                      # 5-95% extent along principal axes, scene units
    extent_ci: list                   # [[lo, hi]] 90% bootstrap interval per axis
    frame_votes: dict                 # source frame index -> number of points (for crops)
    recording: str                    # "A" or "B" (where the points come from)
    displacement: list | None = None  # moved only, B - A
    distance: float | None = None
    distance_ci: list | None = None   # 90% interval
    match_similarity: float | None = None
    partner: int | None = None
    label: str | None = None
    label_prob: float | None = None
    description: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _voxel_down(X: np.ndarray, voxel: float):
    keys = np.floor(X / voxel).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    return np.sort(first)


def cluster(X: np.ndarray, eps: float, min_points: int, min_samples: int = 10) -> np.ndarray:
    """DBSCAN labels (-1 = noise) on a voxel-downsampled copy, propagated to all points."""
    from scipy.spatial import cKDTree
    from sklearn.cluster import DBSCAN

    labels = np.full(len(X), -1)
    if len(X) < min_points:
        return labels
    keep = _voxel_down(X, eps / 2)
    lab = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(X[keep])
    _, nn = cKDTree(X[keep]).query(X, k=1)
    d_ok = np.linalg.norm(X - X[keep][nn], axis=1) <= eps
    labels = np.where(d_ok, lab[nn], -1)
    for c in np.unique(labels[labels >= 0]):
        if (labels == c).sum() < min_points:
            labels[labels == c] = -1
    return labels


def _bootstrap(X: np.ndarray, rng, n: int = 100):
    cents, exts = [], []
    m = min(len(X), 5000)
    for _ in range(n):
        s = X[rng.integers(0, len(X), m)]
        cents.append(s.mean(0))
        exts.append(_extent(s))
    return np.array(cents), np.array(exts)


def _extent(X: np.ndarray) -> np.ndarray:
    c = X - X.mean(0)
    _, _, vt = np.linalg.svd(c[:: max(1, len(c) // 5000)], full_matrices=False)
    p = c @ vt.T
    return np.percentile(p, 95, axis=0) - np.percentile(p, 5, axis=0)


def _make(ps: PointSet, mask: np.ndarray, kind: str, rec_name: str, oid: int, noise: float, rng) -> ChangeObject:
    X = ps.X[mask]
    cents, exts = _bootstrap(X, rng)
    frames, counts = np.unique(ps.frame[mask], return_counts=True)
    obj = ChangeObject(
        id=oid, kind=kind, n_points=int(mask.sum()), centroid=X.mean(0).round(4).tolist(),
        centroid_std=float(np.sqrt(cents.std(0).mean() ** 2 + noise ** 2)),
        extent=_extent(X).round(4).tolist(),
        extent_ci=np.percentile(exts, [5, 95], axis=0).T.round(4).tolist(),
        frame_votes={int(f): int(c) for f, c in zip(frames, counts)}, recording=rec_name)
    if ps.feat is not None:
        f = ps.feat[mask].astype(np.float32).mean(0)
        obj.extra["feat"] = f / (np.linalg.norm(f) + 1e-9)
    obj.extra["mask"] = mask
    return obj


def find_objects(res: ChangeResult, eps_frac: float = 0.03, min_frac: float = 0.002,
                 min_points: int = 200, match_sim: float = 0.7, seed: int = 0) -> list[ChangeObject]:
    rng = np.random.default_rng(seed)
    scale = res.thresholds["scene_scale_median_depth"]
    eps = eps_frac * scale
    noise = res.thresholds["tau_rel_depth"] * scale / 2
    objs: list[ChangeObject] = []
    for ps, name, kind_gone in ((res.A, "A", "removed"), (res.B, "B", "added")):
        n_min = max(min_points, int(min_frac * len(ps.X)))
        for cls, kind in ((GONE, kind_gone), (APPEARANCE, "appearance")):
            idx = np.nonzero(ps.cls == cls)[0]
            if len(idx) < n_min:
                continue
            lab = cluster(ps.X[idx], eps, n_min)
            for c in np.unique(lab[lab >= 0]):
                mask = np.zeros(len(ps.X), bool)
                mask[idx[lab == c]] = True
                objs.append(_make(ps, mask, kind, name, len(objs), noise, rng))

    # re-identification: greedy on appearance similarity, with a size sanity check
    rem = [o for o in objs if o.kind == "removed" and "feat" in o.extra]
    add = [o for o in objs if o.kind == "added" and "feat" in o.extra]
    pairs = []
    for r in rem:
        for a in add:
            sim = float(r.extra["feat"] @ a.extra["feat"])
            size = np.prod(np.maximum(r.extent, 1e-6)) / np.prod(np.maximum(a.extent, 1e-6))
            if sim >= match_sim and 1 / 4 <= size <= 4:
                pairs.append((sim, r, a))
    used = set()
    for sim, r, a in sorted(pairs, key=lambda p: -p[0]):
        if r.id in used or a.id in used:
            continue
        used |= {r.id, a.id}
        XA, XB = res.A.X[r.extra["mask"]], res.B.X[a.extra["mask"]]
        ca, _ = _bootstrap(XA, rng)
        cb, _ = _bootstrap(XB, rng)
        dist = np.linalg.norm(cb - ca, axis=1)
        disp = np.array(a.centroid) - np.array(r.centroid)
        noise_d = np.sqrt(2) * noise
        lo, hi = np.percentile(dist, [5, 95])
        r.kind, a.kind = "moved", "moved"
        for o, p in ((r, a), (a, r)):
            o.displacement = disp.round(4).tolist()
            o.distance = float(np.linalg.norm(disp))
            o.distance_ci = [float(max(0, lo - 1.645 * noise_d)), float(hi + 1.645 * noise_d)]
            o.match_similarity = sim
            o.partner = p.id
    return objs


def summarise(objs: list[ChangeObject]) -> list[dict]:
    """One entry per real-world change (a moved pair counts once). JSON-safe."""
    out, seen = [], set()
    for o in sorted(objs, key=lambda o: -o.n_points):
        if o.id in seen:
            continue
        seen.add(o.id)
        if o.partner is not None:
            seen.add(o.partner)
        d = {k: v for k, v in o.as_dict().items() if k != "extra"}
        d["frame_votes"] = dict(sorted(o.frame_votes.items(), key=lambda kv: -kv[1])[:5])
        out.append(d)
    return out
