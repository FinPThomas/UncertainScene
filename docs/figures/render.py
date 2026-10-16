"""Small point-cloud renderer for the README figures: perspective camera, z-buffer, round splats.

Reads the saved viewer scenes (scene3d.json per step) and object assets; nothing is recomputed.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
RUN = ROOT / "results/change/2026-10-11_scene01-objects-fixed"
LIGHT = RUN / "1e-changes_moved_light-before-a_vs_light-after-a"          # light pair, run 12
DARK = RUN / "1d-objects_moved-and-light-changed_dark-before-a_vs_light-after-a"  # dark vs light, run 11
STATIC = RUN / "1d-objects_no-change_dark-before-a_vs_dark-before-b"        # no-change pair, run 11
EARLY = RUN / "1-main_moved_light-before-a_vs_light-after-a"               # run 6 (before whole-object tracking)


def load_scene(step: Path) -> dict:
    from uncertainscene.viz.scene3d import load
    return load(step)


def table(scene: dict, k: int) -> np.ndarray:
    t = scene["points"][k]
    q = np.frombuffer(base64.b64decode(t["q"]), np.int16).reshape(-1, 3).astype(float)
    X = np.asarray(t["lo"]) + np.asarray(t["sc"]) * (q + 32500)
    X[(q == -32768).any(1)] = np.nan
    return X


def rgb(layer: dict) -> np.ndarray:
    return np.frombuffer(base64.b64decode(layer["rgb"]), np.uint8).reshape(-1, 3)


def layer(scene: dict, name_start: str, rec: str | None = None) -> dict:
    for L in scene["layers"]:
        if L["name"].startswith(name_start) and (rec is None or L.get("rec") == rec):
            return L
    raise KeyError(name_start)


def basis(up: np.ndarray, X: np.ndarray):
    """Right-handed frame: z = up, x = scene's main horizontal axis (sofa length), y = depth."""
    up = up / np.linalg.norm(up)
    Xc = X - np.nanmean(X, 0)
    Xh = Xc - np.outer(Xc @ up, up)
    _, _, vt = np.linalg.svd(Xh[np.isfinite(Xh).all(1)][::7], full_matrices=False)
    ex = vt[0] - (vt[0] @ up) * up
    ex /= np.linalg.norm(ex)
    ey = np.cross(up, ex)
    return np.stack([ex, ey, up])


class Camera:
    def __init__(self, target, azim_deg, elev_deg, dist, B, fov_deg=45.0, size=(900, 600)):
        self.B, self.size = B, size
        a, e = np.radians(azim_deg), np.radians(elev_deg)
        d_local = np.array([np.cos(e) * np.sin(a), -np.cos(e) * np.cos(a), np.sin(e)])  # from target to eye
        self.eye = target + dist * (d_local @ B)
        fwd = target - self.eye
        fwd /= np.linalg.norm(fwd)
        up = B[2]
        right = np.cross(fwd, up)
        right /= np.linalg.norm(right)
        cup = np.cross(right, fwd)
        self.R = np.stack([right, -cup, fwd])
        self.f = 0.5 * size[0] / np.tan(np.radians(fov_deg) / 2)

    def project(self, X):
        P = (X - self.eye) @ self.R.T
        z = P[:, 2]
        u = self.f * P[:, 0] / np.maximum(z, 1e-6) + self.size[0] / 2
        v = self.f * P[:, 1] / np.maximum(z, 1e-6) + self.size[1] / 2
        return u, v, z


