"""Splat viewer site: built from a tiny synthetic .splat file."""

import base64
import re

import numpy as np

from uncertainscene.viz.splat_site import build, encode, keep_visible, read_splat


def _write_splat(path, n, seed=0):
    rng = np.random.default_rng(seed)
    d = np.zeros((n, 32), np.uint8)
    d[:, 0:12] = rng.normal([0.1, 0, 0.86], 0.2, (n, 3)).astype(np.float32).view(np.uint8)
    d[:, 12:24] = np.exp(rng.uniform(-7, -3, (n, 3))).astype(np.float32).view(np.uint8)
    d[:, 24:28] = rng.integers(0, 256, (n, 4))
    d[:, 28:32] = [255, 128, 128, 128]
    d.tofile(path)
    return path


def test_build_writes_viewer_and_compact_data(tmp_path):
    a, b = _write_splat(tmp_path / "a.splat", 500, 0), _write_splat(tmp_path / "b.splat", 300, 1)
    out = build({"Before": a, "After": b}, tmp_path / "site", "Tiny Splat Viewer", max_gaussians=400, notes="hello")
    html = out.read_text()
    assert out.name == "splat_viewer.html" and "<title>Tiny Splat Viewer</title>" in html and "hello" in html
    assert 'src="splats/' not in html and '"file": "splats/before.js"' in html
    for name, n in [("before", 400), ("after", 300)]:
        js = (tmp_path / "site" / "splats" / f"{name}.js").read_text()
        raw = base64.b64decode(re.search(r'="([A-Za-z0-9+/=]+)"', js).group(1))
        assert len(raw) == 17 * n


def test_keep_visible_and_quantisation(tmp_path):
    s = read_splat(_write_splat(tmp_path / "a.splat", 200))
    k = keep_visible(s, 50)
    vis = lambda x: x["rgba"][:, 3] / 255 * np.exp(np.log(x["scale"]).mean(1))
    assert len(k["pos"]) == 50 and vis(k).min() >= np.sort(vis(s))[-50] - 1e-12
    buf, meta = encode(k)
    q = np.frombuffer(buf[:6 * 50], "<u2").reshape(-1, 3)
    pos = np.array(meta["lo"]) + q / 65535 * (np.array(meta["hi"]) - np.array(meta["lo"]))
    assert np.abs(pos - k["pos"]).max() < 1e-4


def test_bin_format(tmp_path):
    a = _write_splat(tmp_path / "a.splat", 100)
    build({"x": a}, tmp_path / "site", "T", fmt="bin")
    assert (tmp_path / "site" / "splats" / "x.bin").stat().st_size == 17 * 100
