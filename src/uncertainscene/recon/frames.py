"""Pick frames from data/frames_6fps/<label>/ (or data/frames/, 3 fps) and prepare them as model input."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ..infra.budget import REPO_ROOT

FRAMES_3FPS = REPO_ROOT / "data" / "frames"
FRAMES_6FPS = REPO_ROOT / "data" / "frames_6fps"     # twice as many frames per recording (2026-10-10)
FRAMES = FRAMES_6FPS


def frames_root(label: str) -> Path:
    """6 fps frames when extracted for this recording, else the original 3 fps set."""
    return FRAMES_6FPS if (FRAMES_6FPS / label / "frames.json").exists() else FRAMES_3FPS


def select_frames(label: str, n: int, offset: int = 0, root: Path | None = None) -> list[Path]:
    """`n` frames spread evenly over the sweep, the sharpest in each of n bins.

    `offset` shifts which frame wins inside a bin (0 = sharpest, 1 = second sharpest, ...),
    giving a different but comparable selection for repeat runs."""
    root = root or frames_root(label)
    meta = json.loads((root / label / "frames.json").read_text())["frames"]
    bins = np.array_split(np.arange(len(meta)), min(n, len(meta)))
    picked = []
    for b in bins:
        order = sorted(b, key=lambda i: -meta[i]["sharpness"])
        picked.append(root / label / meta[order[min(offset, len(order) - 1)]]["file"])
    return picked


def model_size(w: int, h: int, long_side: int = 518, multiple: int = 14) -> tuple[int, int]:
    """Size with the long side = long_side and both sides multiples of `multiple`."""
    s = long_side / max(w, h)
    return (max(multiple, int(round(w * s / multiple)) * multiple),
            max(multiple, int(round(h * s / multiple)) * multiple))


def load_images(paths: list[Path], long_side: int = 518) -> np.ndarray:
    """[S, H, W, 3] uint8, all resized to one model size."""
    w0, h0 = Image.open(paths[0]).size
    w, h = model_size(w0, h0, long_side)
    return np.stack([np.asarray(Image.open(p).convert("RGB").resize((w, h), Image.Resampling.BICUBIC))
                     for p in paths])


@dataclass
class Recon:
    """Joint reconstruction of frames from two recordings, in one world frame.

    E is camera-from-world (OpenCV convention, x right, y down, z forward), 3x4.
    `video` is 0 for recording A (before) and 1 for recording B (after)."""
    images: np.ndarray            # [S, H, W, 3] uint8
    depth: np.ndarray             # [S, H, W] float32
    conf: np.ndarray              # [S, H, W] float32, higher = more reliable
    K: np.ndarray                 # [S, 3, 3]
    E: np.ndarray                 # [S, 3, 4]
    video: np.ndarray             # [S] int
    files: list[str] = field(default_factory=list)
    labels: tuple = ("A", "B")
    # per frame: factor from the model's own (normalised) units to this reconstruction's units
    # (the chunk's similarity scale; 1 when reconstructed in one pass). VGGT's depth confidence
    # describes errors in its normalised units, so errors here are (alpha / conf) * scale.
    scale: np.ndarray | None = None

    def save(self, path: str | Path) -> None:
        extra = {"scale": self.scale} if self.scale is not None else {}
        np.savez_compressed(path, images=self.images, depth=self.depth.astype(np.float16),
                            conf=self.conf.astype(np.float16), K=self.K, E=self.E, video=self.video,
                            files=np.array(self.files), labels=np.array(self.labels), **extra)

    @classmethod
    def load(cls, path: str | Path) -> "Recon":
        z = np.load(path)
        return cls(z["images"], z["depth"].astype(np.float32), z["conf"].astype(np.float32), z["K"], z["E"],
                   z["video"], [str(f) for f in z["files"]], tuple(str(x) for x in z["labels"]),
                   z["scale"] if "scale" in z.files else None)
