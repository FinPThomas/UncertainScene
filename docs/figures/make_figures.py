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
TURNED = {"A17", "B23"}
# names checked against the photos: the pipeline's label is kept in brackets where it was wrong
SHOWN = {"B22": "glass vase, named cup", "B23": "mug", "A17": "mug", "B17": "bat cushion, named bat",
         "B15": "card, named book"}          # the mug did not move; it was turned in place (Fin, 16 Oct)
STATUS = {"turned": "#b8860b", "moved": "#e8710a", "added": "#2ca02c", "removed": "#d62728", "deformed": "#c218c2",
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
             ("A017_cup", "B023_cup", "Mug: turned about 40° in place"),
             ("A015_neck_pillow", "B012_neck_pillow", "Neck pillow: deformed in place")]
    fig, ax = plt.subplots(2, 3, figsize=(10.5, 6.2))
    for j, (a, b, t) in enumerate(pairs):
        for i, nm in enumerate((a, b)):
            im = Image.open(LIGHT / "assets" / f"{nm}.jpg").convert("RGB")
            ax[i, j].imshow(im)
            ax[i, j].set_xticks([]), ax[i, j].set_yticks([])
            for s in ax[i, j].spines.values():
                s.set_color(STATUS["deformed" if "pillow" in nm else "turned" if "cup" in nm else "moved"]), s.set_linewidth(2.5)
        ax[0, j].set_title(t, fontsize=12)
    ax[0, 0].set_ylabel("light, before", fontsize=12)
    ax[1, 0].set_ylabel("light, after", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "changes.png")
    plt.close(fig)


def _dense_after_cloud():
    """Light-after cloud for display: the saved viewer cloud with sparse stray points hidden
    (mean distance to 8 nearest points above its 75th percentile) plus every after-sweep asset
    at full density."""
    import glob
    from scipy.spatial import cKDTree
    sc, up, s = _scene(LIGHT)
    X, C = table(sc, 3), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, X)
    k = _crop(X, B)
    X, C = X[k], C[k]
    d = cKDTree(X).query(X, k=9)[0][:, 1:].mean(1)
    keep = d < np.percentile(d, 75)
    Xs, Cs = [X[keep]], [C[keep]]
    for f in sorted(glob.glob(str(LIGHT / "assets" / "B*.ply"))):
        _, Xa, Ca = load_asset(LIGHT, Path(f).stem)
        Xs.append(Xa / s), Cs.append(Ca)
    return np.concatenate(Xs), np.concatenate(Cs), B, X


