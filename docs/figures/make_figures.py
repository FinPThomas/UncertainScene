"""Redraw every README figure from saved run outputs (no pipeline step is rerun).

    pip install matplotlib pillow scipy numpy
    python docs/figures/make_figures.py            # all figures
    python docs/figures/make_figures.py heatmaps   # one figure

Sources: the viewer scenes (scene3d.json) and exported assets of
  light pair      results/change/2026-10-11_scene01-objects-fixed/1e-changes_moved_light-before-a_vs_light-after-a
  dark-light pair .../1d-objects_moved-and-light-changed_dark-before-a_vs_light-after-a
  run 6           .../1-main_moved_light-before-a_vs_light-after-a  (before whole-object tracking)
and the frames in data/frames/.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from render import (DARK, EARLY, LIGHT, ROOT, Camera, basis, heat_values, layer, load_asset, load_scene,  # noqa: E402
                    render, rgb, table, up_from_height)

OUT = Path(__file__).parent
FRAMES = ROOT / "data/frames"
plt.rcParams.update({"font.size": 11, "axes.titlesize": 12, "figure.dpi": 100, "savefig.dpi": 110,
                     "font.family": "DejaVu Sans"})
STATUS = {"moved": "#e8710a", "added": "#2ca02c", "removed": "#d62728", "deformed": "#c218c2",
          "shifted": "#17a2b8", "changed in place": "#9467bd", "unchanged": "#8c8c8c"}
W, H = 900, 560


def _font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _scene(step):
    sc = load_scene(step)
    up, s, c0 = up_from_height(sc)
    return sc, up, s


def _crop(X, B, q=(3, 97), pad=0.02):
    P = (X - np.nanmedian(X, 0)) @ B.T
    lo, hi = np.nanpercentile(P, q[0], 0), np.nanpercentile(P, q[1], 0)
    return ((P > lo - pad) & (P < hi + pad)).all(1)


def _front(B, X, az=180, el=14, dist=1.25, size=(W, H), fov=50):
    tgt = np.nanmedian(X, 0) + 0.04 * B[2]
    return Camera(tgt, az, el, dist, B, fov_deg=fov, size=size)


def _trim(img, bg=255, margin=10):
    m = (np.asarray(img)[..., :3] != bg).any(2)
    ys, xs = np.nonzero(m)
    if not len(ys):
        return img
    return img[max(ys.min() - margin, 0): ys.max() + margin, max(xs.min() - margin, 0): xs.max() + margin]


# --------------------------------------------------------------------------------------------
def fig_data_gif():
    """Figure 1: the three sweeps used, side by side."""
    recs = [("scene01_light_before_a", "Light, before"), ("scene01_light_after_a", "Light, after"),
            ("scene01_dark_before_a", "Dark, before")]
    w, h = 180, 320
    font = _font(17)
    files = [sorted((FRAMES / r).glob("*.jpg")) for r, _ in recs]
    n = min(len(f) for f in files)
    frames = []
    for k in range(0, n, 4):
        canvas = Image.new("RGB", (3 * w + 20, h + 34), "white")
        for j, ((r, title), fs) in enumerate(zip(recs, files)):
            im = Image.open(fs[k]).convert("RGB").resize((w, h), Image.LANCZOS)
            canvas.paste(im, (j * (w + 10), 34))
            ImageDraw.Draw(canvas).text((j * (w + 10) + 6, 7), title, fill=(20, 20, 20), font=font)
        frames.append(canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    frames[0].save(OUT / "data.gif", save_all=True, append_images=frames[1:], duration=220, loop=0, optimize=True)


def fig_changes():
    """Figure 2: the moved and deformed objects, before and after (asset photos, light pair)."""
    pairs = [("A016_baseball_cap", "B021_baseball_cap", "Cap: moved 73 cm"),
             ("A017_cup", "B023_cup", "Mug: moved 36 cm, turned 40°"),
             ("A015_neck_pillow", "B012_neck_pillow", "Neck pillow: deformed in place")]
    fig, ax = plt.subplots(2, 3, figsize=(10.5, 6.2))
    for j, (a, b, t) in enumerate(pairs):
        for i, nm in enumerate((a, b)):
            im = Image.open(LIGHT / "assets" / f"{nm}.jpg").convert("RGB")
            ax[i, j].imshow(im)
            ax[i, j].set_xticks([]), ax[i, j].set_yticks([])
            for s in ax[i, j].spines.values():
                s.set_color(STATUS["deformed" if "pillow" in nm else "moved"]), s.set_linewidth(2.5)
        ax[0, j].set_title(t, fontsize=12)
    ax[0, 0].set_ylabel("light, before", fontsize=12)
    ax[1, 0].set_ylabel("light, after", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "changes.png")
    plt.close(fig)


def fig_cloud_views():
    """Figure 3: the light-after point cloud from four directions."""
    sc, up, s = _scene(LIGHT)
    X, C = table(sc, 3), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, X)
    k = _crop(X, B)
    views = [(215, 18, "front left"), (180, 12, "front"), (145, 18, "front right")]
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.4))
    for a, (az, el, t) in zip(ax.flat, views):
        im, _ = render(_front(B, X, az, el, 1.3), X[k], C[k], 3.0)
        a.imshow(_trim(im)), a.set_title(t), a.axis("off")
    fig.tight_layout()
    fig.savefig(OUT / "cloud_views.png")
    plt.close(fig)


def _object_cloud(step, rec, az=180, el=14, dist=1.25):
    sc, up, s = _scene(step)
    Xc = table(sc, 3 if rec == "B" else 0)
    B = basis(up, Xc)
    L = layer(sc, "objects, one colour each", rec)
    Xo, Co = table(sc, L["pts"]), rgb(L).astype(float)
    grey = (np.abs(Co - Co.mean(1, keepdims=True)).max(1) < 12)          # unassigned points are grey
    Co[grey] = [228, 228, 228]
    k = _crop(Xc, B)
    ko = _crop(Xo, B)
    cam = _front(B, Xc, az, el, dist)
    return sc, cam, B, render(cam, Xo[ko], Co[ko], 3.0)[0]


def fig_segmentation():
    """Figure 4: objects in run 6 (single-frame masks merged in 3D) vs the light pair (tracked)."""
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))
    for a, (step, t) in zip(ax, [(EARLY, "Run 6: SAM 2 masks merged in 3D (58 objects, guitar in 8 pieces)"),
                                 (LIGHT, "Final: YOLOE-seeded SAM 2 tracks (21 objects, one per thing)")]):
        _, _, _, im = _object_cloud(step, "B")
        a.imshow(_trim(im)), a.set_title(t, fontsize=11.5), a.axis("off")
    fig.tight_layout()
    fig.savefig(OUT / "segmentation.png")
    plt.close(fig)


def fig_scene_objects():
    """Figure 9: every object of the after recording, labelled with its status."""
    sc, up, s = _scene(LIGHT)
    X, C = table(sc, 3), rgb(layer(sc, "lightAa (colour)")).astype(float)
    B = basis(up, X)
    k = _crop(X, B)
    C = 0.35 * C + 0.65 * 245                                             # faded background
    cam = _front(B, X, 180, 18, 1.3, size=(1100, 680))
    objs = [o for o in sc["objects"] if o["rec"] == "B"]
    L = layer(sc, "objects, one colour each", "B")
    Xo, Co = table(sc, L["pts"]), rgb(L).astype(float)
    grey = (np.abs(Co - Co.mean(1, keepdims=True)).max(1) < 12)
    keep = ~grey & _crop(Xo, B)
    # each object's points carry its palette colour ("pal", shared by at most two objects);
    # among objects with that colour the nearest centre owns the point
    cen = np.array([o["center"] for o in objs])
    pals = np.array([[int(o["pal"][i:i + 2], 16) for i in (1, 3, 5)] for o in objs])
    Xo_k, Ck = Xo[keep], Co[keep].astype(int)
    owner = np.full(len(Xo_k), -1)
    for c in np.unique(pals, axis=0):
        cand = np.nonzero((pals == c).all(1))[0]
        sel = np.nonzero((Ck == c).all(1))[0]
        if len(sel):
            d = np.linalg.norm(Xo_k[sel, None, :] - cen[None, cand, :], axis=2)
            owner[sel] = cand[d.argmin(1)]
    ok = owner >= 0
    Xo_k, owner = Xo_k[ok], owner[ok]
    col = np.array([[int(STATUS.get(o["status"], "#8c8c8c")[i:i + 2], 16) for i in (1, 3, 5)] for o in objs],
                   float)[owner]
    Xall = np.r_[X[k], Xo_k]
    Call = np.r_[C[k], col]
    im, zb = render(cam, Xall, Call, 2.8)
    fig, ax = plt.subplots(figsize=(12, 7.4))
    ax.imshow(im), ax.axis("off")
    uo, vo, _ = cam.project(Xo_k)
    placed = []
    for j in np.argsort([np.median(vo[owner == j]) if (owner == j).any() else 0 for j in range(len(objs))]):
        o = objs[j]
        m = owner == j
        if m.sum() < 30 or o["label"] in ("sofa", "skirting board"):
            continue
        ui, vi = float(np.median(uo[m])), float(np.median(vo[m]))
        dy = -40
        while any(abs(ui - pu) < 150 and abs(vi + dy - pv) < 26 for pu, pv in placed):
            dy -= 26
        placed.append((ui, vi + dy))
        ax.annotate(f"{o['label']} ({o['status']})", (ui, vi), (ui, vi + dy), ha="center", fontsize=10,
                    color="white", bbox=dict(boxstyle="round,pad=0.25", fc=STATUS.get(o["status"], "#666"), ec="none",
                                             alpha=0.92),
                    arrowprops=dict(arrowstyle="-", color="#333", lw=0.8))
    ax.set_xlim(60, 1040), ax.set_ylim(640, 30)
    fig.tight_layout()
    fig.savefig(OUT / "scene_objects.png")
    plt.close(fig)


def fig_alignment():
    """Figure 5: dark-before and light-after clouds in one frame (after re-alignment), and the
    reprojection error through pose refinement."""
    m_d = json.loads((DARK / "metrics.json").read_text())
    m_l = json.loads((LIGHT / "metrics.json").read_text())
    sc, up, s = _scene(DARK)
    XA, XB = table(sc, 0), table(sc, 3)
    CA, CB = rgb(layer(sc, "darkBa (colour)")), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, XB)
    cam = _front(B, XB, 180, 16, 1.3, size=(640, 480))
    fig = plt.figure(figsize=(17, 4.9))
    for k, (X, C, t) in enumerate(((XA, CA, "(a) dark before"), (XB, CB, "(b) light after, same camera"))):
        a = fig.add_axes([0.005 + 0.31 * k, 0.02, 0.30, 0.86])
        kk = _crop(X, B)
        a.imshow(_trim(render(cam, X[kk], C[kk], 2.8)[0])), a.axis("off"), a.set_title(t, fontsize=12)
    b = fig.add_axes([0.68, 0.16, 0.3, 0.7])
    for m, lab, c in ((m_l, "light pair", "#e8710a"), (m_d, "dark-light pair", "#1f64c8")):
        p = m["poses"]
        y = [p["vggt"]["median_px"], p["rounds"][0]["median_px"], p["rounds"][1]["median_px"], p["final"]["median_px"]]
        b.plot(range(4), y, "o-", color=c, label=lab, lw=2)
        b.annotate(f"{y[-1]:.2f} px", (3, y[-1]), (2.55, y[-1] + (0.9 if c == "#e8710a" else -0.75)), color=c,
                   fontsize=10.5, weight="bold")
    b.set_xticks(range(4), ["VGGT", "BA round 1", "BA round 2", "+ resection,\nfinal BA"], fontsize=9.5)
    b.set_ylabel("median reprojection error (px)")
    b.set_ylim(0, 9.5), b.grid(alpha=0.3), b.legend(frameon=False)
    b.set_title("(c) camera pose refinement", fontsize=11.5)
    fig.savefig(OUT / "alignment.png")
    plt.close(fig)


def _smooth(X, v, k=16):
    """Display-only smoothing: median over each point's k nearest points (3D)."""
    from scipy.spatial import cKDTree
    _, nn = cKDTree(X).query(X, k=k)
    return np.median(v[nn], axis=1)


