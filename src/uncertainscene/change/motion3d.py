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
3. No-change null, label-free: the same measurement inside one recording, between two halves of
   its frames (alternating blocks in time). The 97.5th percentile of that is the smallest
   displacement that counts as real.
4. Per object (SAM objects of the before recording, by their voxels): a rigid motion fitted by
   RANSAC to the object's links gives the distance moved (of its centre) and the rotation.
   What the rigid motion cannot explain, smoothed over neighbouring links, is the deformation
   map. Deformation metrics: 90th percentile and peak of the smoothed residual, the share of
   the object above the null threshold, and where the peak sits along the object's main axes
   (e.g. one corner of a cushion). Nothing about any particular object is built in.
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
    min_obs: int = 3                # observations for a track to count
    min_angle_deg: float = 2.0
    max_px: float = 3.0             # reprojection gate
    min_votes: int = 2              # feature matches needed to link a before track to an after track
    block: int = 12                 # frames per time block for the no-change null
    null_q: float = 0.975
    min_links: int = 8              # links an object needs for a rigid fit
    smooth_k: int = 6               # neighbours for smoothing the non-rigid residual


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


def _tri(E, K, t, fr, uv, T):
    from ..recon import poses as P

    X, ang = np.zeros((T, 3)), np.zeros(T)
    if len(t):
        x, a = P.triangulate(E, K, t, fr, uv)
        X[: len(x)], ang[: len(a)] = x, a
    return X, ang


def _triangulate_tracks(E, K, t, fr, uv, cfg, use=None):
    """Points of tracks with enough good observations (optionally only observations in `use`).
    Returns X [T, 3] (NaN where invalid) and the boolean mask of observations kept."""
    from ..recon import poses as P

    T = int(t.max()) + 1 if len(t) else 0
    keep = np.ones(len(t), bool) if use is None else use.copy()
    for _ in range(3):
        X, ang = _tri(E, K, t[keep], fr[keep], uv[keep], T)
        err, z = P.reproject(E, K, X, t, fr, uv)
        good = (np.bincount(t[keep], minlength=T) >= cfg.min_obs) & (ang >= cfg.min_angle_deg)
        new = keep & (err < cfg.max_px) & (z > 0) & good[t]
        if (new == keep).all():
            break
        keep = new
    X, ang = _tri(E, K, t[keep], fr[keep], uv[keep], T)
    good = (np.bincount(t[keep], minlength=T) >= cfg.min_obs) & (ang >= cfg.min_angle_deg)
    X[~good] = np.nan
    return X, keep & good[t]


