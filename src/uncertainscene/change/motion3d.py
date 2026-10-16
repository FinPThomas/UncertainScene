"""How far things moved, and how they deformed, from feature points matched between recordings.

A single VGGT depth map is only good to several cm, too coarse to see a cushion corner bend.
Feature points triangulated from many frames are good to millimetres-to-a-centimetre, so this
works from those:

1. Per recording: SIFT tracks across its own frames (fundamental-matrix checked, as in
   recon.poses), triangulated with the (refined) poses -> precise 3D points.
2. Between recordings: before and after frames from similar viewpoints are matched by
   descriptor only (no geometric check, so things that moved are kept). Each before track is
   linked to the after track its features match most often, if that is mutual and seen at
   least twice. Displacement = after point - before point.
3. Uncertainty per point, from the triangulation itself: each point's 3D covariance follows
   from how many frames saw it, from what angles and distances, and the pixel noise of that
   recording (its median reprojection error). A displacement's covariance is the sum of the
   before and after points' covariances plus one shared term for the residual misalignment
   between the two recordings' frames.
   Alignment: links whose matches pass a fundamental-matrix check between the before and after
   frame (a rigid scene; does not use our poses) are static. A similarity fitted to the static
   links (weighted by each point's uncertainty, robust to wrong links) lines the after points up
   with the before points, and the shared misalignment term is then measured on the static
   links only (the term that makes their median displacement statistically ordinary). A displacement
   is real when it is beyond its own uncertainty (chi-square, 97.5%), point by point.
4. Per object (SAM objects of the before recording, by their voxels): a rigid motion fitted by
   RANSAC to the object's links gives the distance moved (of its centre, 90% range by
   bootstrap) and the rotation. What the rigid motion cannot explain is the deformation map,
   judged against each point's own uncertainty over a small neighbourhood. Deformation metrics:
   90th percentile and peak of the residual (cm, with the point uncertainty there), the share
   of the object whose deformation is beyond its uncertainty, and where the peak sits along
   the object's main axes (e.g. one corner of a cushion). Nothing about any object is built in.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class MotionConfig:
    frame_step: int = 2             # use every n-th frame of each recording (6 fps -> 3 fps)
    n_features: int = 4000
    k_near: int = 10                # pose-nearest partner frames within a recording
    k_cross: int = 6                # pose-nearest frames of the other recording to match against
    ratio: float = 0.8
    cross_ratio: float = 0.75       # stricter ratio test for before/after matches (no geometric check possible for moved things)
    min_obs: int = 3                # observations for a track to count
    min_angle_deg: float = 2.0
    max_px: float = 3.0             # reprojection gate
    min_votes: int = 2              # feature matches needed to link a before track to an after track
    level: float = 0.975            # chi-square level for "beyond its uncertainty"
    min_px_sigma: float = 0.3       # floor on the per-recording pixel noise
    min_links: int = 8              # links an object needs for a rigid fit
    smooth_k: int = 6               # neighbours for smoothing the non-rigid residual
    f_px: float = 2.0               # fundamental-matrix RANSAC threshold for the static check
    min_static: int = 30            # static links needed to fit the alignment between recordings


def _within_tracks(rec, frames, kps, descs, cfg, device):
    """Tracks inside one recording. Returns per observation (track, frame, kp, uv)."""
    from ..recon import poses as P

    pc = P.PoseConfig(k_near=cfg.k_near, k_time=2, ratio=cfg.ratio)
    pr = P.pairs(rec.E[frames], np.zeros(len(frames), int), cfg.k_near, 2)
    loc_k = [kps[s] for s in frames]
    loc_d = [descs[s] for s in frames]
    M, _ = P.match(loc_k, loc_d, pr, pc, device)
    t, fr, kp = P.tracks(M, [len(k) for k in loc_k])
    off = np.concatenate([[0], np.cumsum([len(k) for k in loc_k])])
    uv = np.concatenate(loc_k)[off[fr] + kp].astype(np.float64)
    return t, frames[fr], kp, uv


def _tri(E, K, t, fr, uv, T, w=None):
    from ..recon import poses as P

    X, ang = np.zeros((T, 3)), np.zeros(T)
    if len(t):
        x, a = P.triangulate(E, K, t, fr, uv, w)
        X[: len(x)], ang[: len(a)] = x, a
    return X, ang


def _covariances(E, K, X, t, fr, sigma_px, T: int) -> np.ndarray:
    """3D covariance of each track's point from its observations: inverse of sum J^T J / sigma^2,
    J the 2x3 Jacobian of the pixel projection with respect to the point. `sigma_px` is one
    number or one per observation (feature noise plus that frame's pose uncertainty)."""
    sigma_px = np.broadcast_to(np.asarray(sigma_px, float), (len(t),))
    R, tv = E[fr, :, :3], E[fr, :, 3]
    Xc = np.einsum("nij,nj->ni", R, np.nan_to_num(X[t])) + tv
    z = np.maximum(Xc[:, 2], 1e-6)
    fx, fy = K[fr, 0, 0], K[fr, 1, 1]
    Jc = np.zeros((len(t), 2, 3))
    Jc[:, 0, 0], Jc[:, 0, 2] = fx / z, -fx * Xc[:, 0] / z**2
    Jc[:, 1, 1], Jc[:, 1, 2] = fy / z, -fy * Xc[:, 1] / z**2
    J = Jc @ R
    info = np.zeros((T, 3, 3))
    np.add.at(info, t, np.einsum("nki,nkj->nij", J, J) / sigma_px[:, None, None] ** 2)
    return np.linalg.inv(info + np.eye(3)[None] * 1e-12)


def _triangulate_tracks(E, K, t, fr, uv, cfg, use=None, pose_px=None):
    """Points of tracks with enough good observations (optionally only observations in `use`).
    `pose_px`: per-frame pose uncertainty (pixels at this K's size); observations from shaky
    frames weigh less in each point and widen its covariance.
    Returns X [T, 3] (NaN where invalid), their covariances [T, 3, 3], the boolean mask of
    observations kept and the recording's pixel noise (per axis)."""
    from ..recon import poses as P

    T = int(t.max()) + 1 if len(t) else 0
    keep = np.ones(len(t), bool) if use is None else use.copy()
    pp = np.zeros(len(t)) if pose_px is None else np.asarray(pose_px, float)[fr]
    # triangulation weight: 1 / (feature noise^2 + pose^2), feature noise ~1 px until measured
    w = 1.0 / (1.0 + pp**2)
    # gate without refined poses: max_px, or wider when the poses themselves reproject worse than
    # that (VGGT poses were ~8 px off on the first real run, so a 3 px gate left 257 points and no
    # before/after links); the wider spread is measured below and widens every point's covariance
    # with refined poses (pose_px given) the gate is the feature gate plus each frame's own pose
    # uncertainty: a loose gate let wrongly chained tracks in (run 6: 11.7 px "noise" against the
    # adjustment's 0.66 px), and they read as misalignment between the recordings
    if pose_px is not None:
        max_px = np.sqrt(cfg.max_px**2 + (3 * pp) ** 2)
    elif keep.any():
        X, _ = _tri(E, K, t[keep], fr[keep], uv[keep], T, w[keep])
        err, _ = P.reproject(E, K, X, t, fr, uv)
        max_px = max(cfg.max_px, 2.5 * float(np.median(err[keep])))
    else:
        max_px = cfg.max_px
    for _ in range(3):
        X, ang = _tri(E, K, t[keep], fr[keep], uv[keep], T, w[keep])
        err, z = P.reproject(E, K, X, t, fr, uv)
        good = (np.bincount(t[keep], minlength=T) >= cfg.min_obs) & (ang >= cfg.min_angle_deg)
        new = keep & (err < max_px) & (z > 0) & good[t]
        if (new == keep).all():
            break
        keep = new
    X, ang = _tri(E, K, t[keep], fr[keep], uv[keep], T, w[keep])
    good = (np.bincount(t[keep], minlength=T) >= cfg.min_obs) & (ang >= cfg.min_angle_deg)
    keep &= good[t]
    err, _ = P.reproject(E, K, X, t, fr, uv)
    # median of a 2D Gaussian error's length is 1.1774 sigma
    # feature noise from frames with steady poses (their residual is not pose error)
    steady = keep & (pp <= np.median(pp[keep])) if keep.any() else keep
    sigma = max(float(np.median(err[steady])) / 1.1774, cfg.min_px_sigma) if steady.any() else 1.0
    C = _covariances(E, K, X, t[keep], fr[keep], np.sqrt(sigma**2 + pp[keep] ** 2), T)
    X[~good] = np.nan
    return X, C, keep, sigma


def _mahal2(d: np.ndarray, C: np.ndarray) -> np.ndarray:
    return np.einsum("ni,nij,nj->n", d, np.linalg.inv(C), d)


def cross_term(d: np.ndarray, C: np.ndarray) -> float:
    """Variance (per axis) of the shared misalignment between the two recordings: the smallest
    s^2 >= 0 for which the median of d^T (C + s^2 I)^-1 d is the chi-square(3) median, using
    that most of the scene did not move. 0 when the point covariances already explain it."""
    from scipy.stats import chi2

    target = chi2.ppf(0.5, 3)
    if not len(d) or np.median(_mahal2(d, C)) <= target:
        return 0.0
    lo, hi = 0.0, float(np.median((d**2).sum(1))) + 1e-12
    for _ in range(50):
        mid = (lo + hi) / 2
        if np.median(_mahal2(d, C + mid * np.eye(3)[None])) > target:
            lo = mid
        else:
            hi = mid
    return hi


def _static_matches(pa: np.ndarray, pb: np.ndarray, thr: float) -> np.ndarray:
    """Which matches between a before and an after frame agree with one fundamental matrix
    (RANSAC with local refinement; a rigid scene seen from two cameras). Moved objects fail it. All False when there
    are too few matches to decide."""
    import cv2

    if len(pa) < 15:
        return np.zeros(len(pa), bool)
    try:   # run 11: OpenCV's USAC asserts (!model.empty()) on degenerate matches instead of returning None
        _, m = cv2.findFundamentalMat(pa.astype(np.float64), pb.astype(np.float64),
                                      getattr(cv2, "USAC_ACCURATE", cv2.FM_RANSAC), thr, 0.999)
    except cv2.error:
        return np.zeros(len(pa), bool)
    return np.zeros(len(pa), bool) if m is None else m.ravel().astype(bool)


def align_recordings(XA: np.ndarray, XB: np.ndarray, C: np.ndarray, iters: int = 20):
    """Similarity (scale, R, t) with XA ~ scale * R @ XB + t from static points, each weighted by
    its own uncertainty, robust to wrong links and moved objects: RANSAC for the largest rigid
    consensus, then Cauchy-weighted refits with the residual scale taken from that consensus.
    Identity when no correction is needed (see below). Returns scale, R, t and a summary."""
    w0 = 1.0 / (np.trace(C, axis1=1, axis2=2) / 3 + 1e-18)
    # start from the largest rigid consensus (RANSAC), so a moved object or a cluster of wrong
    # links cannot pull the fit into a compromise; inlier distance 3 sigma of each point
    thr = 3 / np.sqrt(w0)
    _, _, inl0 = rigid_ransac(XB, XA, thr, iters=500)
    w = w0 * (inl0 if inl0.sum() >= 3 else 1.0)
    sc, R, t = 1.0, np.eye(3), np.zeros(3)
    for _ in range(iters):
        ww = w / w.sum()
        ma, mb = ww @ XA, ww @ XB
        A, B = XA - ma, XB - mb
        H = (B * ww[:, None]).T @ A
        U, S, Vt = np.linalg.svd(H)
        D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
        R = Vt.T @ D @ U.T
        sc = float(np.trace(np.diag(S) @ D) / max(ww @ (B**2).sum(1), 1e-18))
        t = ma - sc * R @ mb
        r2 = ((XA - (sc * XB @ R.T + t)) ** 2).sum(1) * w0
        core = r2 < 9 * np.median(r2[w > 0.5 * w0]) if (w > 0.5 * w0).any() else np.ones(len(r2), bool)
        c2 = 2.0 * np.median(r2[core]) + 1e-18    # Cauchy scale from the consensus' own residuals
        w = w0 / (1 + r2 / c2)
    inl = r2 < 9 * np.median(r2[core])
    # keep the recordings as they are when they already agree: the joint bundle adjustment ties
    # them together, so a correction must explain clearly more static points than no correction
    # (otherwise a big moved object could be taken for the static scene)
    ident = np.linalg.norm(XB - XA, axis=1) < thr
    fit_in = np.linalg.norm(sc * XB @ R.T + t - XA, axis=1) < thr
    if ident.mean() >= 0.5 or fit_in.sum() < 1.25 * ident.sum():
        sc, R, t, inl = 1.0, np.eye(3), np.zeros(3), ident
    shift = float(np.linalg.norm(t + (sc * R - np.eye(3)) @ np.median(XB, 0)))
    rot = float(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))))
    return sc, R, t, {"shift": shift, "rotation_deg": rot, "inlier_share": float(inl.mean())}