def fig_heatmaps():
    """Figure 8: robot heat maps of the light-after recording, one shared front view."""
    sc, up, s = _scene(LIGHT)
    Xc, Cc = table(sc, 3), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, Xc)
    cam = _front(B, Xc, 180, 20, 1.3, size=(760, 520))
    X, _ = heat_values(sc, "Height above floor")
    k = _crop(X, B)
    X = X[k]
    vals = {key: heat_values(sc, key)[1][k] for key in
            ("Height above floor", "Robot reachability", "Surface uncertainty", "Gripper approachability",
             "Interactability", "Grasp", "Push", "Place")}
    top = {a: np.percentile(vals[a][vals[a] > 0.01], 90) for a in ("Grasp", "Push", "Place")}
    for key in ("Gripper approachability", "Interactability", "Grasp", "Push", "Place", "Surface uncertainty"):
        vals[key] = _smooth(X, vals[key])
    spec = [("Height above floor", "(b) height above floor (m)", "viridis", None),
            ("Robot reachability", "(c) reach: 0 out of reach, 1 in reach", "RdYlGn", (0, 1)),
            ("Surface uncertainty", "(d) surface uncertainty (cm, 1σ)", "magma_r", None),
            ("Gripper approachability", "(e) approachability: free path × solid", "RdYlGn", None),
            ("Interactability", "(f) interactability: reach × certainty × action", "RdYlGn", None)]
    fig, ax = plt.subplots(2, 4, figsize=(19, 8.4))
    kc = _crop(Xc, B)
    im, _ = render(cam, Xc[kc], Cc[kc], 2.6)
    ax[0, 0].imshow(im[30:-20, 50:-50]), ax[0, 0].set_title("(a) colour, light after", fontsize=12)
    for a, (key, title, cmap, lim) in zip(list(ax.flat)[1:6], spec):
        v = vals[key]
        lo, hi = (np.nanpercentile(v, 2), np.nanpercentile(v, 98)) if lim is None else lim
        cm = matplotlib.colormaps[cmap]
        C = cm(np.clip((v - lo) / (hi - lo + 1e-9), 0, 1))[:, :3] * 255
        im, _ = render(cam, X, C, 2.8)
        a.imshow(im[30:-20, 50:-50]), a.set_title(title, fontsize=12)
        sm = plt.cm.ScalarMappable(cmap=cm, norm=plt.Normalize(lo, hi))
        fig.colorbar(sm, ax=a, fraction=0.035, pad=0.01).ax.tick_params(labelsize=9)
    # best action: grasp / push / place, each normalised to its own 99th percentile
    acts = [("Grasp", (30, 150, 60)), ("Push", (235, 120, 30)), ("Place", (40, 110, 210))]
    S = np.stack([vals[a] / top[a] for a, _ in acts], 1).clip(0, 1)
    best, strength = S.argmax(1), S.max(1)
    col = np.array([c for _, c in acts], float)[best]
    C = 228 + (col - 228) * np.clip((strength - 0.05) / 0.5, 0, 1)[:, None]
    im, _ = render(cam, X, C, 2.8)
    a = ax[1, 2]
    a.imshow(im[30:-20, 50:-50]), a.set_title("(g) best action (colour) and its strength", fontsize=12)
    from matplotlib.patches import Patch
    a.legend(handles=[Patch(color=np.array(c) / 255, label=n.lower()) for n, c in acts], loc="lower right",
             fontsize=10, framealpha=0.9)
    # surface shift
    Xs, v = heat_values(sc, "Surface shift")
    ks = _crop(Xs, B)
    v = _smooth(Xs[ks], v[ks])
    hi = np.nanpercentile(np.abs(v), 97)
    cm = matplotlib.colormaps["coolwarm"]
    im, _ = render(cam, Xs[ks], cm(np.clip((v + hi) / (2 * hi), 0, 1))[:, :3] * 255, 2.8)
    ax[1, 3].imshow(im[30:-20, 50:-50]), ax[1, 3].set_title("(h) surface shift vs before (cm, + receded)", fontsize=12)
    fig.colorbar(plt.cm.ScalarMappable(cmap=cm, norm=plt.Normalize(-hi, hi)), ax=ax[1, 3], fraction=0.035,
                 pad=0.01).ax.tick_params(labelsize=9)
    for a in ax.flat:
        a.axis("off")
    fig.tight_layout()
    fig.savefig(OUT / "heatmaps.png")
    plt.close(fig)


