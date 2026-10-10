"""Figures for the SAM 2 object inventory: a labelled sheet of every object and frames with
all object masks painted by change status."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from ..change.inventory import STATUS_COLOR, masked_crop  # noqa: E402
from ..recon.frames import Recon  # noqa: E402

ORDER = ["moved", "added", "removed", "changed in place", "shifted", "unchanged", "not seen in other recording",
         "unknown"]


def _hex(c):
    return "#%02x%02x%02x" % c


def inventory_sheet(objs: list, rec: Recon, out: Path, title: str = "", max_tiles: int = 60) -> Path | None:
    objs = [o for o in objs if not o.structure]
    if not objs:
        return None
    objs = sorted(objs, key=lambda o: (ORDER.index(o.status) if o.status in ORDER else 99, -len(o.voxels)))[:max_tiles]
    cols = 8
    rows = int(np.ceil(len(objs) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(2.2 * cols, 2.6 * rows + 0.6))
    axes = np.atleast_1d(axes).ravel()
    for ax in axes:
        ax.axis("off")
    for ax, o in zip(axes, objs):
        img, _ = masked_crop(o, rec, max_side=200)
        ax.imshow(img)
        col = _hex(STATUS_COLOR.get(o.status, (0, 0, 0)))
        for s in ax.spines.values():
            s.set_visible(True), s.set_color(col), s.set_linewidth(4)
        ax.set_title(f"{o.recording}{o.id} {o.label} ({o.label_prob:.2f})\n{o.status}", fontsize=7.5, color=col)
    fig.suptitle(title + f"\n{len(objs)} objects (structures such as sofa, wall and floor omitted); border colour = status",
                 fontsize=10)
    fig.tight_layout()
    fig.savefig(out, dpi=90)
    plt.close(fig)
    return out


def mask_overlays(objs_a: list, objs_b: list, rec: Recon, out: Path, n_per_video: int = 3) -> Path:
    """Frames from each recording with every object mask painted in its status colour and
    labelled; frames are chosen where the most changed objects are visible."""
    fig, axes = plt.subplots(2, n_per_video, figsize=(4.2 * n_per_video, 14))
    axes = np.atleast_2d(axes)
    for row, objs in enumerate((objs_a, objs_b)):
        score: dict = {}
        for o in objs:
            w = 5 if o.status not in ("unchanged", "not seen in other recording", "unknown") else 1
            for f in o.masks:
                score[f] = score.get(f, 0) + w
        frames = sorted(score, key=score.get, reverse=True)
        picked = []
        for f in frames:            # spread the picks over the sweep
            if all(abs(f - p) > 3 for p in picked):
                picked.append(f)
            if len(picked) == n_per_video:
                break
        for ax, f in zip(axes[row], picked):
            img = rec.images[f].astype(float) / 255
            labels = []
            for o in sorted(objs, key=lambda o: -len(o.voxels)):
                m = o.masks.get(f)
                if m is None or o.structure:
                    continue
                col = np.array(STATUS_COLOR.get(o.status, (0, 0, 0))) / 255
                a = 0.55 if o.status != "unchanged" else 0.3
                img[m] = (1 - a) * img[m] + a * col
                v, u = np.nonzero(m)
                labels.append((u.mean(), v.mean(), f"{o.recording}{o.id} {o.label}", _hex(STATUS_COLOR.get(o.status, (0, 0, 0)))))
            ax.imshow(img)
            for u, v, t, c in labels:
                ax.text(u, v, t, fontsize=6.5, color="w", ha="center", bbox=dict(fc=c, alpha=0.75, pad=1, lw=0))
            ax.set_title(f"{'A before' if row == 0 else 'B after'} - frame {f}", fontsize=9)
            ax.axis("off")
        for ax in axes[row][len(picked):]:
            ax.axis("off")
    handles = [plt.Rectangle((0, 0), 1, 1, color=_hex(c)) for s, c in STATUS_COLOR.items()]
    fig.legend(handles, list(STATUS_COLOR), loc="lower center", ncol=4, fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out, dpi=95)
    plt.close(fig)
    return out
