"""Joint feed-forward reconstruction of two recordings with VGGT.

VGGT (Wang et al., CVPR 2025) predicts camera poses, depth maps and confidences for a set of
images in one forward pass, in one shared frame. Feeding frames from both recordings together
gives the shared 3D representation directly, with no separate registration step, in about a
minute on a T4 for ~80 frames. Install: pip install git+https://github.com/facebookresearch/vggt
"""

from __future__ import annotations

import logging
import time

import numpy as np

from .frames import Recon, load_images, select_frames

log = logging.getLogger(__name__)
_MODEL = {}

# Measured VGGT-1B memory (authors' table, 518 px): ~1.7 GB + ~0.2 GB per frame.
GB_BASE, GB_PER_FRAME = 2.0, 0.2


def max_frames_for_gpu(total_gb: float | None = None, frac: float = 0.88) -> int:
    """Largest frame count that should fit in GPU memory (both recordings together)."""
    import torch

    if total_gb is None:
        if not torch.cuda.is_available():
            return 400
        total_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
    return max(8, int((total_gb * frac - GB_BASE) / GB_PER_FRAME))


def load_model(name: str = "facebook/VGGT-1B"):
    import torch
    from vggt.models.vggt import VGGT

    if name not in _MODEL:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        t0 = time.time()
        model = VGGT.from_pretrained(name)
        for head in ("point_head", "track_head"):   # not used: depth + cameras give the points
            if getattr(model, head, None) is not None:
                setattr(model, head, None)
        _MODEL[name] = model.to(dev).eval()
        log.info("loaded %s on %s in %.0f s", name, dev, time.time() - t0)
    return _MODEL[name]


def reconstruct(label_a: str, label_b: str, n_per_video: int = 40, offset: int = 0,
                long_side: int = 518, model_name: str = "facebook/VGGT-1B") -> tuple[Recon, dict]:
    """Caps the frame count to what fits the GPU, and on CUDA out-of-memory retries with 20%
    fewer frames (up to 3 times). info records requested and used frame counts."""
    import torch

    cap = max_frames_for_gpu() // 2
    n = min(n_per_video, cap)
    if n < n_per_video:
        log.warning("GPU fits ~%d frames per recording; using %d instead of %d", cap, n, n_per_video)
    for attempt in range(4):
        try:
            rec, info = _reconstruct(label_a, label_b, n, offset, long_side, model_name)
            info.update(requested_per_video=n_per_video, used_per_video=n, oom_retries=attempt)
            return rec, info
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if attempt == 3:
                raise
            n = max(8, int(n * 0.8))
            log.warning("CUDA out of memory; retrying with %d frames per recording", n)
    raise RuntimeError("unreachable")


