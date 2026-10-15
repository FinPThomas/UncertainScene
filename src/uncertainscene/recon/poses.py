"""Sharper camera poses: feature tracks + bundle adjustment, started from the VGGT poses.

Why: VGGT estimates poses from 518 px images and the chunks are lined up to roughly 10 cm, so
the poses are a little off (the run 2 splats had to learn a 0.7 deg median correction per
frame). A splat trained on slightly wrong cameras smears every surface and grows floaters
in mid-air to explain the disagreement. Classic bundle adjustment on full-size frames fixes
this, and VGGT's poses are an excellent starting point for it.

1. SIFT features (RootSIFT) on every frame at full frame size.
2. Pairs: each frame with its nearest frames by camera position and viewing direction, plus
   its neighbours in time. Both recordings go in together, so the same 3D points tie the
   before and after cameras to each other.
3. Matches: mutual nearest neighbour + ratio test, kept only where a RANSAC fundamental matrix
   agrees. This check does not use VGGT's poses at all.
4. Tracks: matches chained across frames; tracks seen twice in one frame are dropped.
5. Triangulate with the current poses, drop points behind a camera, with too little
   parallax, or reprojecting badly.
6. Bundle adjustment (robust Huber loss): per-frame rotation and translation corrections, one
   focal length shared by all recordings (same phone, same 1x lens; a focal per recording can
   trade against depth and leave the two recordings at slightly different scales), and every
   3D point.
7. Re-triangulate with a tighter threshold and adjust again.
8. Map the result back onto VGGT's frame (similarity fit of the camera centres), so depth
   maps, floor, objects and every other step keep using the same world frame.

Reported: median reprojection error before (VGGT poses) and after, in pixels at the frame
size, and how far each camera moved.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class PoseConfig:
    n_features: int = 6000
    contrast: float = 0.02          # SIFT contrast threshold (OpenCV default 0.04 finds few points on plain fabric)
    k_near: int = 12                # pose-nearest partner frames per frame
    k_cross: int = 6                # plus this many pose-nearest frames from each OTHER recording
    k_time: int = 2                 # plus this many neighbours in time on each side
    ratio: float = 0.8              # Lowe ratio test
    ransac_px: float = 1.5          # fundamental-matrix RANSAC threshold
    min_inliers: int = 30
    min_angle_deg: float = 1.5      # minimum parallax for a 3D point
    first_px: float = 24.0          # reprojection gate before adjusting (VGGT poses are off by ~10 px)
    final_px: float = 3.0           # gate for the second round
    huber_px: float = 2.0
    rounds: int = 2
    iters: int = 300                # L-BFGS iterations per round
    max_points: int = 400_000
    min_frame_points: int = 30      # frames with fewer adjusted points are resected afterwards
    min_resect: int = 8             # 2D-3D inliers needed to trust a resected pose
    resect_px: float = 4.0          # PnP inlier gate (px)
    sparse_obs: int = 150           # frames with fewer track observations get a wider second match
    min_inliers_sparse: int = 15
    group_link: int = 50            # frames tied to the main group by fewer shared points get a wider match
    polish_iters: int = 150         # final joint adjustment over every frame
    polish_always: bool = True
    shared_focal: bool = True       # one focal length for every recording (same phone, same 1x lens)


def _rootsift(desc: np.ndarray) -> np.ndarray:
    d = desc / (np.abs(desc).sum(1, keepdims=True) + 1e-7)
    return np.sqrt(d).astype(np.float32)


def features(files: list[str], n: int, contrast: float = 0.04):
    """Keypoints [N, 2] (continuous pixel coords: pixel centre of index i at i + 0.5) and RootSIFT
    descriptors per frame, plus the frame size (w, h)."""
    import cv2

    sift = cv2.SIFT_create(nfeatures=n, contrastThreshold=contrast)
    kps, descs, wh = [], [], None
    for f in files:
        g = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        wh = (g.shape[1], g.shape[0])
        k, d = sift.detectAndCompute(g, None)
        if d is None:
            k, d = [], np.zeros((0, 128), np.float32)
        kps.append(np.array([p.pt for p in k], np.float32).reshape(-1, 2) + 0.5)
        descs.append(_rootsift(d))
    return kps, descs, wh


def _centres_dirs(E: np.ndarray):
    R, t = E[:, :, :3], E[:, :, 3]
    return -np.einsum("sji,sj->si", R, t), R[:, 2]


def pairs(E: np.ndarray, video: np.ndarray, k_near: int, k_time: int, k_cross: int = 0) -> np.ndarray:
    """Frame pairs (i < j) to match: pose neighbours (position + viewing direction), time
    neighbours within each recording and, with `k_cross`, the pose-nearest frames of every other
    recording (the only pairs that tie the recordings to each other)."""
    c, d = _centres_dirs(E)
    spread = float(np.linalg.norm(c - c.mean(0), axis=1).max()) + 1e-9
    dist = np.linalg.norm(c[:, None] - c[None], axis=2) / spread + (1 - d @ d.T)
    np.fill_diagonal(dist, np.inf)
    P = set()
    for i in range(len(E)):
        for j in np.argsort(dist[i])[:k_near]:
            P.add((min(i, j), max(i, j)))
        if k_cross:
            other = np.nonzero(video != video[i])[0]
            for j in other[np.argsort(dist[i, other])[:k_cross]]:
                P.add((min(i, int(j)), max(i, int(j))))
    for v in np.unique(video):
        idx = np.nonzero(video == v)[0]
        for a in range(len(idx)):
            for b in range(a + 1, min(a + 1 + k_time, len(idx))):
                P.add((int(idx[a]), int(idx[b])))
    return np.array(sorted(P), dtype=np.int64).reshape(-1, 2)


def match(kps, descs, P: np.ndarray, cfg: PoseConfig, device: str = "cpu"):
    """Verified matches as (frame_a, kp_a, frame_b, kp_b) rows."""
    import cv2
    import torch

    D = [torch.as_tensor(d, device=device) for d in descs]
    rows, kept = [], 0
    for a, b in P:
        if len(D[a]) < 8 or len(D[b]) < 8:
            continue
        s = D[a] @ D[b].T
        dist = (2 - 2 * s).clamp_min(0).sqrt()
        top = dist.topk(2, dim=1, largest=False)
        nn = top.indices[:, 0]
        ok = top.values[:, 0] < cfg.ratio * top.values[:, 1]
        back = dist.argmin(0)
        ok &= back[nn] == torch.arange(len(nn), device=device)
        ia = torch.nonzero(ok)[:, 0].cpu().numpy()
        ib = nn[ok].cpu().numpy()
        if len(ia) < cfg.min_inliers:
            continue
        try:
            _, inl = cv2.findFundamentalMat(kps[a][ia], kps[b][ib], cv2.USAC_MAGSAC, cfg.ransac_px, 0.999, 5000)
        except cv2.error:                       # degenerate (e.g. one flat surface): plain RANSAC
            try:
                _, inl = cv2.findFundamentalMat(kps[a][ia], kps[b][ib], cv2.FM_RANSAC, cfg.ransac_px, 0.999)
            except cv2.error:
                continue
        if inl is None:
            continue
        inl = inl.ravel().astype(bool)
        if inl.sum() < cfg.min_inliers:
            continue
        kept += 1
        rows.append(np.stack([np.full(inl.sum(), a), ia[inl], np.full(inl.sum(), b), ib[inl]], 1))
    log.info("poses: %d of %d frame pairs matched", kept, len(P))
    return np.concatenate(rows) if rows else np.zeros((0, 4), np.int64), kept


def tracks(M: np.ndarray, n_kp: list[int]):
    """Chain matches into tracks. Returns per observation: track id, frame, keypoint index."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    off = np.concatenate([[0], np.cumsum(n_kp)])
    ga, gb = off[M[:, 0]] + M[:, 1], off[M[:, 2]] + M[:, 3]
    n = int(off[-1])
    _, lab = connected_components(coo_matrix((np.ones(len(ga)), (ga, gb)), shape=(n, n)), directed=False)
    used = np.unique(np.concatenate([ga, gb]))
    frame = np.searchsorted(off, used, side="right") - 1
    kp = used - off[frame]
    t = lab[used]
    # a track that sees one frame twice holds a wrong match. One such frame: drop just that
    # frame's observations (the rest still agree); more than one: the chain is unreliable, drop it
    key = t.astype(np.int64) * (len(n_kp) + 1) + frame
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    dup = cnt[inv] > 1
    first = np.zeros(len(t), bool)
    first[np.unique(inv, return_index=True)[1]] = True
    n_bad_frames = np.bincount(t[dup & first], minlength=int(t.max()) + 1 if len(t) else 0)
    keep = ~dup & (n_bad_frames[t] <= 1)
    t, frame, kp = t[keep], frame[keep], kp[keep]
    _, t, size = np.unique(t, return_inverse=True, return_counts=True)
    keep = size[t] >= 2
    _, t = np.unique(t[keep], return_inverse=True)
    return t, frame[keep], kp[keep]


