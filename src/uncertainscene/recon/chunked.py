"""Reconstruct many frames (all of both recordings) with a model that only fits ~54 at once.

The frames are split into chunks that all share the same small set of anchor frames (spread
over both recordings). Each chunk is reconstructed on its own; because the anchor images are
identical in every chunk, their depth maps give dense pixel-to-pixel 3D correspondences, and a
similarity transform (rotation, translation, scale; Umeyama, with outlier trimming) maps every
chunk into the first chunk's frame. The anchors are kept once, from the first chunk.

The alignment residual of the anchor points (relative to their depth) is logged per chunk; a
chunk whose residual is far above the others is reported in `info["chunk_residuals"]`.
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from .frames import Recon
from ..change.geometry import unproject


def unproject_frame(depth, K, E, mask):
    return unproject(depth, K, E, mask)[0]

log = logging.getLogger(__name__)

# run_chunk(indices into the full frame list) -> dict(depth [n,H,W], conf, K [n,3,3], E [n,3,4])
RunChunk = Callable[[list[int]], dict]


def plan_chunks(video: np.ndarray, chunk_size: int, n_anchor: int) -> tuple[list[int], list[list[int]]]:
    """Anchors (evenly spread, half from each recording) and the non-anchor frames per chunk."""
    anchors = []
    for vid in (0, 1):
        idx = np.nonzero(video == vid)[0]
        k = n_anchor // 2 + (n_anchor % 2 if vid == 0 else 0)
        anchors += [int(idx[int(round(x))]) for x in np.linspace(0, len(idx) - 1, min(k, len(idx)))]
    anchors = sorted(set(anchors))
    rest = [i for i in range(len(video)) if i not in set(anchors)]
    per = max(1, chunk_size - len(anchors))
    n_chunks = max(1, int(np.ceil(len(rest) / per)))
    # interleave so every chunk covers the whole sweep of both recordings (better geometry)
    chunks = [rest[c::n_chunks] for c in range(n_chunks)]
    return anchors, chunks


def umeyama(src: np.ndarray, dst: np.ndarray, iters: int = 4, keep: float = 0.8) -> tuple[float, np.ndarray, np.ndarray, float]:
    """s, R, t with dst ~ s R src + t; refits on the best `keep` fraction a few times.
    Returns the median residual of the kept points as the last value."""
    sel = np.arange(len(src))
    for _ in range(iters):
        a, b = src[sel], dst[sel]
        ma, mb = a.mean(0), b.mean(0)
        A, B = a - ma, b - mb
        U, D, Vt = np.linalg.svd(B.T @ A / len(a))
        S = np.eye(3)
        if np.linalg.det(U) * np.linalg.det(Vt) < 0:
            S[2, 2] = -1
        R = U @ S @ Vt
        s = float(np.trace(np.diag(D) @ S) / (A ** 2).sum(1).mean())
        t = mb - s * R @ ma
        res = np.linalg.norm(dst - (s * src @ R.T + t), axis=1)
        sel = np.argsort(res)[: max(10, int(keep * len(src)))]
    return s, R, t, float(np.median(res[sel]))


def transform_cameras(E: np.ndarray, depth: np.ndarray, s: float, R: np.ndarray, t: np.ndarray):
    """Apply X' = s R X + t to cameras (camera-from-world) and depth maps."""
    Re, te = E[:, :, :3], E[:, :, 3]
    Rn = Re @ R.T
    tn = s * te - np.einsum("sij,j->si", Rn, t)
    return np.concatenate([Rn, tn[:, :, None]], 2), depth * s


def reconstruct_chunked(images: np.ndarray, video: np.ndarray, run_chunk: RunChunk, chunk_size: int = 48,
                        n_anchor: int = 8, stride: int = 4, conf_quantile: float = 0.5,
                        files: list | None = None, labels=("A", "B")) -> tuple[Recon, dict]:
    S = len(images)
    if S <= chunk_size:
        out = run_chunk(list(range(S)))
        return Recon(images, out["depth"], out["conf"], out["K"], out["E"], video, files or [], labels), \
            {"chunks": 1, "chunk_residuals": []}
    anchors, chunks = plan_chunks(video, chunk_size, n_anchor)
    H, W = images.shape[1:3]
    depth = np.zeros((S, H, W), np.float32)
    conf = np.zeros((S, H, W), np.float32)
    K = np.zeros((S, 3, 3))
    E = np.zeros((S, 3, 4))
    ref = None                  # anchor outputs of the first chunk
    residuals = []
    for c, rest in enumerate(chunks):
        idx = anchors + rest
        out = run_chunk(idx)
        na = len(anchors)
        if ref is None:
            ref = {k: out[k][:na] for k in ("depth", "conf", "K", "E")}
            s, R, t, res = 1.0, np.eye(3), np.zeros(3), 0.0
            sel = list(range(len(idx)))
        else:
            src, dst = [], []
            for j in range(na):
                ok = (out["conf"][j] >= np.quantile(out["conf"][j], conf_quantile)) & \
                     (ref["conf"][j] >= np.quantile(ref["conf"][j], conf_quantile)) & \
                     (out["depth"][j] > 0) & (ref["depth"][j] > 0)
                grid = np.zeros_like(ok)
                grid[::stride, ::stride] = True
                ok &= grid
                src.append(unproject_frame(out["depth"][j], out["K"][j], out["E"][j], ok))
                dst.append(unproject_frame(ref["depth"][j], ref["K"][j], ref["E"][j], ok))
            src, dst = np.concatenate(src), np.concatenate(dst)
            s, R, t, res = umeyama(src, dst)
            scale_ref = float(np.median(ref["depth"][ref["depth"] > 0]))
            res = res / scale_ref
            sel = list(range(na, len(idx)))
        En, Dn = transform_cameras(out["E"], out["depth"], s, R, t)
        for k, i in enumerate(idx):
            if k < na and c > 0:
                continue        # anchors come from the first chunk
            depth[i], conf[i], K[i], E[i] = Dn[k], out["conf"][k], out["K"][k], En[k]
        if c > 0:
            residuals.append(round(res, 4))
            log.info("chunk %d/%d: %d frames, scale %.3f, anchor residual %.3f (relative to depth)",
                     c + 1, len(chunks), len(idx), s, res)
    # drop chunks that failed to align (residual far above the others): better fewer frames
    # than a ghost copy of the scene
    keep = np.ones(S, bool)
    dropped = []
    if len(residuals) >= 2:
        lim = max(0.05, 4 * float(np.median(residuals)))
        for c, r in enumerate(residuals, start=1):
            if r > lim:
                keep[chunks[c]] = False
                dropped.append(c)
                log.warning("chunk %d dropped: anchor residual %.3f > %.3f", c + 1, r, lim)
    info = {"chunks": len(chunks), "anchors": len(anchors), "chunk_size": chunk_size,
            "chunk_residuals": residuals, "chunks_dropped": dropped,
            "chunk_residual_max": max(residuals) if residuals else 0.0}
    files = files or []
    k = np.nonzero(keep)[0]
    return Recon(images[k], depth[k], conf[k], K[k], E[k], video[k],
                 [files[i] for i in k] if files else [], labels), info
