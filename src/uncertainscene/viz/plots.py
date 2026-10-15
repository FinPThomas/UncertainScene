"""Static figures for every run: change map, frame overlays, calibration histograms, crops.

All functions write a PNG and return its path. Colours are fixed across the project:
unchanged grey, removed red, added green, moved blue arrows, appearance orange, unknown light blue.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from ..change.detect import APPEARANCE, GONE, UNCHANGED, UNKNOWN, ChangeResult  # noqa: E402
from ..change.geometry import cam_centers, project, view_axes  # noqa: E402
from ..recon.frames import Recon  # noqa: E402

COL = {"unchanged": "#9e9e9e", "removed": "#d62728", "added": "#2ca02c", "moved": "#1f77b4",
       "appearance": "#ff7f0e", "unknown": "#9ecae1"}


def _sub(n: int, k: int, rng) -> np.ndarray:
    return rng.choice(n, min(n, k), replace=False) if n else np.zeros(0, int)


def change_map(res: ChangeResult, rec: Recon, objs: list, out: Path, title: str = "", max_bg: int = 60000) -> Path:
    """Top-down and front views of both clouds, coloured by change class, with cameras."""
    rng = np.random.default_rng(0)
    right, up, fwd = view_axes(rec.E)
    views = [("Top-down (looking down)", right, fwd), ("Front (as filmed)", right, up)]
    fig, axes = plt.subplots(1, 2, figsize=(16, 7.5))
    cams = cam_centers(rec.E)
    for ax, (name, a1, a2) in zip(axes, views):
        for ps, gone_name in ((res.A, "removed"), (res.B, "added")):
            for cls, cname, size, k in ((UNCHANGED, "unchanged", 0.3, max_bg), (UNKNOWN, "unknown", 0.3, max_bg // 4),
                                        (APPEARANCE, "appearance", 1.5, 40000), (GONE, gone_name, 1.5, 40000)):
                idx = np.nonzero(ps.cls == cls)[0]
                idx = idx[_sub(len(idx), k, rng)]
                if cls == UNCHANGED:
                    c = ps.rgb[idx] / 255.0
                    ax.scatter(ps.X[idx] @ a1, ps.X[idx] @ a2, s=size, c=c, alpha=0.35, linewidths=0)
                else:
                    ax.scatter(ps.X[idx] @ a1, ps.X[idx] @ a2, s=size, c=COL[cname], alpha=0.8, linewidths=0,
                               label=f"{cname} ({len(np.nonzero(ps.cls == cls)[0]):,} pts)")
        for vid, mk in ((0, "^"), (1, "v")):
            c = cams[rec.video == vid]
            ax.plot(c @ a1, c @ a2, mk + "-", ms=4, lw=0.8, color="k" if vid == 0 else "#7b3294",
                    label=f"cameras {'A (before)' if vid == 0 else 'B (after)'}")
        for o in objs:
            p = np.array(o.centroid)
            if o.kind == "moved" and o.recording == "A" and o.partner is not None:
                q = p + np.array(o.displacement)
                ax.annotate("", xy=(q @ a1, q @ a2), xytext=(p @ a1, p @ a2),
                            arrowprops=dict(arrowstyle="->", color=COL["moved"], lw=2.5))
            ax.text(p @ a1, p @ a2, f" {o.id}:{o.label or o.kind}", fontsize=8, weight="bold",
                    color=COL.get(o.kind, "k"))
        ax.set_title(name)
        ax.set_aspect("equal")
        ax.invert_yaxis() if name.startswith("Top") else None
        ax.set_xticks([]), ax.set_yticks([])
    h, lab = axes[0].get_legend_handles_labels()
    uniq = dict(zip(lab, h))
    fig.legend(uniq.values(), uniq.keys(), loc="lower center", ncol=4, fontsize=8, markerscale=6)
    fig.suptitle(title)
    fig.tight_layout(rect=(0, 0.07, 1, 0.97))
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def frame_overlays(res: ChangeResult, rec: Recon, objs: list, out: Path, n_per_video: int = 3) -> Path:
    """Frames from both recordings with changed points painted in and object boxes labelled."""
    fig, axes = plt.subplots(2, n_per_video, figsize=(4 * n_per_video, 13))
    axes = np.atleast_2d(axes)
    for row, (vid, ps) in enumerate(((0, res.A), (1, res.B))):
        frames = np.nonzero(rec.video == vid)[0]
        votes = {}
        for o in objs:
            if (o.recording == "A") == (vid == 0):
                for f, c in o.frame_votes.items():
                    votes[f] = votes.get(f, 0) + c
        best = sorted(votes, key=votes.get, reverse=True)[:n_per_video]
        best += [f for f in frames[:: max(1, len(frames) // n_per_video)] if f not in best]
        for ax, f in zip(axes[row], best[:n_per_video]):
            img = rec.images[f].astype(float) / 255
            sel = ps.frame == f
            for cls, cname in ((GONE, "removed" if vid == 0 else "added"), (APPEARANCE, "appearance")):
                m = sel & (ps.cls == cls)
                v, u = ps.pix[m, 0], ps.pix[m, 1]
                col = np.array(matplotlib.colors.to_rgb(COL[cname]))
                img[v, u] = 0.4 * img[v, u] + 0.6 * col
            ax.imshow(img)
            for o in objs:
                X = (res.A if o.recording == "A" else res.B).X[o.extra["mask"]]
                X = X[:: max(1, len(X) // 2000)]
                u, v, z = project(X, rec.K[f], rec.E[f])
                H, W = rec.depth.shape[1:]
                ok = (z > 0) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
                if ok.mean() < 0.3:
                    continue
                u0, u1 = np.percentile(u[ok], [3, 97])
                v0, v1 = np.percentile(v[ok], [3, 97])
                ax.add_patch(plt.Rectangle((u0, v0), u1 - u0, v1 - v0, fill=False, lw=1.5, ec=COL.get(o.kind, "w")))
                ax.text(u0, v0 - 3, f"{o.id}:{o.label or o.kind}", fontsize=7, color="w",
                        bbox=dict(fc=COL.get(o.kind, "k"), alpha=0.7, pad=1, lw=0))
            ax.set_title(f"{'A before' if vid == 0 else 'B after'} - frame {f}", fontsize=9)
            ax.axis("off")
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out


def calibration(res: ChangeResult, out: Path) -> Path:
    """Within-recording (null) vs cross-recording distributions with the chosen thresholds.
    The core evaluation picture: changes are only what sits outside the null."""
    n, t = res.null, res.thresholds
    fig, axes = plt.subplots(1, 3, figsize=(16, 4))
    bins = np.linspace(-0.5, 0.5, 121)
    axes[0].hist(np.clip(n["r_within"], -0.5, 0.5), bins, density=True, alpha=0.6, label="within recording (null)")
    axes[0].hist(np.clip(n["r_cross"], -0.5, 0.5), bins, density=True, alpha=0.6, label="A vs B")
    for s in (-1, 1):
        axes[0].axvline(s * t["tau_rel_depth"], color="k", ls="--", lw=1)
    axes[0].set_yscale("log")
    axes[0].set_title("Relative depth residual (+ = sees through = gone)")
    b = np.linspace(0, 1, 41)
    axes[1].hist(n["gone_within"], b, density=True, alpha=0.6, label="within (null)")
    axes[1].hist(n["gone_cross"], b, density=True, alpha=0.6, label="A vs B")
    axes[1].axvline(t["gone_frac"], color="k", ls="--")
    axes[1].set_yscale("log")
    axes[1].set_title("Fraction of views that see through each point")
    if len(n["app_within"]) and len(n["app_cross"]):
        hi = float(np.quantile(np.concatenate([n["app_within"], n["app_cross"]]), 0.999))
        b = np.linspace(0, max(hi, 1e-3), 61)
        axes[2].hist(n["app_within"], b, density=True, alpha=0.6, label="within (null)")
        axes[2].hist(n["app_cross"], b, density=True, alpha=0.6, label="A vs B")
        if np.isfinite(t["appearance"]):
            axes[2].axvline(t["appearance"], color="k", ls="--")
        axes[2].set_yscale("log")
    axes[2].set_title("Appearance distance where geometry agrees")
    for ax in axes:
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out


def crops_sheet(crop_dir: Path, objs: list, out: Path) -> Path | None:
    files = sorted(crop_dir.glob("obj*.jpg")) if crop_dir.exists() else []
    if not files:
        return None
    cols = min(6, len(files))
    rows = int(np.ceil(len(files) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(2.6 * cols, 3 * rows))
    axes = np.atleast_1d(axes).ravel()
    by_id = {o.id: o for o in objs}
    for ax in axes:
        ax.axis("off")
    for ax, f in zip(axes, files):
        oid = int(f.stem[3:5])
        o = by_id.get(oid)
        ax.imshow(Image.open(f))
        if o is not None:
            ax.set_title(f"{oid}: {o.label} ({o.label_prob:.2f})\n{o.kind}", fontsize=8, color=COL.get(o.kind, "k"))
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out


def display_max(val) -> float:
    """Top of a colour scale left open (vmax None): the 95th percentile of the positive values,
    so a map that is mostly zero (interactability, grasp) still shows where it is highest."""
    v = np.asarray(val, float)
    v = v[np.isfinite(v) & (v > 0)]
    return float(np.quantile(v, 0.95)) if len(v) else 1.0


FIELD_STYLE = {  # field -> (title, colormap, vmin, vmax or None for display_max)
    "uncertainty_cm": ("Surface uncertainty (cm, 1-sigma)", "magma", 0, None),
    "shift_cm": ("Surface shift vs other recording (cm, + receded)", "coolwarm", None, None),
    "height_m": ("Height above floor (m)", "viridis", 0, None),
    "reach": ("Robot reachability (0-1)", "RdYlGn", 0, 1),
    "approach": ("Gripper approachability (0-1, free path x solid surface)", "RdYlGn", 0, 1),
    "interact": ("Interactability (reach x certainty x approach/object; colour top = 95th pct)", "RdYlGn", 0, None),
    "grasp": ("Grasp: objects that fit the gripper, nothing on top (x rigidity, thin parts higher; "
              "colour top = 95th pct)", "Greens", 0, None),
    "push": ("Push: movable objects too big to grasp", "Oranges", 0, 1),
    "place": ("Place: free upward surfaces in reach", "Blues", 0, 1),
}


def field_maps(f: dict, rec: Recon, out: Path, title: str = "") -> Path:
    """Each heat map from the front (as filmed) and from above, with the robot base marked."""
    right, up_cam, fwd = view_axes(rec.E)
    up = np.array(f["floor"]["up"])
    fwd = fwd - (fwd @ up) * up
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    X = f["X"]
    names = [k for k in FIELD_STYLE if k in f]
    rows = (2 * len(names) + 3) // 4
    fig, axes = plt.subplots(rows, 4, figsize=(22, 17 * rows / 3), squeeze=False)
    for k, name in enumerate(names):
        val = f[name]
        ttl, cmap, vmin, vmax = FIELD_STYLE[name]
        fin = np.isfinite(val)
        if name == "shift_cm":
            m = float(np.nanquantile(np.abs(val), 0.99)) if fin.any() else 1
            vmin, vmax = -m, m
        if vmax is None:
            vmax = display_max(val)
        for j, (vname, a1, a2) in enumerate((("front", right, up), ("top", right, fwd))):
            ax = axes[(2 * k + j) // 4, (2 * k + j) % 4]
            order = np.argsort(np.nan_to_num(np.abs(val), nan=-1))
            sc = ax.scatter(X[order] @ a1, X[order] @ a2, c=val[order], s=0.6, cmap=cmap, vmin=vmin, vmax=vmax,
                            linewidths=0)
            p1, p2 = X @ a1, X @ a2
            ax.set_xlim(*np.quantile(p1, [0.01, 0.99]))
            ax.set_ylim(*np.quantile(p2, [0.01, 0.99]))
            if vname == "top":
                b = f["base"]
                ax.plot(b @ a1, b @ a2, "k*", ms=14, label="robot base")
                ax.set_ylim(min(ax.get_ylim()[0], b @ a2), ax.get_ylim()[1])
                ax.invert_yaxis()
            ax.set_aspect("equal", adjustable="box")
            ax.set_xticks([]), ax.set_yticks([])
            ax.set_title(f"{ttl} - {vname}", fontsize=10)
            fig.colorbar(sc, ax=ax, fraction=0.035)
    fig.suptitle(title + f"\nscale {f['floor']['scale_m_per_unit']:.3f} m/unit (assumes phone at "
                 f"{f['robot']['phone_height_m']} m), reach {f['robot']['reach_m']} m from a "
                 f"{f['robot']['shoulder_height_m']} m mount"
                 + (", base drives to any free floor spot" if f['robot'].get('mobile') else ", base fixed"), fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out, dpi=90)
    plt.close(fig)
    return out


def base_map(bm: dict, out: Path, title: str = "") -> Path | None:
    """Top-down map of where a robot could stand and how much of the scene it could reach."""
    if not bm:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))
    P, h = bm["P_m"], bm["height_m"]
    ext = [bm["grid_x"][0], bm["grid_x"][-1], bm["grid_y"][0], bm["grid_y"][-1]]
    for ax, key, ttl in ((axes[0], "score", "Share of all surfaces in reach"),
                         (axes[1], "score_changed", "Share of changed surfaces in reach")):
        ax.scatter(P[:, 0], P[:, 1], c=np.clip(h, 0, 1.5), s=0.3, cmap="Greys", alpha=0.4, linewidths=0)
        im = ax.imshow(bm[key].T, origin="lower", extent=ext, cmap="viridis", vmin=0, vmax=1, alpha=0.75)
        ax.plot(*bm["base_now_m"], "r*", ms=14, label="camera operator")
        if bm["best_m"] is not None:
            ax.plot(*bm["best_m"], "w*", ms=16, mec="k", label="best base")
        ax.set_aspect("equal")
        ax.set_title(ttl)
        ax.set_xlabel("m"), ax.set_ylabel("m")
        ax.legend(loc="upper right", fontsize=8)
        fig.colorbar(im, ax=ax, fraction=0.04)
    fig.suptitle(title + "\nBlank cells: no observed floor, or an obstacle within the robot footprint", fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=90)
    plt.close(fig)
    return out
