"""Figures for object position uncertainty.

- floor_plan: top-down map of the scene (faint points) with each object's 90% position region
  as an ellipse, labelled with its name and size of the region ("cup: within 4 cm").
- precision_curves: how the 90% radius shrinks with more views and with more clips.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..change.position import floor_basis


def _setup():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse

    return plt, Ellipse


def floor_plan(panels: list[dict], up: np.ndarray, scale: float, out: Path, title: str = "",
               min_ellipse_cm: float = 1.0) -> Path:
    """panels: [{"title", "bg" (points [N, 3] scene units), "bg_rgb" [N, 3] uint8 or None,
    "items": [{"label", "mean" (scene units), "cov" (scene units^2), "color" (rgb 0-255)}]}].
    All panels share one view window so ellipses can be compared."""
    plt, Ellipse = _setup()
    B = floor_basis(up)
    k = scale * 100
    allxy = [(p["bg"] @ B.T) * k for p in panels if len(p["bg"])]
    xy = np.concatenate(allxy) if allxy else np.zeros((1, 2))
    lo, hi = np.quantile(xy, 0.01, axis=0), np.quantile(xy, 0.99, axis=0)
    pad = 0.05 * (hi - lo).max()
    n = len(panels)
    fig, axes = plt.subplots(1, n, figsize=(7.5 * n, 7.5), squeeze=False)
    for ax, p in zip(axes[0], panels):
        if len(p["bg"]):
            q = (p["bg"] @ B.T) * k
            c = p["bg_rgb"] / 255 if p.get("bg_rgb") is not None else "#bbbbbb"
            ax.scatter(q[:, 0], q[:, 1], s=0.6, c=c, alpha=0.35, linewidths=0)
        for it in p["items"]:
            m2 = B @ it["mean"] * k
            c2 = B @ it["cov"] @ B.T * k * k
            w, v = np.linalg.eigh(c2)
            w = np.clip(w, 0, None)
            a, b = 2 * np.sqrt(4.6052 * w[1]), 2 * np.sqrt(4.6052 * w[0])
            ang = np.degrees(np.arctan2(v[1, 1], v[0, 1]))
            col = np.array(it["color"]) / 255
            ax.add_patch(Ellipse(m2, max(a, min_ellipse_cm), max(b, min_ellipse_cm), angle=ang, facecolor=(*col, 0.25),
                                 edgecolor=col, lw=1.6))
            ax.plot(*m2, "+", color=col, ms=6)
            r = it.get("r90_cm")
            ax.annotate(f"{it['label']}" + (f"\n±{r:.0f} cm" if r is not None else ""), m2, xytext=(4, 4),
                        textcoords="offset points", fontsize=7.5, color="black",
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))
        ax.set_xlim(lo[0] - pad, hi[0] + pad)
        ax.set_ylim(lo[1] - pad, hi[1] + pad)
        ax.set_aspect("equal")
        ax.set_xlabel("cm (floor plane)")
        ax.set_title(p["title"], fontsize=11)
        ax.grid(alpha=0.25)
    fig.suptitle(title + "\nEllipses: 90% region where each object's centre is (top-down view)", fontsize=12)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def precision_curves(views: dict | None, clips: dict | None, out: Path, title: str = "",
                     model: dict | None = None) -> Path:
    """views: {n: {"median_radius90_cm"}} from views disagreeing (subsampled); model: same from
    the VGGT + SAM 2 filter after n views; clips: clips_curve output (optional)."""
    plt, _ = _setup()
    n = 1 + (clips is not None)
    fig, axes = plt.subplots(1, n, figsize=(6.5 * n, 4.6), squeeze=False)
    ax = axes[0][0]

    def line(ax, d, style, label):
        if not d:
            return
        x = np.array(sorted(int(k) for k in d))
        y = np.array([(d.get(k) or d.get(str(k)))["median_radius90_cm"] for k in x])
        ax.plot(x, y, style, label=label)
        return x

    xs = [line(ax, model, "o-", "model: VGGT confidence + SAM 2 wobble, filtered"),
          line(ax, views, "s--", "check: views disagreeing")]
    xs = [x for x in xs if x is not None]
    if xs:
        x = np.unique(np.concatenate(xs))
        ax.set_xscale("log", base=2)
        ax.set_xticks(x, [str(v) for v in x])
    ax.set_xlabel("views of the object")
    ax.set_ylabel("90% position range (cm), median over objects")
    ax.set_title("More views of one clip")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    if clips is not None:
        ax = axes[0][1]
        xs = [line(ax, clips.get("by_clips"), "o-", "model, filtered over all views"),
              line(ax, clips.get("by_clips_views"), "s--", "check: views disagreeing")]
        xs = [x for x in xs if x is not None]
        if xs:
            ax.set_xticks(np.unique(np.concatenate(xs)))
        nis = clips.get("nis_mean")
        ax.set_title("More clips of the same scene" + (f"\nconsistency (NIS) {nis:.1f}; 3 = honest" if nis else ""),
                     fontsize=10)
        ax.set_xlabel("clips combined")
        ax.set_ylabel("90% position range (cm)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def region_grid(rows: list[dict], up: np.ndarray, scale: float, out: Path, title: str = "", max_rows: int = 12) -> Path:
    """Close-ups: one row per object, one column per number of clips fused, all cells of a row
    at the same zoom, so the shrinking 90% region is visible even when it is only a few cm.
    rows: [{"label", "ests": [{"mean", "cov"} for 1, 2, ... clips]}]."""
    plt, Ellipse = _setup()
    B = floor_basis(up)
    k = scale * 100
    rows = rows[:max_rows]
    ncol = max((len(r["ests"]) for r in rows), default=1)
    fig, axes = plt.subplots(max(len(rows), 1), ncol, figsize=(2.6 * ncol + 1.2, 2.4 * max(len(rows), 1)),
                             squeeze=False)
    for i, r in enumerate(rows):
        shapes = []
        for e in r["ests"]:
            m2 = B @ e["mean"] * k
            w, v = np.linalg.eigh(B @ e["cov"] @ B.T * k * k)
            w = np.clip(w, 0, None)
            shapes.append((m2, 2 * np.sqrt(4.6052 * w[1]), 2 * np.sqrt(4.6052 * w[0]),
                           np.degrees(np.arctan2(v[1, 1], v[0, 1]))))
        c = shapes[-1][0]
        half = max(0.65 * max(s[1] for s in shapes), 1.0)
        for j in range(ncol):
            ax = axes[i][j]
            if j < len(shapes):
                m2, a, b, ang = shapes[j]
                ax.add_patch(Ellipse(m2, a, b, angle=ang, facecolor=(0.12, 0.47, 0.71, 0.25), edgecolor="C0", lw=1.5))
                ax.plot(*m2, "+", color="C0")
                ax.set_title(f"{j + 1} clip{'s' if j else ''}: ±{a / 2:.1f} cm", fontsize=8.5)  # semi-major axis
            else:
                ax.set_title("not seen", fontsize=8.5)
            ax.set_xlim(c[0] - half, c[0] + half)
            ax.set_ylim(c[1] - half, c[1] + half)
            ax.set_aspect("equal")
            ax.tick_params(labelsize=6.5)
            ax.grid(alpha=0.3)
        axes[i][0].set_ylabel(r["label"], fontsize=9)
    fig.suptitle(title + "\nClose-up of each object's 90% region (top-down, cm) as clips are added", fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=100)
    plt.close(fig)
    return out