def fig_positions():
    """Figure 7: top-down map of the light pair: each object's 90% position region and the moves."""
    sc, up, s = _scene(LIGHT)
    X = table(sc, 3)
    C = rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, X)
    k = _crop(X, B)
    F = np.diag([-1.0, -1.0, 1.0])                 # turn so the camera side is at the bottom, as filmed
    P = (X[k] - np.nanmedian(X, 0)) @ B.T @ F * s
    o = np.argsort(P[:, 2])
    fig, ax = plt.subplots(figsize=(11, 5.2))
    ax.scatter(P[o, 0], P[o, 1], c=(0.3 * C[k][o] + 0.7 * 255) / 255, s=2.5, lw=0)
    c0 = np.nanmedian(X, 0)
    done, names = {}, {}
    for ob in sc["objects"]:
        if ob["label"] in ("sofa", "skirting board") or "ring" not in ob:
            continue
        R = table(sc, ob["ring"])
        Q = (R - c0) @ B.T @ F * s
        col = STATUS.get(ob["status"], "#666")
        ax.plot(Q[:, 0], Q[:, 1], color=col, lw=1.6, ls="-" if ob["rec"] == "B" else "--")
        cc = (np.asarray(ob["center"]) - c0) @ B.T @ F * s
        if ob["status"] != "unchanged":
            key = ob["label"]
            if key not in done:
                done[key] = len(done) + 1
            yl = np.nanmax(Q[:, 1]) + 0.03 if ob["rec"] == "B" else np.nanmin(Q[:, 1]) - 0.03
            ax.text(cc[0], yl, str(done[key]), ha="center", va="center", fontsize=9, color="white", weight="bold",
                    bbox=dict(boxstyle="circle,pad=0.2", fc=col, ec="none"))
            names.setdefault(done[key], (key, col))
    # moves: before centre -> after centre of the matched pairs
    pa = {o["id"]: o for o in sc["objects"]}
    for ida, idb, d in (("A16", "B21", "73 cm"), ("A17", "B23", "36 cm")):
        if ida in pa and idb in pa:
            ca = (np.asarray(pa[ida]["center"]) - c0) @ B.T @ F * s
            cb = (np.asarray(pa[idb]["center"]) - c0) @ B.T @ F * s
            ax.annotate("", cb[:2], ca[:2], arrowprops=dict(arrowstyle="-|>", color=STATUS["moved"], lw=2.2))
            mid = (ca + cb) / 2
            ax.text(mid[0], mid[1] - 0.05, d, color=STATUS["moved"], fontsize=10.5, ha="center", weight="bold",
                    bbox=dict(fc="white", ec="none", alpha=0.8, pad=1))
    ax.set_aspect("equal"), ax.set_xlabel("along the sofa (m)"), ax.set_ylabel("depth, camera side at bottom (m)")
    from matplotlib.lines import Line2D
    hs = [Line2D([], [], color=STATUS[k], lw=2, label=k) for k in ("moved", "added", "removed", "deformed", "shifted",
                                                                       "unchanged")]
    hs += [Line2D([], [], color="k", ls="--", label="before"), Line2D([], [], color="k", label="after")]
    hs += [Line2D([], [], ls="", label=f"{n}  {nm}") for n, (nm, c) in sorted(names.items())]
    ax.legend(handles=hs, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=10)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "positions.png")
    plt.close(fig)


