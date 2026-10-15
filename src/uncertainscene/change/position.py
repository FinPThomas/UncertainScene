"""Where each object is, how sure we are, and how that improves with more views and clips.

Two measures, both from one video and neither tuned:

1. Model uncertainty, fused view by view (the main one, Fin 2026-10-10). Each view where SAM 2
   found the object is one measurement of its centre, with a covariance from the models' own
   uncertainty: VGGT's per-pixel depth confidence along the viewing ray, SAM 2's mask wobble
   sideways (change.segment.view_measurement). A Kalman filter (an EKF with a static state:
   the object does not move during a clip) fuses the views in recording order, so the region
   shrinks with every view that carries information, and more clips simply add more views.

2. Views disagreeing (the check). The per-view centres scatter; their covariance divided by
   the number of views is a purely empirical region. Across clips, the scatter is split into
   within-clip and between-clip parts (one-way random effects), so clips that disagree do
   not shrink it.

Consistency check (normalised innovation squared, NIS): before each view is added, the filter
predicts how far that view should land from its current estimate (covariance P + R). The
squared distance in units of that prediction averages 3 (degrees of freedom) when the stated
uncertainties are honest; much more than 3 means the models are overconfident and the
fused region is too small. It is reported, never used to rescale anything.

90% regions use chi-square quantiles (3 dof in 3D, 2 dof for the top-down ellipses).
"""

from __future__ import annotations

from itertools import combinations

import numpy as np

CHI2_3_90 = 6.2514      # 90% quantile, chi-square with 3 degrees of freedom
CHI2_2_90 = 4.6052      # same, 2 degrees of freedom (floor-plan ellipses)
CHI2_3_99 = 11.345


def view_centres(o, min_points: int = 10) -> tuple[np.ndarray, np.ndarray]:
    """(frames, centres [n, 3]) of the views that hold at least `min_points` of the object."""
    fs, P = [], []
    for f in np.unique(o.frame):
        m = o.frame == f
        if m.sum() >= min_points:
            fs.append(int(f))
            P.append(np.median(o.X[m], 0))
    return np.array(fs, int), np.array(P, float).reshape(-1, 3)


def _robust(P: np.ndarray) -> np.ndarray:
    """Drop views whose centre is far from the others (a mask that leaked onto the wall)."""
    if len(P) < 5:
        return P
    med = np.median(P, 0)
    d = np.linalg.norm(P - med, axis=1)
    mad = np.median(d) + 1e-12
    return P[d <= 4 * mad]


def radius90(cov: np.ndarray) -> float:
    """Radius of the sphere with the same volume as the 90% ellipsoid."""
    return float(np.sqrt(CHI2_3_90) * np.prod(np.sqrt(np.clip(np.linalg.eigvalsh(cov), 0, None))) ** (1 / 3))


def axes_sd(cov: np.ndarray) -> np.ndarray:
    """Standard deviations along the ellipsoid's own axes, largest first."""
    return np.sqrt(np.clip(np.linalg.eigvalsh(cov), 0, None))[::-1]


def floor_basis(up: np.ndarray) -> np.ndarray:
    """Two unit vectors spanning the floor plane (rows)."""
    up = np.asarray(up, float) / np.linalg.norm(up)
    a = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 0, 1.0])
    e1 = a - (a @ up) * up
    e1 /= np.linalg.norm(e1)
    return np.stack([e1, np.cross(up, e1)])


def floor_ellipse(mean: np.ndarray, cov: np.ndarray, up: np.ndarray) -> dict:
    """Top-down footprint: 2D centre, semi-axes (90%) and angle in degrees."""
    B = floor_basis(up)
    c2 = B @ cov @ B.T
    w, v = np.linalg.eigh(c2)
    w = np.clip(w, 0, None)
    return {"xy": (B @ mean).tolist(), "a": float(np.sqrt(CHI2_2_90 * w[1])), "b": float(np.sqrt(CHI2_2_90 * w[0])),
            "angle": float(np.degrees(np.arctan2(v[1, 1], v[0, 1])))}


