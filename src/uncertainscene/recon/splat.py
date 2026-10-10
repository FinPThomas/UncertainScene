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
    export_max: int = 140_000       # Gaussians in the committed .splat file (32 bytes each, < 5 MB)
    seed: int = 0


def held_out(frames: np.ndarray, every: int) -> np.ndarray:
    """Boolean per frame of one recording: True = held out (every n-th, starting at the 4th)."""
    k = np.arange(len(frames))
    return (k % every) == (every // 2)


def init_points(rec, frames: np.ndarray, n: int, conf_quantile: float = 0.5, seed: int = 0):
    """Confident VGGT depth points (world frame) with colours, from the given frames."""
    from ..change.geometry import unproject

    rng = np.random.default_rng(seed)
    per = max(1, n // max(len(frames), 1))
    X, C = [], []
    for s in frames:
        good = (rec.depth[s] > 0) & (rec.conf[s] >= np.quantile(rec.conf[s], conf_quantile))
        x, v, u = unproject(rec.depth[s], rec.K[s], rec.E[s], good)
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


def train(rec, video: int, cfg: SplatConfig = SplatConfig(), check=None) -> tuple[Splat, dict]:
    """Train the splat of recording `video` on its non-held-out frames."""
    import torch
    from gsplat import rasterization
    from gsplat.strategy import DefaultStrategy

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

    X, C = init_points(rec, tr, cfg.max_init, seed=cfg.seed)
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
    strategy = DefaultStrategy(refine_stop_iter=int(cfg.iters * 0.75), reset_every=3000, verbose=False)
    strategy.check_sanity(params, opts)
    state = strategy.initialize_state(scene_scale=scene_scale)
    E4 = np.tile(np.eye(4, dtype=np.float32), (len(tr), 1, 1))
    E4[:, :3, :] = rec.E[tr]
    E4 = torch.as_tensor(E4, device=dev)
    Kt = torch.as_tensor(Ks, dtype=torch.float32, device=dev)
    rng = np.random.default_rng(cfg.seed)
    t0, losses = time.time(), []
    for it in range(cfg.iters):
        i = int(rng.integers(len(tr)))
        gt = torch.as_tensor(imgs[i], device=dev).float() / 255
        vm = E4[i:i + 1]
        if popt is not None:
            D = torch.eye(4, device=dev)[None].clone()
            D[0, :3, :3] = _rodrigues(pose[i:i + 1, :3])[0]
            D[0, :3, 3] = pose[i, 3:]
            vm = D @ vm
        out, _, info = rasterization(params["means"], params["quats"], torch.exp(params["scales"]),
                                     torch.sigmoid(params["opacities"]), torch.sigmoid(params["colors"]),
                                     vm, Kt[i:i + 1], wh[0], wh[1], packed=False)
        strategy.step_pre_backward(params, opts, state, it, info)
        img = out[0]
        l1 = (img - gt).abs().mean()
        loss = (1 - cfg.ssim_weight) * l1 + cfg.ssim_weight * (1 - _ssim(img.permute(2, 0, 1)[None],
                                                                          gt.permute(2, 0, 1)[None]))
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
        losses.append(float(l1))
        if it % 1000 == 0:
            log.info("splat %s: iter %d, L1 %.4f, %d Gaussians, %.0f s", rec.labels[video], it,
                     np.mean(losses[-200:]), len(params["means"]), time.time() - t0)
            if check is not None:
                check()
    info = {"recording": rec.labels[video], "train_frames": int(len(tr)), "held_out_frames": int(hold.sum()),
            "iters": cfg.iters, "gaussians": int(len(params["means"])), "seconds": round(time.time() - t0),
            "image_size": list(wh), "pose_correction_deg_median": round(float(np.degrees(
                pose[:, :3].norm(dim=1).median().item())), 3) if popt is not None else 0.0,
            "final_l1": round(float(np.mean(losses[-200:])), 4)}
    return Splat({k: v.detach() for k, v in params.items()}, cfg.sh_degree), info


def test_views(rec, video: int, every: int) -> np.ndarray:
    frames = np.nonzero(rec.video == video)[0]
    return frames[held_out(frames, every)]


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    return float(-10 * np.log10(np.mean((a - b) ** 2) + 1e-12))
