"""Gaussian splats of each recording, started from the VGGT reconstruction (gsplat).

Why: a splat can render the scene from any camera, so the before splat and the after splat can
be compared from exactly the same viewpoint, pixel by pixel. It is also a far nicer thing to
look at than a point cloud.

- One splat per recording, both in the reconstruction's shared world frame (VGGT poses).
- Started from VGGT's confident depth points with their colours; trained with L1 + SSIM;
  gsplat's default densify/prune strategy; a small per-camera pose correction is learnt
  because VGGT poses come from 518 px images.
- Every 8th frame of each recording is held out (not trained on). Rendering a held-out frame
  of the same recording measures how good the splat is (PSNR) and gives the noise level of a
  same-view comparison when nothing changed; rendering the other recording's splat from the
  same held-out camera gives the before/after comparison.

pip install gsplat (needs a CUDA GPU). Everything else here is numpy/torch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class SplatConfig:
    iters: int = 7000
    long_side: int = 768            # training image size (frames are 1024 on the long side)
    max_init: int = 250_000         # starting Gaussians from VGGT points
    hold_every: int = 8             # every n-th frame held out for testing
    ssim_weight: float = 0.2
    pose_lr: float = 1e-4           # 0 switches pose correction off
    sh_degree: int = 0              # colour only; view-dependent colour is not needed here
    export_max: int = 140_000       # Gaussians in a small .splat export (32 bytes each, < 5 MB)
    viewer_max: int = 190_000       # Gaussians per splat in the committed web viewer (17 bytes, base64 < 4.8 MB)
    seed: int = 0
    refine_stop: float = 0.7        # densify/prune until this share of the steps
    reset_every: int = 3000         # opacity reset (lets unneeded semi-transparent Gaussians fade)
    prune_opa: float = 0.01
    prune_at: tuple = (0.55, 0.8)   # floater pruning (share of the steps); training fills the holes after
    depth_weight: float = 0.05      # VGGT depth guide (relative L1), pulls Gaussians onto surfaces


def held_out(frames: np.ndarray, every: int) -> np.ndarray:
    """Boolean per frame of one recording: True = held out (every n-th, starting at the 4th)."""
    k = np.arange(len(frames))
    return (k % every) == (every // 2)


def init_points(rec, frames: np.ndarray, n: int, conf_quantile: float = 0.5, seed: int = 0,
                depth_scale: dict | None = None):
    """Confident VGGT depth points (world frame) with colours, from the given frames (each
    frame's depth multiplied by `depth_scale[frame]` when given)."""
    from ..change.geometry import unproject

    rng = np.random.default_rng(seed)
    per = max(1, n // max(len(frames), 1))
    X, C = [], []
    for s in frames:
        good = (rec.depth[s] > 0) & (rec.conf[s] >= np.quantile(rec.conf[s], conf_quantile))
        x, v, u = unproject(rec.depth[s] * (depth_scale or {}).get(int(s), 1.0), rec.K[s], rec.E[s], good)
        if len(x) > per:
            k = rng.choice(len(x), per, replace=False)
            x, v, u = x[k], v[k], u[k]
        X.append(x)
        C.append(rec.images[s][v, u])
    return np.concatenate(X).astype(np.float32), np.concatenate(C).astype(np.float32) / 255


def _images(files: list[str], long_side: int):
    from PIL import Image

    w0, h0 = Image.open(files[0]).size
    s = long_side / max(w0, h0)
    w, h = int(round(w0 * s)), int(round(h0 * s))
    return np.stack([np.asarray(Image.open(f).convert("RGB").resize((w, h), Image.Resampling.LANCZOS))
                     for f in files]), (w, h)


def _ssim(a, b):
    """SSIM of [B, 3, H, W] tensors in 0..1 (11 px Gaussian window)."""
    import torch
    import torch.nn.functional as F

    g = torch.exp(-((torch.arange(11, device=a.device) - 5.0) ** 2) / (2 * 1.5**2))
    g = (g / g.sum())[:, None] @ (g / g.sum())[None, :]
    w = g.expand(3, 1, 11, 11)

    def f(x):
        return F.conv2d(x, w, padding=5, groups=3)

    mu_a, mu_b = f(a), f(b)
    va, vb, cov = f(a * a) - mu_a**2, f(b * b) - mu_b**2, f(a * b) - mu_a * mu_b
    c1, c2 = 0.01**2, 0.03**2
    return (((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (va + vb + c2))).mean()


def _rodrigues(r):
    import torch

    th = r.norm(dim=-1, keepdim=True).clamp_min(1e-8)
    k = r / th
    K = torch.zeros(r.shape[0], 3, 3, device=r.device)
    K[:, 0, 1], K[:, 0, 2], K[:, 1, 0] = -k[:, 2], k[:, 1], k[:, 2]
    K[:, 1, 2], K[:, 2, 0], K[:, 2, 1] = -k[:, 0], -k[:, 1], k[:, 0]
    th = th[..., None]
    return torch.eye(3, device=r.device) + torch.sin(th) * K + (1 - torch.cos(th)) * K @ K


class Splat:
    """A trained splat: Gaussian parameters (torch, on the GPU) plus render()."""

    def __init__(self, params: dict, sh_degree: int = 0):
        self.p = params
        self.sh_degree = sh_degree

    def render(self, E: np.ndarray, K: np.ndarray, wh: tuple[int, int]):
        """RGB [H, W, 3] in 0..1, depth [H, W] (scene units) and alpha [H, W] from camera E (3x4)."""
        import torch
        from gsplat import rasterization

        dev = self.p["means"].device
        vm = torch.eye(4, device=dev)[None].repeat(1, 1, 1)
        vm[0, :3, :] = torch.as_tensor(E, dtype=torch.float32, device=dev)
        with torch.no_grad():
            out, alpha, _ = rasterization(self.p["means"], self.p["quats"], torch.exp(self.p["scales"]),
                                          torch.sigmoid(self.p["opacities"]), torch.sigmoid(self.p["colors"]),
                                          vm, torch.as_tensor(K, dtype=torch.float32, device=dev)[None],
                                          wh[0], wh[1], render_mode="RGB+ED")
        o = out[0].cpu().numpy()
        return np.clip(o[..., :3], 0, 1), o[..., 3], alpha[0, ..., 0].cpu().numpy()

    def export_splat(self, path: Path, max_n: int) -> int:
        """antimatter15 .splat (32 bytes per Gaussian: xyz f32, scale f32 x3, rgba u8, quat u8),
        keeping the `max_n` most visible Gaussians (opacity x size)."""
        import torch

        with torch.no_grad():
            op = torch.sigmoid(self.p["opacities"]).cpu().numpy()
            sc = torch.exp(self.p["scales"]).cpu().numpy()
            xyz = self.p["means"].cpu().numpy()
            rgb = torch.sigmoid(self.p["colors"]).cpu().numpy()
            q = torch.nn.functional.normalize(self.p["quats"], dim=-1).cpu().numpy()
        order = np.argsort(-(op * sc.prod(1) ** (1 / 3)))[:max_n]
        n = len(order)
        buf = np.zeros(n, dtype=[("p", "<f4", 3), ("s", "<f4", 3), ("c", "u1", 4), ("r", "u1", 4)])
        buf["p"], buf["s"] = xyz[order], sc[order]
        buf["c"] = np.clip(np.concatenate([rgb[order], op[order, None]], 1) * 255, 0, 255).astype(np.uint8)
        buf["r"] = np.clip(q[order] * 128 + 128, 0, 255).astype(np.uint8)
        path.write_bytes(buf.tobytes())
        return n


def depth_scale(rec, s: int, X: np.ndarray, min_points: int = 20) -> float:
    from .poses import depth_scale as f

    return f(rec, s, X, min_points)


@dataclass
class _Cams:
    E4: object                      # [N, 4, 4] torch
    K: object                       # [N, 3, 3] torch
    wh: tuple


def find_floaters(params, cams: _Cams, views, margin: float = 0.05, min_views: int = 3, front_share: float = 0.5):
    """Mask of Gaussians that float in front of the rendered surface.

    For each check view: render the expected depth and alpha, project every Gaussian centre; a
    Gaussian inside the image is 'seen'; it is 'in front' when it sits more than `margin`
    (relative) in front of the rendered surface at its pixel where that surface is solid
    (alpha > 0.5). A real surface Gaussian defines the rendered depth, so it is rarely in front;
    a floater is in front of the surface from most viewpoints. Flags Gaussians in front in more
    than `front_share` of the views that see them, or seen by fewer than `min_views` views."""
    import torch
    from gsplat import rasterization

    W, H = cams.wh
    n = len(params["means"])
    dev = params["means"].device
    seen = torch.zeros(n, device=dev)
    front = torch.zeros(n, device=dev)
    with torch.no_grad():
        for i in views:
            out, alpha, _ = rasterization(params["means"], params["quats"], torch.exp(params["scales"]),
                                          torch.sigmoid(params["opacities"]), torch.sigmoid(params["colors"]),
                                          cams.E4[i:i + 1], cams.K[i:i + 1], W, H, render_mode="ED")
            D, A = out[0, ..., 0], alpha[0, ..., 0]
            Xc = params["means"] @ cams.E4[i, :3, :3].T + cams.E4[i, :3, 3]
            z = Xc[:, 2]
            u = (cams.K[i, 0, 0] * Xc[:, 0] / z.clamp_min(1e-6) + cams.K[i, 0, 2]).floor().long()
            v = (cams.K[i, 1, 1] * Xc[:, 1] / z.clamp_min(1e-6) + cams.K[i, 1, 2]).floor().long()
            ins = (z > 1e-6) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
            uu, vv = u.clamp(0, W - 1), v.clamp(0, H - 1)
            d, a = D[vv, uu], A[vv, uu]
            seen += ins.float()
            front += (ins & (a > 0.5) & (z < d * (1 - margin))).float()
    return (seen < min_views) | (front > front_share * seen.clamp_min(1)), seen


def train(rec, video: int, cfg: SplatConfig = SplatConfig(), check=None, points: dict | None = None):
    """Train the splat of recording `video` on its non-held-out frames.

    `points`: bundle-adjusted 3D points of the recording (from recon.poses), {"X", "rgb"}. They
    are added to the starting Gaussians and line each frame's VGGT depth up for the depth guide."""
    import torch
    from gsplat import rasterization
    from gsplat.strategy import DefaultStrategy
    from gsplat.strategy.ops import remove, reset_opa

    torch.manual_seed(cfg.seed)
    dev = "cuda"
    frames = np.nonzero(rec.video == video)[0]
    hold = held_out(frames, cfg.hold_every)
    tr = frames[~hold]
    imgs, wh = _images([rec.files[s] for s in tr], cfg.long_side)
    h0, w0 = rec.depth.shape[1:]
    Ks = rec.K[tr].copy()
    Ks[:, 0] *= wh[0] / w0
    Ks[:, 1] *= wh[1] / h0

    PX = None if points is None else points["X"]
    ks = np.array([depth_scale(rec, s, PX) for s in tr])
    X, C = init_points(rec, tr, cfg.max_init, seed=cfg.seed, depth_scale=dict(zip(tr.tolist(), ks)))
    if PX is not None and len(PX):
        X = np.concatenate([X, PX.astype(np.float32)])
        C = np.concatenate([C, points["rgb"].astype(np.float32) / 255])
    Xt = torch.as_tensor(X, device=dev)
    from scipy.spatial import cKDTree

    d = cKDTree(X).query(X, k=4)[0][:, 1:].mean(1)
    d = np.clip(d, np.quantile(d, 0.02), np.quantile(d, 0.98))
    centres = -np.einsum("sij,sj->si", rec.E[tr, :, :3].transpose(0, 2, 1), rec.E[tr, :, 3])
    scene_scale = float(np.linalg.norm(centres - centres.mean(0), axis=1).max()) * 1.1 + 1e-6
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(Xt),
        "scales": torch.nn.Parameter(torch.log(torch.as_tensor(d, device=dev, dtype=torch.float32))[:, None].repeat(1, 3)),
        "quats": torch.nn.Parameter(torch.rand(len(X), 4, device=dev)),
        "opacities": torch.nn.Parameter(torch.logit(torch.full((len(X),), 0.1, device=dev))),
        "colors": torch.nn.Parameter(torch.logit(torch.as_tensor(C, device=dev).clamp(0.02, 0.98))),
    })
    lrs = {"means": 1.6e-4 * scene_scale, "scales": 5e-3, "quats": 1e-3, "opacities": 5e-2, "colors": 2.5e-3}
    opts = {k: torch.optim.Adam([params[k]], lr=v, eps=1e-15) for k, v in lrs.items()}
    sched = torch.optim.lr_scheduler.ExponentialLR(opts["means"], gamma=0.01 ** (1 / cfg.iters))
    pose = torch.nn.Parameter(torch.zeros(len(tr), 6, device=dev))
    popt = torch.optim.Adam([pose], lr=cfg.pose_lr) if cfg.pose_lr > 0 else None
    refine_stop = int(cfg.iters * cfg.refine_stop)
    strategy = DefaultStrategy(prune_opa=cfg.prune_opa, refine_stop_iter=refine_stop, reset_every=10**9, verbose=False)
    strategy.check_sanity(params, opts)
    state = strategy.initialize_state(scene_scale=scene_scale)
    E4 = np.tile(np.eye(4, dtype=np.float32), (len(tr), 1, 1))
    E4[:, :3, :] = rec.E[tr]
    E4 = torch.as_tensor(E4, device=dev)
    Kt = torch.as_tensor(Ks, dtype=torch.float32, device=dev)

    # depth guide: VGGT depth (lined up with the adjusted points) where VGGT is confident
    dmaps = None
    if cfg.depth_weight > 0:
        import torch.nn.functional as F

        dep = torch.as_tensor(rec.depth[tr] * ks[:, None, None], dtype=torch.float32)
        cf = torch.as_tensor(rec.conf[tr], dtype=torch.float32)
        ok = (dep > 0) & (cf >= cf.flatten(1).median(1).values[:, None, None])
        dep = F.interpolate(dep[:, None], size=(wh[1], wh[0]), mode="nearest")[:, 0]
        ok = F.interpolate(ok[:, None].float(), size=(wh[1], wh[0]), mode="nearest")[:, 0] > 0.5
        dmaps = (dep.half(), ok)

    rng = np.random.default_rng(cfg.seed)
    t0, losses, pruned = time.time(), [], {}
    prune_at = {int(cfg.iters * f) for f in cfg.prune_at}
    # frames with shaky poses (bundle-adjustment uncertainty, in training-image pixels) are
    # drawn less often: weight 1 / (1 + sigma^2), so a pixel of pose error halves a frame's share
    pw = np.ones(len(tr)) if rec.pose_px is None else 1.0 / (1.0 + (np.asarray(rec.pose_px)[tr] * wh[0] / w0) ** 2)
    pick = pw / pw.sum()
    for it in range(cfg.iters):
        i = int(rng.choice(len(tr), p=pick))
        gt = torch.as_tensor(imgs[i], device=dev).float() / 255
        vm = E4[i:i + 1]
        if popt is not None:
            D = torch.eye(4, device=dev)[None].clone()
            D[0, :3, :3] = _rodrigues(pose[i:i + 1, :3])[0]
            D[0, :3, 3] = pose[i, 3:]
            vm = D @ vm
        out, alpha, info = rasterization(params["means"], params["quats"], torch.exp(params["scales"]),
                                         torch.sigmoid(params["opacities"]), torch.sigmoid(params["colors"]),
                                         vm, Kt[i:i + 1], wh[0], wh[1], packed=False, render_mode="RGB+ED")
        strategy.step_pre_backward(params, opts, state, it, info)
        img = out[0, ..., :3]
        l1 = (img - gt).abs().mean()
        loss = (1 - cfg.ssim_weight) * l1 + cfg.ssim_weight * (1 - _ssim(img.permute(2, 0, 1)[None],
                                                                          gt.permute(2, 0, 1)[None]))
        if dmaps is not None:
            dg = dmaps[0][i].to(dev).float()
            m = dmaps[1][i].to(dev) & (alpha[0, ..., 0] > 0.5)
            if m.any():
                w = cfg.depth_weight * (1.0 if it < refine_stop else 0.3)
                loss = loss + w * ((out[0, ..., 3] - dg).abs() / dg.clamp_min(1e-3))[m].mean()
        loss.backward()
        for o in opts.values():
            o.step()
            o.zero_grad(set_to_none=True)
        if popt is not None and it > 500:
            popt.step()
        if popt is not None:
            popt.zero_grad(set_to_none=True)
        sched.step()
        strategy.step_post_backward(params, opts, state, it, info, packed=False)
        # gsplat 1.5.3 never resets opacities (its reset condition is always false), so do it
        # here: semi-transparent Gaussians that the images do not need then fade and get pruned
        if 0 < it < refine_stop - cfg.reset_every // 2 and it % cfg.reset_every == 0:
            reset_opa(params, opts, state, value=cfg.prune_opa * 2)
        if it in prune_at:
            with torch.no_grad():
                Ec = E4.clone()
                if popt is not None:
                    Dm = torch.eye(4, device=dev)[None].repeat(len(tr), 1, 1)
                    Dm[:, :3, :3] = _rodrigues(pose[:, :3])
                    Dm[:, :3, 3] = pose[:, 3:]
                    Ec = Dm @ Ec
            views = np.linspace(0, len(tr) - 1, min(len(tr), 80)).round().astype(int)
            fl, seen = find_floaters(params, _Cams(Ec, Kt, wh), views)
            op = torch.sigmoid(params["opacities"]).detach()
            big = torch.exp(params["scales"]).detach().max(1).values > 0.05 * scene_scale
            mask = fl | (op < cfg.prune_opa) | big
            pruned[str(it)] = {"floating_or_rarely_seen": int(fl.sum()), "faint": int((op < cfg.prune_opa).sum()),
                               "huge": int(big.sum()), "removed": int(mask.sum()), "of": int(len(mask))}
            log.info("splat %s: pruned %d of %d Gaussians at iter %d (%d floating or seen by < 3 views, "
                     "%d faint, %d huge)", rec.labels[video], int(mask.sum()), len(mask), it, int(fl.sum()),
                     int((op < cfg.prune_opa).sum()), int(big.sum()))
            remove(params, opts, state, mask)
        losses.append(float(l1))
        if it % 1000 == 0:
            log.info("splat %s: iter %d, L1 %.4f, %d Gaussians, %.0f s", rec.labels[video], it,
                     np.mean(losses[-200:]), len(params["means"]), time.time() - t0)
            if check is not None:
                check()
    info = {"recording": rec.labels[video], "train_frames": int(len(tr)),
            "train_frames_effective": round(float(pw.sum() ** 2 / (pw**2).sum()), 1), "held_out_frames": int(hold.sum()),
            "iters": cfg.iters, "gaussians": int(len(params["means"])), "seconds": round(time.time() - t0),
            "image_size": list(wh), "pose_correction_deg_median": round(float(np.degrees(
                pose[:, :3].norm(dim=1).median().item())), 3) if popt is not None else 0.0,
            "final_l1": round(float(np.mean(losses[-200:])), 4), "pruned": pruned,
            "start_points_adjusted": 0 if PX is None else int(len(PX)),
            "depth_scale_median": round(float(np.median(ks)), 4)}
    sp = Splat({k: v.detach() for k, v in params.items()}, cfg.sh_degree)
    sp.pose_frames, sp.pose_deltas = tr, pose.detach().cpu().numpy() if popt is not None else np.zeros((len(tr), 6))
    return sp, info


def corrected_poses(E: np.ndarray, video: np.ndarray, v: int, frames: np.ndarray, deltas: np.ndarray) -> np.ndarray:
    """Camera poses with the corrections the splat learnt while training (poses keep improving
    as it trains). Training frames get their own correction; a held-out frame gets the mean of
    the corrections of the training frames just before and after it in time (its own photo is
    never used)."""
    import torch

    E = E.copy()
    allv = np.nonzero(video == v)[0]
    pos = np.searchsorted(frames, allv)
    for s, p in zip(allv, pos):
        if p < len(frames) and frames[p] == s:
            d = deltas[p]
        else:
            nb = [i for i in (p - 1, p) if 0 <= i < len(frames)]
            d = deltas[nb].mean(0)
        R = _rodrigues(torch.as_tensor(d[None, :3], dtype=torch.float32))[0].numpy()
        E[s, :, :3] = R @ E[s, :, :3]
        E[s, :, 3] = R @ E[s, :, 3] + d[3:]
    return E


def test_views(rec, video: int, every: int) -> np.ndarray:
    frames = np.nonzero(rec.video == video)[0]
    return frames[held_out(frames, every)]


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    return float(-10 * np.log10(np.mean((a - b) ** 2) + 1e-12))