def ellipsoid_points(mean: np.ndarray, cov: np.ndarray, n: int = 14) -> np.ndarray:
    """Three orthogonal rings of the 90% ellipsoid (for the 3D viewer), [3*(n+1), 3] with NaN breaks."""
    w, v = np.linalg.eigh(cov)
    r = np.sqrt(CHI2_3_90 * np.clip(w, 0, None))
    t = np.linspace(0, 2 * np.pi, n + 1)
    rings = []
    for i, j in ((0, 1), (1, 2), (0, 2)):
        ring = mean + np.outer(np.cos(t) * r[i], v[:, i]) + np.outer(np.sin(t) * r[j], v[:, j])
        rings += [ring, np.full((1, 3), np.nan)]
    return np.concatenate(rings)


def measurement_cov(v: dict) -> np.ndarray:
    """Covariance of one view's centre: depth error along the ray, mask wobble sideways
    (or a quarter pixel footprint when SAM 2 gave no wobble) plus the camera-pose uncertainty
    of that frame sideways (a shaky pose makes the view count for less)."""
    r = np.asarray(v["ray"], float)
    side = v["sd_side"] if v.get("sd_side") is not None else v["pixel"] / np.sqrt(12)
    side2 = side ** 2 + (v.get("sd_pose") or 0.0) ** 2
    rr = np.outer(r, r)
    return v["sd_depth"] ** 2 * rr + side2 * (np.eye(3) - rr) + np.eye(3) * 1e-14


def ekf(views: list[dict]) -> dict:
    """Kalman filter over view measurements (static state, measurement = centre). Returns
    mean, cov, the 90% radius after each view and the NIS of every view after the first."""
    if not views:
        return {}
    mu = np.asarray(views[0]["c"], float)
    P = measurement_cov(views[0])
    trace, nis = [radius90(P)], []
    for v in views[1:]:
        R = measurement_cov(v)
        S = P + R
        nu = np.asarray(v["c"], float) - mu
        Si = np.linalg.inv(S)
        nis.append(float(nu @ Si @ nu))
        K = P @ Si
        mu = mu + K @ nu
        P = (np.eye(3) - K) @ P
        P = (P + P.T) / 2
        trace.append(radius90(P))
    return {"mean": mu, "cov": P, "trace": trace, "nis": nis, "n_views": len(views)}