def _rays(E, K, frame, uv):
    """World-frame camera centres and unit ray directions of observations."""
    R, tv = E[frame, :, :3], E[frame, :, 3]
    c = -np.einsum("nji,nj->ni", R, tv)
    x = np.stack([(uv[:, 0] - K[frame, 0, 2]) / K[frame, 0, 0], (uv[:, 1] - K[frame, 1, 2]) / K[frame, 1, 1],
                  np.ones(len(uv))], 1)
    d = np.einsum("nji,nj->ni", R, x)
    return c, d / np.linalg.norm(d, axis=1, keepdims=True)


def triangulate(E, K, t, frame, uv, w=None):
    """Least-squares intersection of each track's rays (optionally weighted per observation by
    `w`, e.g. 1 / pixel variance). Returns points [T, 3] and the largest angle between any ray
    and the track's mean ray (degrees), as a parallax measure."""
    T = int(t.max()) + 1 if len(t) else 0
    c, d = _rays(E, K, frame, uv)
    A = np.eye(3)[None] - d[:, :, None] * d[:, None, :]
    if w is not None:
        A = A * np.asarray(w, float)[:, None, None]
    SA = np.zeros((T, 3, 3))
    Sb = np.zeros((T, 3))
    np.add.at(SA, t, A)
    np.add.at(Sb, t, np.einsum("nij,nj->ni", A, c))
    SA += np.eye(3)[None] * 1e-9
    X = np.linalg.solve(SA, Sb[..., None])[..., 0]
    md = np.zeros((T, 3))
    np.add.at(md, t, d)
    md /= np.linalg.norm(md, axis=1, keepdims=True) + 1e-12
    ang = np.zeros(T)
    np.maximum.at(ang, t, np.degrees(np.arccos(np.clip((d * md[t]).sum(1), -1, 1))))
    return X, ang


