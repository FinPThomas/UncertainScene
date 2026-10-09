"""Camera geometry helpers (OpenCV convention, E = camera-from-world 3x4)."""

from __future__ import annotations

import numpy as np


def cam_centers(E: np.ndarray) -> np.ndarray:
    """[S, 3] camera centres in world coordinates."""
    R, t = E[:, :, :3], E[:, :, 3]
    return -np.einsum("sji,sj->si", R, t)


def unproject(depth: np.ndarray, K: np.ndarray, E: np.ndarray, mask: np.ndarray):
    """World points [N, 3] and their pixel coords (v, u) for pixels where mask is True."""
    v, u = np.nonzero(mask)
    z = depth[v, u]
    x = (u + 0.5 - K[0, 2]) / K[0, 0] * z
    y = (v + 0.5 - K[1, 2]) / K[1, 1] * z
    pc = np.stack([x, y, z], 1)
    R, t = E[:, :3], E[:, 3]
    return (pc - t) @ R, v, u      # R^T (pc - t)


def project(X: np.ndarray, K: np.ndarray, E: np.ndarray):
    """Pixel coords (u, v) as float and depth z of world points X in one camera."""
    pc = X @ E[:, :3].T + E[:, 3]
    z = pc[:, 2]
    zs = np.where(np.abs(z) < 1e-9, 1e-9, z)
    u = K[0, 0] * pc[:, 0] / zs + K[0, 2] - 0.5
    v = K[1, 1] * pc[:, 1] / zs + K[1, 2] - 0.5
    return u, v, z


def up_vector(E: np.ndarray) -> np.ndarray:
    """World 'up' estimated as the mean camera up axis (-y of each camera). Phones are held
    roughly upright, so this is a good proxy for gravity without an IMU."""
    up = -E[:, 1, :3].mean(0)
    return up / np.linalg.norm(up)


def view_axes(E: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Orthonormal (right, up, forward) axes for plotting and for describing directions:
    up from the cameras, forward = mean viewing direction made horizontal."""
    up = up_vector(E)
    fwd = E[:, 2, :3].mean(0)
    fwd = fwd - fwd.dot(up) * up
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    return right, up, fwd