def kabsch(A: np.ndarray, B: np.ndarray):
    """R, t with B ~ R A + t (least squares)."""
    ma, mb = A.mean(0), B.mean(0)
    U, _, Vt = np.linalg.svd((B - mb).T @ (A - ma))
    S = np.eye(3)
    S[2, 2] = np.sign(np.linalg.det(U @ Vt)) or 1.0
    R = U @ S @ Vt
    return R, mb - R @ ma


def rigid_ransac(A: np.ndarray, B: np.ndarray, thr, iters: int = 300, seed: int = 0):
    """Robust rigid motion A -> B: RANSAC on 3-point samples, refit on the inliers. `thr`: inlier
    distance, one number or one per point."""
    rng = np.random.default_rng(seed)
    best = np.zeros(len(A), bool)
    for _ in range(iters):
        k = rng.choice(len(A), 3, replace=False)
        R, t = kabsch(A[k], B[k])
        inl = np.linalg.norm(A @ R.T + t - B, axis=1) < thr
        if inl.sum() > best.sum():
            best = inl
    if best.sum() < 3:
        best[:] = True
    R, t = kabsch(A[best], B[best])
    inl = np.linalg.norm(A @ R.T + t - B, axis=1) < thr
    if inl.sum() >= 3:
        R, t = kabsch(A[inl], B[inl])
    return R, t, inl