def _reconstruct(label_a, label_b, n_per_video, offset, long_side, model_name):
    import torch
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri

    files_a = select_frames(label_a, n_per_video, offset)
    files_b = select_frames(label_b, n_per_video, offset)
    files = files_a + files_b
    imgs = load_images(files, long_side)
    video = np.array([0] * len(files_a) + [1] * len(files_b))

    model = load_model(model_name)
    dev = next(model.parameters()).device
    x = torch.from_numpy(imgs).permute(0, 3, 1, 2).float().div(255).to(dev)[None]
    use_bf16 = dev.type == "cuda" and torch.cuda.get_device_capability()[0] >= 8
    dtype = torch.bfloat16 if use_bf16 else torch.float16
    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats()
        free, total = torch.cuda.mem_get_info()
        log.info("VGGT on %d frames %s; GPU free %.1f/%.1f GB", len(files), imgs.shape[1:3], free / 1024**3,
                 total / 1024**3)
    t0 = time.time()
    with torch.no_grad(), torch.autocast(device_type=dev.type, dtype=dtype, enabled=dev.type == "cuda"):
        pred = model(x)
    extr, intr = pose_encoding_to_extri_intri(pred["pose_enc"], x.shape[-2:])
    secs = time.time() - t0
    rec = Recon(
        images=imgs,
        depth=pred["depth"][0, ..., 0].float().cpu().numpy(),
        conf=pred["depth_conf"][0].float().cpu().numpy(),
        K=intr[0].float().cpu().numpy(),
        E=extr[0].float().cpu().numpy(),
        video=video,
        files=[str(f) for f in files],
        labels=(label_a, label_b),
    )
    info = {"model": model_name, "frames": len(files), "size_hw": list(imgs.shape[1:3]),
            "seconds": round(secs, 1),
            "gpu_peak_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 2) if dev.type == "cuda" else None}
    del pred, x
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    log.info("reconstructed %d frames in %.0f s (GPU peak %s GB)", len(files), secs, info["gpu_peak_gb"])
    return rec, info


def _forward(imgs: np.ndarray, model_name: str) -> dict:
    """One VGGT pass over uint8 images [n, H, W, 3] -> depth, conf, K, E as numpy."""
    import torch
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri

    model = load_model(model_name)
    dev = next(model.parameters()).device
    x = torch.from_numpy(imgs).permute(0, 3, 1, 2).float().div(255).to(dev)[None]
    use_bf16 = dev.type == "cuda" and torch.cuda.get_device_capability()[0] >= 8
    dtype = torch.bfloat16 if use_bf16 else torch.float16
    with torch.no_grad(), torch.autocast(device_type=dev.type, dtype=dtype, enabled=dev.type == "cuda"):
        pred = model(x)
    extr, intr = pose_encoding_to_extri_intri(pred["pose_enc"], x.shape[-2:])
    out = {"depth": pred["depth"][0, ..., 0].float().cpu().numpy(), "conf": pred["depth_conf"][0].float().cpu().numpy(),
           "K": intr[0].float().cpu().numpy().astype(np.float64), "E": extr[0].float().cpu().numpy().astype(np.float64)}
    del pred, x
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    return out


def reconstruct_all(label_a: str, label_b: str, n_per_video: int | None = None, offset: int = 0,
                    long_side: int = 518, model_name: str = "facebook/VGGT-1B", n_anchor: int = 8,
                    chunk_size: int | None = None) -> tuple[Recon, dict]:
    """Many frames per recording (None = every extracted frame) in overlapping chunks that fit
    the GPU, aligned through shared anchor frames (see recon.chunked). On CUDA out-of-memory
    the chunk size shrinks by 20% and the reconstruction restarts (up to 3 times)."""
    return reconstruct_many([label_a, label_b], n_per_video, offset, long_side, model_name, n_anchor, chunk_size)


def reconstruct_many(labels: list[str], n_per_video: int | None = None, offset: int = 0, long_side: int = 518,
                     model_name: str = "facebook/VGGT-1B", n_anchor: int = 8,
                     chunk_size: int | None = None) -> tuple[Recon, dict]:
    """Any number of recordings in one shared frame (video id = position in `labels`)."""
    import torch

    from .chunked import reconstruct_chunked

    n = n_per_video or 10_000
    per = [select_frames(lab, n, offset) for lab in labels]
    files = [f for p in per for f in p]
    files_a, files_b = per[0], (per[1] if len(per) > 1 else [])
    imgs = load_images(files, long_side)
    video = np.concatenate([np.full(len(p), i) for i, p in enumerate(per)])
    n_anchor = max(n_anchor, 3 * len(labels))
    size = chunk_size or min(48, max_frames_for_gpu() - 4)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    for attempt in range(4):
        t0 = time.time()
        try:
            rec, cinfo = reconstruct_chunked(imgs, video, lambda idx: _forward(imgs[idx], model_name),
                                             chunk_size=size, n_anchor=n_anchor, files=[str(f) for f in files],
                                             labels=tuple(labels))
            break
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if attempt == 3:
                raise
            size = max(16, int(size * 0.8))
            log.warning("CUDA out of memory; restarting with chunks of %d frames", size)
    secs = time.time() - t0
    info = {"model": model_name, "frames": len(files), "frames_a": len(files_a), "frames_b": len(files_b),
            "size_hw": list(imgs.shape[1:3]), "seconds": round(secs, 1), "used_per_video": len(files_a),
            "requested_per_video": n_per_video, "oom_retries": attempt, **cinfo,
            "gpu_peak_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 2) if torch.cuda.is_available() else None}
    info["frames_per_recording"] = [len(p) for p in per]
    log.info("reconstructed %d frames (%s) in %d chunks in %.0f s; worst chunk residual %.3f",
             len(files), " + ".join(str(len(p)) for p in per), cinfo["chunks"], secs, cinfo.get("chunk_residual_max", 0))
    return rec, info
