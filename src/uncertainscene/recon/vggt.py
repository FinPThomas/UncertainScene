"""Joint feed-forward reconstruction of two recordings with VGGT.

VGGT (Wang et al., CVPR 2025) predicts camera poses, depth maps and confidences for a set of
images in one forward pass, in one shared frame. Feeding frames from both recordings together
gives the shared 3D representation directly, with no separate registration step, in about a
minute on a T4 for ~80 frames. Install: pip install git+https://github.com/facebookresearch/vggt
"""

from __future__ import annotations

import time

import numpy as np

from .frames import Recon, load_images, select_frames

_MODEL = {}


def load_model(name: str = "facebook/VGGT-1B"):
    import torch
    from vggt.models.vggt import VGGT

    if name not in _MODEL:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _MODEL[name] = VGGT.from_pretrained(name).to(dev).eval()
    return _MODEL[name]


def reconstruct(label_a: str, label_b: str, n_per_video: int = 40, offset: int = 0,
                long_side: int = 518, model_name: str = "facebook/VGGT-1B") -> tuple[Recon, dict]:
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
    return rec, info