def _null(E, K, t, fr, uv, frames, cfg) -> np.ndarray:
    """Displacement of the same track triangulated from two halves of one recording's frames
    (alternating time blocks): what 'moved' looks like when nothing moved."""
    pos = {int(s): i for i, s in enumerate(frames)}
    half = np.array([(pos[int(f)] // cfg.block) % 2 for f in fr])
    X0, _ = _triangulate_tracks(E, K, t, fr, uv, cfg, use=half == 0)
    X1, _ = _triangulate_tracks(E, K, t, fr, uv, cfg, use=half == 1)
    ok = np.isfinite(X0).all(1) & np.isfinite(X1).all(1)
    return np.linalg.norm(X1[ok] - X0[ok], axis=1)


def kabsch(A: np.ndarray, B: np.ndarray):
    """R, t with B ~ R A + t (least squares)."""
    ma, mb = A.mean(0), B.mean(0)
    U, _, Vt = np.linalg.svd((B - mb).T @ (A - ma))
    S = np.eye(3)
    S[2, 2] = np.sign(np.linalg.det(U @ Vt)) or 1.0
    R = U @ S @ Vt
    return R, mb - R @ ma


def rigid_ransac(A: np.ndarray, B: np.ndarray, thr: float, iters: int = 300, seed: int = 0):
    """Robust rigid motion A -> B: RANSAC on 3-point samples, refit on the inliers."""
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


def object_motion(XA: np.ndarray, XB: np.ndarray, obj_X: np.ndarray, thr: float, cfg: MotionConfig, seed: int = 0) -> dict:
    """Rigid motion + deformation of one object from its links (XA -> XB), all in scene units.
    `obj_X`: the object's own points (for its centre and main axes)."""
    from scipy.spatial import cKDTree

    R, t, inl = rigid_ransac(XA, XB, thr * 2, seed=seed)
    c = np.median(obj_X, 0)
    moved = R @ c + t - c
    rng = np.random.default_rng(seed)
    boots = []
    idx = np.nonzero(inl)[0]
    for _ in range(100):                         # how sure is the distance moved
        k = rng.choice(idx, len(idx), replace=True)
        if len(np.unique(k)) < 3:
            continue
        Rb, tb = kabsch(XA[k], XB[k])
        boots.append(np.linalg.norm(Rb @ c + tb - c))
    resid = np.linalg.norm(XA @ R.T + t - XB, axis=1)
    k = min(cfg.smooth_k, len(XA))
    _, nn = cKDTree(XA).query(XA, k=k)
    smooth = np.median(resid[nn.reshape(len(XA), -1)], 1)
    # where the deformation peaks, along the object's main axes (-1..1 = one end .. other end)
    Xc = obj_X - c
    _, _, Vt = np.linalg.svd(Xc - Xc.mean(0), full_matrices=False)
    half = np.maximum(np.quantile(np.abs(Xc @ Vt.T), 0.95, axis=0), 1e-9)
    p = int(np.argmax(smooth))
    ax = np.clip((XA[p] - c) @ Vt.T / half, -1.5, 1.5)
    return {"R": R, "t": t, "inliers": inl, "moved": float(np.linalg.norm(moved)), "moved_vec": moved,
            "moved_ci": [float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95))] if boots else None,
            "rotation_deg": float(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))),
            "resid": resid, "smooth": smooth, "peak": XA[p], "peak_axes": ax.tolist()}


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

    tracks, Xs, nulls = [], [], []
    for v in (0, 1):
        t, fr, kp, uv = _within_tracks(rec, use[v], kps, descs, cfg, device)
        X, _ = _triangulate_tracks(E, K, t, fr, uv, cfg)
        nulls.append(_null(E, K, t, fr, uv, use[v], cfg))
        tid = {int(s): np.full(len(kps[int(s)]), -1) for s in use[v]}
        for tt, f, k in zip(t, fr, kp):
            if np.isfinite(X[tt]).all():
                tid[int(f)][k] = tt
        tracks.append(tid)
        Xs.append(X)
        log.info("motion: recording %d: %d 3D feature points (%.0f s)", v, int(np.isfinite(X).all(1).sum()),
                 time.time() - t0)
    null = np.concatenate(nulls) * cm
    thr_cm = float(np.quantile(null, cfg.null_q)) if len(null) else float("nan")

    # links: before track -> after track by descriptor votes between nearby before/after frames
    c, d = P._centres_dirs(E)
    spread = float(np.linalg.norm(c - c.mean(0), axis=1).max()) + 1e-9
    dist = np.linalg.norm(c[use[0]][:, None] - c[use[1]][None], axis=2) / spread + (1 - d[use[0]] @ d[use[1]].T)
    votes: dict = {}
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
            ok = (top.values[:, 0] < cfg.ratio * top.values[:, 1]) & (dd.argmin(0)[nn] == torch.arange(len(nn), device=device))
            ia = torch.nonzero(ok)[:, 0].cpu().numpy()
            ib = nn[ok].cpu().numpy()
            ta, tb = tracks[0][int(a)][ia], tracks[1][b][ib]
            for x, y in zip(ta[(ta >= 0) & (tb >= 0)], tb[(ta >= 0) & (tb >= 0)]):
                votes[(int(x), int(y))] = votes.get((int(x), int(y)), 0) + 1
    best_a: dict = {}
    best_b: dict = {}
    for (x, y), n in votes.items():
        if n > best_a.get(x, (None, 0))[1]:
            best_a[x] = (y, n)
        if n > best_b.get(y, (None, 0))[1]:
            best_b[y] = (x, n)
    links = np.array([(x, y) for x, (y, n) in best_a.items() if n >= cfg.min_votes and best_b[y][0] == x],
                     np.int64).reshape(-1, 2)
    XA, XB = Xs[0][links[:, 0]], Xs[1][links[:, 1]]
    disp = np.linalg.norm(XB - XA, axis=1) * cm
    log.info("motion: %d before/after links; no-change displacement 97.5%% = %.2f cm; %.0f s", len(links), thr_cm,
             time.time() - t0)

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
    rows = []
    for j in np.unique(obj_of[obj_of >= 0]):
        sel = np.nonzero(obj_of == j)[0]
        if len(sel) < cfg.min_links:
            continue
        o = objs[j]
        r = object_motion(XA[sel], XB[sel], o.X, thr_cm / cm, cfg, seed=int(j))
        resid[sel] = r["smooth"] * cm
        sm = r["smooth"] * cm
        rows.append({"recording": o.recording, "id": o.id, "label": o.label, "status": o.status, "links": int(len(sel)),
                     "rigid_inlier_share": round(float(r["inliers"].mean()), 3),
                     "moved_cm": round(r["moved"] * cm, 2),
                     "moved_ci_cm": None if r["moved_ci"] is None else [round(x * cm, 2) for x in r["moved_ci"]],
                     "moved_beyond_noise": bool(r["moved_ci"] is not None and r["moved_ci"][0] * cm > thr_cm),
                     "rotation_deg": round(r["rotation_deg"], 1),
                     "deform_p90_cm": round(float(np.quantile(sm, 0.9)), 2), "deform_peak_cm": round(float(sm.max()), 2),
                     "deformed_share": round(float((sm > thr_cm).mean()), 3),
                     "deformed": bool((sm > thr_cm).sum() >= max(3, 0.05 * len(sm))),
                     "deform_peak_where": where(r["peak_axes"]), "deform_peak_axes": [round(x, 2) for x in r["peak_axes"]],
                     "deform_peak_xyz": np.round(r["peak"], 4).tolist()})
    rows.sort(key=lambda r: -r["deform_p90_cm"])
    return {"links_XA": XA, "links_XB": XB, "disp_cm": disp, "resid_cm": resid, "obj_of": obj_of, "objects": rows,
            "null_cm": null, "threshold_cm": thr_cm, "seconds": round(time.time() - t0),
            "summary": {"links": int(len(links)), "points_before": int(np.isfinite(Xs[0]).all(1).sum()),
                        "points_after": int(np.isfinite(Xs[1]).all(1).sum()), "threshold_cm": round(thr_cm, 2),
                        "null_median_cm": round(float(np.median(null)), 3) if len(null) else None,
                        "links_moved_share": round(float((disp > thr_cm).mean()), 3) if len(disp) else None,
                        "objects_measured": len(rows), "objects_moved": sum(r["moved_beyond_noise"] for r in rows),
                        "objects_deformed": sum(r["deformed"] for r in rows)}}


