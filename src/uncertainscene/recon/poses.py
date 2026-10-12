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
   focal length per recording (same phone, same zoom, so one number), and every 3D point.
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
    n_features: int = 4000
    k_near: int = 12                # pose-nearest partner frames per frame
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


def _rootsift(desc: np.ndarray) -> np.ndarray:
    d = desc / (np.abs(desc).sum(1, keepdims=True) + 1e-7)
    return np.sqrt(d).astype(np.float32)


def features(files: list[str], n: int):
    """Keypoints [N, 2] (continuous pixel coords: pixel centre of index i at i + 0.5) and RootSIFT
    descriptors per frame, plus the frame size (w, h)."""
    import cv2

    sift = cv2.SIFT_create(nfeatures=n)
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


def pairs(E: np.ndarray, video: np.ndarray, k_near: int, k_time: int) -> np.ndarray:
    """Frame pairs (i < j) to match: pose neighbours (position + viewing direction) and time
    neighbours within each recording."""
    c, d = _centres_dirs(E)
    spread = float(np.linalg.norm(c - c.mean(0), axis=1).max()) + 1e-9
    dist = np.linalg.norm(c[:, None] - c[None], axis=2) / spread + (1 - d @ d.T)
    np.fill_diagonal(dist, np.inf)
    P = set()
    for i in range(len(E)):
        for j in np.argsort(dist[i])[:k_near]:
            P.add((min(i, j), max(i, j)))
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
    # drop tracks that see the same frame twice (ambiguous) and tracks of one observation
    key = t.astype(np.int64) * (len(n_kp) + 1) + frame
    _, first, cnt = np.unique(key, return_index=True, return_counts=True)
    bad_t = np.unique(t[first[cnt > 1]])
    keep = ~np.isin(t, bad_t)
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


def triangulate(E, K, t, frame, uv):
    """Least-squares intersection of each track's rays. Returns points [T, 3] and the largest
    angle between any ray and the track's mean ray (degrees), as a parallax measure."""
    T = int(t.max()) + 1 if len(t) else 0
    c, d = _rays(E, K, frame, uv)
    A = np.eye(3)[None] - d[:, :, None] * d[:, None, :]
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
    """Jointly refine per-frame poses, one focal length per recording and the 3D points (or,
    with `fix_points`, only the poses and focals: resection, well conditioned).
    Returns new E, K, X."""
    import torch

    dt = torch.float64
    E0 = torch.as_tensor(E, dtype=dt, device=device)
    c0, _ = _centres_dirs(E)
    spread = float(np.linalg.norm(c0 - c0.mean(0), axis=1).max()) + 1e-9
    nv = int(video.max()) + 1
    f0 = torch.as_tensor([np.median(K[video == v, 0, 0]) for v in range(nv)], dtype=dt, device=device)
    pp = torch.as_tensor(K[:, :2, 2], dtype=dt, device=device)
    aspect = torch.as_tensor(K[:, 1, 1] / K[:, 0, 0], dtype=dt, device=device)
    vid = torch.as_tensor(video, device=device)
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
    Kn[:, 0, 0] = f[video]
    Kn[:, 1, 1] = f[video] * K[:, 1, 1] / K[:, 0, 0]
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