def fig_cloud_views():
    """Figure 3: the light-after point cloud from three directions."""
    Xd, Cd, B, X = _dense_after_cloud()
    views = [(215, 20, "front left"), (180, 12, "front"), (145, 20, "front right")]
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.6))
    ims = [render(_front(B, X, az, el, 1.3, size=(900, 620)), Xd, Cd, 1.8)[0] for az, el, _ in views]
    m = np.stack([(np.asarray(i)[..., :3] != 255).any(2) for i in ims]).any(0)
    ys, xs = np.nonzero(m)
    for a, im, (_, _, t) in zip(ax, ims, views):
        a.imshow(im[ys.min():ys.max() + 1, xs.min():xs.max() + 1]), a.set_title(t, fontsize=13), a.axis("off")
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
    objs = [dict(o, status="turned") if o["id"] in TURNED else o for o in sc["objects"] if o["rec"] == "B"]
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
        nm = SHOWN.get(o["id"], o["label"])
        ax.annotate(f"{nm} ({o['status']})", (ui, vi), (ui, vi + dy), ha="center", fontsize=10,
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


def fig_change_map():
    """Figure 7: (a) light pair from above, changed objects only; (b) each verdict's evidence
    against its own noise level."""
    sc, up, s = _scene(LIGHT)
    X = table(sc, 3)
    C = rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, X)
    k = _crop(X, B)
    F = np.diag([-1.0, -1.0, 1.0])                 # camera side at the bottom, left-right as filmed
    c0 = np.nanmedian(X, 0)
    P = (X[k] - c0) @ B.T @ F * s
    o = np.argsort(P[:, 2])
    fig = plt.figure(figsize=(15, 5.4))
    ax = fig.add_axes([0.03, 0.1, 0.5, 0.8])
    ax.scatter(P[o, 0], P[o, 1], c=(0.25 * C[k][o] + 0.75 * 255) / 255, s=2.5, lw=0)
    pa = {ob["id"]: ob for ob in sc["objects"]}

    def at(i):
        return (np.asarray(pa[i]["center"]) - c0) @ B.T @ F * s

    def ring(i, col, ls):
        Q = (table(sc, pa[i]["ring"]) - c0) @ B.T @ F * s
        ax.plot(Q[:, 0], Q[:, 1], color=col, lw=1.8, ls=ls)

    # cap: moved
    ring("A16", STATUS["moved"], "--"), ring("B21", STATUS["moved"], "-")
    ca, cb = at("A16"), at("B21")
    ax.annotate("", cb[:2], ca[:2], arrowprops=dict(arrowstyle="-|>", color=STATUS["moved"], lw=2.4))
    ax.text((ca[0] + cb[0]) / 2 + 0.12, 0.53, "cap moved 73 cm\n(90%: 67–79 cm)", color=STATUS["moved"],
            ha="center", fontsize=10.5, weight="bold", bbox=dict(fc="white", ec="none", alpha=0.85, pad=1.5))
    # mug: turned in place
    cm_ = at("B23")
    ring("B23", STATUS["turned"], "-")
    from matplotlib.patches import FancyArrowPatch
    ax.add_patch(FancyArrowPatch((cm_[0] + 0.07, cm_[1]), (cm_[0], cm_[1] + 0.07), connectionstyle="arc3,rad=0.5",
                                 arrowstyle="-|>", mutation_scale=14, color=STATUS["turned"], lw=2))
    ax.text(cm_[0] + 0.03, cm_[1] - 0.12, "mug turned 40 ± 2°", color=STATUS["turned"], ha="center", fontsize=10.5,
            weight="bold")
    # neck pillow: deformed in place
    ring("A15", STATUS["deformed"], "--"), ring("B12", STATUS["deformed"], "-")
    cn = at("B12")
    ax.text(cn[0] - 0.05, cn[1] - 0.33, "neck pillow deformed\n4.2 cm vs 3.0 cm noise", color=STATUS["deformed"],
            ha="center", fontsize=10.5, weight="bold")
    # shifted surfaces
    for i, lab, dx, dy in (("A8", "cushion\nsquashed", -0.17, -0.02), ("B10", "guitar shifted", 0.02, -0.17)):
        ring(i, STATUS["shifted"], "-")
        cc = at(i)
        ax.text(cc[0] + dx, cc[1] + dy, lab, color=STATUS["shifted"], ha="center", fontsize=10, weight="bold")
    ax.set_ylim(top=0.66), ax.set_aspect("equal"), ax.set_xlabel("along the sofa (m)"), ax.set_ylabel("depth, camera side at bottom (m)")
    ax.grid(alpha=0.25)
    ax.set_title("(a) Light pair from above: 90% position regions (dashed before, solid after)", fontsize=11.5)

    # (b) evidence against noise
    bx = fig.add_axes([0.63, 0.14, 0.35, 0.72])

    def thr(ra, rb, align=3.0):            # 90% noise distance from both position regions + misalignment
        return float(np.sqrt(6.25) * np.sqrt((ra / 2.5) ** 2 + (rb / 2.5) ** 2 + align ** 2))
    rows = [("cap moved (light)", 73.2, (67.0, 79.4), thr(3.36, 8.8), "cm", STATUS["moved"]),
            ("glass vase moved (dark-light)", 146, (130, 162), thr(6.56, 8.6, 2.8), "cm", STATUS["moved"]),
            ("bat cushion moved (dark-light)", 19, (17, 21), thr(5.71, 8.94, 2.8), "cm", STATUS["moved"]),
            ("mug turned (light)", 40, (38, 42), 12.0, "°", STATUS["turned"]),
            ("neck pillow shape (light)", 4.2, (4.2, 4.2), 3.0, "cm", STATUS["deformed"])]
    for j, (lab, v, (lo, hi), n, u, col) in enumerate(rows[::-1]):
        bx.plot([lo / n, hi / n], [j, j], color=col, lw=4, alpha=0.5, solid_capstyle="round")
        bx.plot(v / n, j, "o", color=col, ms=9)
        bx.text(v / n, j + 0.22, f"{v:g} {u} vs {n:.0f} {u} noise".replace(" °", "°"), ha="center", fontsize=9.5, color="#333")
    bx.axvline(1, color="#555", ls=":", lw=1.5)
    bx.text(1.04, -0.6, "noise level", fontsize=9.5, color="#555")
    bx.set_xscale("log"), bx.set_xlim(0.7, 30)
    bx.set_yticks(range(len(rows)), [r[0] for r in rows[::-1]], fontsize=10.5)
    bx.set_ylim(-0.8, len(rows) - 0.4)
    bx.set_xlabel("measured change / this pair's own 90% noise level")
    bx.grid(alpha=0.25, axis="x")
    bx.set_title("(b) Evidence against noise (bar = 90% range)", fontsize=11.5)
    fig.savefig(OUT / "change_map.png")
    plt.close(fig)