def reproject(E, K, X, t, frame, uv):
    """Reprojection error (pixels) and camera depth of each observation."""
    Xc = np.einsum("nij,nj->ni", E[frame, :, :3], X[t]) + E[frame, :, 3]
    z = Xc[:, 2]
    u = K[frame, 0, 0] * Xc[:, 0] / np.where(z > 1e-9, z, 1e-9) + K[frame, 0, 2]
    v = K[frame, 1, 1] * Xc[:, 1] / np.where(z > 1e-9, z, 1e-9) + K[frame, 1, 2]
    return np.hypot(u - uv[:, 0], v - uv[:, 1]), z


def _filter(E, K, t, frame, uv, gate, min_angle, max_points):
    """Triangulate, then keep observations that reproject within `gate` px in front of the
    camera, on tracks with enough parallax and at least 2 kept observations."""
    X, ang = triangulate(E, K, t, frame, uv)
    err, z = reproject(E, K, X, t, frame, uv)
    keep = (err < gate) & (z > 0) & (ang[t] >= min_angle)
    cnt = np.bincount(t[keep], minlength=len(X))
    keep &= cnt[t] >= 2
    if keep.any() and len(np.unique(t[keep])) > max_points:  # longest tracks first
        tl = np.unique(t[keep])
        tl = tl[np.argsort(-cnt[tl])[:max_points]]
        keep &= np.isin(t, tl)
    _, tn = np.unique(t[keep], return_inverse=True)
    X, _ = triangulate(E, K, tn, frame[keep], uv[keep])
    return X, tn, frame[keep], uv[keep]


def _rodrigues(r):
    import torch

    th = r.norm(dim=-1, keepdim=True).clamp_min(1e-12)
    k = r / th
    Kx = torch.zeros(r.shape[0], 3, 3, dtype=r.dtype, device=r.device)
    Kx[:, 0, 1], Kx[:, 0, 2], Kx[:, 1, 0] = -k[:, 2], k[:, 1], k[:, 2]
    Kx[:, 1, 2], Kx[:, 2, 0], Kx[:, 2, 1] = -k[:, 0], -k[:, 1], k[:, 0]
    th = th[..., None]
    return torch.eye(3, dtype=r.dtype, device=r.device) + torch.sin(th) * Kx + (1 - torch.cos(th)) * Kx @ Kx