def _asset_panel(ax, step, name, up, az=200, el=25, nodes=True):
    meta, X, C = load_asset(step, name)
    B = basis(up, X)
    c = X.mean(0)
    ext = np.ptp((X - c) @ B.T, 0).max()
    cam = Camera(c, az, el, ext * 1.9, B, size=(420, 360), fov_deg=40)
    im, zb = render(cam, X, C, 2.2)
    ax.imshow(im)
    if nodes:
        N = np.array([n["xyz"] for n in meta["nodes"]])
        u, v, z = cam.project(N)
        E = meta["edges"]
        ks = np.array([e["stiffness_n_per_m"] for e in E], float)
        for e in E:
            i, j = e["i"], e["j"]
            ax.plot([u[i], u[j]], [v[i], v[j]], color="#222", lw=0.6, alpha=0.55)
        w = np.array([n["weight"] for n in meta["nodes"]])
        ax.scatter(u, v, s=8 + 900 * w, c="#ffd23f", ec="#222", lw=0.6, zorder=3)
    ax.axis("off")
    return meta


def fig_assets():
    """Figure 10: two exported assets: photo, coloured cloud, node graph."""
    up = np.array(json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())["up"])
    fig, ax = plt.subplots(2, 3, figsize=(12, 7.2), gridspec_kw=dict(width_ratios=[0.8, 1, 1]))
    for r, nm in enumerate(("A016_baseball_cap", "A017_cup")):
        ax[r, 0].imshow(Image.open(LIGHT / "assets" / f"{nm}.jpg")), ax[r, 0].axis("off")
        _asset_panel(ax[r, 1], LIGHT, nm, up, nodes=False)
        meta = _asset_panel(ax[r, 2], LIGHT, nm, up, nodes=True)
        rc = meta["rigidity_copilot"]
        ax[r, 0].set_title(f"{meta['label']} ({meta['recording']}{meta['id']})", fontsize=11.5)
        ax[r, 1].set_title(f"exported cloud ({meta['points']:,} points)", fontsize=11.5)
        ax[r, 2].set_title(f"node graph: {len(meta['nodes'])} nodes, {len(meta['edges'])} edges\n"
                           f"E ≈ {rc['youngs_modulus_pa']:.1e} Pa ({rc['materials'][0][0]})", fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT / "assets.png")
    plt.close(fig)


def fig_asset_issues():
    """Figure 6: assets cut from views whose poses sit off (stacked or doubled surfaces)."""
    up = np.array(json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())["up"])
    items = [("A017_cup", "clean: mug, before", 200), ("B017_table_tennis_bat", "doubled: bat cushion, after", 200),
             ("A008_cushion", "stacked layers: cushion, before", 240), ("B023_cup", "fragmented: mug, after", 200)]
    fig, ax = plt.subplots(1, 4, figsize=(14, 3.9))
    for a, (nm, t, az) in zip(ax, items):
        _asset_panel(a, LIGHT, nm, up, az=az, nodes=False)
        a.set_title(t, fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT / "asset_issues.png")
    plt.close(fig)


FIGS = {"data": fig_data_gif, "changes": fig_changes, "cloud_views": fig_cloud_views, "segmentation": fig_segmentation,
        "alignment": fig_alignment, "asset_issues": fig_asset_issues, "positions": fig_positions,
        "heatmaps": fig_heatmaps, "scene_objects": fig_scene_objects, "assets": fig_assets}

if __name__ == "__main__":
    for name in sys.argv[1:] or FIGS:
        FIGS[name]()
        print("wrote", name)
