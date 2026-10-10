"""Interactive and exportable 3D views.

- `write_ply`: dense coloured point cloud with a per-point `change` field (0 unchanged,
  1 gone, 2 appearance, 3 unknown) and `rec` (0 = A before, 1 = B after). Opens in MeshLab,
  CloudCompare or Blender; colour by the change field to see the changes in 3D.
- `write_html`: a single HTML file (plotly from a CDN) to rotate the scene in a browser, with
  the RGB cloud and the change overlay as toggleable layers and object labels.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..change.detect import APPEARANCE, GONE, UNKNOWN, ChangeResult
from .plots import COL


def write_ply(res: ChangeResult, out: Path, max_points: int = 260000, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    X = np.concatenate([res.A.X, res.B.X])
    rgb = np.concatenate([res.A.rgb, res.B.rgb])
    cls = np.concatenate([res.A.cls, res.B.cls]).astype(np.uint8)
    rec = np.concatenate([np.zeros(len(res.A.X)), np.ones(len(res.B.X))]).astype(np.uint8)
    changed = np.nonzero((cls == GONE) | (cls == APPEARANCE))[0]
    rest = np.setdiff1d(np.arange(len(X)), changed)
    k = max(0, max_points - len(changed))
    keep = np.concatenate([changed[:max_points], rng.choice(rest, min(k, len(rest)), replace=False)])
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1"),
                   ("change", "u1"), ("rec", "u1")])
    arr = np.empty(len(keep), dt)
    arr["x"], arr["y"], arr["z"] = X[keep, 0], X[keep, 1], X[keep, 2]
    arr["red"], arr["green"], arr["blue"] = rgb[keep, 0], rgb[keep, 1], rgb[keep, 2]
    arr["change"], arr["rec"] = cls[keep], rec[keep]
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(arr)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              "property uchar change\nproperty uchar rec\nend_header\n")
    with open(out, "wb") as f:
        f.write(header.encode())
        f.write(arr.tobytes())
    return out


def write_html(res: ChangeResult, objs: list, out: Path, title: str = "", n_bg: int = 45000,
               n_change: int = 15000, seed: int = 0, fields: dict | None = None, n_field: int = 12000,
               max_mb: float = 4.5, markers: list | None = None) -> Path:
    """markers: optional list of dicts (x, y, z, text, color, group, hover) drawn as labelled
    points, one legend entry per group (e.g. every SAM 2 object by change status)."""
    import plotly.graph_objects as go

    rng = np.random.default_rng(seed)
    traces = []
    for ps, name, gone in ((res.A, "A before", "removed"), (res.B, "B after", "added")):
        bg = np.nonzero(ps.cls != GONE)[0]
        bg = bg[rng.choice(len(bg), min(n_bg // 2, len(bg)), replace=False)]
        cols = [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in ps.rgb[bg]]
        traces.append(go.Scatter3d(x=ps.X[bg, 0].round(3), y=ps.X[bg, 1].round(3), z=ps.X[bg, 2].round(3),
                                   mode="markers", marker=dict(size=1.5, color=cols), name=f"{name} (colour)",
                                   visible=True if name == "A before" else "legendonly", hoverinfo="skip"))
        for cls, cname in ((GONE, gone), (APPEARANCE, "appearance"), (UNKNOWN, "unknown")):
            idx = np.nonzero(ps.cls == cls)[0]
            idx = idx[rng.choice(len(idx), min(n_change // 3, len(idx)), replace=False)] if len(idx) else idx
            if not len(idx):
                continue
            traces.append(go.Scatter3d(x=ps.X[idx, 0].round(3), y=ps.X[idx, 1].round(3), z=ps.X[idx, 2].round(3),
                                       mode="markers", marker=dict(size=2.5, color=COL[cname]),
                                       name=f"{cname} ({name})", hoverinfo="skip",
                                       visible=True if cls != UNKNOWN else "legendonly"))
    if objs:
        c = np.array([o.centroid for o in objs])
        traces.append(go.Scatter3d(x=c[:, 0], y=c[:, 1], z=c[:, 2], mode="markers+text",
                                   text=[f"{o.id}:{o.label or o.kind} ({o.kind})" for o in objs],
                                   marker=dict(size=6, color=[COL.get(o.kind, "#000") for o in objs]),
                                   name="change objects", hovertext=[o.description for o in objs]))
        for o in objs:
            if o.kind == "moved" and o.recording == "A" and o.displacement:
                p, q = np.array(o.centroid), np.array(o.centroid) + np.array(o.displacement)
                traces.append(go.Scatter3d(x=[p[0], q[0]], y=[p[1], q[1]], z=[p[2], q[2]], mode="lines",
                                           line=dict(color=COL["moved"], width=8), name=f"move {o.id}",
                                           showlegend=False))
    if fields is not None:
        from .plots import FIELD_STYLE

        sel = rng.choice(len(fields["X"]), min(n_field, len(fields["X"])), replace=False)
        Xf = fields["X"][sel]
        for name, (ttl, cmap, vmin, vmax) in FIELD_STYLE.items():
            val = np.nan_to_num(fields[name][sel], nan=0.0)
            if name == "shift_cm":
                m = float(np.quantile(np.abs(val), 0.99)) or 1.0
                vmin, vmax = -m, m
            vmax = vmax if vmax is not None else float(np.quantile(val, 0.95)) or 1.0
            plotly_cmap = {"magma": "Magma", "coolwarm": "RdBu_r", "viridis": "Viridis", "RdYlGn": "RdYlGn"}[cmap]
            traces.append(go.Scatter3d(x=Xf[:, 0].round(3), y=Xf[:, 1].round(3), z=Xf[:, 2].round(3), mode="markers",
                                       marker=dict(size=2, color=val.round(3), colorscale=plotly_cmap, cmin=vmin,
                                                   cmax=vmax, showscale=False),
                                       name=f"heat map: {ttl}", visible="legendonly", hoverinfo="skip"))
    if markers:
        groups: dict = {}
        for mk in markers:
            groups.setdefault(mk["group"], []).append(mk)
        for g, ms in groups.items():
            traces.append(go.Scatter3d(x=[m["x"] for m in ms], y=[m["y"] for m in ms], z=[m["z"] for m in ms],
                                       mode="markers+text", text=[m["text"] for m in ms],
                                       hovertext=[m.get("hover", m["text"]) for m in ms], textfont=dict(size=10),
                                       marker=dict(size=5, color=[m["color"] for m in ms]), name=f"objects: {g}",
                                       visible=True if g != "unchanged" else "legendonly"))
    fig = go.Figure(traces)
    fig.update_layout(title=title, scene=dict(aspectmode="data", camera=dict(up=dict(x=0, y=-1, z=0)),
                                              xaxis_visible=False, yaxis_visible=False, zaxis_visible=False),
                      margin=dict(l=0, r=0, t=40, b=0), legend=dict(itemsizing="constant"))
    fig.write_html(out, include_plotlyjs="cdn", full_html=True)
    if Path(out).stat().st_size > max_mb * 1024**2 and n_bg > 2000:   # keep under the git file limit
        return write_html(res, objs, out, title, int(n_bg * 0.6), int(n_change * 0.6), seed, fields,
                          int(n_field * 0.6), max_mb, markers)
    return out


def write_fields_ply(f: dict, out: Path, max_mb: float = 4.5) -> Path:
    """Voxelised cloud of recording B with every heat-map field as a float property
    (subsampled to stay under max_mb)."""
    names = ["uncertainty_cm", "shift_cm", "height_m", "reach", "approach", "interact"]
    cap = int(max_mb * 1024**2 / (15 + 4 * len(names)))
    if len(f["X"]) > cap:
        sel = np.sort(np.random.default_rng(0).choice(len(f["X"]), cap, replace=False))
        f = {k: (f[k][sel] if k in names + ["X", "rgb"] else f[k]) for k in f}
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")]
                  + [(n, "<f4") for n in names])
    arr = np.empty(len(f["X"]), dt)
    for i, c in enumerate("xyz"):
        arr[c] = f["X"][:, i]
    for i, c in enumerate(("red", "green", "blue")):
        arr[c] = f["rgb"][:, i]
    for n in names:
        arr[n] = np.nan_to_num(f[n], nan=0.0)
    props = "".join(f"property float {n}\n" for n in names)
    header = ("ply\nformat binary_little_endian 1.0\n" f"element vertex {len(arr)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n" + props + "end_header\n")
    with open(out, "wb") as fh:
        fh.write(header.encode())
        fh.write(arr.tobytes())
    return out