def bundle_adjust(E, K, video, X, t, frame, uv, cfg: PoseConfig, device: str = "cpu", fix_points: bool = False):
    """Jointly refine per-frame poses, the focal length (shared by all recordings, or one per
    recording without `cfg.shared_focal`) and the 3D points (or,
    with `fix_points`, only the poses and focals: resection, well conditioned).
    Returns new E, K, X."""
    import torch

    dt = torch.float64
    E0 = torch.as_tensor(E, dtype=dt, device=device)
    c0, _ = _centres_dirs(E)
    spread = float(np.linalg.norm(c0 - c0.mean(0), axis=1).max()) + 1e-9
    # focal index per frame: one for all recordings (same phone and lens, CAPTURE.md), or one each
    fvid = np.zeros_like(video) if cfg.shared_focal else video
    nv = int(fvid.max()) + 1
    f0 = torch.as_tensor([np.median(K[fvid == v, 0, 0]) for v in range(nv)], dtype=dt, device=device)
    pp = torch.as_tensor(K[:, :2, 2], dtype=dt, device=device)
    aspect = torch.as_tensor(K[:, 1, 1] / K[:, 0, 0], dtype=dt, device=device)
    vid = torch.as_tensor(fvid, device=device)
    om = torch.zeros(len(E), 3, dtype=dt, device=device, requires_grad=True)
    tau = torch.zeros(len(E), 3, dtype=dt, device=device, requires_grad=True)
    lf = torch.zeros(nv, dtype=dt, device=device, requires_grad=True)
    Xs = torch.as_tensor(X / spread, dtype=dt, device=device).requires_grad_(not fix_points)
    ti = torch.as_tensor(t, device=device)
    fi = torch.as_tensor(frame, device=device)
    obs = torch.as_tensor(uv, dtype=dt, device=device)
    C0 = torch.as_tensor(c0, dtype=dt, device=device)
    # cheirality: points must stay in front of the cameras that see them (a projection through
    # z ~ 0 is what made the first real run diverge from 8 px to 341 px)
    z_ref = np.median((np.einsum("nij,nj->ni", E[frame, :, :3], X[t]) + E[frame, :, 3])[:, 2])
    z_min = 0.02 * float(abs(z_ref))

    def cams():
        R = _rodrigues(om) @ E0[:, :, :3]
        tv = (_rodrigues(om) @ E0[:, :, 3:])[..., 0] + tau * spread
        return R, tv

    def closure():
        opt.zero_grad()
        R, tv = cams()
        Xc = torch.einsum("nij,nj->ni", R[fi], Xs[ti] * spread) + tv[fi]
        z = Xc[:, 2].clamp_min(z_min)
        f = (f0 * torch.exp(lf))[vid][fi]
        u = f * Xc[:, 0] / z + pp[fi, 0]
        v = f * aspect[fi] * Xc[:, 1] / z + pp[fi, 1]
        r = torch.sqrt((u - obs[:, 0]) ** 2 + (v - obs[:, 1]) ** 2 + 1e-12)
        h = cfg.huber_px
        loss = torch.where(r < h, 0.5 * r**2, h * (r - 0.5 * h)).mean()
        loss = loss + 10 * h * (torch.relu(z_min - Xc[:, 2]) / z_min).mean()
        # weak pull of camera centres towards VGGT's: fixes the free scale/position of the
        # problem without stopping real corrections (a 1% of spread move costs ~0.01 px^2)
        c = -torch.einsum("sji,sj->si", R, tv)
        loss = loss + 1e-2 * (((c - C0) / spread) ** 2).sum(1).mean() * 100
        loss.backward()
        return loss

    def median_px():
        with torch.no_grad():
            R, tv = cams()
            Xc = torch.einsum("nij,nj->ni", R[fi], Xs[ti] * spread) + tv[fi]
            f = (f0 * torch.exp(lf))[vid][fi]
            u = f * Xc[:, 0] / Xc[:, 2] + pp[fi, 0]
            v = f * aspect[fi] * Xc[:, 1] / Xc[:, 2] + pp[fi, 1]
            r = torch.sqrt((u - obs[:, 0]) ** 2 + (v - obs[:, 1]) ** 2)
            r = torch.where((Xc[:, 2] > 0) & torch.isfinite(r), r, torch.full_like(r, 1e9))
            return float(r.median())

    # short L-BFGS runs; a run that makes the median reprojection error clearly worse (or
    # non-finite) is undone and ends the adjustment, so it cannot wander off the poses it was given
    params = [om, tau, lf] if fix_points else [om, tau, lf, Xs]
    start_px = best_px = median_px()
    best = [q.detach().clone() for q in params]
    chunk, prev = 25, None
    opt = torch.optim.LBFGS(params, lr=1, max_iter=chunk, history_size=30, line_search_fn="strong_wolfe",
                            tolerance_grad=1e-12, tolerance_change=1e-14)
    for _ in range(max(1, cfg.iters // chunk)):
        loss = float(opt.step(closure))
        px = median_px()
        if not np.isfinite(px) or not np.isfinite(loss) or px > 1.05 * best_px + 0.05:
            with torch.no_grad():
                for q, b in zip(params, best):
                    q.copy_(b)
            break
        best_px, best = min(px, best_px), [q.detach().clone() for q in params]
        if prev is not None and abs(prev - loss) < 1e-7 * max(abs(prev), 1e-12):
            break
        prev = loss
    log.debug("bundle adjustment: %.2f -> %.2f px median", start_px, median_px())
    with torch.no_grad():
        R, tv = cams()
        En = torch.cat([R, tv[..., None]], 2).cpu().numpy()
        f = (f0 * torch.exp(lf)).cpu().numpy()
    Kn = K.copy()
    Kn[:, 0, 0] = f[fvid]
    Kn[:, 1, 1] = f[fvid] * K[:, 1, 1] / K[:, 0, 0]
    return En, Kn, Xs.detach().cpu().numpy() * spread


def _to_frame(E_new, X, E_ref):
    """Similarity that maps the refined camera centres back onto the reference centres, applied
    to cameras and points (keeps VGGT's world frame and scale)."""
    from .chunked import transform_cameras, umeyama

    c_new, _ = _centres_dirs(E_new)
    c_ref, _ = _centres_dirs(E_ref)
    s, R, t, _ = umeyama(c_new, c_ref, iters=3, keep=0.9)
    E2, _ = transform_cameras(E_new, np.ones(1), s, R, t)
    return E2, s * X @ R.T + t


def _pose_delta(E_new, E_old):
    """Camera-side correction C with E_new = C @ E_old (as 4x4), per frame."""
    def h(E):
        H = np.tile(np.eye(4), (len(E), 1, 1))
        H[:, :3] = E
        return H
    return np.einsum("nij,njk->nik", h(E_new), np.linalg.inv(h(E_old)))[:, :3]


def _blend(C0, C1, w):
    """Blend two corrections: rotations by slerp, translations linearly (w = weight of C1)."""
    from scipy.spatial.transform import Rotation as Rot
    from scipy.spatial.transform import Slerp

    r = Slerp([0, 1], Rot.from_matrix(np.stack([C0[:, :3], C1[:, :3]])))([w]).as_matrix()[0]
    return np.c_[r, (1 - w) * C0[:, 3] + w * C1[:, 3]]


def _interpolate_weak(E_new, E_old, video, weak):
    """Initial pose of each weak frame: its VGGT pose moved by the correction the adjustment
    made to its nearest well-constrained neighbours in time (blended before/after)."""
    C = _pose_delta(E_new, E_old)
    out = E_new.copy()
    for v in np.unique(video):
        idx = np.nonzero(video == v)[0]
        good = idx[~weak[idx]]
        if not len(good):
            continue
        for w in idx[weak[idx]]:
            a, b = good[good < w], good[good > w]
            if len(a) and len(b):
                Cw = _blend(C[a[-1]], C[b[0]], (w - a[-1]) / (b[0] - a[-1]))
            else:
                Cw = C[a[-1] if len(a) else b[0]]
            out[w] = np.c_[Cw[:, :3] @ E_old[w][:, :3], Cw[:, :3] @ E_old[w][:, 3] + Cw[:, 3]]
    return out


def _resect(E_init, K, X, uv, gate, min_inliers):
    """One frame's pose from 2D-3D matches (PnP + RANSAC from `E_init`, then Levenberg-Marquardt
    on the inliers). Returns the pose and the inlier mask, or None if it is not trustworthy."""
    import cv2

    if len(X) < min_inliers:
        return None
    Kc = K.astype(np.float64)
    rv, _ = cv2.Rodrigues(E_init[:, :3].astype(np.float64))
    tv = E_init[:, 3].astype(np.float64).reshape(3, 1)
    obj, img = X.astype(np.float64), uv.astype(np.float64)
    try:
        ok, rv2, tv2, inl = cv2.solvePnPRansac(obj, img, Kc, None, rv.copy(), tv.copy(), True, 2000, gate, 0.999,
                                               flags=cv2.SOLVEPNP_ITERATIVE)
    except cv2.error:
        return None
    if not ok or inl is None or len(inl) < min_inliers:
        return None
    inl = inl.ravel()
    rv2, tv2 = cv2.solvePnPRefineLM(obj[inl], img[inl], Kc, None, rv2, tv2)
    R = cv2.Rodrigues(rv2)[0]
    E = np.c_[R, tv2.ravel()]
    # a resected pose far from the interpolated guess is a wrong solution, not a correction
    turn = np.degrees(np.arccos(np.clip((np.trace(R @ E_init[:, :3].T) - 1) / 2, -1, 1)))
    if turn > 10:
        return None
    Xc = obj @ R.T + E[:, 3]
    z = Xc[:, 2]
    if (z[inl] <= 0).any():
        return None
    u = Kc[0, 0] * Xc[:, 0] / np.where(z > 1e-9, z, 1e-9) + Kc[0, 2]
    v = Kc[1, 1] * Xc[:, 1] / np.where(z > 1e-9, z, 1e-9) + Kc[1, 2]
    mask = (np.hypot(u - img[:, 0], v - img[:, 1]) < gate) & (z > 0)
    if mask.sum() < min_inliers:
        return None
    return E, mask


def _place_weak_frames(Ec, Kc, E, video, t_, frame, uv, weak, gate, cfg):
    """Pose every weak frame: interpolate its correction from its neighbours, then resect it
    against points triangulated from the well-constrained frames alone. Returns new poses, the
    extra observations (track, frame, uv) that support them, and counts."""
    Ec = _interpolate_weak(Ec, E, video, weak)
    strong = ~weak[frame]
    T = int(t_.max()) + 1
    X, ang = triangulate(Ec, Kc, t_[strong], frame[strong], uv[strong])
    X = np.r_[X, np.zeros((T - len(X), 3))] if len(X) < T else X
    err, z = reproject(Ec, Kc, X, t_, frame, uv)
    okobs = strong & (err < gate) & (z > 0)
    cnt = np.bincount(t_[okobs], minlength=T)
    good_t = (cnt >= 2) & (np.r_[ang, np.zeros(T - len(ang))] >= cfg.min_angle_deg)
    resected, extra = 0, []
    for w in np.nonzero(weak)[0]:
        o = np.nonzero((frame == w) & good_t[t_])[0]
        r = _resect(Ec[w], Kc[w], X[t_[o]], uv[o], gate, cfg.min_resect)
        if r is None:
            continue
        Ec[w], m = r
        resected += 1
        extra.append(o[m])
    extra = np.concatenate(extra) if extra else np.zeros(0, np.int64)
    return Ec, extra, resected


def pose_uncertainty(err, fr, n_frames: int, vggt_px: float, min_points: int = 8) -> np.ndarray:
    """Per-frame pose uncertainty, as the pixel error (1 sigma per axis) the pose alone adds to
    any point seen in that frame. Measured, not tuned:
    - a frame with n adjusted points and residual spread s (median error / 1.1774) has a 6-dof
      pose fitted from n points, so its pose error projects to about s * sqrt(6 / n); any
      residual beyond the whole problem's spread is also put down to the pose;
    - a frame with fewer than `min_points` points is unconstrained: it gets VGGT's own measured
      error level, the error of the poses it started from."""
    sg = float(np.median(err)) / 1.1774
    vg = vggt_px / 1.1774
    out = np.full(n_frames, vg)
    n = np.bincount(fr, minlength=n_frames)
    for s in np.nonzero(n >= min_points)[0]:
        ss = float(np.median(err[fr == s])) / 1.1774
        out[s] = min(np.sqrt(ss**2 * 6 / n[s] + max(ss**2 - sg**2, 0.0)), vg)
    return out


def refine_tracks(E: np.ndarray, Kf: np.ndarray, video: np.ndarray, t_: np.ndarray, frame: np.ndarray,
                  uv: np.ndarray, cfg: PoseConfig = PoseConfig(), device: str = "cpu", t0: float | None = None):
    """Bundle adjustment of poses (camera-from-world E, intrinsics Kf at the frame size) from
    feature tracks (track id, frame, keypoint position per observation). Every frame gets a
    refined pose: frames with too few points of their own are resected against the others.
    Returns E, K, the points and their observations, and a report."""
    t0 = t0 or time.time()
    Ec = E.astype(np.float64)
    X, tn, fr, uvn = _filter(Ec, Kf, t_, frame, uv, cfg.first_px, cfg.min_angle_deg, cfg.max_points)
    err0, _ = reproject(Ec, Kf, X, tn, fr, uvn)
    before = {"median_px": float(np.median(err0)), "points": int(len(X)), "observations": int(len(tn))}
    log.info("poses: VGGT poses reproject at %.2f px median (%d points)", before["median_px"], len(X))
    Kc = Kf
    rounds = []
    for r in range(cfg.rounds):
        # resection-intersection first (poses with points fixed, then points re-triangulated):
        # cannot run away, and hands the joint adjustment a start that is already close
        for _ in range(2):
            Ec, Kc, _ = bundle_adjust(Ec, Kc, video, X, tn, fr, uvn, cfg, device, fix_points=True)
            X, _ = triangulate(Ec, Kc, tn, fr, uvn)
        Ec, Kc, X = bundle_adjust(Ec, Kc, video, X, tn, fr, uvn, cfg, device)
        err, _ = reproject(Ec, Kc, X, tn, fr, uvn)
        rounds.append({"median_px": float(np.median(err)), "points": int(len(X)), "observations": int(len(tn))})
        log.info("poses: round %d: %.2f px median, %d points (%.0f s)", r + 1, rounds[-1]["median_px"], len(X),
                 time.time() - t0)
        if r + 1 < cfg.rounds:  # re-triangulate all tracks with the better poses, tighter gate
            gate = max(cfg.final_px * 2, cfg.huber_px * 3, 2 * rounds[-1]["median_px"])
            X2, tn2, fr2, uvn2 = _filter(Ec, Kc, t_, frame, uv, gate, cfg.min_angle_deg, cfg.max_points)
            if len(X2) >= 100:
                X, tn, fr, uvn = X2, tn2, fr2, uvn2
    # final gate: final_px, or wider when the poses are still that far off (never an empty set)
    final_gate = max(cfg.final_px, 2 * (rounds[-1]["median_px"] if rounds else before["median_px"]))
    X, tn, fr, uvn = _filter(Ec, Kc, t_, frame, uv, final_gate, cfg.min_angle_deg, cfg.max_points)
    if len(X) < 100:
        raise RuntimeError(f"only {len(X)} points left after bundle adjustment")

    # frames the adjustment could not constrain (few points of their own): interpolate, resect
    # against the rest, then one more joint adjustment over every frame
    seen = np.bincount(fr, minlength=len(E))
    weak = seen < cfg.min_frame_points
    n_weak, resected = int(weak.sum()), 0
    if n_weak:
        Ec, extra, resected = _place_weak_frames(Ec, Kc, E.astype(np.float64), video, t_, frame, uv, weak,
                                                 max(final_gate, cfg.resect_px), cfg)
        log.info("poses: %d frames with < %d points: %d resected, %d interpolated from neighbours", n_weak,
                 cfg.min_frame_points, resected, n_weak - resected)
    if cfg.polish_iters and (n_weak or cfg.polish_always):
        X2, tn2, fr2, uvn2 = _filter(Ec, Kc, t_, frame, uv, final_gate, cfg.min_angle_deg, cfg.max_points)
        if len(X2) >= 100:
            e_before = float(np.median(reproject(Ec, Kc, X2, tn2, fr2, uvn2)[0]))
            Ep, Kp, Xp = bundle_adjust(Ec, Kc, video, X2, tn2, fr2, uvn2, replace(cfg, iters=cfg.polish_iters), device)
            e_after = float(np.median(reproject(Ep, Kp, Xp, tn2, fr2, uvn2)[0]))
            if np.isfinite(e_after) and e_after <= e_before:
                Ec, Kc = Ep, Kp
            X, tn, fr, uvn = _filter(Ec, Kc, t_, frame, uv, final_gate, cfg.min_angle_deg, cfg.max_points)
            # the polish can only help: refit points once more with the kept poses
            X, _ = triangulate(Ec, Kc, tn, fr, uvn)
    Ec, X = _to_frame(Ec, X, E.astype(np.float64))
    err, _ = reproject(Ec, Kc, X, tn, fr, uvn)
    seen = np.bincount(fr, minlength=len(E))
    per_frame = np.array([np.median(err[fr == s]) if (fr == s).any() else np.nan for s in range(len(E))])
    pose_px = pose_uncertainty(err, fr, len(E), before["median_px"], cfg.min_resect)
    vids = np.unique(video)
    nvid = np.zeros((len(X), len(vids)), bool)
    for k, v in enumerate(vids):
        nvid[tn[video[fr] == v], k] = True
    both = nvid.all(1) if len(vids) > 1 else np.zeros(len(X), bool)

    c0, d0 = _centres_dirs(E.astype(np.float64))
    c1, d1 = _centres_dirs(Ec)
    R0, R1 = E[:, :, :3].astype(np.float64), Ec[:, :, :3]
    rot = np.degrees(np.arccos(np.clip((np.trace(R1 @ R0.transpose(0, 2, 1), axis1=1, axis2=2) - 1) / 2, -1, 1)))
    spread = float(np.linalg.norm(c0 - c0.mean(0), axis=1).max())
    fin = np.isfinite(per_frame)
    report = {
        "vggt": before, "rounds": rounds,
        "final": {"median_px": float(np.median(err)), "p90_px": float(np.quantile(err, 0.9)),
                  "points": int(len(X)), "observations": int(len(tn))},
        "per_frame_median_px": {"median": float(np.median(per_frame[fin])) if fin.any() else None,
                                "p90": float(np.quantile(per_frame[fin], 0.9)) if fin.any() else None,
                                "worst": float(per_frame[fin].max()) if fin.any() else None},
        "pose_px": {"median": float(np.median(pose_px)), "p90": float(np.quantile(pose_px, 0.9)),
                    "frames_unconstrained": int((pose_px >= before["median_px"] / 1.1774 - 1e-9).sum())},
        "frames_with_few_points": n_weak, "frames_resected": int(resected),
        "frames_interpolated": int(n_weak - resected), "frames_without_points": int((seen == 0).sum()),
        "rotation_change_deg_median": float(np.median(rot)), "rotation_change_deg_p90": float(np.quantile(rot, 0.9)),
        "centre_change_median_rel": float(np.median(np.linalg.norm(c1 - c0, axis=1)) / (spread + 1e-12)),
        "focal_change": [float(np.median(Kc[video == v, 0, 0] / Kf[video == v, 0, 0])) for v in np.unique(video)],
        # how well each recording fits on its own, and how strongly the recordings are tied
        # together: points seen in both are the only thing that lines the recordings up
        "per_recording_median_px": [float(np.median(err[video[fr] == v])) if (video[fr] == v).any() else None
                                    for v in np.unique(video)],
        "points_in_both": int(both.sum()),
        "trajectory_jumps": trajectory_jumps(Ec, video),
        "points_in_both_median_px": float(np.median(err[both[tn]])) if both.any() else None,
        "observations_kept_share": float(len(tn) / max(len(t_), 1)),
        "seconds": round(time.time() - t0),
    }
    log.info("poses: final %.2f px median, %.2f px p90 (VGGT %.2f px); per-frame median worst %.2f px; cameras "
             "turned %.2f deg median; %d weak frames (%d resected, %d interpolated)", report["final"]["median_px"],
             report["final"]["p90_px"], before["median_px"], report["per_frame_median_px"]["worst"] or 0,
             report["rotation_change_deg_median"], n_weak, resected, n_weak - resected)
    log.info("poses: per recording %s px median; %d points seen in both recordings (%.2f px); %.0f%% of feature "
             "observations kept", ", ".join(f"{x:.2f}" for x in report["per_recording_median_px"] if x is not None),
             report["points_in_both"], report["points_in_both_median_px"] or float("nan"),
             100 * report["observations_kept_share"])
    return Ec.astype(np.float32), Kc.astype(np.float32), {"X": X.astype(np.float32), "t": tn, "frame": fr,
                                                          "uv": uvn.astype(np.float32),
                                                          "pose_px": pose_px.astype(np.float32)}, report


def loose_frames(t: np.ndarray, frame: np.ndarray, n_frames: int, min_shared: int) -> np.ndarray:
    """Frames outside the main group: frames are linked when they share at least `min_shared`
    tracks, and every frame not in the largest linked group is loose. A loose group is tied to
    the rest by a few points only, so the adjustment cannot place it and it keeps VGGT's offset
    (the camera groups that sit slightly apart)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(t):
        return np.ones(n_frames, bool)
    A = coo_matrix((np.ones(len(t)), (frame, t)), shape=(n_frames, int(t.max()) + 1)).tocsr()
    A.data[:] = 1
    C = (A @ A.T).tocoo()
    ok = (C.data >= min_shared) & (C.row != C.col)
    G = coo_matrix((np.ones(ok.sum()), (C.row[ok], C.col[ok])), shape=(n_frames, n_frames))
    _, lab = connected_components(G, directed=False)
    return lab != np.bincount(lab).argmax()


def trajectory_jumps(E: np.ndarray, video: np.ndarray, factor: float = 4.0) -> list[int]:
    """Per recording, how many steps between consecutive cameras are over `factor` times the
    median step: a phone moves smoothly, so a jump means a group of poses sits apart."""
    c, _ = _centres_dirs(E)
    out = []
    for v in np.unique(video):
        idx = np.nonzero(video == v)[0]
        if len(idx) < 3:
            out.append(0)
            continue
        st = np.linalg.norm(np.diff(c[idx], axis=0), axis=1)
        out.append(int((st > factor * np.median(st)).sum()))
    return out


def _extra_pairs(E, video, sparse, P_done, cfg):
    """More partners for frames that ended up with few track observations or outside the main
    group: wider pose neighbourhood, more time neighbours and more frames of the other
    recording, skipping pairs already tried."""
    P2 = pairs(E, video, cfg.k_near * 3, cfg.k_time * 3, cfg.k_cross * 2)
    keep = (sparse[P2[:, 0]] | sparse[P2[:, 1]])
    done = set(map(tuple, P_done.tolist()))
    return np.array([p for p in P2[keep].tolist() if tuple(p) not in done], dtype=np.int64).reshape(-1, 2)


def refine(E: np.ndarray, K: np.ndarray, video: np.ndarray, files: list[str], model_wh: tuple[int, int],
           cfg: PoseConfig = PoseConfig(), device: str | None = None):
    """Refined camera-from-world E [S, 3, 4] and intrinsics K [S, 3, 3] at the FRAME size (the
    size of the images in `files`), triangulated points and a report. `K` is given at the model
    size `model_wh` (w, h) and scaled to the frame size here."""
    import torch

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    kps, descs, wh = features(files, cfg.n_features, cfg.contrast)
    Kf = K.astype(np.float64).copy()
    Kf[:, 0] *= wh[0] / model_wh[0]
    Kf[:, 1] *= wh[1] / model_wh[1]
    P = pairs(E, video, cfg.k_near, cfg.k_time, cfg.k_cross)
    M, n_pairs = match(kps, descs, P, cfg, device)
    n_kp = [len(k) for k in kps]
    t_, frame, kp = tracks(M, n_kp)
    # frames with few track observations, or in a group only loosely tied to the rest, get a
    # second, wider matching pass (lower inlier bar)
    few = np.bincount(frame, minlength=len(E)) < cfg.sparse_obs
    loose = loose_frames(t_, frame, len(E), cfg.group_link)
    sparse = few | loose
    loose_after = int(loose.sum())
    if sparse.any():
        P2 = _extra_pairs(E, video, sparse, P, cfg)
        M2, n2 = match(kps, descs, P2, replace(cfg, min_inliers=cfg.min_inliers_sparse), device)
        log.info("poses: %d sparse frames, %d frames outside the main group: %d extra pairs tried, %d matched",
                 int(few.sum()), int(loose.sum()), len(P2), n2)
        if len(M2):
            M, n_pairs, P = np.r_[M, M2], n_pairs + n2, np.r_[P, P2]
            t_, frame, kp = tracks(M, n_kp)
            loose_after = int(loose_frames(t_, frame, len(E), cfg.group_link).sum())
            log.info("poses: %d frames still outside the main group", loose_after)
    off = np.concatenate([[0], np.cumsum(n_kp)])
    uv = np.concatenate(kps)[off[frame] + kp].astype(np.float64)
    log.info("poses: %d features/frame, %d tracks, %d observations (%.0f s)", int(np.mean(n_kp)),
             int(t_.max()) + 1 if len(t_) else 0, len(t_), time.time() - t0)
    if len(t_) < 100:
        raise RuntimeError(f"too few feature tracks ({len(t_)}) to refine poses")
    Ec, Kc, pts, report = refine_tracks(E, Kf, video, t_, frame, uv, cfg, device, t0)
    report = {"frame_size": list(wh), "frame_pairs_tried": int(len(P)), "frame_pairs_matched": int(n_pairs),
              "sparse_frames_rematched": int(sparse.sum()), "frames_outside_main_group": int(loose.sum()),
              "frames_outside_main_group_after": loose_after,
              "trajectory_jumps_vggt": trajectory_jumps(E, video)} | report
    return Ec, Kc, pts, report


def refine_recon(rec, cache=None, key: str = "", cfg: PoseConfig = PoseConfig()):
    """Recon with refined poses (K stays at the model size, rescaled from the frame size), the
    triangulated points ({"X", "rgb", "seen" [N, recordings]}) and the report. Cached by `key`
    under `cache`."""
    import json

    from PIL import Image

    f = cache / f"poses5_{key}.npz" if cache is not None else None
    if f is not None and f.exists():
        z = np.load(f)
        log.info("reusing cached refined poses %s", f.name)
        return (_aligned(replace(rec, E=z["E"], K=z["K"], pose_px=z["pose_px"]), z["ks"]),
                {"X": z["X"], "rgb": z["rgb"], "seen": z["seen"]},
                json.loads(str(z["report"])) | {"cached": True})
    h0, w0 = rec.depth.shape[1:]
    E, Kf, pts, report = refine(rec.E, rec.K, rec.video, rec.files, (w0, h0), cfg)
    w, h = report["frame_size"]
    K = Kf.copy()
    K[:, 0] *= w0 / w
    K[:, 1] *= h0 / h
    # colour of each point: mean over its observations
    rgb = np.zeros((len(pts["X"]), 3))
    cnt = np.zeros(len(pts["X"]))
    for s in np.unique(pts["frame"]):
        sel = pts["frame"] == s
        im = np.asarray(Image.open(rec.files[s]).convert("RGB"))
        u = np.clip(pts["uv"][sel, 0].astype(int), 0, im.shape[1] - 1)
        v = np.clip(pts["uv"][sel, 1].astype(int), 0, im.shape[0] - 1)
        np.add.at(rgb, pts["t"][sel], im[v, u])
        np.add.at(cnt, pts["t"][sel], 1)
    rgb = (rgb / np.maximum(cnt, 1)[:, None]).astype(np.uint8)
    # which recordings saw each point (a point seen only in the after recording may be a moved object)
    seen = np.zeros((len(pts["X"]), int(rec.video.max()) + 1), bool)
    seen[pts["t"], rec.video[pts["frame"]]] = True
    # pose uncertainty in pixels of the model-size depth maps (the size rec.K describes)
    pose_px = (pts["pose_px"] * w0 / w).astype(np.float32)
    out = replace(rec, E=E, K=K, pose_px=pose_px)
    ks = np.array([depth_scale(out, s, pts["X"][seen[:, out.video[s]]]) for s in range(len(E))], np.float32)
    report["depth_scale_median"] = float(np.median(ks))
    report["depth_scale_p10_p90"] = [float(np.quantile(ks, 0.1)), float(np.quantile(ks, 0.9))]
    if f is not None:
        tmp = f.with_name(f.stem + "_tmp.npz")
        np.savez_compressed(tmp, E=E, K=K, X=pts["X"], rgb=rgb, seen=seen, ks=ks, pose_px=pose_px,
                            report=json.dumps(report))
        tmp.replace(f)
    return _aligned(out, ks), {"X": pts["X"], "rgb": rgb, "seen": seen}, report


def depth_scale(rec, s: int, X: np.ndarray, min_points: int = 20) -> float:
    """Factor that lines frame s's VGGT depth up with bundle-adjusted 3D points X (median ratio of
    the points' camera depth to VGGT's depth at their pixels); 1 when too few points are seen."""
    from ..change.geometry import project

    if X is None or not len(X):
        return 1.0
    u, v, z = project(X, rec.K[s], rec.E[s])
    u, v = np.round(u), np.round(v)
    h, w = rec.depth.shape[1:]
    ok = (z > 0) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    if ok.sum() < min_points:
        return 1.0
    d = rec.depth[s][v[ok].astype(int), u[ok].astype(int)]
    good = d > 0
    if good.sum() < min_points:
        return 1.0
    return float(np.clip(np.median(z[ok][good] / d[good]), 0.8, 1.25))


def _aligned(rec, ks: np.ndarray):
    """Depth maps (and their per-frame error scale) multiplied by each frame's factor."""
    return replace(rec, depth=rec.depth * ks[:, None, None],
                   scale=None if rec.scale is None else rec.scale * ks)