def object_motion(XA: np.ndarray, XB: np.ndarray, C: np.ndarray, obj_X: np.ndarray, cfg: MotionConfig,
                  seed: int = 0) -> dict:
    """Rigid motion + deformation of one object from its links (XA -> XB, displacement
    covariances C), all in scene units. `obj_X`: the object's own points (centre, main axes)."""
    from scipy.spatial import cKDTree
    from scipy.stats import chi2

    sig = np.sqrt(np.trace(C, axis1=1, axis2=2) / 3)                 # per-point uncertainty (1 sigma)
    R, t, inl = rigid_ransac(XA, XB, np.sqrt(chi2.ppf(0.99, 3)) * sig, seed=seed)
    c = np.median(obj_X, 0)
    moved = R @ c + t - c
    rng = np.random.default_rng(seed)
    boots = []
    idx = np.nonzero(inl)[0]
    for _ in range(200):                         # how sure is the move (resampling the inliers)
        k = rng.choice(idx, len(idx), replace=True)
        if len(np.unique(k)) < 3:
            continue
        Rb, tb = kabsch(XA[k], XB[k])
        boots.append(Rb @ c + tb - c)
    boots = np.array(boots).reshape(-1, 3)
    if len(boots) > 3:
        Cb = np.cov(boots.T) + np.eye(3) * 1e-12
        m2 = float(moved @ np.linalg.solve(Cb, moved))
        norms = np.linalg.norm(boots, axis=1)
        ci = [float(np.quantile(norms, 0.05)), float(np.quantile(norms, 0.95))]
    else:
        m2, ci = 0.0, None
    rv = XB - (XA @ R.T + t)
    resid = np.linalg.norm(rv, axis=1)
    z2 = _mahal2(rv, C)
    k = min(cfg.smooth_k, len(XA))
    _, nn = cKDTree(XA).query(XA, k=k)
    nn = nn.reshape(len(XA), -1)
    smooth = np.median(resid[nn], 1)
    # a neighbourhood deforms when its residuals together are beyond their uncertainty
    sig_def = z2[nn].sum(1) > chi2.ppf(cfg.level, 3 * nn.shape[1])
    # where the deformation peaks, along the object's main axes (-1..1 = one end .. other end)
    Xc = obj_X - c
    _, _, Vt = np.linalg.svd(Xc - Xc.mean(0), full_matrices=False)
    half = np.maximum(np.quantile(np.abs(Xc @ Vt.T), 0.95, axis=0), 1e-9)
    p = int(np.argmax(np.where(sig_def, smooth, -1))) if sig_def.any() else int(np.argmax(smooth))
    ax = np.clip((XA[p] - c) @ Vt.T / half, -1.5, 1.5)
    return {"R": R, "t": t, "inliers": inl, "moved": float(np.linalg.norm(moved)), "moved_vec": moved,
            "moved_ci": ci, "moved_significant": m2 > chi2.ppf(cfg.level, 3),
            "rotation_deg": float(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))),
            "resid": resid, "smooth": smooth, "significant": sig_def, "sigma": sig, "peak": XA[p], "peak_i": p,
            "peak_axes": ax.tolist()}