def scene_layers(m: dict, pts, rec_key: str = "A") -> list:
    """Viewer layers: distance moved per feature point, deformation per point, motion arrows."""
    from ..viz import scene3d

    if not len(m["links_XA"]):
        return []
    thr = m["threshold_cm"]
    t = pts.add(m["links_XA"])
    hi = max(float(np.quantile(m["disp_cm"], 0.98)), 2 * thr, 1.0)
    rgb, stops = scene3d._scale_rgb(m["disp_cm"], "viridis", 0, hi)
    L = [{"name": "heat map: distance moved (matched feature points, cm)", "kind": "heat", "rec": rec_key,
          "visible": False, "size": 4, "pts": t, "rgb": scene3d._b64(rgb),
          "legend": {"lo": 0, "hi": round(hi, 2), "stops": stops}}]
    ok = np.isfinite(m["resid_cm"])
    if ok.any():
        hi = max(float(np.quantile(m["resid_cm"][ok], 0.98)), 2 * thr, 1.0)
        rgb, stops = scene3d._scale_rgb(m["resid_cm"][ok], "magma", 0, hi)
        L.append({"name": "heat map: deformation (beyond each object's rigid motion, cm)", "kind": "heat",
                  "rec": rec_key, "visible": False, "size": 4, "pts": pts.add(m["links_XA"][ok]),
                  "rgb": scene3d._b64(rgb), "legend": {"lo": 0, "hi": round(hi, 2), "stops": stops}})
    mv = np.nonzero(m["disp_cm"] > thr)[0]
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

    XA, disp, res, thr = m["links_XA"], m["disp_cm"], m["resid_cm"], m["threshold_cm"]
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
    bins = np.linspace(0, max(np.quantile(disp, 0.99), 3 * thr), 60)
    ax.hist(np.clip(m["null_cm"], 0, bins[-1]), bins, density=True, alpha=0.6, label="no change (two halves of one recording)")
    ax.hist(np.clip(disp, 0, bins[-1]), bins, density=True, alpha=0.6, label="before vs after")
    ax.axvline(thr, color="k", ls="--", label=f"97.5% no change = {thr:.1f} cm")
    ax.set_xlabel("displacement, cm")
    ax.legend(fontsize=8)
    ax.set_title("how far feature points moved")
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
        ax.set_title(f"{o.label or 'object'} {o.recording}{o.id}: deformation p90 {r['deform_p90_cm']:.1f} cm, "
                     f"peak {r['deform_peak_cm']:.1f} cm {r['deform_peak_where']}\nmoved {r['moved_cm']:.1f} cm, "
                     f"turned {r['rotation_deg']:.0f} deg", fontsize=9)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=80)
    plt.close(fig)
