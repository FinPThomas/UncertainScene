"""Gravity, floor and metric scale for a monocular reconstruction.

Gravity: phones are held roughly upright, so the mean camera up axis is a first guess. It is
refined with the scene itself: horizontal surfaces (floor, seat, table, shelves) all share one
normal, so the dominant surface normal within 35 degrees of the guess is taken as "up".

Floor: the lowest substantial layer of horizontal surface points along "up" (a height
histogram), not the plane with the most points (which is often a sofa seat or a table).

Scale, from independent cues combined in log space by inverse variance:
1. phone height: the median camera height above the floor is assumed to be 1.35 m
   (sd ~ 12% for a hand-held sweep that includes close passes);
2. known object sizes: objects SAM 2 + CLIP labelled with a confident common class (mug,
   guitar, coaster, ...) have a typical size; each gives a scale estimate (object prior size
   / reconstructed size). Their weighted median and spread form the second cue.
3. a metric depth model (change.metric): its metres-per-unit ratio to VGGT's depth over many
   frames; works without a visible floor.
Each estimate is reported, so a wrong floor (e.g. the seat taken as floor) shows up as a
disagreement between the two cues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .geometry import cam_centers, up_vector

log = logging.getLogger(__name__)

# typical size of the largest dimension in metres, and log-sd of that size across products
SIZE_PRIORS = {
    "mug": (0.12, 0.15), "cup": (0.11, 0.2), "coaster": (0.10, 0.12), "guitar": (1.0, 0.08),
    "acoustic guitar": (1.0, 0.07), "baseball cap": (0.27, 0.12), "hat": (0.28, 0.2),
    "table tennis bat": (0.26, 0.06), "book": (0.24, 0.2), "neck pillow": (0.30, 0.12),
    "remote control": (0.18, 0.15), "phone": (0.15, 0.08), "a4 paper sheet": (0.297, 0.03),
    "bottle": (0.25, 0.2), "glass vase": (0.30, 0.35), "cushion": (0.45, 0.15), "pillow": (0.50, 0.15),
    "laptop": (0.33, 0.1), "shoe": (0.28, 0.1), "keys": (0.08, 0.3), "headphones": (0.20, 0.15),
}


@dataclass
class Floor:
    up: list
    height: float                        # floor offset along `up`, scene units
    inlier_frac: float                   # share of horizontal-surface points on the floor layer
    scale_m_per_unit: float
    scale_range: list                    # ~90% interval
    camera_height_units: float
    gravity_change_deg: float = 0.0      # refinement of the camera-based guess
    floor_points: int = 0
    scale_cues: dict = field(default_factory=dict)


def _normals(X: np.ndarray, k: int = 12) -> np.ndarray:
    from scipy.spatial import cKDTree

    _, nn = cKDTree(X).query(X, k=min(k, len(X)))
    P = X[nn] - X[nn].mean(1, keepdims=True)
    _, vecs = np.linalg.eigh(np.einsum("nki,nkj->nij", P, P))
    return vecs[:, :, 0]


def gravity(X: np.ndarray, E: np.ndarray, max_points: int = 40000, seed: int = 0) -> tuple[np.ndarray, float]:
    rng = np.random.default_rng(seed)
    up0 = up_vector(E)
    sub = X[rng.choice(len(X), min(len(X), max_points), replace=False)]
    n = _normals(sub)
    n *= np.sign(n @ up0)[:, None]
    up = up0
    for cone in (35, 20, 10):          # mean of normals near the current guess, narrowing
        sel = n @ up > np.cos(np.radians(cone))
        if sel.sum() < 100:
            break
        up = n[sel].mean(0)
        up /= np.linalg.norm(up)
    return up, float(np.degrees(np.arccos(np.clip(up @ up0, -1, 1))))


def fit_floor(X: np.ndarray, E: np.ndarray, phone_height_m: float = 1.35, phone_sd_log: float = 0.12,
              seed: int = 0) -> Floor:
    rng = np.random.default_rng(seed)
    up, dg = gravity(X, E, seed=seed)
    sub = X[rng.choice(len(X), min(len(X), 60000), replace=False)]
    n = _normals(sub)
    horiz = np.abs(n @ up) > np.cos(np.radians(15))
    h = sub @ up
    cams_h = cam_centers(E) @ up
    hh = h[horiz & (h < np.median(cams_h))] if (horiz & (h < np.median(cams_h))).sum() > 200 else h
    span = np.quantile(h, 0.99) - np.quantile(h, 0.01)
    bw = span / 150
    lo = np.quantile(hh, 0.002)
    edges = np.arange(lo - bw, np.quantile(hh, 0.9) + bw, bw)
    hist, edges = np.histogram(hh, edges)
    sm = np.convolve(hist, np.ones(3) / 3, mode="same")
    need = max(30, 0.03 * len(hh))           # a layer must hold >= 3% of horizontal points
    floor_h = None
    for i in range(1, len(sm) - 1):           # lowest local peak with enough mass
        if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and hist[max(0, i - 2):i + 3].sum() >= need:
            floor_h = float((edges[i] + edges[i + 1]) / 2)
            break
    if floor_h is None:
        floor_h = float(np.quantile(hh, 0.02))
    on = np.abs(hh - floor_h) < 2 * bw
    if on.sum() > 30:
        floor_h = float(np.median(hh[on]))
    cam_h = float(np.median(cams_h - floor_h))
    if cam_h <= 1e-6:
        cam_h = float(np.median(cams_h) - np.quantile(h, 0.02))
    s = phone_height_m / cam_h
    f = Floor(up=up.round(6).tolist(), height=floor_h, inlier_frac=float(on.mean()), scale_m_per_unit=float(s),
              scale_range=[float(s * np.exp(-1.645 * phone_sd_log)), float(s * np.exp(1.645 * phone_sd_log))],
              camera_height_units=cam_h, gravity_change_deg=round(dg, 1), floor_points=int(on.sum()))
    f.scale_cues = {"phone_height": {"scale": float(s), "sd_log": phone_sd_log, "assumed_m": phone_height_m}}
    log.info("floor: gravity refined by %.1f deg, %d floor points (%.0f%% of horizontal), camera %.3f units above "
             "floor -> %.3f m/unit", dg, on.sum(), 100 * on.mean(), cam_h, s)
    return f


def object_scale(objects: list[dict], min_prob: float = 0.35) -> dict | None:
    """objects: dicts with 'label', 'label_prob', 'extent_units' (largest dimension, scene
    units) and optional 'complete' (False = cut by the image border / partly seen)."""
    est, w, used = [], [], []
    for o in objects:
        pr = SIZE_PRIORS.get(o.get("label"))
        if not pr or o.get("label_prob", 0) < min_prob or not o.get("complete", True) or o["extent_units"] <= 0:
            continue
        est.append(np.log(pr[0] / o["extent_units"]))
        w.append(1 / (pr[1] ** 2 + 0.1 ** 2))     # + reconstruction error of the extent
        used.append(o["label"])
    if not est:
        return None
    est, w = np.array(est), np.array(w)
    order = np.argsort(est)
    cw = np.cumsum(w[order]) / w.sum()
    med = float(est[order][np.searchsorted(cw, 0.5)])
    spread = float(np.sqrt(np.sum(w * (est - med) ** 2) / w.sum())) if len(est) > 1 else 0.25
    sd = max(0.08, np.sqrt(1 / w.sum()), spread / np.sqrt(len(est)))
    return {"scale": float(np.exp(med)), "sd_log": float(sd), "n": len(est), "labels": used,
            "per_object": [round(float(np.exp(e)), 4) for e in est]}


def combine_scale(floor: Floor, obj: dict | None = None, metric: dict | None = None) -> Floor:
    """Inverse-variance combination (log space) of every scale cue present: phone height,
    metric depth model and object sizes. Cues already attached to `floor` are kept, so cues can
    be added one at a time. When cues contradict (more than 2 sigma apart) the range widens to
    cover them rather than trusting either."""
    if obj:
        floor.scale_cues["objects"] = obj
    if metric:
        floor.scale_cues["metric_depth"] = metric
    cues = {k: v for k, v in floor.scale_cues.items() if k in ("phone_height", "metric_depth", "objects")}
    if len(cues) < 2:
        return floor
    L = np.array([np.log(v["scale"]) for v in cues.values()])
    V = np.array([v["sd_log"] ** 2 for v in cues.values()])
    lc = float(np.sum(L / V) / np.sum(1 / V))
    sd = float(np.sqrt(1 / np.sum(1 / V)))
    disagree = max(abs(L[i] - L[j]) / np.sqrt(V[i] + V[j]) for i in range(len(L)) for j in range(i + 1, len(L)))
    if disagree > 2:
        sd = max(sd, float(np.max(np.abs(L - lc))))
    floor.scale_m_per_unit = float(np.exp(lc))
    floor.scale_range = [float(np.exp(lc - 1.645 * sd)), float(np.exp(lc + 1.645 * sd))]
    floor.scale_cues["combined"] = {"scale": floor.scale_m_per_unit, "sd_log": sd,
                                    "disagreement_sigma": round(float(disagree), 2), "cues": list(cues)}
    log.info("scale: %s -> combined %.3f m/unit (90%% %.3f-%.3f; cues up to %.1f sigma apart)",
             ", ".join(f"{k} {v['scale']:.3f}" for k, v in cues.items()), floor.scale_m_per_unit,
             *floor.scale_range, disagree)
    return floor


def add_metric_cue(floor: Floor, rec, stage=None) -> Floor:
    """Add the metric-depth-model cue (change.metric) to `floor`; any failure (model not
    downloadable, no usable frames) leaves the other cues as they are."""
    try:
        from .metric import metric_scale

        m = metric_scale(rec)
    except Exception:  # noqa: BLE001 - a missing cue must not lose the step
        log.exception("metric depth scale failed; keeping the other scale cues")
        return floor
    if m is None:
        return floor
    floor = combine_scale(floor, metric=m)
    if stage is not None:
        stage(f"metric scale (depth model {m['scale']:.3f} m/unit from {m['n']} frames; combined "
              f"{floor.scale_m_per_unit:.3f}, 90% {floor.scale_range[0]:.3f}-{floor.scale_range[1]:.3f})")
    return floor