def fig_progress():
    """Figure 11: how the light pair improved over runs, and what each run cost. Values from each
    run's metrics.json / report.md (run 1: results/change/2026-10-10_scene01-heatmaps; run 2:
    2026-10-10_scene01-objects; runs 6, 10, 12: 2026-10-11_scene01-objects-fixed 1-main, 1c, 1e)."""
    runs = ["run 1\n10 Oct", "run 2\n10 Oct", "run 6\n13 Oct", "run 10\n15 Oct", "run 12\n16 Oct"]
    x = np.arange(len(runs))
    fig, ax = plt.subplots(1, 4, figsize=(17, 3.9))
    a = ax[0]
    a.bar(x, [28, 364, 364, 364, 364], color="#7a8fa6")
    a.set_title("(a) frames per sweep"), a.set_ylabel("frames")
    a = ax[1]
    v = [np.nan, 947, 58, 45, 21]
    a.bar(x, v, color="#4c72b0"), a.set_yscale("log")
    for xi, vi in zip(x, v):
        if np.isfinite(vi):
            a.text(xi, vi * 1.15, f"{vi:.0f}", ha="center", fontsize=10)
    a.text(0, 1.5, "point-level\nonly", ha="center", fontsize=9, color="#555")
    a.set_title("(b) objects found, both sweeps"), a.set_ylim(1, 3000)
    a = ax[2]
    a.plot(x[1:], [8.3, 0.66, 0.59, 1.04], "o-", color="#e8710a", lw=2, label="reprojection error (px)")
    a.plot(x[2:], [14, 12.6, 1.9], "s-", color="#1f64c8", lw=2, label="misalignment of sweeps (cm)")
    a.legend(frameon=False, fontsize=9.5, loc="upper left", bbox_to_anchor=(0.0, 1.0))
    for xi, vi in zip(x[1:], [8.3, 0.66, 0.59, 1.04]):
        a.text(xi - 0.22, vi + 0.5, f"{vi:g}", color="#e8710a", fontsize=9.5, ha="center")
    for xi, vi in zip(x[2:], [14, 12.6, 1.9]):
        a.text(xi + 0.08, vi + 0.4, f"{vi:g}", color="#1f64c8", fontsize=9.5)
    a.set_title("(c) pose error and sweep misalignment"), a.set_ylim(0, 19)
    a = ax[3]
    a.bar(x, [7.2, 40.0, 120.3, 110.1, 46.4], color="#55a868")
    a.text(4, 52, "reconstruction\ncached", ha="center", fontsize=9, color="#555")
    a.set_title("(d) Colab T4 minutes per pair"), a.set_ylabel("minutes"), a.set_ylim(0, 140)
    for a in ax:
        a.set_xticks(x, runs, fontsize=9.5), a.grid(alpha=0.25, axis="y")
    fig.tight_layout()
    fig.savefig(OUT / "progress.png")
    plt.close(fig)