def independent_views(frames, block: int = 48) -> int:
    """How many views carry independent errors: views from the same stretch of video (`block`
    frames, ~8 s) look at the object along nearly the same ray, so they share the depth model's
    bias for that surface and drifting pose errors; they count once (run 10: 12 views fused as if independent gave "within 0 cm", while unchanged objects
    were found ~12 cm apart; position coverage 0%)."""
    f = np.asarray(list(frames), int)
    return max(1, len(np.unique(f // block))) if len(f) else 1


def honest_cov(views: list[dict], cov_view: np.ndarray | None, n_eff: int) -> np.ndarray:
    """Fused covariance when errors are shared within a chunk: the typical single-view
    covariance over the independent views, or the views' own scatter over them when that is
    larger (whichever has the larger volume). Shrinks only with more independent views."""
    Rm = np.mean([measurement_cov(v) for v in views], 0) / n_eff
    if cov_view is None:
        return Rm
    Cv = np.asarray(cov_view, float) / n_eff
    vol = lambda C: float(np.prod(np.clip(np.linalg.eigvalsh(C), 1e-30, None)))
    return Cv if vol(Cv) > vol(Rm) else Rm


def all_views(objs: list) -> list[dict]:
    """Every view measurement of the same physical object over one or more clips, in order."""
    return [v for o in objs for v in o.extra.get("views", [])]


def annotate(objs: list, scale: float, up: np.ndarray) -> None:
    """Adds o.extra['position'] (main estimate, scene units), o.extra['position_views']
    (views-disagreeing estimate) and o.extra['position_cm'] (metric summary)."""
    k = scale * 100
    for o in objs:
        emp = pooled([o])
        P = _robust(view_centres(o)[1])
        emp["cov_view"] = np.cov(P.T) + np.eye(3) * 1e-12 if len(P) >= 3 else emp["cov"]
        views = o.extra.get("views") or []
        main = ekf(views) if views else None
        if main:
            n_eff = independent_views(v.get("frame", i) for i, v in enumerate(views))
            main["cov_independent_views"] = main["cov"]
            main["cov"] = honest_cov(views, emp["cov_view"] if len(P) >= 3 else None, n_eff)
            main["n_independent"] = n_eff
        o.extra["position_views"] = emp
        o.extra["position"] = main if main else emp
        est = o.extra["position"]
        pc = {"method": "model (VGGT confidence + SAM 2 wobble, fused)" if main else "views disagreeing",
              "sd_axes_cm": (axes_sd(est["cov"]) * k).round(2).tolist(),
              "radius90_cm": round(radius90(est["cov"]) * k, 2),
              "radius90_cm_views": None if emp.get("too_few_views") else round(radius90(emp["cov"]) * k, 2),
              "n_views": int(est["n_views"]), "n_independent_views": int(est.get("n_independent", est["n_views"])),
              "too_few_views": bool(emp.get("too_few_views") and not main),
              "floor": floor_ellipse(est["mean"] * k, est["cov"] * k * k, up)}
        if main:
            Rs = [measurement_cov(v) for v in views]
            pc.update({"single_view_radius90_cm": round(float(np.median([radius90(R) for R in Rs])) * k, 2),
                       "depth_sd_cm": round(float(np.median([v["sd_depth"] for v in views])) * k, 2),
                       "side_sd_cm": round(float(np.median([v["sd_side"] if v.get("sd_side") is not None
                                                            else v["pixel"] / np.sqrt(12) for v in views])) * k, 2),
                       "pose_sd_cm": round(float(np.median([v.get("sd_pose") or 0.0 for v in views])) * k, 2),
                       "wobble_views": int(sum(v.get("sd_side") is not None for v in views)),
                       "nis_mean": round(float(np.mean(main["nis"])), 2) if main["nis"] else None,
                       "trace_cm": [round(x * k, 2) for x in main["trace"]]})
        else:
            pc["single_view_radius90_cm"] = round(radius90(emp["cov_view"]) * k, 2)
        o.extra["position_cm"] = pc


def ekf_curve(objs: list, scale: float, ns=(1, 2, 4, 8, 16, 32)) -> dict:
    """Median 90% radius of the fused model estimate after the first n views."""
    out = {}
    for n in ns:
        r = [o.extra["position_cm"]["trace_cm"][n - 1] for o in objs
             if not o.structure and len(o.extra.get("position_cm", {}).get("trace_cm", [])) >= n]
        if r:
            out[n] = {"median_radius90_cm": round(float(np.median(r)), 2), "objects": len(r)}
    return out


def views_curve(objs: list, scale: float, ns=(1, 2, 4, 8, 16, 32), reps: int = 30, seed: int = 0) -> dict:
    """How the 90% radius shrinks with the number of views, measured by subsampling views.

    For a random subset of n of an object's N views, the subset centre differs from the
    all-view centre with variance sigma^2 (1/n - 1/N) (sampling without replacement), so
    sigma^2 / n = observed variance / (1 - n/N). Median over objects, in cm."""
    rng = np.random.default_rng(seed)
    out = {}
    for n in ns:
        r = []
        for o in objs:
            if o.structure:
                continue
            _, P = view_centres(o)
            P = _robust(P)
            N = len(P)
            if N < max(4, 2 * n):
                continue
            full = P.mean(0)
            d = np.array([P[rng.choice(N, n, replace=False)].mean(0) - full for _ in range(reps)])
            cov = np.cov(d.T) / (1 - n / N) + np.eye(3) * 1e-12
            r.append(radius90(cov) * scale * 100)
        if r:
            out[n] = {"median_radius90_cm": round(float(np.median(r)), 2), "objects": len(r)}
    return out


def pooled(objs: list) -> dict:
    """Position of one physical object from several clips (one object per clip), from the
    pooled views. cov = W / N + B / k, where W is the within-clip covariance of view centres,
    N the number of views, and B the between-clip covariance in excess of what W explains
    (zero when the clips agree; only estimated from the data, never assumed)."""
    Ps = [_robust(view_centres(o)[1]) for o in objs]
    Ps = [P for P in Ps if len(P)]
    if not Ps:
        return {"mean": np.asarray(objs[0].centroid, float), "cov": np.eye(3), "n_views": 0, "k": 0}
    N = sum(len(P) for P in Ps)
    k = len(Ps)
    allP = np.concatenate(Ps)
    resid = np.concatenate([P - P.mean(0) for P in Ps])
    dof = N - k
    if dof >= 3:
        W = resid.T @ resid / dof + np.eye(3) * 1e-12
    else:                                   # too few views to measure the spread
        return {"mean": allP.mean(0), "cov": np.eye(3) * (0.25 * float(np.max(objs[0].extent))) ** 2,
                "n_views": int(N), "k": k, "too_few_views": True}
    means = np.array([P.mean(0) for P in Ps])
    weights = np.array([len(P) for P in Ps], float)
    mean = (weights[:, None] * means).sum(0) / weights.sum()
    cov = W / N
    if k >= 2:
        Sb = np.cov(means.T, aweights=weights) if k > 2 else np.outer(means[0] - means[1], means[0] - means[1]) / 2
        n_eff = N / k
        excess = Sb - W / n_eff
        w, v = np.linalg.eigh((excess + excess.T) / 2)
        B = (v * np.clip(w, 0, None)) @ v.T
        cov = cov + B / k
    return {"mean": mean, "cov": cov, "n_views": int(N), "k": k}


def mahalanobis2(e1: dict, e2: dict) -> float:
    d = e1["mean"] - e2["mean"]
    return float(d @ np.linalg.solve(e1["cov"] + e2["cov"], d))


def match_groups(clips: list[list], max_rel: float = 0.6, min_sim: float = 0.6) -> list[dict]:
    """Same physical object across clips: mutual nearest centres closer than `max_rel` of the
    object's size and similar appearance (DINO / CLIP descriptor). Returns
    [{clip index: object}] with at least two clips."""
    from .inventory import _sim

    parent: dict = {}

    def find(k):
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, j in combinations(range(len(clips)), 2):
        A = [o for o in clips[i] if not o.structure]
        B = [o for o in clips[j] if not o.structure]
        if not A or not B:
            continue
        ca = np.array([o.extra.get("position", {}).get("mean", o.centroid) for o in A])
        cb = np.array([o.extra.get("position", {}).get("mean", o.centroid) for o in B])
        D = np.linalg.norm(ca[:, None] - cb[None], axis=2)
        for a in range(len(A)):
            b = int(np.argmin(D[a]))
            if int(np.argmin(D[:, b])) != a:
                continue
            size = max(float(A[a].extent[0]), float(B[b].extent[0]))
            if D[a, b] <= max_rel * size and _sim(A[a], B[b]) >= min_sim:
                parent[find((i, id(A[a])))] = find((j, id(B[b])))
    objs = {(i, id(o)): o for i, c in enumerate(clips) for o in c}
    groups: dict = {}
    for k in list(parent):
        groups.setdefault(find(k), {})[k[0]] = objs[k]
    return [g for g in groups.values() if len(g) >= 2]


def clips_curve(groups: list[dict], scale: float) -> dict:
    """For each number of clips k (averaged over every subset of k clips), the median 90% radius
    of (a) the fused model estimate over all their views and (b) views disagreeing; plus how
    often two single clips agree within their combined 90% region (should be ~90%) and the
    mean NIS of the fused filter (should be ~3)."""
    model, emp, d2m, d2e, nis = {}, {}, [], [], []
    for g in groups:
        idx = sorted(g)
        for k in range(1, len(idx) + 1):
            rm, re_ = [], []
            for sub in combinations(idx, k):
                e = ekf(all_views([g[i] for i in sub]))
                if e:
                    rm.append(radius90(e["cov"]) * scale * 100)
                    if k == len(idx):
                        nis += e["nis"]
                p_ = pooled([g[i] for i in sub])
                if not p_.get("too_few_views"):
                    re_.append(radius90(p_["cov"]) * scale * 100)
            if rm:
                model.setdefault(k, []).append(float(np.mean(rm)))
            if re_:
                emp.setdefault(k, []).append(float(np.mean(re_)))
        for i, j in combinations(idx, 2):
            a, b = ekf(all_views([g[i]])), ekf(all_views([g[j]]))
            if a and b:
                d2m.append(mahalanobis2(a, b))
            a, b = pooled([g[i]]), pooled([g[j]])
            if not a.get("too_few_views") and not b.get("too_few_views"):
                d2e.append(mahalanobis2(a, b))

    def summ(d):
        return {k: {"median_radius90_cm": round(float(np.median(v)), 2), "objects": len(v)} for k, v in sorted(d.items())}

    cov = (lambda d: round(float(np.mean(np.array(d) <= CHI2_3_90)), 3) if d else None)
    return {"by_clips": summ(model), "by_clips_views": summ(emp), "coverage90": cov(d2m), "coverage90_views": cov(d2e),
            "nis_mean": round(float(np.mean(nis)), 2) if nis else None, "pairs": len(d2m)}