def render(cam: Camera, X, C, radius_px=2.2, bg=(255, 255, 255)):
    """Z-buffered round splats (radius shrinks with distance). Returns image [H, W, 3] and depth."""
    W, H = cam.size
    ok = np.isfinite(X).all(1)
    X, C = X[ok], np.asarray(C, float)[ok]
    u, v, z = cam.project(X)
    vis = z > 1e-3
    u, v, z, C = u[vis], v[vis], z[vis], C[vis]
    r = np.clip(radius_px * np.median(z) / z, 0.7, radius_px * 2.5)
    R = int(np.ceil(r.max()))
    pix, zs, cs = [], [], []
    for dy in range(-R, R + 1):
        for dx in range(-R, R + 1):
            d = np.hypot(dx, dy)
            m = r >= d
            x = np.round(u[m]).astype(int) + dx
            y = np.round(v[m]).astype(int) + dy
            inb = (x >= 0) & (x < W) & (y >= 0) & (y < H)
            pix.append(y[inb] * W + x[inb]); zs.append(z[m][inb]); cs.append(C[m][inb])
    pix, zs, cs = np.concatenate(pix), np.concatenate(zs), np.concatenate(cs)
    o = np.lexsort((zs, pix))
    pix, zs, cs = pix[o], zs[o], cs[o]
    first = np.r_[True, pix[1:] != pix[:-1]]
    img = np.zeros((H * W, 3)) + np.asarray(bg, float)
    zb = np.full(H * W, np.inf)
    img[pix[first]] = cs[first]
    zb[pix[first]] = zs[first]
    return img.reshape(H, W, 3).clip(0, 255).astype(np.uint8), zb.reshape(H, W)


def heat_values(scene: dict, name_start: str):
    """Points and values of one saved heat-map layer, recovered by inverting its colour map
    (8-bit colours, so values are quantised to 1/255 of the layer's range)."""
    from matplotlib import colormaps
    from scipy.spatial import cKDTree

    L = layer(scene, name_start)
    X = table(scene, L["pts"])
    stops = L["legend"]["stops"]
    name = {"#000004": "magma", "#440154": "viridis", "#a50026": "RdYlGn"}[stops[0]]
    lut = (colormaps[name](np.linspace(0, 1, 1024))[:, :3] * 255)
    d, i = cKDTree(lut).query(rgb(L).astype(float))
    t = i / 1023
    return X, L["legend"]["lo"] + t * (L["legend"]["hi"] - L["legend"]["lo"])


def up_from_height(scene: dict):
    """Gravity 'up' and the floor offset used by the run, from its saved height-above-floor layer."""
    X, h = heat_values(scene, "Height above floor")
    ok = np.isfinite(X).all(1) & (h > 0.02) & (h < 0.98 * h.max())
    A = np.c_[X[ok], np.ones(ok.sum())]
    coef, *_ = np.linalg.lstsq(A, h[ok], rcond=None)
    s = np.linalg.norm(coef[:3])                 # metres per scene unit along up
    return coef[:3] / s, s, coef[3]


def load_asset(step: Path, name: str):
    """An exported asset: metadata, its points in metres (world frame) and colours."""
    meta = json.loads((step / "assets" / f"{name}.json").read_text())
    raw = (step / "assets" / f"{name}.ply").read_bytes()
    head, body = raw.split(b"end_header\n", 1)
    n = int(head.split(b"element vertex ")[1].split(b"\n")[0])
    a = np.frombuffer(body[: n * 15], np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                                ("r", "u1"), ("g", "u1"), ("b", "u1")]))
    P = np.stack([a["x"], a["y"], a["z"]], 1).astype(float)
    R, t = np.asarray(meta["pose"]["R"]), np.asarray(meta["pose"]["t_m"])
    X = P @ R.T + t
    C = np.stack([a["r"], a["g"], a["b"]], 1)
    return meta, X, C


def render_aa(cam: Camera, X, C, radius_px=2.2, ss=2, bg=(255, 255, 255)):
    """render() at ss-times the resolution, then downsampled: smoother splat edges, finer points."""
    from PIL import Image
    big = Camera.__new__(Camera)
    big.__dict__.update(cam.__dict__)
    big.size = (cam.size[0] * ss, cam.size[1] * ss)
    if hasattr(cam, "f"):
        big.f = cam.f * ss
    img, zb = render(big, X, C, radius_px * ss, bg)
    im = Image.fromarray(img).resize(cam.size, Image.LANCZOS)
    return np.asarray(im), zb[::ss, ::ss]
