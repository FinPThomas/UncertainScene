"""Gravity, floor and metric scale for a monocular reconstruction.

Gravity: phones are held roughly upright, so the mean camera up axis is a first guess. It is
refined with the scene itself: horizontal surfaces (floor, seat, table, shelves) all share one
normal, so the dominant surface normal within 35 degrees of the guess is taken as "up".

Floor: the lowest substantial layer of horizontal surface points along "up" (a height
histogram), not the plane with the most points (which is often a sofa seat or a table).

Scale: the most likely value given two predictions, no direct measurement (Fin, 2026-10-13):
1. a metric depth model (change.metric): its metres-per-unit ratio to VGGT's depth over many
   frames;
2. known object sizes (change.sizes): every labelled object votes with its typical size over
   its reconstructed size, as a mixture over its top CLIP labels, with a share for wrong labels.
The scale is the mode of the joint posterior over log scale. The phone-height cue (camera
assumed 1.35 m above the floor) is still computed as a placeholder and reported, but not used:
the phone's height changes all through a sweep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .geometry import cam_centers, up_vector

log = logging.getLogger(__name__)
_trapz = getattr(np, "trapezoid", None) or np.trapz     # numpy < 2 only has trapz

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


def _object_terms(objects: list[dict], trim: float, recon_sd: float):
    """Per object: a list of (log-scale estimate, sd, probability) over its CLIP labels, plus
    the probability of labels that carry no size (they vote for nothing)."""
    from .inventory import canonical
    from .sizes import size_prior

    out = []
    for o in objects:
        if not o.get("complete", True):
            continue                       # cut by the image border: its extent is a lower bound only
        ext = np.asarray(o.get("extent") if o.get("extent") is not None else [o["extent_units"]], float) / trim
        top = o.get("label_top3") or [(o.get("label"), o.get("label_prob", 1.0))]
        terms, mass = [], 0.0
        for lab, p in top:
            mass += p
            pr = size_prior(canonical(lab))
            if not pr or ext[0] <= 0:
                continue
            dims = [pr[0]] + ([pr[1]] if len(ext) > 1 and ext[1] > 0 else [])
            est = float(np.mean([np.log(d / e) for d, e in zip(dims, ext)]))
            terms.append((est, float(np.hypot(pr[2], recon_sd)), float(p)))
        if terms:
            out.append({"label": o.get("label"), "terms": terms, "other": float(max(1.0 - mass, 0.0))
                        + sum(p for lab, p in top if not size_prior(canonical(lab)))})
    return out


def _posterior(gauss: list, objs: list, outlier: float, half_width: float = 3.0, n: int = 4001):
    """Posterior over log scale (flat prior) from Gaussian cues [(mu, sd)] and object votes
    (each a mixture over its labels, plus `outlier` for a wrong label or a partial object).
    Returns grid, normalised density."""
    centres = [g[0] for g in gauss] + [t[0] for o in objs for t in o["terms"]]
    grid = np.linspace(min(centres) - half_width, max(centres) + half_width, n)
    span = grid[-1] - grid[0]
    logp = np.zeros(n)
    for mu, sd in gauss:
        logp += -0.5 * ((grid - mu) / sd) ** 2
    for o in objs:
        tot = sum(t[2] for t in o["terms"]) + o["other"]
        mix = np.zeros(n)
        for mu, sd, p in o["terms"]:
            mix += (p / tot) * np.exp(-0.5 * ((grid - mu) / sd) ** 2) / (sd * np.sqrt(2 * np.pi))
        mix += (o["other"] / tot) / span                        # labels with no size: flat
        logp += np.log((1 - outlier) * mix + outlier / span)
    p = np.exp(logp - logp.max())
    return grid, p / _trapz(p, grid)


def _summary(grid, p) -> dict:
    """Most likely scale (posterior mode), 90% interval and an equivalent log-sd."""
    c = np.cumsum(p) * (grid[1] - grid[0])
    lo, hi = np.interp([0.05, 0.95], c, grid)
    mode = float(grid[int(np.argmax(p))])
    return {"scale": float(np.exp(mode)), "range": [float(np.exp(lo)), float(np.exp(hi))],
            "sd_log": float((hi - lo) / (2 * 1.645))}


def size_votes(objs) -> list[dict]:
    """object_scale input from SceneObjects (labels, top-3 CLIP labels, sorted extents)."""
    return [{"label": o.label, "label_prob": o.label_prob, "label_top3": getattr(o, "label_top3", None),
             "extent": [float(x) for x in o.extent], "complete": o.complete}
            for o in objs if o.extent is not None and not o.structure]   # wall-sized fragments: no vote


def object_scale(objects: list[dict], trim: float = 0.9, recon_sd: float = 0.10, outlier: float = 0.1) -> dict | None:
    """Scale from objects of known typical size. objects: dicts with 'label', 'label_prob',
    optional 'label_top3' [(label, p)], 'extent' (sorted dimensions, scene units) or
    'extent_units' (largest), optional 'complete'.

    Each object votes with a mixture over its top CLIP labels weighted by their probabilities
    (a guitar that might be a ukulele votes for both scales), plus a flat `outlier` share for
    a wrong label; the most likely scale is the mode of the product of all votes. Extents are
    5-95% spans, so they are divided by `trim` (0.9: the span of 90% of points along a length);
    `recon_sd` is the reconstruction error of an extent."""
    objs = _object_terms(objects, trim, recon_sd)
    if not objs:
        return None
    grid, p = _posterior([], objs, outlier)
    out = _summary(grid, p)
    out.update({"n": len(objs), "labels": [o["label"] for o in objs], "votes": objs, "outlier": outlier,
                "per_object": [round(float(np.exp(max(o["terms"], key=lambda t: t[2])[0])), 4) for o in objs]})
    return out


def combine_scale(floor: Floor, obj: dict | None = None, metric: dict | None = None) -> Floor:
    """Most likely scale from the metric depth model and the object sizes together (mode of
    the joint posterior over log scale). The phone-height cue is reported but not used: the
    phone's height above the floor changes all through a sweep (Fin, 2026-10-13). With neither
    cue, the phone-height value stays as a placeholder and is flagged."""
    if obj:
        floor.scale_cues["objects"] = obj
    if metric:
        floor.scale_cues["metric_depth"] = metric
    m, o = floor.scale_cues.get("metric_depth"), floor.scale_cues.get("objects")
    if not m and not o:
        floor.scale_cues["combined"] = {"scale": floor.scale_m_per_unit, "cues": ["phone_height"],
                                        "warning": "no metric depth or object cue; phone-height placeholder"}
        return floor
    gauss = [(np.log(m["scale"]), m["sd_log"])] if m else []
    votes = o["votes"] if o else []
    grid, p = _posterior(gauss, votes, o["outlier"] if o else 0.1)
    sm = _summary(grid, p)
    floor.scale_m_per_unit, floor.scale_range = sm["scale"], sm["range"]
    agree = None
    if m and o:   # how far apart the two cues are, in their combined sigma
        agree = round(abs(np.log(m["scale"]) - np.log(o["scale"])) / np.hypot(m["sd_log"], o["sd_log"]), 2)
    floor.scale_cues["combined"] = {"scale": sm["scale"], "range": sm["range"], "sd_log": sm["sd_log"],
                                    "cues": [k for k in ("metric_depth", "objects") if floor.scale_cues.get(k)],
                                    "disagreement_sigma": agree}
    log.info("scale: most likely %.3f m/unit (90%% %.3f-%.3f) from %s; phone height (not used) %.3f",
             sm["scale"], *sm["range"],
             ", ".join(f"{k} {floor.scale_cues[k]['scale']:.3f}" for k in ("metric_depth", "objects")
                       if floor.scale_cues.get(k)),
             floor.scale_cues.get("phone_height", {}).get("scale", float("nan")))
    return floor


def add_metric_cue(floor: Floor, rec, stage=None) -> Floor:
    """Add the metric-depth-model cue (change.metric) to `floor`; any failure (model not
    downloadable, no usable frames) leaves the other cues as they are."""
    try:
        from .metric import metric_scale

        m = metric_scale(rec)
        if m is None:
            return floor
        floor = combine_scale(floor, metric=m)
    except Exception:  # noqa: BLE001 - a missing cue must not lose the step
        log.exception("metric depth scale failed; keeping the other scale cues")
        return floor
    if stage is not None:
        stage(f"metric scale (depth model {m['scale']:.3f} m/unit from {m['n']} frames; combined "
              f"{floor.scale_m_per_unit:.3f}, 90% {floor.scale_range[0]:.3f}-{floor.scale_range[1]:.3f})")
    return floor