def where(axes: list) -> str:
    """Plain words for a position along an object's main axes."""
    a = [abs(x) for x in axes[:2]]
    if max(a) < 0.35:
        return "in the middle"
    if a[0] > 0.6 and a[1] > 0.6:
        return "at a corner"
    if a[0] >= a[1]:
        return "towards one end"
    return "along one side"


def measure(rec, objs: list, scale_m_per_unit: float, cfg: MotionConfig = MotionConfig(), device: str | None = None) -> dict:
    """Feature links between the two recordings, the no-change null, and per-object motion and
    deformation (objects of the before recording). Distances in cm."""
    import torch

    from ..recon import poses as P

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    cm = scale_m_per_unit * 100
    use = [np.nonzero(rec.video == v)[0][:: cfg.frame_step] for v in (0, 1)]
    allf = np.concatenate(use)
    kps0, descs0, wh = P.features([rec.files[s] for s in allf], cfg.n_features)
    kps, descs = {int(s): k for s, k in zip(allf, kps0)}, {int(s): d for s, d in zip(allf, descs0)}
    h0, w0 = rec.depth.shape[1:]
    K = rec.K.astype(np.float64).copy()
    K[:, 0] *= wh[0] / w0
    K[:, 1] *= wh[1] / h0
    E = rec.E.astype(np.float64)

    tracks, Xs, Cs, px = [], [], [], []
    for v in (0, 1):
        t, fr, kp, uv = _within_tracks(rec, use[v], kps, descs, cfg, device)
        pose_px = None if rec.pose_px is None else rec.pose_px * wh[0] / w0
        X, C, _, sigma = _triangulate_tracks(E, K, t, fr, uv, cfg, pose_px=pose_px)
        Cs.append(C)
        px.append(sigma)
        tid = {int(s): np.full(len(kps[int(s)]), -1) for s in use[v]}
        for tt, f, k in zip(t, fr, kp):
            if np.isfinite(X[tt]).all():
                tid[int(f)][k] = tt
        tracks.append(tid)
        Xs.append(X)
        log.info("motion: recording %d: %d 3D feature points, pixel noise %.2f px (%.0f s)", v,
                 int(np.isfinite(X).all(1).sum()), sigma, time.time() - t0)

    # links: before track -> after track by descriptor votes between nearby before/after frames
    c, d = P._centres_dirs(E)
    spread = float(np.linalg.norm(c - c.mean(0), axis=1).max()) + 1e-9
    dist = np.linalg.norm(c[use[0]][:, None] - c[use[1]][None], axis=2) / spread + (1 - d[use[0]] @ d[use[1]].T)
    votes: dict = {}
    static_votes: dict = {}
    for i, a in enumerate(use[0]):
        Da = torch.as_tensor(descs[int(a)], device=device)
        for j in np.argsort(dist[i])[: cfg.k_cross]:
            b = int(use[1][j])
            Db = torch.as_tensor(descs[b], device=device)
            if len(Da) < 8 or len(Db) < 8:
                continue
            dd = (2 - 2 * Da @ Db.T).clamp_min(0).sqrt()
            top = dd.topk(2, dim=1, largest=False)
            nn = top.indices[:, 0]
            ok = (top.values[:, 0] < cfg.cross_ratio * top.values[:, 1]) & (dd.argmin(0)[nn] == torch.arange(len(nn), device=device))
            ia = torch.nonzero(ok)[:, 0].cpu().numpy()
            ib = nn[ok].cpu().numpy()
            inl = _static_matches(kps[int(a)][ia], kps[b][ib], cfg.f_px)
            ta, tb = tracks[0][int(a)][ia], tracks[1][b][ib]
            both = (ta >= 0) & (tb >= 0)
            for x, y, st in zip(ta[both], tb[both], inl[both]):
                key = (int(x), int(y))
                votes[key] = votes.get(key, 0) + 1
                static_votes[key] = static_votes.get(key, 0) + int(st)
    best_a: dict = {}
    best_b: dict = {}
    for (x, y), n in votes.items():
        if n > best_a.get(x, (None, 0))[1]:
            best_a[x] = (y, n)
        if n > best_b.get(y, (None, 0))[1]:
            best_b[y] = (x, n)
    links = np.array([(x, y) for x, (y, n) in best_a.items() if n >= cfg.min_votes and best_b[y][0] == x],
                     np.int64).reshape(-1, 2)
    from scipy.stats import chi2

    # static links: most of their matches agree with a rigid scene between the two frames
    # (fundamental-matrix RANSAC, which does not use our poses); only these measure alignment
    static = np.array([2 * static_votes.get((int(x), int(y)), 0) > votes[(int(x), int(y))] for x, y in links], bool)
    XA, XB = Xs[0][links[:, 0]], Xs[1][links[:, 1]]
    CA, CB = Cs[0][links[:, 0]], Cs[1][links[:, 1]]
    align = {"static_links": int(static.sum()), "applied": False}
    if static.sum() >= cfg.min_static:
        sc, R, tr, info = align_recordings(XA[static], XB[static], (CA + CB)[static])
        XB = sc * XB @ R.T + tr
        CB = sc**2 * np.einsum("ij,njk,lk->nil", R, CB, R)
        align.update(applied=True, shift_cm=round(info["shift"] * cm, 2), rotation_deg=round(info["rotation_deg"], 3),
                     scale_change_pct=round(100 * (sc - 1), 3), inlier_share=round(info["inlier_share"], 3))
    Cd = CA + CB
    ref = static if static.sum() >= cfg.min_static else np.ones(len(links), bool)
    s2 = cross_term((XB - XA)[ref], Cd[ref])
    Cd = Cd + s2 * np.eye(3)[None]
    disp = np.linalg.norm(XB - XA, axis=1) * cm
    sig_cm = np.sqrt(np.trace(Cd, axis1=1, axis2=2) / 3) * cm
    z = np.sqrt(_mahal2(XB - XA, Cd))
    moved_pt = z**2 > chi2.ppf(cfg.level, 3)
    log.info("motion: %d of %d links static; alignment between recordings %s", align["static_links"], len(links),
             f"corrected by {align['shift_cm']} cm, {align['rotation_deg']} deg, scale {align['scale_change_pct']}%"
             if align["applied"] else "not corrected (too few static links)")
    log.info("motion: %d before/after links; point uncertainty %.2f cm median (of which recording misalignment "
             "%.2f cm); %.0f%% moved beyond their uncertainty; %.0f s", len(links),
             float(np.median(sig_cm)) if len(sig_cm) else float("nan"), np.sqrt(s2) * cm,
             100 * moved_pt.mean() if len(z) else 0, time.time() - t0)

    # per object
    obj_of = np.full(len(XA), -1)
    owner = {}
    for i, o in enumerate(objs):
        if o.structure:
            continue
        vox = o.extra.get("voxel")
        if not vox:
            continue
        for kk in o.voxels:
            owner.setdefault((vox, kk), i)
    voxes = {o.extra.get("voxel") for o in objs if o.extra.get("voxel")}
    for vox in voxes:
        keys = np.floor(XA / vox).astype(np.int64)
        for n, kk in enumerate(map(tuple, keys)):
            j = owner.get((vox, kk))
            if j is not None and obj_of[n] < 0:
                obj_of[n] = j
    resid = np.full(len(XA), np.nan)
    defsig = np.zeros(len(XA), bool)
    rows = []
    for j in np.unique(obj_of[obj_of >= 0]):
        sel = np.nonzero(obj_of == j)[0]
        if len(sel) < cfg.min_links:
            continue
        o = objs[j]
        r = object_motion(XA[sel], XB[sel], Cd[sel], o.X, cfg, seed=int(j))
        resid[sel] = r["smooth"] * cm
        defsig[sel] = r["significant"]
        sm = r["smooth"] * cm
        sg = r["significant"]
        rows.append({"recording": o.recording, "id": o.id, "label": o.label, "status": o.status, "links": int(len(sel)),
                     "rigid_inlier_share": round(float(r["inliers"].mean()), 3),
                     "moved_cm": round(r["moved"] * cm, 2),
                     "moved_ci_cm": None if r["moved_ci"] is None else [round(x * cm, 2) for x in r["moved_ci"]],
                     "moved_beyond_noise": bool(r["moved_significant"]),
                     "rotation_deg": round(r["rotation_deg"], 1),
                     "point_sigma_cm": round(float(np.median(r["sigma"])) * cm, 2),
                     "deform_p90_cm": round(float(np.quantile(sm, 0.9)), 2), "deform_peak_cm": round(float(sm[r["peak_i"]]), 2),
                     "deform_peak_sigma_cm": round(float(r["sigma"][r["peak_i"]]) * cm, 2),
                     "deformed_share": round(float(sg.mean()), 3),
                     "deformed": bool(sg.sum() >= max(3, 0.05 * len(sg))),
                     "deform_peak_where": where(r["peak_axes"]), "deform_peak_axes": [round(x, 2) for x in r["peak_axes"]],
                     "deform_peak_xyz": np.round(r["peak"], 4).tolist()})
    rows.sort(key=lambda r: -r["deform_p90_cm"])
    med = float(np.median(sig_cm)) if len(sig_cm) else float("nan")
    return {"links_XA": XA, "links_XB": XB, "C": Cd, "disp_cm": disp, "sigma_cm": sig_cm, "z": z, "moved_pt": moved_pt,
            "resid_cm": resid, "deform_sig": defsig, "obj_of": obj_of, "objects": rows, "level": cfg.level,
            "seconds": round(time.time() - t0),
            "summary": {"links": int(len(links)), "points_before": int(np.isfinite(Xs[0]).all(1).sum()),
                        "points_after": int(np.isfinite(Xs[1]).all(1).sum()),
                        "pixel_noise_px": [round(x, 2) for x in px], "point_sigma_cm_median": round(med, 3),
                        "misalignment_sigma_cm": round(float(np.sqrt(s2)) * cm, 3),
                        "alignment": align,
                        "links_moved_share": round(float(moved_pt.mean()), 3) if len(z) else None,
                        "objects_measured": len(rows), "objects_moved": sum(r["moved_beyond_noise"] for r in rows),
                        "objects_deformed": sum(r["deformed"] for r in rows)}}