def refine(E: np.ndarray, K: np.ndarray, video: np.ndarray, files: list[str], model_wh: tuple[int, int],
           cfg: PoseConfig = PoseConfig(), device: str | None = None):
    """Refined camera-from-world E [S, 3, 4] and intrinsics K [S, 3, 3] at the FRAME size (the
    size of the images in `files`), triangulated points and a report. `K` is given at the model
    size `model_wh` (w, h) and scaled to the frame size here."""
    import torch

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    kps, descs, wh = features(files, cfg.n_features)
    Kf = K.astype(np.float64).copy()
    Kf[:, 0] *= wh[0] / model_wh[0]
    Kf[:, 1] *= wh[1] / model_wh[1]
    P = pairs(E, video, cfg.k_near, cfg.k_time)
    M, n_pairs = match(kps, descs, P, cfg, device)
    t_, frame, kp = tracks(M, [len(k) for k in kps])
    off = np.concatenate([[0], np.cumsum([len(k) for k in kps])])
    uv = np.concatenate(kps)[off[frame] + kp].astype(np.float64)
    log.info("poses: %d features/frame, %d tracks, %d observations (%.0f s)", int(np.mean([len(k) for k in kps])),
             int(t_.max()) + 1 if len(t_) else 0, len(t_), time.time() - t0)
    if len(t_) < 100:
        raise RuntimeError(f"too few feature tracks ({len(t_)}) to refine poses")

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
            continue
    # final gate: final_px, or wider when the poses are still that far off (never an empty set)
    final_gate = max(cfg.final_px, 2 * (rounds[-1]["median_px"] if rounds else before["median_px"]))
    X, tn, fr, uvn = _filter(Ec, Kc, t_, frame, uv, final_gate, cfg.min_angle_deg, cfg.max_points)
    if len(X) < 100:
        raise RuntimeError(f"only {len(X)} points left after bundle adjustment")
    Ec, X = _to_frame(Ec, X, E.astype(np.float64))
    err, _ = reproject(Ec, Kc, X, tn, fr, uvn)

    c0, d0 = _centres_dirs(E.astype(np.float64))
    c1, d1 = _centres_dirs(Ec)
    R0, R1 = E[:, :, :3].astype(np.float64), Ec[:, :, :3]
    rot = np.degrees(np.arccos(np.clip((np.trace(R1 @ R0.transpose(0, 2, 1), axis1=1, axis2=2) - 1) / 2, -1, 1)))
    spread = float(np.linalg.norm(c0 - c0.mean(0), axis=1).max())
    seen = np.bincount(fr, minlength=len(E))
    report = {
        "frame_size": list(wh), "frame_pairs_tried": int(len(P)), "frame_pairs_matched": int(n_pairs),
        "vggt": before, "rounds": rounds,
        "final": {"median_px": float(np.median(err)), "p90_px": float(np.quantile(err, 0.9)),
                  "points": int(len(X)), "observations": int(len(tn))},
        "frames_with_few_points": int((seen < 30).sum()),
        "rotation_change_deg_median": float(np.median(rot)), "rotation_change_deg_p90": float(np.quantile(rot, 0.9)),
        "centre_change_median_rel": float(np.median(np.linalg.norm(c1 - c0, axis=1)) / (spread + 1e-12)),
        "focal_change": [float(np.median(Kc[video == v, 0, 0] / Kf[video == v, 0, 0])) for v in np.unique(video)],
        "seconds": round(time.time() - t0),
    }
    log.info("poses: final %.2f px median (VGGT %.2f px); cameras turned %.2f deg median; %d frames with < 30 points",
             report["final"]["median_px"], before["median_px"], report["rotation_change_deg_median"],
             report["frames_with_few_points"])
    # frames the adjustment could not constrain keep their VGGT pose
    weak = seen < 30
    Ec[weak] = E[weak]
    Kout = Kc.copy()
    Kout[weak] = Kf[weak]
    return Ec.astype(np.float32), Kout.astype(np.float32), {"X": X.astype(np.float32), "t": tn, "frame": fr,
                                                           "uv": uvn.astype(np.float32)}, report


def refine_recon(rec, cache=None, key: str = "", cfg: PoseConfig = PoseConfig()):
    """Recon with refined poses (K stays at the model size, rescaled from the frame size), the
    triangulated points ({"X", "rgb", "seen" [N, recordings]}) and the report. Cached by `key`
    under `cache`."""
    import json

    from PIL import Image

    f = cache / f"poses_{key}.npz" if cache is not None else None
    if f is not None and f.exists():
        z = np.load(f)
        log.info("reusing cached refined poses %s", f.name)
        return (_aligned(replace(rec, E=z["E"], K=z["K"]), z["ks"]), {"X": z["X"], "rgb": z["rgb"], "seen": z["seen"]},
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
    out = replace(rec, E=E, K=K)
    ks = np.array([depth_scale(out, s, pts["X"][seen[:, out.video[s]]]) for s in range(len(E))], np.float32)
    report["depth_scale_median"] = float(np.median(ks))
    report["depth_scale_p10_p90"] = [float(np.quantile(ks, 0.1)), float(np.quantile(ks, 0.9))]
    if f is not None:
        tmp = f.with_name(f.stem + "_tmp.npz")
        np.savez_compressed(tmp, E=E, K=K, X=pts["X"], rgb=rgb, seen=seen, ks=ks, report=json.dumps(report))
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