def fig_uncertainty():
    """Figure 12: (a) the scale posterior's two cues; (b) stated vs observed position precision."""
    m = json.loads((LIGHT / "metrics.json").read_text())
    sc_ = m["scale_cues"]
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.3))
    a = ax[0]
    a.hist(sc_["metric_depth"]["per_frame"], bins=np.linspace(1.4, 4.2, 29), color="#9db4cf", label="metric depth, per frame (80)")
    yl = a.get_ylim()[1]
    votes = {}
    for v, lab in zip(sc_["objects"]["per_object"], sc_["objects"]["labels"]):
        votes.setdefault(round(v, 2), []).append(lab)
    for v, labs in votes.items():
        a.plot([v, v], [0, yl * 0.35], color="#2ca02c", lw=2)
        a.text(v, yl * 0.37, " + ".join(labs), rotation=90, fontsize=9, ha="center", va="bottom", color="#2ca02c")
    cmb = sc_["combined"]
    a.axvspan(*cmb["range"], color="#e8710a", alpha=0.15, label="combined, 90% range")
    a.axvline(cmb["scale"], color="#e8710a", lw=2.5, label=f"combined mode {cmb['scale']:.2f}")
    a.axvline(sc_["metric_depth"]["scale"], color="#1f64c8", ls="--", lw=1.5, label="metric depth alone")
    a.axvline(sc_["objects"]["scale"], color="#2ca02c", ls="--", lw=1.5, label="object votes alone")
    a.set_xlabel("scale (metres per scene unit)"), a.set_ylabel("frames")
    a.legend(frameon=False, fontsize=9, loc="upper right"), a.set_title("(a) Metric scale from two predictions (light pair)")
    a = ax[1]
    vc, mc = m["pos_views_curve"], m["pos_model_curve"]
    kv = sorted(int(k) for k in vc if vc[k]["objects"] >= 3)
    km = sorted(int(k) for k in mc if mc[k]["objects"] >= 3)
    a.plot(km, [mc[str(k)]["median_radius90_cm"] for k in km], "o-", color="#c44e52", lw=2,
           label="stated by the fused model")
    a.plot(kv, [vc[str(k)]["median_radius90_cm"] for k in kv], "s-", color="#4c72b0", lw=2,
           label="observed scatter of the views")
    a.set_xscale("log", base=2), a.set_yscale("log")
    a.set_xticks([1, 2, 4, 8, 16], ["1", "2", "4", "8", "16"])
    a.set_xlabel("views fused"), a.set_ylabel("median 90% radius (cm)")
    a.legend(frameon=False, fontsize=10), a.grid(alpha=0.3, which="both")
    a.set_title("(b) Position precision: stated vs observed (light pair)")
    fig.tight_layout()
    fig.savefig(OUT / "uncertainty.png")
    plt.close(fig)


