"""Metric scale from a metric monocular depth model.

VGGT's reconstruction has no units. A metric depth model (Depth Anything V2 Metric, indoor,
trained to predict depth in metres) looks at each frame on its own; on the pixels where VGGT is
confident, metric depth / VGGT depth is the scale in metres per scene unit. The median over
many frames is the estimate; its uncertainty has two parts:
- the frames disagreeing (between-frame spread / sqrt(frames)), measured here;
- the model's own scale bias on a new camera and room, shared by every frame so more frames
  cannot shrink it: `bias_sd_log` (0.10, roughly the model family's published indoor relative
  error; a prior, never fitted to this scene).
Unlike the phone-height cue it does not need the floor to be visible.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

MODEL = "depth-anything/Depth-Anything-V2-Metric-Indoor-Large-hf"
_CACHE: dict = {}


def _predictor(model_id: str):
    """Function: image file -> depth in metres at the image's size."""
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    if model_id not in _CACHE:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _CACHE[model_id] = (AutoImageProcessor.from_pretrained(model_id),
                            AutoModelForDepthEstimation.from_pretrained(model_id).to(dev).eval(), dev)
    proc, model, dev = _CACHE[model_id]

    def predict(path: str) -> np.ndarray:
        im = Image.open(path).convert("RGB")
        with torch.no_grad():
            d = model(**proc(images=im, return_tensors="pt").to(dev)).predicted_depth
            d = torch.nn.functional.interpolate(d[:, None], size=im.size[::-1], mode="bilinear", align_corners=False)
        return d[0, 0].float().cpu().numpy()

    return predict


def release(model_id: str = MODEL) -> None:
    """Free the depth network (~1.3 GB of GPU memory) once the scale is known; SAM 2 and CLIP
    load after it."""
    if _CACHE.pop(model_id, None) is not None:
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def metric_scale(rec, n_frames: int = 80, model_id: str = MODEL, predict=None, bias_sd_log: float = 0.10,
                 conf_quantile: float = 0.5, min_pixels: int = 500) -> dict | None:
    """{"scale" (m per scene unit), "sd_log", "n", "per_frame" (scales), "between_sd_log", ...}
    or None when no frame gave a usable ratio."""
    predict = predict or _predictor(model_id)
    S = len(rec.files)
    frames = np.unique(np.linspace(0, S - 1, min(n_frames, S)).round().astype(int))
    w = rec.pose_weight() if hasattr(rec, "pose_weight") else np.ones(S)
    H, W = rec.depth.shape[1:]
    logs, ws = [], []
    for s in frames:
        try:
            m = predict(rec.files[s])
        except Exception:  # noqa: BLE001 - one unreadable frame must not stop the cue
            log.exception("metric depth failed on frame %d", s)
            continue
        if m.shape != (H, W):   # nearest-neighbour resample to the depth-map grid
            yi = (np.arange(H) * m.shape[0] / H).astype(int)
            xi = (np.arange(W) * m.shape[1] / W).astype(int)
            m = m[yi][:, xi]
        d, c = rec.depth[s], rec.conf[s]
        ok = (d > 0) & (m > 0.05) & np.isfinite(m) & (c >= np.quantile(c, conf_quantile))
        if ok.sum() < min_pixels:
            continue
        logs.append(float(np.median(np.log(m[ok] / d[ok]))))
        ws.append(float(w[s]))
    release(model_id)
    if not logs:
        return None
    logs, ws = np.array(logs), np.array(ws)
    order = np.argsort(logs)
    cw = np.cumsum(ws[order]) / ws.sum()
    med = float(logs[order][np.searchsorted(cw, 0.5)])               # pose-weighted median
    between = float(1.4826 * np.median(np.abs(logs - med)))          # robust spread across frames
    sd = float(np.sqrt(between**2 / len(logs) + bias_sd_log**2))
    out = {"scale": float(np.exp(med)), "sd_log": sd, "n": int(len(logs)), "between_sd_log": round(between, 4),
           "bias_sd_log": bias_sd_log, "model": model_id,
           "per_frame": [round(float(np.exp(x)), 4) for x in logs]}
    log.info("scale: metric depth %.3f m/unit from %d frames (frames spread %.1f%%, +-%.0f%% 1 sigma)",
             out["scale"], out["n"], 100 * between, 100 * sd)
    return out