def scene_layers(m: dict, pts, rec_key: str = "A") -> list:
    """Viewer layers: distance moved per feature point, deformation per point, motion arrows."""
    from ..viz import scene3d

    if not len(m["links_XA"]):
        return []
    thr = float(np.median(m["sigma_cm"]))
    t = pts.add(m["links_XA"])
    hi = max(float(np.quantile(m["disp_cm"], 0.98)), 2 * thr, 1.0)
    rgb, stops = scene3d._scale_rgb(m["disp_cm"], "viridis", 0, hi)
    L = [{"name": "heat map: distance moved (matched feature points, cm)", "kind": "heat", "rec": rec_key,
          "visible": False, "size": 4, "pts": t, "rgb": scene3d._b64(rgb),
          "legend": {"lo": 0, "hi": round(hi, 2), "stops": stops}}]
    hu = max(float(np.quantile(m["sigma_cm"], 0.95)), 0.1)
    rgb, stops = scene3d._scale_rgb(m["sigma_cm"], "cividis", 0, hu)
    L.append({"name": "heat map: uncertainty of each matched point (1 sigma, cm)", "kind": "heat", "rec": rec_key,
              "visible": False, "size": 4, "pts": t, "rgb": scene3d._b64(rgb),
              "legend": {"lo": 0, "hi": round(hu, 2), "stops": stops}})
    ok = np.isfinite(m["resid_cm"])
    if ok.any():
        hi = max(float(np.quantile(m["resid_cm"][ok], 0.98)), 2 * thr, 1.0)
        rgb, stops = scene3d._scale_rgb(m["resid_cm"][ok], "magma", 0, hi)
        L.append({"name": "heat map: deformation (beyond each object's rigid motion, cm)", "kind": "heat",
                  "rec": rec_key, "visible": False, "size": 4, "pts": pts.add(m["links_XA"][ok]),
                  "rgb": scene3d._b64(rgb), "legend": {"lo": 0, "hi": round(hi, 2), "stops": stops}})
    mv = np.nonzero(m["moved_pt"])[0]
    if len(mv):
        mv = mv[np.argsort(-m["disp_cm"][mv])[:3000]]
        seg = np.full((3 * len(mv), 3), np.nan)
        seg[0::3], seg[1::3] = m["links_XA"][mv], m["links_XB"][mv]
        L.append({"name": "moved feature points (before -> after)", "kind": "line", "rec": None, "visible": False,
                  "pts": pts.add(seg), "color": "#ff7f0e"})
    return L


