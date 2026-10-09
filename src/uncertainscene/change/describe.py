"""Crops, labels and plain-English descriptions of change objects."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..recon.frames import Recon
from .detect import ChangeResult
from .geometry import view_axes
from .objects import ChangeObject


def crop_for(obj: ChangeObject, res: ChangeResult, rec: Recon, margin: float = 0.25):
    """Crop of the object from the full-resolution frame where most of its points come from."""
    ps = res.A if obj.recording == "A" else res.B
    mask = obj.extra["mask"]
    frame = max(obj.frame_votes, key=obj.frame_votes.get)
    sel = mask & (ps.frame == frame)
    v, u = ps.pix[sel, 0], ps.pix[sel, 1]
    full = Image.open(rec.files[frame]).convert("RGB")
    sy, sx = full.height / rec.images.shape[1], full.width / rec.images.shape[2]
    v0, v1 = np.percentile(v, [2, 98]) * sy
    u0, u1 = np.percentile(u, [2, 98]) * sx
    mv, mu = (v1 - v0) * margin + 8, (u1 - u0) * margin + 8
    box = (int(max(0, u0 - mu)), int(max(0, v0 - mv)), int(min(full.width, u1 + mu)), int(min(full.height, v1 + mv)))
    return full.crop(box), frame, box


def direction_words(vec: np.ndarray, E: np.ndarray) -> str:
    right, up, fwd = view_axes(E)
    comps = {"to the right": vec @ right, "up": vec @ up, "further from the camera": vec @ fwd}
    words = []
    for name, val in sorted(comps.items(), key=lambda kv: -abs(kv[1])):
        if abs(val) < 0.3 * np.linalg.norm(vec):
            continue
        words.append(name if val > 0 else {"to the right": "to the left", "up": "down",
                                           "further from the camera": "towards the camera"}[name])
    return " and ".join(words) or "slightly"


def describe(obj: ChangeObject, E: np.ndarray, unit: str = "scene units", scale: float = 1.0) -> str:
    name = obj.label or "object"
    if obj.kind == "moved":
        d = obj.distance * scale
        lo, hi = (x * scale for x in obj.distance_ci)
        return (f"The {name} moved {direction_words(np.array(obj.displacement), E)} by about "
                f"{d:.2f} {unit} (90% interval {lo:.2f} to {hi:.2f}).")
    if obj.kind == "removed":
        return f"A {name} that was there in the first recording is no longer there."
    if obj.kind == "added":
        return f"A {name} appears in the second recording that was not there before."
    return f"The {name} looks different (turned over, deformed or swapped) but occupies the same place."


def label_objects(objs: list[ChangeObject], res: ChangeResult, rec: Recon, crop_dir: Path | None = None,
                  unit: str = "scene units", scale: float = 1.0) -> None:
    from .features import BACKGROUND, clip_labels

    crops = []
    for o in objs:
        img, frame, box = crop_for(o, res, rec)
        o.extra["crop_frame"], o.extra["crop_box"] = frame, box
        crops.append(img)
        if crop_dir is not None:
            crop_dir.mkdir(parents=True, exist_ok=True)
            img.thumbnail((256, 256))
            img.save(crop_dir / f"obj{o.id:02d}_{o.kind}.jpg", quality=85)
    top, _ = clip_labels([c.copy() for c in crops]) if crops else ([], None)
    for o, (lab, p) in zip(objs, top):
        o.label, o.label_prob = lab, round(p, 3)
        o.extra["is_background"] = lab in BACKGROUND
    for o in objs:
        o.description = describe(o, rec.E, unit, scale)
