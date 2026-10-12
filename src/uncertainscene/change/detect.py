"""Visibility-aware change detection between two recordings in one reconstruction.

Idea: a point seen in recording A is tested against every camera of recording B.
- If B's depth at the point's projection is about the point's own depth, B sees the same
  surface: evidence the point is **unchanged** (and its appearance can be compared).
- If B's depth is clearly *larger*, B looks straight through where the point was: free-space
  evidence the point is **gone** (removed or moved away).
- If B's depth is clearly *smaller*, something is in front: **occluded**, no evidence.
- If no B camera saw it at all: **unknown**. Unobserved is never reported as a change.
Run A->B for removed and B->A for added. Appearance change (same geometry, different look,
e.g. a cushion turned over) is the feature distance between A and B where geometry agrees.

Every threshold is calibrated **without labels** from within-recording comparisons (A frames
against other A frames, where nothing can have changed): the spread there is the noise floor
of the reconstruction and features, so a cross-recording difference counts only when it is
outside that spread. This also gives the uncertainty attached to every detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..recon.frames import Recon
from .geometry import cam_centers, project, unproject


def _centers(rec: Recon) -> np.ndarray:
    return cam_centers(rec.E)

UNCHANGED, GONE, APPEARANCE, UNKNOWN = 0, 1, 2, 3
CLASS_NAMES = {UNCHANGED: "unchanged", GONE: "gone", APPEARANCE: "appearance", UNKNOWN: "unknown"}


@dataclass
class DetectConfig:
    stride: int = 2                 # pixel stride when sampling points (2 = dense)
    conf_quantile: float = 0.35     # drop the least confident 35% of pixels in each frame
    min_obs: int = 2                # views needed before a point is called observed
    null_quantile: float = 0.99     # threshold = this quantile of the within-recording null
    min_gone_frac: float = 0.5
    use_visibility: bool = True     # False = naive nearest-neighbour baseline (ablation)
    use_appearance: bool = True
    max_null_points: int = 60000
    max_points: int = 400000
    seed: int = 0


@dataclass
class PointSet:
    """Points of one recording with their test results."""
    X: np.ndarray                   # [N, 3]
    rgb: np.ndarray                 # [N, 3] uint8
    frame: np.ndarray               # [N] source frame index
    pix: np.ndarray                 # [N, 2] (v, u) in the source frame
    n_same: np.ndarray = None
    n_gone: np.ndarray = None
    app: np.ndarray = None          # mean appearance distance where geometry agrees (nan if none)
    cls: np.ndarray = None
    feat: np.ndarray | None = None  # [N, C] appearance feature, if features were given
    conf: np.ndarray = None         # model confidence of the source pixel
    disp: np.ndarray = None         # mean signed surface shift seen by the other recording
                                    # (d_other - z, scene units; + = surface receded), nan if unseen
    unc: np.ndarray = None          # 1-sigma depth disagreement among own-recording views (scene units)


@dataclass
class ChangeResult:
    A: PointSet
    B: PointSet
    thresholds: dict
    null: dict = field(default_factory=dict)   # samples of null distributions, for plots


def _points(rec: Recon, vid: int, cfg: DetectConfig, rng) -> PointSet:
    Xs, rgbs, frames, pixs, confs = [], [], [], [], []
    for s in np.nonzero(rec.video == vid)[0]:
        c = rec.conf[s]
        mask = c >= np.quantile(c, cfg.conf_quantile)
        grid = np.zeros_like(mask)
        grid[::cfg.stride, ::cfg.stride] = True
        X, v, u = unproject(rec.depth[s], rec.K[s], rec.E[s], mask & grid & (rec.depth[s] > 0))
        Xs.append(X)
        rgbs.append(rec.images[s][v, u])
        frames.append(np.full(len(X), s))
        pixs.append(np.stack([v, u], 1))
        confs.append(c[v, u])
    ps = PointSet(np.concatenate(Xs).astype(np.float32), np.concatenate(rgbs),
                  np.concatenate(frames), np.concatenate(pixs), conf=np.concatenate(confs))
    if len(ps.X) > cfg.max_points:
        keep = np.sort(rng.choice(len(ps.X), cfg.max_points, replace=False))
        ps = PointSet(ps.X[keep], ps.rgb[keep], ps.frame[keep], ps.pix[keep], conf=ps.conf[keep])
    return ps


def _sample_feat(feats: np.ndarray, s: int, v: np.ndarray, u: np.ndarray, H: int, W: int) -> np.ndarray:
    fh, fw = feats.shape[1:3]
    fv = np.clip((v * fh / H).astype(int), 0, fh - 1)
    fu = np.clip((u * fw / W).astype(int), 0, fw - 1)
    return feats[s, fv, fu]


def _test(rec: Recon, X: np.ndarray, src_frame: np.ndarray, src_feat: np.ndarray | None,
          targets: np.ndarray, feats: np.ndarray | None, cfg: DetectConfig, tau: float | None,
          conf_thr: np.ndarray, collect_r: bool = False, return_extra: bool = False):
    """Test points against target frames. Returns counts, mean appearance distance and,
    if collect_r, samples of the relative depth residual r = (d_target - z) / z.

    Counts are weighted by camera-pose confidence (Recon.pose_weight) of both the frame the
    point came from and the frame it is tested in, so a shaky pose casts only part of a vote."""
    H, W = rec.depth.shape[1:]
    n = len(X)
    pw = rec.pose_weight()
    w_src = pw[src_frame]
    n_same = np.zeros(n, np.float64)
    n_gone = np.zeros(n, np.float64)
    app_sum = np.zeros(n, np.float32)
    app_cnt = np.zeros(n, np.int32)
    disp_sum = np.zeros(n, np.float64)
    disp_cnt = np.zeros(n, np.int32)
    r1 = np.zeros(n, np.float64)
    r2 = np.zeros(n, np.float64)
    rc = np.zeros(n, np.int32)
    rs = []
    for t in targets:
        u, v, z = project(X, rec.K[t], rec.E[t])
        ui, vi = np.round(u).astype(int), np.round(v).astype(int)
        ok = (z > 1e-6) & (ui >= 0) & (ui < W) & (vi >= 0) & (vi < H) & (src_frame != t)
        idx = np.nonzero(ok)[0]
        d = rec.depth[t, vi[idx], ui[idx]]
        good = rec.conf[t, vi[idx], ui[idx]] >= conf_thr[t]
        idx, d = idx[good], d[good]
        r = (d - z[idx]) / z[idx]
        if collect_r:
            rs.append(r)
        small = np.abs(r) < 0.5
        np.add.at(r1, idx[small], r[small])
        np.add.at(r2, idx[small], r[small] ** 2)
        np.add.at(rc, idx[small], 1)
        if tau is None:
            continue
        vis = r > -tau                      # not occluded: the other view sees this spot
        disp_sum[idx[vis]] += (d - z[idx])[vis]
        disp_cnt[idx[vis]] += 1
        same = np.abs(r) <= tau
        gone = r > tau
        n_same[idx[same]] += w_src[idx[same]] * pw[t]
        n_gone[idx[gone]] += w_src[idx[gone]] * pw[t]
        if feats is not None and src_feat is not None:
            j = idx[same]
            ft = _sample_feat(feats, t, vi[j], ui[j], H, W)
            app_sum[j] += 1 - np.sum(ft * src_feat[j], axis=1)
            app_cnt[j] += 1
    app = np.where(app_cnt > 0, app_sum / np.maximum(app_cnt, 1), np.nan)
    r_all = np.concatenate(rs) if rs else np.zeros(0)
    extra = {"disp": np.where(disp_cnt > 0, disp_sum / np.maximum(disp_cnt, 1), np.nan),
             "r_std": np.where(rc >= 2, np.sqrt(np.maximum(r2 / np.maximum(rc, 1) - (r1 / np.maximum(rc, 1)) ** 2, 0)), np.nan)}
    if return_extra:
        return n_same, n_gone, app, r_all, extra
    return n_same, n_gone, app, r_all


def _naive(PA: PointSet, PB: PointSet, thr: float) -> np.ndarray:
    """Ablation baseline: a point is 'gone' if no point of the other cloud is within thr."""
    from scipy.spatial import cKDTree

    d, _ = cKDTree(PB.X).query(PA.X, k=1)
    return d > thr


def detect(rec: Recon, feats: np.ndarray | None = None, cfg: DetectConfig | None = None) -> ChangeResult:
    """feats: optional [S, h, w, C] L2-normalised per-frame feature maps (e.g. DINOv2 patches)."""
    cfg = cfg or DetectConfig()
    rng = np.random.default_rng(cfg.seed)
    if not cfg.use_appearance:
        feats = None
    conf_thr = np.array([np.quantile(c, cfg.conf_quantile) for c in rec.conf])
    H, W = rec.depth.shape[1:]
    sets = [_points(rec, 0, cfg, rng), _points(rec, 1, cfg, rng)]
    if feats is not None:
        for ps in sets:
            ps.feat = _sample_feat(feats, ps.frame, ps.pix[:, 0], ps.pix[:, 1], H, W)

    # ---- 1. noise floor from within-recording comparisons (nothing changed there) -------
    null_r, null_gone, null_app = [], [], []
    for vid, ps in enumerate(sets):
        same_vid = np.nonzero(rec.video == vid)[0]
        sub = rng.choice(len(ps.X), min(cfg.max_null_points, len(ps.X)), replace=False)
        _, _, _, r = _test(rec, ps.X[sub], ps.frame[sub], None, same_vid, None, cfg, None,
                           conf_thr, collect_r=True)
        null_r.append(r)
    r0 = np.abs(np.concatenate(null_r))
    tau = float(np.clip(3 * np.median(r0[r0 < 0.5]), 0.01, 0.3))
    for vid, ps in enumerate(sets):
        same_vid = np.nonzero(rec.video == vid)[0]
        sub = rng.choice(len(ps.X), min(cfg.max_null_points, len(ps.X)), replace=False)
        ns, ng, app, _ = _test(rec, ps.X[sub], ps.frame[sub], None if ps.feat is None else ps.feat[sub],
                               same_vid, feats, cfg, tau, conf_thr)
        obs = ns + ng >= cfg.min_obs
        null_gone.append(ng[obs] / (ns[obs] + ng[obs]))
        null_app.append(app[obs & ~np.isnan(app)])
    g0, a0 = np.concatenate(null_gone), np.concatenate(null_app)
    gone_thr = float(max(cfg.min_gone_frac, np.quantile(g0, cfg.null_quantile))) if len(g0) else cfg.min_gone_frac
    app_thr = float(np.quantile(a0, cfg.null_quantile)) if len(a0) else np.inf

    # ---- 2. cross-recording tests -------------------------------------------------------
    scale = float(np.median(rec.depth[rec.depth > 0]))
    cross_r = []
    for vid, ps in enumerate(sets):
        other = np.nonzero(rec.video != vid)[0]
        ns, ng, app, r, ex = _test(rec, ps.X, ps.frame, ps.feat, other, feats, cfg, tau, conf_thr,
                                   collect_r=True, return_extra=True)
        ps.disp = ex["disp"].astype(np.float32)
        own = np.nonzero(rec.video == vid)[0]
        *_, ex_own = _test(rec, ps.X, ps.frame, None, own, None, cfg, None, conf_thr, return_extra=True)
        depth_src = np.linalg.norm(ps.X - _centers(rec)[ps.frame], axis=1)
        ps.unc = (ex_own["r_std"] * depth_src).astype(np.float32)
        cross_r.append(r[rng.choice(len(r), min(len(r), 200000), replace=False)] if len(r) else r)
        ps.n_same, ps.n_gone, ps.app = ns, ng, app
        nobs = ns + ng
        cls = np.full(len(ps.X), UNKNOWN, np.int8)
        observed = nobs >= cfg.min_obs
        cls[observed] = UNCHANGED
        frac = np.where(nobs > 0, ng / np.maximum(nobs, 1), 0)
        cls[observed & (frac > gone_thr) & (ng >= cfg.min_obs)] = GONE
        if feats is not None:
            cls[(cls == UNCHANGED) & (np.nan_to_num(app, nan=0) > app_thr)] = APPEARANCE
        if not cfg.use_visibility:     # ablation: naive cloud difference, no unknown class
            gone = _naive(ps, sets[1 - vid], 3 * tau * scale)
            cls = np.where(gone, GONE, UNCHANGED).astype(np.int8)
        ps.cls = cls

    thresholds = {"tau_rel_depth": tau, "gone_frac": gone_thr, "appearance": app_thr,
                  "scene_scale_median_depth": scale}
    sub0 = rng.choice(len(r0), min(len(r0), 200000), replace=False) if len(r0) else []
    null = {"r_within": np.concatenate(null_r)[sub0] if len(r0) else r0, "r_cross": np.concatenate(cross_r),
            "gone_within": g0, "app_within": a0,
            "gone_cross": np.concatenate([s.n_gone / np.maximum(s.n_same + s.n_gone, 1) for s in sets]),
            "app_cross": np.concatenate([s.app[~np.isnan(s.app)] for s in sets])}
    return ChangeResult(sets[0], sets[1], thresholds, null)
