"""Scene-wide heat maps for robots and simulators, computed on the reconstructed point cloud.

Fields (one value per point):
- uncertainty_cm: how much the recording's own views disagree about where the surface is
  (1-sigma), the per-point boundary uncertainty used for "cup radius +/- x" style ranges;
- shift_cm: signed surface shift seen by the other recording (+ receded, - came forward); its
  magnitude is the deformation / movement heat map (near zero on unchanged surfaces);
- height_m: height above the fitted floor plane;
- reach: 0-1, can an arm mounted on a base standing on the floor reach this point;
- approach: 0-1, share of gripper approach directions (around the surface normal) with free
  space in front of the surface;
- interact: reach x approach x confidence (low where the geometry is uncertain).

Metric scale: monocular reconstructions have no scale. The floor plane gives camera height
above the floor; assuming the phone was held at `phone_height_m` (default 1.35 m, plausible
range 1.1-1.6 m) turns scene units into metres, and that range is carried as a scale interval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from ..recon.frames import Recon
from .detect import ChangeResult, PointSet
from .geometry import cam_centers, up_vector


@dataclass
class RobotModel:
    phone_height_m: float = 1.35
    phone_height_range_m: tuple = (1.1, 1.6)
    shoulder_height_m: float = 0.75      # arm mount height above floor (mobile manipulator torso)
    reach_m: float = 1.0                 # arm reach from the mount
    mobile: bool = True                  # base can drive to any free floor spot (False: stays where the operator stood)
    base_offset_m: float = 0.0           # fixed base only: moved this far from the operator towards the scene
    footprint_m: float = 0.25            # free radius the base needs on the floor
    gripper_clearance_m: float = 0.12    # free length needed in front of a surface
    gripper_radius_m: float = 0.03
    uncertainty_ok_cm: float = 3.0       # uncertainty at which interactability is halved


from .floor import Floor, fit_floor as _fit_floor  # noqa: E402


def fit_floor(X: np.ndarray, E: np.ndarray, robot: RobotModel, seed: int = 0) -> Floor:
    lo, hi = robot.phone_height_range_m
    return _fit_floor(X, E, robot.phone_height_m, phone_sd_log=float(np.log(hi / lo) / (2 * 1.645)), seed=seed)


def _normals(X: np.ndarray, toward: np.ndarray, k: int = 16) -> np.ndarray:
    from scipy.spatial import cKDTree

    _, nn = cKDTree(X).query(X, k=min(k, len(X)))
    P = X[nn] - X[nn].mean(1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", P, P)
    _, vecs = np.linalg.eigh(cov)
    n = vecs[:, :, 0]
    flip = np.sum(n * (toward - X), 1) < 0
    n[flip] *= -1
    return n


def _approach(X: np.ndarray, normals: np.ndarray, up: np.ndarray, length: float, radius: float,
              n_samples: int = 5) -> np.ndarray:
    """Share of 7 directions (normal + 6 tilted 40 deg) with no points within `radius` of the
    path from 1.5 radius to `length` in front of the surface."""
    from scipy.spatial import cKDTree

    tree = cKDTree(X)
    a = np.cross(normals, up)
    bad = np.linalg.norm(a, axis=1) < 1e-3
    a[bad] = np.cross(normals[bad], np.array([1.0, 0, 0]))
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b = np.cross(normals, a)
    dirs = [normals]
    t = np.radians(40)
    for k in range(6):
        ang = 2 * np.pi * k / 6
        d = np.cos(t) * normals + np.sin(t) * (np.cos(ang) * a + np.sin(ang) * b)
        dirs.append(d)
    free = np.zeros(len(X))
    steps = np.linspace(1.5 * radius, length, n_samples)
    for d in dirs:
        blocked = np.zeros(len(X), bool)
        for s in steps:
            q = X + s * d
            cnt = tree.query_ball_point(q, r=radius, return_length=True)
            blocked |= cnt > 0
        free += ~blocked
    return free / len(dirs)


def _voxel(X: np.ndarray, size: float) -> np.ndarray:
    keys = np.floor(X / size).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    return np.sort(first)


def standable_floor(X: np.ndarray, height_m: np.ndarray, up: np.ndarray, floor_h: float, s: float,
                    grid_m: float = 0.1, footprint_m: float = 0.25) -> np.ndarray:
    """Floor positions (scene units, on the floor plane) where a robot base fits: observed floor
    nearby and no obstacle 0.05-1.0 m high within the footprint radius."""
    from scipy.spatial import cKDTree

    e1 = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    P = np.stack([X @ e1, X @ e2], 1) * s
    fl = height_m < 0.03
    obst = (height_m > 0.05) & (height_m < 1.0)
    if fl.sum() < 50:
        return np.zeros((0, 3))
    lo, hi = P[fl].min(0), P[fl].max(0)
    G = np.stack(np.meshgrid(np.arange(lo[0], hi[0] + grid_m, grid_m), np.arange(lo[1], hi[1] + grid_m, grid_m),
                             indexing="ij"), -1).reshape(-1, 2)
    ok = cKDTree(P[fl]).query(G, k=1)[0] < 1.5 * grid_m
    if obst.any():
        ok &= cKDTree(P[obst]).query(G, k=1)[0] > footprint_m
    G = G[ok] / s
    return G[:, :1] * e1 + G[:, 1:] * e2 + floor_h * up


def compute_fields(res: ChangeResult, rec: Recon, robot: RobotModel | None = None, which: str = "B",
                   voxel_m: float = 0.01, max_points: int = 120000, floor: Floor | None = None) -> dict:
    """Fields for the points of recording `which` (B = the scene as it is now), on a voxel
    grid of `voxel_m` metres. Returns dict with indices into the PointSet and arrays."""
    robot = robot or RobotModel()
    ps: PointSet = res.B if which == "B" else res.A
    allX = np.concatenate([res.A.X, res.B.X])
    floor = floor or fit_floor(allX, rec.E, robot)
    up = np.array(floor.up)
    s = floor.scale_m_per_unit
    idx = _voxel(ps.X, voxel_m / s)
    if len(idx) > max_points:
        idx = np.sort(np.random.default_rng(0).choice(idx, max_points, replace=False))
    X = ps.X[idx].astype(np.float64)
    vid = 1 if which == "B" else 0
    cams = cam_centers(rec.E)
    toward = cams[ps.frame[idx]]

    height = (X @ up - floor.height) * s
    cam_b = cams[rec.video == vid].mean(0)
    base = cam_b - ((cam_b @ up) - floor.height) * up              # operator position on the floor
    to_scene = X.mean(0) - base
    to_scene -= (to_scene @ up) * up
    to_scene /= np.linalg.norm(to_scene) + 1e-9
    base = base + robot.base_offset_m / s * to_scene
    shoulder = base + robot.shoulder_height_m / s * up
    bases = standable_floor(X, height, up, floor.height, s, footprint_m=robot.footprint_m) if robot.mobile else None
    if bases is not None and len(bases):
        # mobile robot: distance from each surface to the nearest shoulder position over free floor
        from scipy.spatial import cKDTree

        dist = cKDTree(bases + robot.shoulder_height_m / s * up).query(X, k=1)[0] * s
    else:
        dist = np.linalg.norm(X - shoulder, axis=1) * s
    reach = np.clip((robot.reach_m - dist) / 0.1 + 0.5, 0, 1) * (height > -0.05)

    normals = _normals(X, toward)
    approach = _approach(X, normals, up, robot.gripper_clearance_m / s, robot.gripper_radius_m / s)
    unc_cm = np.nan_to_num(ps.unc[idx] * s * 100, nan=np.nanmedian(ps.unc) * s * 100 if np.any(~np.isnan(ps.unc)) else 0)
    conf_term = 1 / (1 + (unc_cm / robot.uncertainty_ok_cm) ** 2)
    shift_cm = ps.disp[idx] * s * 100

    return {"which": which, "idx": idx, "X": X, "rgb": ps.rgb[idx], "floor": asdict(floor), "robot": asdict(robot),
            "up_dot": (normals @ up).astype(np.float32),
            "base": base, "shoulder": shoulder, "bases": bases if bases is not None else np.zeros((0, 3)),
            "uncertainty_cm": unc_cm.astype(np.float32), "shift_cm": shift_cm.astype(np.float32),
            "height_m": height.astype(np.float32), "reach": reach.astype(np.float32),
            "approach": approach.astype(np.float32),
            "interact": (reach * approach * conf_term).astype(np.float32), "certainty": conf_term.astype(np.float32),
            "cls": ps.cls[idx]}


def field_stats(f: dict) -> dict:
    def q(a, p):
        a = a[np.isfinite(a)]
        return round(float(np.quantile(a, p)), 3) if len(a) else None
    sh = np.abs(f["shift_cm"])
    cues = f["floor"].get("scale_cues", {})
    extra = {"scale_phone_height": round(cues["phone_height"]["scale"], 4) if "phone_height" in cues else None,
             "scale_objects": round(cues["objects"]["scale"], 4) if "objects" in cues else None,
             "scale_metric_depth": round(cues["metric_depth"]["scale"], 4) if "metric_depth" in cues else None,
             "scale_objects_n": cues["objects"]["n"] if "objects" in cues else 0,
             "scale_disagreement_sigma": cues.get("combined", {}).get("disagreement_sigma"),
             "gravity_change_deg": f["floor"].get("gravity_change_deg")}
    return {**extra, "floor_inlier_frac": round(f["floor"]["inlier_frac"], 3),
            "scale_m_per_unit": round(f["floor"]["scale_m_per_unit"], 4),
            "scale_range": [round(x, 4) for x in f["floor"]["scale_range"]],
            "uncertainty_cm_median": q(f["uncertainty_cm"], 0.5), "uncertainty_cm_p90": q(f["uncertainty_cm"], 0.9),
            "shift_cm_median_abs": q(sh, 0.5), "shift_cm_p99_abs": q(sh, 0.99),
            "reachable_frac": round(float((f["reach"] > 0.5).mean()), 3),
            "approachable_frac": round(float((f["approach"] > 0.5).mean()), 3),
            "interactable_frac": round(float((f["interact"] > 0.5).mean()), 3)}


def object_fields(objs: list, res: ChangeResult, f: dict) -> None:
    """Attach metric sizes, uncertainty and robot scores to each change object."""
    from scipy.spatial import cKDTree

    s = f["floor"]["scale_m_per_unit"]
    lo_s, hi_s = f["floor"]["scale_range"]
    tree = cKDTree(f["X"])
    for o in objs:
        ps = res.A if o.recording == "A" else res.B
        X = ps.X[o.extra["mask"]]
        _, nn = tree.query(X[:: max(1, len(X) // 3000)], k=1)
        o.extra["metric"] = {
            "size_cm": (np.array(o.extent) * s * 100).round(1).tolist(),
            "size_cm_ci": (np.array(o.extent_ci) * s * 100).round(1).tolist(),
            "size_cm_scale_range": [(np.array(o.extent) * lo_s * 100).round(1).tolist(),
                                    (np.array(o.extent) * hi_s * 100).round(1).tolist()],
            "position_sd_cm": round(o.centroid_std * s * 100, 1),
            "boundary_uncertainty_cm": round(float(np.nanmedian(ps.unc[o.extra["mask"]])) * s * 100, 2)
            if np.any(np.isfinite(ps.unc[o.extra["mask"]])) else None,
            "mean_abs_shift_cm": round(float(np.nanmean(np.abs(ps.disp[o.extra["mask"]]))) * s * 100, 1)
            if np.any(np.isfinite(ps.disp[o.extra["mask"]])) else None,
            "reach": round(float(f["reach"][nn].mean()), 2), "approach": round(float(f["approach"][nn].mean()), 2),
            "height_m": round(float(np.median(f["height_m"][nn])), 2),
        }
        if o.distance is not None:
            o.extra["metric"]["distance_cm"] = round(o.distance * s * 100, 1)
            o.extra["metric"]["distance_cm_ci"] = [round(o.distance_ci[0] * lo_s * 100, 1),
                                                   round(o.distance_ci[1] * hi_s * 100, 1)]


def object_affordance(f: dict, objs: list, gripper_open_m: float = 0.12, max_lift_m: float = 0.6,
                      near_vox: float = 2.0, place_clear_m: float = 0.08) -> dict:
    """What the robot could do at each surface point, from the segmented objects (SceneObjects
    of the field's recording, labelled, with context and rigidity when available):

    - grasp: the point belongs to a movable object that fits the gripper (smallest size <=
      `gripper_open_m`, largest <= `max_lift_m`) with nothing resting on it, times
      reach and certainty; rigid objects score higher (a soft
      object deforms in the fingers: x (0.5 + 0.5 rigidity)); reach x certainty, as the
      per-point approach score is too sparse on cluttered soft surfaces;
    - push: movable objects too big to grasp (cushions, blankets), times reach and certainty;
    - place: upward-facing surface (normal within ~25 deg of up) of a support or the floor with no
      object within `place_clear_m`, times reach.
    Adds f["object_id"] (index into objs, -1 none), f["grasp"], f["push"], f["place"] and sets
    o.extra["affordance"] (mean scores over the object's points, best action). Returns counts."""
    from scipy.spatial import cKDTree

    s = f["floor"]["scale_m_per_unit"]
    rec = "B" if f["which"] == "B" else "A"
    X = f["X"]
    n = len(X)
    owner = np.full(n, -1)
    if len(X) > 1:
        spacing = float(np.median(cKDTree(X).query(X[:: max(1, n // 2000)], k=2)[0][:, 1]))
    else:
        spacing = 1.0
    best = np.full(n, np.inf)
    mine = [(i, o) for i, o in enumerate(objs) if o.recording == rec]
    for i, o in mine:
        d, _ = cKDTree(np.asarray(o.X, float)).query(X, k=1, distance_upper_bound=near_vox * spacing)
        win = d < best
        owner[win], best[win] = i, d[win]
    grasp, push, place = (np.zeros(n, np.float32) for _ in range(3))
    movable = np.zeros(len(objs), bool)
    for i, o in mine:
        sel = owner == i
        if not sel.any():
            continue
        size = np.sort(np.asarray(o.extent, float) * s)[::-1]
        ctx = o.extra.get("context") or {}
        bg = bool(o.extra.get("background") or o.structure)
        movable[i] = not bg
        rig = (o.extra.get("rigidity") or {}).get("rigidity", 0.5)
        fits = size[-1] <= gripper_open_m and size[0] <= max_lift_m
        # reach x certainty, not x the per-point approach score: run 10 (1c-objects) had only 5% of
        # points "approachable" (normals of cluttered soft surfaces), which zeroed every object
        doable = f["reach"][sel] * f.get("certainty", np.ones(n))[sel]
        if not bg and fits and not ctx.get("supports"):
            grasp[sel] = doable * (0.5 + 0.5 * rig)
        elif not bg:
            push[sel] = doable
    up_ok = f.get("up_dot", np.ones(n)) > 0.9
    support = (owner < 0) | ~movable[np.clip(owner, 0, None)]
    occupied = np.zeros(n, bool)
    mv = owner >= 0
    mv[mv] = movable[owner[mv]]
    if mv.any():
        occupied = cKDTree(X[mv]).query(X, k=1)[0] * s < place_clear_m
    place[:] = (f["reach"] * (up_ok & support & ~occupied)).astype(np.float32)
    f.update({"object_id": owner, "grasp": grasp, "push": push, "place": place})
    for i, o in mine:
        sel = owner == i
        if sel.any():
            a = {"grasp": round(float(grasp[sel].mean()), 3), "push": round(float(push[sel].mean()), 3),
                 "place_on": round(float(place[sel].mean()), 3), "points": int(sel.sum())}
            a["best"] = max(("grasp", "push", "place_on"), key=lambda k: a[k]) if max(a["grasp"], a["push"], a["place_on"]) > 0.2 else "none"
            o.extra["affordance"] = a
    return {"grasp_frac": round(float((grasp > 0.5).mean()), 3), "push_frac": round(float((push > 0.5).mean()), 3),
            "place_frac": round(float((place > 0.5).mean()), 3), "points_on_objects": round(float((owner >= 0).mean()), 3)}


def base_placement(f: dict, grid_m: float = 0.1, footprint_m: float = 0.25, max_targets: int = 20000) -> dict:
    """Where on the floor should the robot stand? For candidate base positions on a grid over
    the observed floor, the share of the scene's surfaces (and of the changed surfaces) its arm
    could reach. Candidates need observed floor nearby and no obstacle (0.05-1.0 m high) within
    the footprint radius. Returns grid coordinates, scores (nan where the robot cannot stand)
    and the best position."""
    from scipy.spatial import cKDTree

    s = f["floor"]["scale_m_per_unit"]
    r = f["robot"]
    up = np.array(f["floor"]["up"])
    e1 = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    X, h = f["X"], f["height_m"]
    P = np.stack([X @ e1, X @ e2], 1) * s                     # metres in the floor plane
    floor = h < 0.03
    obst = (h > 0.05) & (h < 1.0)
    if floor.sum() < 50:
        return {}
    lo, hi = P[floor].min(0), P[floor].max(0)
    gx = np.arange(lo[0], hi[0] + grid_m, grid_m)
    gy = np.arange(lo[1], hi[1] + grid_m, grid_m)
    G = np.stack(np.meshgrid(gx, gy, indexing="ij"), -1).reshape(-1, 2)
    near_floor = cKDTree(P[floor]).query(G, k=1)[0] < 1.5 * grid_m
    free = np.ones(len(G), bool)
    if obst.any():
        free = cKDTree(P[obst]).query(G, k=1)[0] > footprint_m
    ok = near_floor & free
    rng = np.random.default_rng(0)
    tgt = np.nonzero(h > 0.05)[0]
    tgt = tgt[rng.choice(len(tgt), min(len(tgt), max_targets), replace=False)] if len(tgt) else tgt
    chg = np.nonzero((f["cls"] == 1) | (f["cls"] == 2))[0]
    T = np.concatenate([P[tgt], h[tgt, None]], 1) if len(tgt) else np.zeros((0, 3))
    C = np.concatenate([P[chg], h[chg, None]], 1) if len(chg) else np.zeros((0, 3))
    shoulders = np.concatenate([G, np.full((len(G), 1), r["shoulder_height_m"])], 1)
    score = np.full(len(G), np.nan)
    score_chg = np.full(len(G), np.nan)
    if len(T):
        cnt = cKDTree(T).query_ball_point(shoulders[ok], r=r["reach_m"], return_length=True)
        score[ok] = cnt / len(T)
    if len(C):
        cnt = cKDTree(C).query_ball_point(shoulders[ok], r=r["reach_m"], return_length=True)
        score_chg[ok] = cnt / len(C)
    best = int(np.nanargmax(score)) if np.isfinite(score).any() else None
    base_now = np.array([f["base"] @ e1, f["base"] @ e2]) * s
    return {"grid_x": gx, "grid_y": gy, "score": score.reshape(len(gx), len(gy)),
            "score_changed": score_chg.reshape(len(gx), len(gy)), "e1": e1, "e2": e2,
            "base_now_m": base_now, "P_m": P, "height_m": h,
            "best_m": G[best] if best is not None else None,
            "best_score": float(score[best]) if best is not None else None,
            "best_score_changed": float(score_chg[best]) if best is not None and np.isfinite(score_chg[best]) else None,
            "standable_frac": float(ok.mean())}