def figure(m: dict, rec, objs: list, out, title: str, n_obj: int = 3) -> None:
    """motion_map.png: distance moved and deformation on a before frame, the no-change null,
    and close-ups of the most deformed objects (links in the object's own main-axes plane)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .geometry import project

    XA, disp, res = m["links_XA"], m["disp_cm"], m["resid_cm"]
    thr = float(np.median(m["sigma_cm"])) * 3
    if not len(XA):
        return
    fa = np.nonzero(rec.video == 0)[0]
    counts = []
    for s in fa[:: max(1, len(fa) // 40)]:
        u, v, z = project(XA, rec.K[s], rec.E[s])
        h, w = rec.depth.shape[1:]
        counts.append(((z > 0) & (u >= 0) & (u < w) & (v >= 0) & (v < h)).sum())
    s = fa[:: max(1, len(fa) // 40)][int(np.argmax(counts))]
    u, v, z = project(XA, rec.K[s], rec.E[s])
    h, w = rec.depth.shape[1:]
    vis = (z > 0) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    top = [r for r in m["objects"] if r["recording"] == "A"][:n_obj]
    fig = plt.figure(figsize=(16, 9 + 3.5 * (len(top) > 0)))
    gs = fig.add_gridspec(2 if top else 1, 3, height_ratios=[1.6, 1] if top else [1])
    ax = fig.add_subplot(gs[0, 0])
    ax.imshow(rec.images[s])
    sc = ax.scatter(u[vis], v[vis], c=disp[vis], s=4, cmap="viridis", vmin=0, vmax=max(np.quantile(disp, 0.98), 2 * thr))
    fig.colorbar(sc, ax=ax, fraction=0.046, label="distance moved, cm")
    ax.set_title("distance moved (feature points, before frame)")
    ax.axis("off")
    ax = fig.add_subplot(gs[0, 1])
    ax.imshow(rec.images[s])
    ok = vis & np.isfinite(res)
    sc = ax.scatter(u[ok], v[ok], c=res[ok], s=4, cmap="magma", vmin=0, vmax=max(np.nanquantile(res, 0.98) if np.isfinite(res).any() else 1, 2 * thr))
    fig.colorbar(sc, ax=ax, fraction=0.046, label="deformation, cm")
    ax.set_title("deformation (beyond each object's rigid motion)")
    ax.axis("off")
    ax = fig.add_subplot(gs[0, 2])
    from scipy.stats import chi
    zc = np.sqrt(chi.ppf(m["level"], 3) ** 2)
    bins = np.linspace(0, 4 * zc, 50)                    # everything beyond lands in the last bar
    ax.hist(np.clip(m["z"], 0, bins[-1]), bins, density=True, alpha=0.7, label="before vs after, matched points")
    xs = np.linspace(0, bins[-1], 200)
    ax.plot(xs, chi.pdf(xs, 3), "k-", lw=1, label="expected if nothing moved")
    ax.axvline(zc, color="k", ls="--", label=f"{m['level']:.1%} level")
    sm = m.get("summary", {})
    ax.set_xlabel(f"displacement / its own uncertainty (last bar: {4 * zc:.0f} or more)")
    ax.legend(fontsize=8)
    ax.set_title(f"moved beyond their uncertainty: {m['moved_pt'].mean():.0%} of points\n"
                 f"(point uncertainty {np.median(m['sigma_cm']):.2f} cm median)", fontsize=10)
    by = {(o.recording, o.id): o for o in objs}
    for i, r in enumerate(top):
        o = by[(r["recording"], r["id"])]
        sel = np.nonzero(m["obj_of"] == objs.index(o))[0]
        c = np.median(o.X, 0)
        _, _, Vt = np.linalg.svd(o.X - o.X.mean(0), full_matrices=False)
        P2 = (XA[sel] - c) @ Vt[:2].T
        O2 = (o.X - c) @ Vt[:2].T
        ax = fig.add_subplot(gs[1, i])
        ax.scatter(O2[:, 0], O2[:, 1], s=1, c="0.85")
        sc = ax.scatter(P2[:, 0], P2[:, 1], c=res[sel], s=10, cmap="magma", vmin=0, vmax=max(r["deform_peak_cm"], 2 * thr))
        fig.colorbar(sc, ax=ax, fraction=0.046, label="cm")
        ax.set_aspect("equal")
        ax.set_xticks([]), ax.set_yticks([])
        sg = m["deform_sig"][sel]
        ax.scatter(P2[sg, 0], P2[sg, 1], s=40, facecolors="none", edgecolors="tab:cyan", lw=0.6)
        ax.set_title(f"{o.label or 'object'} {o.recording}{o.id}: deformation peak {r['deform_peak_cm']:.1f} "
                     f"± {r['deform_peak_sigma_cm']:.1f} cm {r['deform_peak_where']}\n(circled: beyond uncertainty, "
                     f"{r['deformed_share']:.0%}); moved {r['moved_cm']:.1f} cm, turned {r['rotation_deg']:.0f} deg",
                     fontsize=9)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=80)
    plt.close(fig)