def fig_properties():
    """Figure 13: (a) Rigidity Copilot per object; (b) robot action scores per object (after sweep)."""
    import glob
    metas = [json.loads(open(f).read()) for f in sorted(glob.glob(str(LIGHT / "assets" / "*.json")))]
    metas = [m for m in metas if m.get("rigidity_copilot") and m["label"] not in ("sofa", "skirting board")]
    best = {}
    for m in metas:                                   # one entry per name: the one with more points
        if m["label"] not in best or m["points"] > best[m["label"]]["points"]:
            best[m["label"]] = m
    items = sorted(best.values(), key=lambda m: m["rigidity_copilot"]["rigidity"])
    fig, ax = plt.subplots(1, 2, figsize=(15, 4.8), gridspec_kw=dict(width_ratios=[1.15, 1]))
    a = ax[0]
    names = []
    for j, m in enumerate(items):
        rc = m["rigidity_copilot"]
        lo, hi = rc["youngs_modulus_range_pa"]
        col = matplotlib.colormaps["RdYlBu_r"](rc["rigidity"])
        a.plot([lo, hi], [j, j], color=col, lw=7, alpha=0.55, solid_capstyle="butt")
        a.plot(rc["youngs_modulus_pa"], j, "o", color=col, mec="#222", ms=9)
        a.text(hi * 1.6, j, f"{rc['materials'][0][0]}  ·  rigidity {rc['rigidity']:.2f}", va="center", fontsize=9.5)
        names.append(SHOWN.get(m["recording"] + str(m["id"]), m["label"]))
    a.set_xscale("log"), a.set_xlim(1e3, 1e15)
    a.set_yticks(range(len(items)), names, fontsize=10.5)
    a.set_xlabel("Young's modulus (Pa): expected value and range")
    a.set_title("(a) Rigidity Copilot: material and stiffness from appearance"), a.grid(alpha=0.25, axis="x")
    a = ax[1]
    acts = [m for m in metas if m["recording"] == "B" and m.get("affordance")]
    acts = [m for m in acts if max(m["affordance"][k] for k in ("grasp", "push", "place_on")) > 0.05]
    acts = sorted(acts, key=lambda m: -max(m["affordance"][k] for k in ("grasp", "push", "place_on")))
    lab = {22: "glass vase B22\n(named cup)", 17: "bat cushion B17\n(named bat)", 15: "card B15\n(named book)",
           23: "mug B23"}
    y = np.arange(len(acts))
    for dk, (key, col) in enumerate((("grasp", "#2ca02c"), ("push", "#e8710a"), ("place_on", "#1f64c8"))):
        a.barh(y + (dk - 1) * 0.26, [m["affordance"][key] for m in acts], 0.26, color=col,
               label=key.replace("_on", ""))
    a.set_yticks(y, [lab.get(m["id"], f"{m['label']} B{m['id']}") for m in acts], fontsize=9.5), a.invert_yaxis()
    a.set_xlabel("mean score over the object (reach × certainty × action)"), a.legend(frameon=False)
    a.set_title("(b) What a robot could do with each object (after sweep)"), a.grid(alpha=0.25, axis="x")
    fig.tight_layout()
    fig.savefig(OUT / "properties.png")
    plt.close(fig)


def fig_asset_gif(name="A016_baseball_cap", partner="B021_baseball_cap", out="asset_cap.gif", title=None,
                  lines=None):
    """Figure 10: an exported asset turning: convex hull and node graph, with what is exported beside it."""
    from matplotlib.collections import PolyCollection
    up = np.array(json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())["up"])
    meta, X, _ = load_asset(LIGHT, name)
    V = np.asarray(meta["hull"]["vertices"])
    Fc = np.asarray(meta["hull"]["faces"])
    N = np.array([n["xyz"] for n in meta["nodes"]])
    w = np.array([n["weight"] for n in meta["nodes"]])
    E = [(e["i"], e["j"]) for e in meta["edges"]]
    B = basis(up, X)
    c = X.mean(0)
    ext = np.linalg.norm(np.ptp(V, 0))          # hull diagonal: whole asset in view at every angle
    c = V.mean(0)
    frames = []
    for az in np.arange(0, 360, 10):
        cam = Camera(c, az, 25, ext * 1.7, B, size=(440, 400), fov_deg=40)
        fig = plt.figure(figsize=(10, 4.2), dpi=80)
        ax = fig.add_axes([0.0, 0.0, 0.44, 1.0])
        u, v, z = cam.project(V)
        tris = np.stack([np.c_[u[f], v[f]] for f in Fc])
        depth = z[Fc].mean(1)
        o = np.argsort(-depth)
        ax.add_collection(PolyCollection(tris[o], facecolor=(0.55, 0.7, 0.9, 0.18), edgecolor=(0.3, 0.45, 0.65, 0.2),
                                         lw=0.3))
        un, vn, zn = cam.project(N)
        for i, j in E:
            ax.plot([un[i], un[j]], [vn[i], vn[j]], color="#333", lw=0.5, alpha=0.5)
        on = np.argsort(-zn)
        ax.scatter(un[on], vn[on], s=10 + 1100 * w[on], c="#ffb000", ec="#222", lw=0.6, zorder=3)
        ax.set_xlim(0, 440), ax.set_ylim(400, 0), ax.axis("off")
        tx = fig.add_axes([0.46, 0.0, 0.54, 1.0])
        tx.axis("off")
        tx.text(0, 0.95, title, fontsize=13, weight="bold", va="top")
        tx.text(0, 0.84, "\n".join(lines), fontsize=10.2, va="top", family="DejaVu Sans", linespacing=1.45)
        fig.canvas.draw()
        frames.append(Image.fromarray(np.asarray(fig.canvas.buffer_rgba())[..., :3]).quantize(colors=96))
        plt.close(fig)
    frames[0].save(OUT / out, save_all=True, append_images=frames[1:], duration=110, loop=0, optimize=True)


