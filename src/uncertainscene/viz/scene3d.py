"""Compact 3D scene for the results viewer (scene3d.json per step).

Format: {"points": [table], "layers": [layer], "objects": [object]}
- table: {"n", "lo" [3], "sc" [3], "q": base64 int16 [n, 3]}; x = lo + sc * (q + 32500)
- layer: {"name", "kind" colour|change|heat|line|ellipse|objects, "rec" A|B|null, "visible", "size",
          "pts" (table index), and "rgb" (base64 uint8 [n, 3]) or "color" (one hex), optional
          "legend" {"lo", "hi", "stops" [hex x 6]} for heat maps}
- object: {"center", "label", "color", "status", "rec", "hover", "corners" [8][3], "ring" (table
           index of its 90% position ellipsoid rings, NaN-separated), "pal" (its own colour in
           the "objects" layer, where every object's points are painted one solid colour)}
Quantised coordinates cost ~12 bytes per point; a step holds ~120 000 points per recording.
Large scenes are split over scene3d.json, scene3d.1.json, ... (each under the 5 MB git limit);
`save` and `load` handle that.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import numpy as np

# distinct colours for painting each object (tab20 without the greys)
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#bcbd22", "#17becf",
           "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5", "#c49c94", "#f7b6d2", "#dbdb8d", "#9edae5"]


def save(scene: dict, out_dir: Path, part_mb: float = 4.5) -> list[Path]:
    """Write scene3d.json (+ scene3d.1.json, ...) so no file passes the git size limit."""
    txt = json.dumps(scene, separators=(",", ":"), default=float)
    size = int(part_mb * 1024**2)
    for old in Path(out_dir).glob("scene3d.*json"):
        old.unlink()
    paths = []
    for k in range(0, max(len(txt), 1), size):
        p = Path(out_dir) / ("scene3d.json" if k == 0 else f"scene3d.{k // size}.json")
        p.write_text(txt[k:k + size])
        paths.append(p)
    return paths


def load(d: Path) -> dict | None:
    d = Path(d)
    if not (d / "scene3d.json").exists():
        return None
    parts = [d / "scene3d.json"]
    k = 1
    while (d / f"scene3d.{k}.json").exists():
        parts.append(d / f"scene3d.{k}.json")
        k += 1
    return json.loads("".join(p.read_text() for p in parts))


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def _hex_rgb(cols, n: int) -> np.ndarray:
    if isinstance(cols, str) or cols is None:
        cols = [cols or "#888888"] * n
    out = np.zeros((n, 3), np.uint8)
    for k, c in enumerate(cols):
        c = str(c)
        if c.startswith("#") and len(c) >= 7:
            out[k] = [int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)]
        elif c.startswith("rgb"):
            out[k] = [int(float(v)) for v in re.findall(r"[\d.]+", c)[:3]]
        else:
            out[k] = 136
    return out


MPL = {"magma": "magma", "viridis": "viridis", "rdbu_r": "RdBu_r", "rdbu": "RdBu", "rdylgn": "RdYlGn"}


def _cmap(colorscale):
    from matplotlib import colormaps
    from matplotlib.colors import LinearSegmentedColormap, to_rgb

    if isinstance(colorscale, str):
        return colormaps[MPL.get(colorscale.lower(), "viridis")]
    stops = [(float(v), to_rgb(c) if not str(c).startswith("rgb") else
              tuple(float(x) / 255 for x in re.findall(r"[\d.]+", c)[:3])) for v, c in colorscale]
    return LinearSegmentedColormap.from_list("cs", stops)


def _scale_rgb(vals, colorscale, cmin, cmax) -> tuple[np.ndarray, list]:
    v = np.nan_to_num(np.asarray(vals, float), nan=cmin if cmin is not None else 0.0)
    lo = float(np.nanmin(v)) if cmin is None else float(cmin)
    hi = float(np.nanmax(v)) if cmax is None else float(cmax)
    cm = _cmap(colorscale)
    t = np.clip((v - lo) / (hi - lo + 1e-12), 0, 1)
    rgb = (cm(t)[:, :3] * 255).round().astype(np.uint8)
    stops = ["#%02x%02x%02x" % tuple((np.array(cm(x)[:3]) * 255).round().astype(int)) for x in np.linspace(0, 1, 6)]
    return rgb, stops


class Points:
    """Shared, quantised coordinate tables (int16 per axis), so layers that use the same
    points (the heat maps) store them once."""

    def __init__(self):
        self.tables: list[dict] = []
        self._keys: dict = {}

    def add(self, X: np.ndarray) -> int:
        X = np.asarray(X, float)
        key = (len(X), np.nan_to_num(X[:3]).round(4).tobytes(), np.nan_to_num(X[-3:]).round(4).tobytes())
        if key in self._keys:
            return self._keys[key]
        X = np.asarray(X, float)
        ok = np.isfinite(X).all(1)
        lo, hi = (X[ok].min(0), X[ok].max(0)) if ok.any() else (np.zeros(3), np.ones(3))
        sc = (hi - lo) / 65000 + 1e-12
        q = np.full(X.shape, -32768, np.int16)           # -32768 = break (NaN) for line layers
        q[ok] = np.round((X[ok] - lo) / sc - 32500).astype(np.int16)
        self.tables.append({"n": int(len(X)), "lo": lo.tolist(), "sc": sc.tolist(), "q": _b64(q)})
        self._keys[key] = len(self.tables) - 1
        return self._keys[key]


def _rgb_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(x) for x in c)


def box_corners(X: np.ndarray) -> list:
    """8 corners of the oriented bounding box (5-95% extent along principal axes); corner k
    has bit 0 = axis 0 high, bit 1 = axis 1 high, bit 2 = axis 2 high."""
    c = X.mean(0)
    if len(X) < 4:
        return [c.tolist()] * 8
    _, _, vt = np.linalg.svd(X - c, full_matrices=False)
    P = (X - c) @ vt.T
    lo, hi = np.quantile(P, 0.05, axis=0), np.quantile(P, 0.95, axis=0)
    out = []
    for k in range(8):
        v = np.array([hi[i] if k >> i & 1 else lo[i] for i in range(3)])
        out.append((c + v @ vt).round(4).tolist())
    return out


def from_analysis(res, objs_a: list, objs_b: list, fields: dict | None = None, n_colour: int = 120000,
                  n_change: int = 20000, n_heat: int = 50000, labels=("before", "after"), seed: int = 0,
                  n_per_object: int = 1500) -> dict:
    """Scene for one pair step: colour cloud of each recording, change classes, every heat map
    (sharing one point table) and every SAM 2 object with its box and 90% position region."""
    from ..change.detect import APPEARANCE, GONE
    from ..change.inventory import STATUS_COLOR
    from .plots import FIELD_STYLE

    rng = np.random.default_rng(seed)
    pts = Points()
    layers = []
    for ps, rec, lab, gone in ((res.A, "A", labels[0], "removed"), (res.B, "B", labels[1], "added")):
        idx = np.arange(len(ps.X))
        if len(idx) > n_colour:
            idx = np.sort(rng.choice(len(idx), n_colour, replace=False))
        layers.append({"name": f"{lab} (colour)", "kind": "colour", "rec": rec, "visible": rec == "A", "size": 2,
                       "pts": pts.add(ps.X[idx]), "rgb": _b64(ps.rgb[idx].astype(np.uint8))})
        for cls, cname, col in ((GONE, gone, "#d62728"), (APPEARANCE, "looks different", "#9467bd")):
            k = np.nonzero(ps.cls == cls)[0]
            if not len(k):
                continue
            if len(k) > n_change:
                k = np.sort(rng.choice(k, n_change, replace=False))
            layers.append({"name": f"{cname} ({lab})", "kind": "change", "rec": rec, "visible": False, "size": 3,
                           "pts": pts.add(ps.X[k]), "color": col})
    if fields is not None:
        sel = np.arange(len(fields["X"]))
        if len(sel) > n_heat:
            sel = np.sort(rng.choice(len(sel), n_heat, replace=False))
        t = pts.add(fields["X"][sel])
        for name, (ttl, cmap, vmin, vmax) in FIELD_STYLE.items():
            val = np.nan_to_num(fields[name][sel].astype(float), nan=0.0)
            if name == "shift_cm":
                m = float(np.quantile(np.abs(val), 0.99)) or 1.0
                vmin, vmax = -m, m
            vmax = vmax if vmax is not None else (float(np.quantile(val, 0.95)) or 1.0)
            rgb, stops = _scale_rgb(val, cmap, vmin, vmax)
            layers.append({"name": ttl, "kind": "heat", "rec": "A" if fields.get("which") == "A" else "B",
                           "visible": False, "size": 2, "pts": t, "rgb": _b64(rgb),
                           "legend": {"lo": round(float(vmin), 3), "hi": round(float(vmax), 3), "stops": stops}})
    for rec, lab, objs in (("A", labels[0], objs_a), ("B", labels[1], objs_b)):
        layers += object_layer(pts, [o for o in objs if not o.structure], rec, lab, n_per_object, rng)
    objects = []
    for o in objs_a + objs_b:
        if o.structure:
            continue
        pos = o.extra.get("position")
        pc = o.extra.get("position_cm", {})
        hover = o.extra.get("description", "") + (f" Position known to within {pc['radius90_cm']:.0f} cm (90%), "
                                                   f"from {pc['n_views']} views." if pc else "")
        ob = {"center": np.round(pos["mean"] if pos else o.centroid, 4).tolist(),
              "label": f"{o.label or 'object'}", "id": f"{o.recording}{o.id}", "status": o.status,
              "color": _rgb_hex(STATUS_COLOR.get(o.status, (90, 90, 90))), "rec": o.recording,
              "hover": hover, "corners": box_corners(o.X), "pal": object_colour(o)}
        if pos is not None:
            from ..change.position import ellipsoid_points

            ob["ring"] = pts.add(ellipsoid_points(pos["mean"], pos["cov"]))
        objects.append(ob)
    return {"points": pts.tables, "layers": layers, "objects": objects}


def object_colour(o) -> str:
    return PALETTE[int(o.id) % len(PALETTE)]


def object_layer(pts: Points, objs: list, rec: str, lab: str, n_per_object: int, rng) -> list:
    """Every object's own 3D points painted one solid colour per object, so objects stand out
    as filled shapes rather than as a box around part of the room."""
    X, C = [], []
    for o in objs:
        x = np.asarray(o.X)
        if not len(x):
            continue
        if len(x) > n_per_object:
            x = x[rng.choice(len(x), n_per_object, replace=False)]
        X.append(x)
        C.append(np.repeat(_hex_rgb(object_colour(o), 1), len(x), 0))
    if not X:
        return []
    return [{"name": f"objects, one colour each ({lab})", "kind": "objects", "rec": rec, "visible": False, "size": 3,
             "pts": pts.add(np.concatenate(X)), "rgb": _b64(np.concatenate(C))}]
