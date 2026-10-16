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
                    render, render_aa, rgb, table, up_from_height)

OUT = Path(__file__).parent
FRAMES = ROOT / "data/frames"
plt.rcParams.update({"font.size": 11, "axes.titlesize": 12, "figure.dpi": 100, "savefig.dpi": 110,
                     "font.family": "DejaVu Sans"})
TURNED = {"A17", "B23"}
SQUASHED = {"B8"}            # the grey cushion was squashed; the pipeline saw it only as a surface shift
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
    (mean distance to 8 nearest points above its 60th percentile) plus every after-sweep asset
    at full density."""
    import glob
    from scipy.spatial import cKDTree
    sc, up, s = _scene(LIGHT)
    X, C = table(sc, 3), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, X)
    k = _crop(X, B, q=(8, 92))                                           # central region: sofa and objects
    X, C = X[k], C[k]
    d = cKDTree(X).query(X, k=9)[0][:, 1:].mean(1)
    keep = d < np.percentile(d, 60)
    Xs, Cs = [X[keep]], [C[keep]]
    for f in sorted(glob.glob(str(LIGHT / "assets" / "B*.ply"))):
        _, Xa, Ca = load_asset(LIGHT, Path(f).stem)
        Xs.append(Xa / s), Cs.append(Ca)
    return np.concatenate(Xs), np.concatenate(Cs), B, X


def fig_cloud_views():
    """Figure 3: the light-after point cloud from three directions."""
    Xd, Cd, B, X = _dense_after_cloud()
    views = [(215, 25, "front left"), (180, 22, "front"), (145, 25, "front right")]
    ims = [_fill_holes(render_aa(_front(B, X, az, el, 1.15, size=(1000, 700)), Xd, Cd, 1.7)[0])
           for az, el, _ in views]
    ims = _shared_trim(ims)
    fig, ax = plt.subplots(1, 3, figsize=(18, 4.9))
    for a, im, (_, _, t) in zip(ax, ims, views):
        a.imshow(im), a.set_title(t, fontsize=13), a.axis("off")
    fig.tight_layout(w_pad=0.3)
    fig.savefig(OUT / "cloud_views.png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def _object_cloud(step, rec, az=180, el=14, dist=1.25):
    """Objects of one recording, one colour each; the largest object (the sofa) and unassigned points
    are drawn pale grey so the objects stand out."""
    from scipy.spatial import cKDTree
    sc, up, s = _scene(step)
    Xc = table(sc, 3 if rec == "B" else 0)
    B = basis(up, Xc)
    L = layer(sc, "objects, one colour each", rec)
    Xo, Co = table(sc, L["pts"]), rgb(L).astype(float)
    ko = _crop(Xo, B)
    Xo, Co = Xo[ko], Co[ko]
    d = cKDTree(Xo).query(Xo, k=9)[0][:, 1:].mean(1)                     # hide sparse strays (display only)
    ok = d < np.percentile(d, 85)
    Xo, Co = Xo[ok], Co[ok]
    grey = (np.abs(Co - Co.mean(1, keepdims=True)).max(1) < 12)
    cols, cnt = np.unique(Co[~grey].astype(int), axis=0, return_counts=True)
    big = (Co.astype(int) == cols[cnt.argmax()]).all(1)
    Co[grey | big] = [222, 222, 222]
    cam = _front(B, Xc, az, el, dist)
    return sc, cam, B, render_aa(cam, Xo, Co, 2.2)[0]


def fig_segmentation():
    """Figure 6: objects in run 6 (single-frame masks merged in 3D) vs the light pair (tracked)."""
    ims = _shared_trim([_object_cloud(EARLY, "B")[3], _object_cloud(LIGHT, "B")[3]])
    fig, ax = plt.subplots(1, 2, figsize=(15, 5.0))
    for a, im, t in zip(ax, ims, ["(a) run 6: single-frame masks merged in 3D\n58 objects, guitar in 8 pieces",
                                  "(b) run 12: YOLOE-seeded SAM 2 video tracks\n21 objects, one per thing"]):
        a.imshow(im), a.set_title(t, fontsize=13), a.axis("off")
    fig.tight_layout(w_pad=1.5)
    fig.savefig(OUT / "segmentation.png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def fig_scene_objects():
    """Figure 11: every object of the after recording, labelled with its status in two side columns."""
    sc, up, s = _scene(LIGHT)
    X, C = table(sc, 3), rgb(layer(sc, "lightAa (colour)")).astype(float)
    B = basis(up, X)
    k = _crop(X, B)
    C = 0.3 * C + 0.7 * 245                                               # faded background
    cam = _front(B, X, 180, 18, 1.3, size=(1100, 680))
    objs = [dict(o, status="turned") if o["id"] in TURNED else dict(o, status="deformed") if o["id"] in SQUASHED
            else o for o in sc["objects"] if o["rec"] == "B"]
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
    im, _ = render_aa(cam, np.r_[X[k], Xo_k], np.r_[C[k], col], 2.4)
    nz = np.nonzero((im < 250).any(2))
    y0, y1, x0, x1 = nz[0].min(), nz[0].max(), nz[1].min(), nz[1].max()
    fig, ax = plt.subplots(figsize=(14, 7.2))
    ax.imshow(im), ax.axis("off")
    uo, vo, _ = cam.project(Xo_k)
    items = []
    for j, o in enumerate(objs):
        m = owner == j
        if m.sum() < 30 or o["label"] in ("sofa", "skirting board"):
            continue
        items.append((float(np.median(uo[m])), float(np.median(vo[m])), o))
    mid = (x0 + x1) / 2
    for side, sel in (("left", [t for t in items if t[0] < mid]), ("right", [t for t in items if t[0] >= mid])):
        sel = sorted(sel, key=lambda t: t[1])
        ys = np.linspace(y0 + 30, y1 - 30, len(sel)) if len(sel) > 1 else [np.mean([y0, y1])]
        xl = x0 - 40 if side == "left" else x1 + 40
        for (ui, vi, o), yl in zip(sel, ys):
            nm = SHOWN.get(o["id"], o["label"])
            st = "deformed (squashed)" if o["id"] in SQUASHED else o["status"]
            ax.annotate(f"{nm}: {st}", (ui, vi), (xl, yl), ha="right" if side == "left" else "left",
                        va="center", fontsize=11, color="white",
                        bbox=dict(boxstyle="round,pad=0.3", fc=STATUS.get(o["status"], "#666"), ec="none"),
                        arrowprops=dict(arrowstyle="-", color="#444", lw=0.9, shrinkA=0, shrinkB=2))
            ax.plot(ui, vi, "o", ms=4.5, color="#222")
    ax.set_xlim(x0 - 330, x1 + 330), ax.set_ylim(y1 + 10, y0 - 10)
    fig.savefig(OUT / "scene_objects.png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def fig_alignment():
    """Figure 4: dark-before and light-after clouds in one frame (after re-alignment), and the
    reprojection error through pose refinement."""
    m_d = json.loads((DARK / "metrics.json").read_text())
    m_l = json.loads((LIGHT / "metrics.json").read_text())
    sc, up, s = _scene(DARK)
    XA, XB = table(sc, 0), table(sc, 3)
    CA, CB = rgb(layer(sc, "darkBa (colour)")), rgb(layer(sc, "lightAa (colour)"))
    B = basis(up, XB)
    cam = _front(B, XB, 180, 16, 1.3, size=(760, 560))
    ims = []
    for X, C in ((XA, CA), (XB, CB)):
        kk = _crop(X, B)
        ims.append(render_aa(cam, X[kk], C[kk], 2.0)[0])
    ims = _shared_trim(ims)
    fig = plt.figure(figsize=(17, 4.6))
    gs = fig.add_gridspec(1, 3, width_ratios=[1, 1, 0.85], wspace=0.08)
    for k, (im, t) in enumerate(zip(ims, ("(a) dark before", "(b) light after, same virtual camera"))):
        a = fig.add_subplot(gs[k])
        a.imshow(im), a.axis("off"), a.set_title(t, fontsize=12.5)
    b = fig.add_subplot(gs[2])
    steps = ["VGGT", "adjustment\nround 1", "adjustment\nround 2", "resection +\nfinal round"]
    for m, lab, c in ((m_l, "light pair", "#e8710a"), (m_d, "dark-light pair", "#1f64c8")):
        p = m["poses"]
        y = [p["vggt"]["median_px"], p["rounds"][0]["median_px"], p["rounds"][1]["median_px"], p["final"]["median_px"]]
        b.plot(range(4), y, "o-", color=c, label=lab, lw=2.2, ms=7)
        b.text(3.12, y[-1] + (0.3 if lab == "light pair" else -0.3), f"{y[-1]:.2f} px", color=c, fontsize=11,
               weight="bold", va="center")
    b.set_xticks(range(4), steps, fontsize=10)
    b.set_xlim(-0.2, 3.75), b.set_ylim(0, 9.5)
    b.set_ylabel("median reprojection error (px)")
    b.legend(frameon=False, loc="upper right")
    b.set_title("(c) camera pose refinement", fontsize=12.5)
    _clean(b)
    fig.savefig(OUT / "alignment.png", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def _smooth(X, v, k=16):
    """Display-only smoothing: median over each point's k nearest points (3D)."""
    from scipy.spatial import cKDTree
    _, nn = cKDTree(X).query(X, k=k)
    return np.median(v[nn], axis=1)