def fig_asset_gifs():
    cap = json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())
    rc = cap["rigidity_copilot"]
    capB = json.loads((LIGHT / "assets/B021_baseball_cap.json").read_text())["affordance"]
    fig_asset_gif("A016_baseball_cap", out="asset_cap.gif", title="Asset: baseball cap (A16)", lines=[
        f"Name: baseball cap, 98% sure (detector votes)",
        f"Change: moved 73 cm (90%: 67–79 cm), turned 18 ± 3°",
        f"   from next to the sofa and the cushion",
        f"   to on top of the sofa",
        f"Pose: tilted about 15° before, 10° after",
        f"Geometry: {cap['points']:,} points, {len(cap['nodes'])} nodes, {len(cap['edges'])} edges",
        f"   hull {cap['hull']['volume_m3'] * 1000:.1f} L, area {cap['hull']['area_m2']:.2f} m²",
        f"Rigidity Copilot: fabric or cloth (52%), soft foam (33%)",
        f"   rigidity {rc['rigidity']:.2f}, E ≈ {rc['youngs_modulus_pa']:.1e} Pa",
        f"   (range {rc['youngs_modulus_range_pa'][0]:.0e}–{rc['youngs_modulus_range_pa'][1]:.0e} Pa)",
        f"Actions (after): grasp {capB['grasp']:.2f}, push {capB['push']:.2f}, place {capB['place_on']:.2f}",
        f"Hull: orange nodes sized by mass share, edges carry k ≈ E·L"])
    cu = json.loads((LIGHT / "assets/B008_cushion.json").read_text())
    rc = cu["rigidity_copilot"]
    af = cu["affordance"]
    mats = ", ".join(f"{m} ({p:.0%})" for m, p in rc["materials"][:2])
    fig_asset_gif("B008_cushion", out="asset_cushion.gif", title="Asset: grey cushion (B8)", lines=[
        f"Name: cushion (detector votes)",
        f"Change: unchanged between the two light sweeps",
        f"Geometry: {cu['points']:,} points, {len(cu['nodes'])} nodes, {len(cu['edges'])} edges",
        f"   hull {cu['hull']['volume_m3'] * 1000:.0f} L",
        f"Rigidity Copilot: {mats}",
        f"   rigidity {rc['rigidity']:.2f}, E ≈ {rc['youngs_modulus_pa']:.1e} Pa",
        f"Actions: push {af['push']:.2f} (best), grasp {af['grasp']:.2f}, place {af['place_on']:.2f}",
        f"   large and soft, so pushing scores highest",
        f"Contacts: rests on the sofa"])


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
        ax[r, 0].set_title(f"{SHOWN.get(meta['recording'] + str(meta['id']), meta['label'])} ({meta['recording']}{meta['id']})", fontsize=11.5)
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
        "alignment": fig_alignment, "asset_issues": fig_asset_issues, "change_map": fig_change_map,
        "progress": fig_progress, "uncertainty": fig_uncertainty, "properties": fig_properties,
        "asset_gifs": fig_asset_gifs,
        "heatmaps": fig_heatmaps, "scene_objects": fig_scene_objects, "assets": fig_assets}

if __name__ == "__main__":
    for name in sys.argv[1:] or FIGS:
        FIGS[name]()
        print("wrote", name)
