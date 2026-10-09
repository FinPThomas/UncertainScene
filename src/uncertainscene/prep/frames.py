"""Turn a phone recording into a small, model-ready frame set.

For each video:
- `probe`: duration, resolution, frame rate, rotation, codec, creation time (ffprobe).
- `extract`: decode at a few candidate frames per output frame, keep the sharpest of each
  group (variance of the Laplacian), downscale so the long side is `long_side` px, save JPEGs.
  Picking the sharpest frame per window removes most motion blur at no extra recording cost.
- `contact_sheet`: one image with thumbnails of every kept frame, for a quick visual check.
- `frames.json`: per frame its source time, sharpness and mean brightness.

Only ffmpeg/ffprobe, numpy and Pillow are needed, so it runs locally, in Colab and in CI.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass
class VideoInfo:
    duration_s: float
    width: int          # after rotation, as displayed
    height: int
    fps: float
    codec: str
    rotation: int
    creation_time: str | None
    bit_rate: int | None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def probe(path: str | Path) -> VideoInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True).stdout
    meta = json.loads(out)
    v = next(s for s in meta["streams"] if s["codec_type"] == "video")
    num, den = (int(x) for x in v.get("avg_frame_rate", "0/1").split("/"))
    rotation = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = int(v.get("tags", {}).get("rotate", rotation)) % 360
    w, h = int(v["width"]), int(v["height"])
    if rotation in (90, 270):
        w, h = h, w
    fmt = meta.get("format", {})
    return VideoInfo(
        duration_s=float(fmt.get("duration") or v.get("duration") or 0),
        width=w, height=h, fps=num / den if den else 0.0, codec=v.get("codec_name", "?"),
        rotation=rotation,
        creation_time=fmt.get("tags", {}).get("creation_time"),
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
    )


def sharpness(gray: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian; higher is sharper."""
    g = gray.astype(np.float32)
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def _scaled_size(w: int, h: int, long_side: int) -> tuple[int, int]:
    s = min(1.0, long_side / max(w, h))
    return int(round(w * s / 2) * 2), int(round(h * s / 2) * 2)


def extract(path: str | Path, out_dir: str | Path, fps: float = 3.0, long_side: int = 1024,
            candidates: int = 3, quality: int = 90, start_s: float = 0.0, end_s: float | None = None) -> list[dict]:
    """Write the sharpest of every `candidates` frames, at `fps` output frames per second."""
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(path)
    w, h = _scaled_size(info.width, info.height, long_side)
    rate = fps * candidates
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start_s}", "-i", str(path)]
    if end_s is not None:
        cmd += ["-t", f"{end_s - start_s}"]
    cmd += ["-vf", f"fps={rate},scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = w * h * 3
    kept, group, i = [], [], 0

    def flush():
        if not group:
            return
        t, rgb, sharp = max(group, key=lambda x: x[2])
        name = f"{len(kept):05d}.jpg"
        Image.fromarray(rgb).save(out_dir / name, quality=quality)
        kept.append({"file": name, "t": round(t, 3), "sharpness": round(sharp, 1),
                     "brightness": round(float(rgb.mean()), 1)})
        group.clear()

    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        rgb = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        gray = rgb[::2, ::2].mean(axis=2)
        group.append((start_s + i / rate, rgb.copy(), sharpness(gray)))
        i += 1
        if len(group) == candidates:
            flush()
    flush()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed on {path}")
    (out_dir / "frames.json").write_text(json.dumps(
        {"source": path.name, "fps": fps, "candidates": candidates, "size": [w, h],
         "video": info.as_dict(), "frames": kept}, indent=1) + "\n")
    return kept


def contact_sheet(frame_dir: str | Path, out: str | Path, cols: int = 8, thumb: int = 240,
                  max_frames: int = 64) -> Path:
    """Grid of evenly spaced thumbnails from an extracted frame folder."""
    frame_dir = Path(frame_dir)
    files = sorted(frame_dir.glob("*.jpg"))
    if not files:
        raise FileNotFoundError(f"no frames in {frame_dir}")
    step = max(1, math.ceil(len(files) / max_frames))
    files = files[::step]
    first = Image.open(files[0])
    th = int(thumb * first.height / first.width)
    rows = math.ceil(len(files) / cols)
    sheet = Image.new("RGB", (cols * thumb, rows * th), "black")
    for k, f in enumerate(files):
        im = Image.open(f).resize((thumb, th), Image.Resampling.LANCZOS)
        sheet.paste(im, ((k % cols) * thumb, (k // cols) * th))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=80)
    return out