def fig_heatmaps():
    """Figure 10: robot heat maps of the light-after recording, one shared front view."""
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
    for key in ("Interactability", "Grasp", "Push", "Place", "Surface uncertainty"):
        vals[key] = _smooth(X, vals[key])
    vals["Gripper approachability"] = _smooth(X, vals["Gripper approachability"], 40)     # speckled: wider
    r = 2.1
    panels = []                                                           # (image, title, colour bar or None)
    kc = _crop(Xc, B)
    panels.append((render_aa(cam, Xc[kc], Cc[kc], r)[0], "(a) colour, light after", None))
    spec = [("Height above floor", "(b) height above floor (m)", "viridis", None),
            ("Robot reachability", "(c) reach (1 = in reach)", "RdYlGn", (0, 1)),
            ("Surface uncertainty", "(d) surface uncertainty (cm, 1σ)", "magma_r", None),
            ("Gripper approachability", "(e) approachability", "YlGn", None),
            ("Interactability", "(f) interactability", "YlGn", None)]
    for key, title, cmap, lim in spec:
        v = vals[key]
        lo, hi = (np.nanpercentile(v, 2), np.nanpercentile(v, 98)) if lim is None else lim
        cm = matplotlib.colormaps[cmap]
        t = np.clip((v - lo) / (hi - lo + 1e-9), 0, 1)
        if cmap == "YlGn":
            t = 0.08 + 0.92 * t                                           # zero stays pale, not white
        C = cm(t)[:, :3] * 255
        panels.append((render_aa(cam, X, C, r)[0], title, (cm, lo, hi, cmap == "YlGn")))
    acts = [("Grasp", (30, 150, 60)), ("Push", (235, 120, 30)), ("Place", (40, 110, 210))]
    S = np.stack([vals[a] / top[a] for a, _ in acts], 1).clip(0, 1)
    best, strength = S.argmax(1), S.max(1)
    col = np.array([c for _, c in acts], float)[best]
    C = 232 + (col - 232) * np.clip((strength - 0.05) / 0.5, 0, 1)[:, None]
    panels.append((render_aa(cam, X, C, r)[0], "(g) best action, shade = strength", "acts"))
    Xs, v = heat_values(sc, "Surface shift")
    ks = _crop(Xs, B)
    v = _smooth(Xs[ks], v[ks])
    hi = np.nanpercentile(np.abs(v), 97)
    cm = matplotlib.colormaps["coolwarm"]
    panels.append((render_aa(cam, Xs[ks], cm(np.clip((v + hi) / (2 * hi), 0, 1))[:, :3] * 255, r)[0],
                   "(h) surface shift vs before (cm)", (cm, -hi, hi, False)))
    ims = _shared_trim([p[0] for p in panels])
    fig, ax = plt.subplots(2, 4, figsize=(19, 7.6))
    from matplotlib.patches import Patch
    for a, im, (_, title, cb) in zip(ax.flat, ims, panels):
        a.imshow(im), a.set_title(title, fontsize=12.5), a.axis("off")
        if cb is None or cb == "acts":                                  # same panel size as the others
            c = fig.colorbar(plt.cm.ScalarMappable(cmap="Greys"), ax=a, orientation="horizontal", fraction=0.05,
                             pad=0.02, aspect=30)
            c.ax.set_visible(False)
        if cb == "acts":
            a.legend(handles=[Patch(color=np.array(c) / 255, label=n.lower()) for n, c in acts], loc="lower center",
                     ncol=3, fontsize=10.5, frameon=False, bbox_to_anchor=(0.5, -0.12))
        elif cb is not None:
            cmap, lo, hi, pale = cb
            norm = plt.Normalize(lo, hi)
            if pale:
                from matplotlib.colors import ListedColormap
                cmap = ListedColormap(cmap(np.linspace(0.08, 1, 256)))
            c = fig.colorbar(plt.cm.ScalarMappable(cmap=cmap, norm=norm), ax=a, orientation="horizontal",
                             fraction=0.05, pad=0.02, aspect=30)
            c.ax.tick_params(labelsize=9.5)
    fig.tight_layout(h_pad=2.6, w_pad=0.6)
    fig.savefig(OUT / "heatmaps.png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def fig_change_map():
    """Figure 8: (a) light pair from above, changed objects only; (b) each verdict's evidence
    against its own noise level."""
    from matplotlib.patches import Ellipse, FancyArrowPatch
    sc, up, s = _scene(LIGHT)
    X = table(sc, 3)
    B = basis(up, X)
    k = _crop(X, B)
    F = np.diag([-1.0, -1.0, 1.0])                 # camera side at the bottom, left-right as filmed
    c0 = np.nanmedian(X, 0)
    P = (X[k] - c0) @ B.T @ F * s
    fig = plt.figure(figsize=(16, 5.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.35, 1], wspace=0.42)
    ax = fig.add_subplot(gs[0])
    Cp = rgb(layer(sc, "lightAa (colour)"))[k] / 255.0
    o = np.argsort(P[:, 2])                                              # higher points drawn on top
    ax.scatter(P[o, 0], P[o, 1], c=0.4 * Cp[o] + 0.6, s=2.2, lw=0, rasterized=True)
    pa = {ob["id"]: ob for ob in sc["objects"]}

    def at(i):
        return ((np.asarray(pa[i]["center"]) - c0) @ B.T @ F * s)[:2]

    def region(i, col, before):
        """The object's saved 90% region outline, drawn as the ellipse with the same spread."""
        Q = ((table(sc, pa[i]["ring"]) - c0) @ B.T @ F * s)[:, :2]
        cut = np.nonzero(np.isnan(Q).any(1))[0]                          # rings are NaN-separated circles;
        segs = [g[~np.isnan(g).any(1)] for g in np.split(Q, cut)]        # the horizontal one is widest from above
        Q = max((g for g in segs if len(g) > 4), key=lambda g: np.prod(np.ptp(g, 0)))
        w, V = np.linalg.eigh(np.cov(Q.T))
        a2 = 2 * np.sqrt(np.maximum(w, 1e-8) * 2)                       # points on an ellipse: var = a^2 / 2
        ang = np.degrees(np.arctan2(V[1, 1], V[0, 1]))
        ax.add_patch(Ellipse(Q.mean(0), a2[1], a2[0], angle=ang, fill=not before, fc=col, alpha=0.18 if not before
                             else 1, ec=col, lw=2, ls="--" if before else "-"))
        if not before:
            ax.add_patch(Ellipse(Q.mean(0), a2[1], a2[0], angle=ang, fill=False, ec=col, lw=2))

    def label(xy, text, col, ha="center"):
        ax.text(*xy, text, color=col, ha=ha, va="center", fontsize=11, weight="bold",
                bbox=dict(fc="white", ec="none", alpha=0.9, pad=1.5))

    region("A16", STATUS["moved"], True), region("B21", STATUS["moved"], False)
    ca, cb = at("A16"), at("B21")
    ax.add_patch(FancyArrowPatch(ca, cb, arrowstyle="-|>", mutation_scale=22, color=STATUS["moved"], lw=2.6,
                                 shrinkA=4, shrinkB=4))
    label(((ca[0] + cb[0]) / 2 + 0.1, 0.58), "cap moved 73 cm\n(90%: 67–79 cm)", STATUS["moved"])
    region("B23", STATUS["turned"], False)
    cm_ = at("B23")
    ax.add_patch(FancyArrowPatch((cm_[0] + 0.08, cm_[1] - 0.02), (cm_[0] - 0.02, cm_[1] + 0.08),
                                 connectionstyle="arc3,rad=0.55", arrowstyle="-|>", mutation_scale=16,
                                 color=STATUS["turned"], lw=2.2))
    label((cm_[0] + 0.1, cm_[1] - 0.2), "mug turned\n40 ± 2° in place", STATUS["turned"])
    region("A15", STATUS["deformed"], True), region("B12", STATUS["deformed"], False)
    cn = at("B12")
    label((cn[0], cn[1] - 0.3), "neck pillow deformed\n4.2 cm vs 3.0 cm noise", STATUS["deformed"])
    for i, lab, d in (("A8", "cushion squashed", (-0.25, 0.0)), ("B10", "guitar shifted", (-0.2, -0.13))):
        region(i, STATUS["shifted"], False)
        cc = at(i)
        label((cc[0] + d[0], cc[1] + d[1]), lab, STATUS["shifted"])
    ax.set_aspect("equal")
    ax.set_xlim(P[:, 0].min() - 0.05, P[:, 0].max() + 0.05), ax.set_ylim(P[:, 1].min() - 0.05, 0.7)
    ax.set_xlabel("along the sofa (m)"), ax.set_ylabel("depth (m), camera side at the bottom")
    _clean(ax, None)
    ax.set_title("(a) Light pair from above: 90% regions, dashed before, filled after", fontsize=12.5)

    bx = fig.add_subplot(gs[1])

    def thr(ra, rb, align=3.0):            # 90% noise distance from both position regions + misalignment
        return float(2.5 * np.sqrt((ra / 2.5) ** 2 + (rb / 2.5) ** 2 + align ** 2))
    rows = [("cap moved", "light pair", 73.2, (67.0, 79.4), thr(3.36, 8.8), "cm", STATUS["moved"]),
            ("glass vase moved", "dark-light pair", 146, (130, 162), thr(6.56, 8.6, 2.8), "cm", STATUS["moved"]),
            ("mug turned", "light pair", 40, (38, 42), 12.0, "°", STATUS["turned"]),
            ("bat cushion moved", "dark-light pair", 19, (17, 21), thr(5.71, 8.94, 2.8), "cm", STATUS["moved"]),
            ("neck pillow deformed", "light pair", 4.2, None, 3.0, "cm", STATUS["deformed"])]
    rows = rows[::-1]
    bx.axvspan(0, 1, color="#eeeeee", zorder=0)
    for j, (lab, pair, v, rng, n, u, col) in enumerate(rows):
        r = v / n
        bx.barh(j, r, 0.55, color=col, alpha=0.85)
        if rng:
            bx.plot([rng[0] / n, rng[1] / n], [j, j], color="#222", lw=1.6)
        sp = "" if u == "°" else " "
        bx.text(max(r, rng[1] / n if rng else r) + 0.3, j, f"{r:.1f}x   ({v:g}{sp}{u} vs {n:.0f}{sp}{u})", va="center", fontsize=10.5)
    bx.set_yticks(range(len(rows)), [f"{r[0]}\n{r[1]}" for r in rows], fontsize=10.5)
    bx.set_xlim(0, 18.5), bx.set_ylim(-0.6, len(rows) - 0.4)
    bx.set_xlabel("change ÷ the pair's own 90% noise level (grey band: within noise)")
    bx.set_title("(b) How far each change stands above noise", fontsize=12.5)
    _clean(bx, "x")
    fig.canvas.draw()
    pa_, pb_ = ax.get_position(), bx.get_position()                      # same height as the equal-aspect map
    bx.set_position([pb_.x0, pa_.y0, pb_.width, pa_.height])
    fig.savefig(OUT / "change_map.png", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def fig_progress():
    """Figure 14: how the light pair improved over runs, and what each run cost. Values from each
    run's metrics.json / report.md (run 1: results/change/2026-10-10_scene01-heatmaps; run 2:
    2026-10-10_scene01-objects; runs 6, 10, 12: 2026-10-11_scene01-objects-fixed 1-main, 1c, 1e)."""
    runs = ["run 1\n10 Oct", "run 2\n10 Oct", "run 6\n13 Oct", "run 10\n15 Oct", "run 12\n16 Oct"]
    added = ["point-level change,\n28 frames per sweep", "all 364 frames,\nper-frame masks",
             "bundle adjustment,\nmasks merged in 3D", "YOLOE-seeded\nSAM 2 tracks",
             "resection, re-alignment\non unchanged objects"]
    x = np.arange(len(runs))
    fig, (a, b) = plt.subplots(2, 1, figsize=(13, 8.4), sharex=True, gridspec_kw=dict(height_ratios=[1.25, 1]))
    series = [("objects found (fragmentation)", [np.nan, 947, 58, 45, 21], "#4c72b0", "o", "{:.0f}", [(-12, -16)] * 5),
              ("reprojection error", [np.nan, 8.3, 0.66, 0.59, 1.04], "#e8710a", "s", "{:g} px",
               [None, (10, -15), (10, 7), (10, 7), (10, -15)]),
              ("misalignment between sweeps", [np.nan, np.nan, 14, 12.6, 1.9], "#1f9e89", "D", "{:g} cm", [(10, 8)] * 5)]
    for lab, v, col, mk, fmt, off in series:
        v = np.array(v, float)
        first = v[np.isfinite(v)][0]
        y = 100 * v / first
        a.plot(x, y, marker=mk, color=col, lw=2.6, ms=9, label=f"{lab}, first measured = 100%")
        for xi, yi, vi, o in zip(x, y, v, off):
            if np.isfinite(vi):
                a.annotate(fmt.format(vi), (xi, yi), o, textcoords="offset points", fontsize=10.5, color=col,
                           ha="right" if o[0] < 0 else "left")
    a.set_yscale("log"), a.set_ylim(1, 250)
    a.set_yticks([1, 2, 5, 10, 20, 50, 100], ["1%", "2%", "5%", "10%", "20%", "50%", "100%"])
    a.set_ylabel("remaining error\n(% of first measurement, log)")
    a.legend(frameon=False, fontsize=10.5, loc="lower left")
    a.set_title("(a) Light pair: each error measure fell 7–45x across the runs (lower is better)", fontsize=13)
    _clean(a)
    v = [7.2, 40.0, 120.3, 110.1, 46.4]
    bars = b.bar(x, v, 0.55, color=["#7fbf8e"] * 4 + ["#c8e6cf"])
    for xi, vi, t in zip(x, v, added):
        b.text(xi, vi + 3, f"{vi:.0f} min", ha="center", fontsize=11, weight="bold")
        b.text(xi, vi + 14, f"+ {t}", ha="center", va="bottom", fontsize=9.8, color="#333")
    b.text(4, 4, "reconstruction\nreused from run 10", ha="center", va="bottom", fontsize=9.5, color="#555")
    b.set_ylim(0, 175), b.set_ylabel("Colab T4 minutes for the pair")
    b.set_title("(b) Compute spent per run, and what each run added", fontsize=13)
    b.set_xticks(x, runs, fontsize=11), b.set_xlim(-0.6, 4.6)
    _clean(b)
    fig.tight_layout(h_pad=1.5)
    fig.savefig(OUT / "progress.png", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def fig_uncertainty():
    """Figure 7: (a) how the two scale predictions combine; (b) stated vs observed position precision."""
    m = json.loads((LIGHT / "metrics.json").read_text())
    sc_ = m["scale_cues"]
    fig, (a, b) = plt.subplots(1, 2, figsize=(16, 5.0), gridspec_kw=dict(width_ratios=[1.2, 1], wspace=0.28))
    s = np.linspace(1.4, 4.9, 700)

    def belief(c):
        z = (np.log(s) - np.log(c["scale"])) / c["sd_log"]
        p = np.exp(-0.5 * z ** 2) / s
        return p / p.max()
    md, ob, cb = sc_["metric_depth"], sc_["objects"], sc_["combined"]
    a.fill_between(s, belief(md), color="#1f64c8", alpha=0.12)
    a.plot(s, belief(md), color="#1f64c8", lw=2.2, label=f"metric depth model\n(80 frames): {md['scale']:.2f}")
    a.fill_between(s, belief(ob), color="#2ca02c", alpha=0.12)
    a.plot(s, belief(ob), color="#2ca02c", lw=2.2, label=f"typical sizes of\n4 named objects: {ob['scale']:.2f}")
    a.plot(s, belief(cb), color="#e8710a", lw=3.2, label=f"combined:\n{cb['scale']:.2f} m per unit")
    lo, hi = cb["range"]
    a.errorbar(cb["scale"], 1.05, xerr=[[cb["scale"] - lo], [hi - cb["scale"]]], fmt="none", ecolor="#e8710a",
               elinewidth=2, capsize=5)
    a.text(lo - 0.03, 1.05, f"90%: {lo:.2f}–{hi:.2f}", ha="right", va="center", fontsize=10.5, color="#e8710a",
           weight="bold")
    a.plot(md["per_frame"], np.full(len(md["per_frame"]), -0.05), "|", color="#1f64c8", ms=9, alpha=0.6)
    for v in ob["per_object"]:
        a.plot(v, -0.12, "v", color="#2ca02c", ms=8)
    a.text(1.42, -0.05, "frames", fontsize=9.5, color="#1f64c8", va="center")
    a.text(1.42, -0.12, "objects", fontsize=9.5, color="#2ca02c", va="center")
    a.set_ylim(-0.18, 1.12), a.set_xlim(1.4, 4.9), a.set_yticks([])
    a.set_xlabel("metres per scene unit (the reconstruction's unknown scale)")
    a.set_ylabel("relative belief")
    a.legend(frameon=False, fontsize=10.5, loc="upper right", bbox_to_anchor=(1.02, 0.85))
    a.set_title("(a) Metric scale: two independent predictions, combined", fontsize=13)
    _clean(a, None)
    vc, mc = m["pos_views_curve"], m["pos_model_curve"]
    ks = [1, 2, 4]
    ob_ = [vc[str(k)]["median_radius90_cm"] for k in ks]
    st_ = [mc[str(k)]["median_radius90_cm"] for k in ks]
    xx = np.arange(len(ks))
    b.bar(xx - 0.2, ob_, 0.38, color="#4c72b0", label="observed: how far repeated views of an object scatter")
    b.bar(xx + 0.2, st_, 0.38, color="#c44e52", label="stated: what the fused per-view covariances claim")
    for xi, o_, s_ in zip(xx, ob_, st_):
        b.text(xi - 0.2, o_ * 1.12, f"{o_:.1f} cm", ha="center", fontsize=10.5)
        b.text(xi + 0.2, s_ * 1.12, f"{s_:.2f} cm", ha="center", fontsize=10.5)
        b.text(xi, 30, f"stated\n{o_ / s_:.0f}x too small", ha="center", fontsize=11, weight="bold",
               color="#c44e52")
    b.set_yscale("log"), b.set_ylim(0.05, 600)
    b.set_xticks(xx, [f"{k} view" + ("s" if k > 1 else "") + " fused" for k in ks], fontsize=11)
    b.set_xlim(-0.6, 2.6)
    b.set_ylabel("median 90% position radius (cm, log)")
    b.legend(frameon=False, fontsize=10, loc="upper center")
    b.set_title("(b) Is the stated position uncertainty honest? Not yet", fontsize=13)
    _clean(b)
    fig.savefig(OUT / "uncertainty.png", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def fig_properties():
    """Figure 9: (a) Rigidity Copilot per object; (b) best robot action per object (after sweep)."""
    import glob
    from matplotlib.transforms import blended_transform_factory
    metas = [json.loads(open(f).read()) for f in sorted(glob.glob(str(LIGHT / "assets" / "*.json")))]
    metas = [m for m in metas if m.get("rigidity_copilot") and m["label"] not in ("sofa", "skirting board")]
    best = {}
    for m in metas:                                   # one entry per name: the one with more points
        if m["label"] not in best or m["points"] > best[m["label"]]["points"]:
            best[m["label"]] = m
    items = sorted(best.values(), key=lambda m: m["rigidity_copilot"]["rigidity"])
    fig = plt.figure(figsize=(18, 5.2))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.25, 1], wspace=0.95)
    a = fig.add_subplot(gs[0])
    tr = blended_transform_factory(a.transAxes, a.transData)
    cmap = matplotlib.colormaps["coolwarm"]
    for j, m in enumerate(items):
        rc = m["rigidity_copilot"]
        lo, hi = rc["youngs_modulus_range_pa"]
        col = cmap(rc["rigidity"])
        a.plot([lo, hi], [j, j], color="#c9c9c9", lw=8, solid_capstyle="butt")
        a.plot(rc["youngs_modulus_pa"], j, "o", color=col, mec="#222", ms=10)
        wrong = {"mug": "metal (is ceramic)"}.get(SHOWN.get(m["recording"] + str(m["id"]), ""))
        a.text(1.03, j, wrong or rc["materials"][0][0], transform=tr, va="center", fontsize=10.5,
               color="#c0392b" if wrong else "black")
        a.text(1.4, j, f"{rc['rigidity']:.2f}", transform=tr, va="center", ha="right", fontsize=10.5,
               weight="bold")
    a.text(1.03, len(items) - 0.35, "top material", transform=tr, fontsize=10.5, color="#555")
    a.text(1.4, len(items) - 0.35, "rigidity", transform=tr, fontsize=10.5, color="#555", ha="right")
    a.set_xscale("log"), a.set_xlim(3e3, 3e12), a.set_ylim(-0.6, len(items) - 0.1)
    a.set_yticks(range(len(items)), [SHOWN.get(m["recording"] + str(m["id"]), m["label"]) for m in items],
                 fontsize=11)
    a.set_xlabel("Young's modulus (Pa): expected value (dot, coloured by rigidity) and range (bar)")
    a.set_title("(a) Rigidity Copilot: stiffness from appearance, soft to rigid", fontsize=12.5)
    _clean(a, "x")
    b = fig.add_subplot(gs[1])
    acts = [m for m in metas if m["recording"] == "B" and m.get("affordance")]
    keys = (("grasp", "#2ca02c"), ("push", "#e8710a"), ("place_on", "#1f64c8"))
    acts = [m for m in acts if max(m["affordance"][k] for k, _ in keys) > 0.05]
    acts = sorted(acts, key=lambda m: -max(m["affordance"][k] for k, _ in keys))
    for j, m in enumerate(acts):
        sc_ = [m["affordance"][k] for k, _ in keys]
        i = int(np.argmax(sc_))
        b.barh(j, sc_[i], 0.62, color=keys[i][1])
        b.text(sc_[i] + 0.012, j, f"{sc_[i]:.2f} {keys[i][0].replace('_on', '')}", va="center", fontsize=10.5)
    nlab = [m["label"] for m in acts]
    b.set_yticks(range(len(acts)), [SHOWN.get("B" + str(m["id"]), m["label"] + (f" B{m['id']}" if nlab.count(m["label"])
                                                                                > 1 else "")) for m in acts], fontsize=11)
    b.invert_yaxis(), b.set_xlim(0, 1.05)
    b.set_xlabel("best action score (reach × certainty × action, mean over the object)")
    from matplotlib.patches import Patch
    b.legend(handles=[Patch(color=c, label=k.replace("_on", "")) for k, c in keys], frameon=False,
             loc="lower right", fontsize=10.5)
    b.set_title("(b) Best robot action per object, after sweep", fontsize=12.5)
    _clean(b, "x")
    fig.savefig(OUT / "properties.png", bbox_inches="tight", pad_inches=0.08)
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


def _asset_panel(ax, step, name, up, az=200, el=25, nodes=True, zoom=1.9):
    meta, X, C = load_asset(step, name)
    B = basis(up, X)
    c = X.mean(0)
    ext = np.ptp((X - c) @ B.T, 0).max()
    cam = Camera(c, az, el, ext * zoom, B, size=(420, 360), fov_deg=40)
    im, zb = render_aa(cam, X, C, 2.0)
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
    """Figure 13: what exported assets still get wrong: photo crop against the exported cloud."""
    up = np.array(json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())["up"])
    items = [("A017_cup", "mug (A17)", 200,
              "only the wall facing the camera;\nhandle missing. Copilot: metal\n(ceramic), rigidity 0.92 still right"),
             ("B022_cup", "glass vase (B22, named cup)", 200,
              "transparent glass gives no\nreliable depth: a flat sheet\ntinted by what lies behind"),
             ("B021_baseball_cap", "cap after the move (B21)", 200,
              "split into pieces: views from\noff-pose frames cut away as\nghost copies"),
             ("B012_neck_pillow", "neck pillow, after (B12)", 200,
              "scattered pieces: soft, dark,\nlittle texture to match\nbetween views")]
    fig, ax = plt.subplots(2, 4, figsize=(17, 8.2), gridspec_kw=dict(height_ratios=[1, 1.15]))
    for j, (nm, t, az, note) in enumerate(items):
        ax[0, j].imshow(Image.open(LIGHT / "assets" / f"{nm}.jpg")), ax[0, j].axis("off")
        ax[0, j].set_title(t, fontsize=12.5)
        meta = _asset_panel(ax[1, j], LIGHT, nm, up, az=az, nodes=False)
        ax[1, j].set_title(f"exported cloud, {meta['points']:,} points", fontsize=11, color="#555")
        ax[1, j].text(0.5, -0.04, note, transform=ax[1, j].transAxes, ha="center", va="top", fontsize=11,
                      color="#c0392b")
    fig.tight_layout(h_pad=1.0, w_pad=0.8)
    fig.savefig(OUT / "assets.png", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def fig_asset_issues():
    """Figure 5: a clean asset and assets cut from views whose poses sit off."""
    up = np.array(json.loads((LIGHT / "assets/A016_baseball_cap.json").read_text())["up"])
    items = [("A016_baseball_cap", "clean: cap, before", 200, "#2ca02c", 1.9),
             ("B017_table_tennis_bat", "doubled: bat cushion, after", 200, "#d62728", 1.9),
             ("A008_cushion", "stacked layers: cushion, before", 240, "#d62728", 1.9),
             ("B023_cup", "fragmented: mug, after", 200, "#d62728", 2.25)]
    fig, ax = plt.subplots(1, 4, figsize=(16, 4.0))
    for a, (nm, t, az, col, zm) in zip(ax, items):
        _asset_panel(a, LIGHT, nm, up, az=az, nodes=False, zoom=zm)
        a.set_title(t, fontsize=12.5, color=col)
    fig.tight_layout(w_pad=0.5)
    fig.savefig(OUT / "asset_issues.png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def _clean(ax, grid="y"):
    """Shared chart style: no top/right frame, light grid on one axis."""
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    if grid:
        ax.grid(alpha=0.25, axis=grid)
        ax.set_axisbelow(True)


def _shared_trim(ims, bg=255, margin=8):
    """Crop several renders to one common box around their non-background pixels."""
    m = np.stack([(np.asarray(i)[..., :3] < bg - 3).any(2) for i in ims]).any(0)
    ys, xs = np.nonzero(m)
    y0, y1 = max(ys.min() - margin, 0), ys.max() + margin
    x0, x1 = max(xs.min() - margin, 0), xs.max() + margin
    return [np.asarray(i)[y0:y1, x0:x1] for i in ims]


def _fill_holes(im, bg=255, rounds=2, need=5):
    """Display-only: a background pixel with at least `need` of its 8 neighbours covered takes their
    mean colour (closes pinholes between splats, never grows the outline by more than `rounds` px)."""
    im = np.asarray(im, float).copy()
    for _ in range(rounds):
        empty = (im >= bg - 1).all(2)
        acc = np.zeros_like(im)
        cnt = np.zeros(im.shape[:2])
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == dx == 0:
                    continue
                sh = np.roll(np.roll(im, dy, 0), dx, 1)
                ok = ~np.roll(np.roll(empty, dy, 0), dx, 1)
                acc += sh * ok[..., None]
                cnt += ok
        fill = empty & (cnt >= need)
        im[fill] = acc[fill] / cnt[fill][:, None]
    return im.clip(0, 255).astype(np.uint8)


FIGS = {"data": fig_data_gif, "changes": fig_changes, "cloud_views": fig_cloud_views, "segmentation": fig_segmentation,
        "alignment": fig_alignment, "asset_issues": fig_asset_issues, "change_map": fig_change_map,
        "progress": fig_progress, "uncertainty": fig_uncertainty, "properties": fig_properties,
        "asset_gifs": fig_asset_gifs,
        "heatmaps": fig_heatmaps, "scene_objects": fig_scene_objects, "assets": fig_assets}

if __name__ == "__main__":
    for name in sys.argv[1:] or FIGS:
        FIGS[name]()
        print("wrote", name)
