"""Frame extraction on a short synthetic video (ffmpeg test pattern)."""

import json
import shutil
import subprocess

import pytest

from uncertainscene.prep.frames import contact_sheet, extract, probe

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-t", "2", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_probe(video):
    info = probe(video)
    assert (info.width, info.height) == (640, 360)
    assert info.fps == pytest.approx(30)
    assert info.duration_s == pytest.approx(2, abs=0.1)


def test_extract_keeps_one_frame_per_window_and_downscales(video, tmp_path):
    out = tmp_path / "frames"
    frames = extract(video, out, fps=3, long_side=320, candidates=3)
    assert len(frames) == pytest.approx(6, abs=1)
    meta = json.loads((out / "frames.json").read_text())
    assert meta["size"] == [320, 180]
    assert all((out / f["file"]).exists() for f in frames)
    times = [f["t"] for f in frames]
    assert times == sorted(times)
    sheet = contact_sheet(out, tmp_path / "sheet.jpg", cols=4, thumb=80)
    assert sheet.exists()
