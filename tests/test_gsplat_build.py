"""gsplat's CUDA build runs in a watched child process: jobs sized to free RAM, stopped and
retried with fewer jobs if RAM still climbs, and killed when the guard interrupts the step."""
import sys
import time

import pytest

from uncertainscene.recon import gsplat_build as G


def test_jobs_fit_in_free_ram():
    assert G.jobs_for(1.6, 12.7) == 3          # fresh Colab at setup: 3 jobs, ~9.1 GB worst case
    assert G.jobs_for(6.5, 12.7) == 1          # after a reconstruction: one job only
    assert G.jobs_for(0.5, 64.0) == G.MAX_JOBS


def test_build_stops_and_retries_with_fewer_jobs(tmp_path, monkeypatch):
    seen = tmp_path / "jobs.txt"
    child = [sys.executable, "-c",
             f"import os, time; open({str(seen)!r}, 'a').write(os.environ['MAX_JOBS'] + ' '); time.sleep(0.5)"]
    calls = {"n": 0}

    def ram():                                 # first build: RAM jumps to 95%; second build: fine
        calls["n"] += 1
        first = seen.exists() and seen.read_text().split() == ["4"]
        return (12.0 if first else 2.0), 12.7

    monkeypatch.setattr(G, "_ram", ram)
    monkeypatch.setattr(G, "jobs_for", lambda used, total: 4)
    real_sleep = time.sleep
    monkeypatch.setattr(G.time, "sleep", lambda s: real_sleep(min(s, 0.2)))
    r = G.build(cmd=child, poll_s=0.2)
    assert r["ok"] and r["tries"] == 2 and r["jobs"] == 2
    assert seen.read_text().split() == ["4", "2"]
    assert r["peak_ram_gb"] == 12.0


def test_guard_interrupt_kills_the_build(monkeypatch):
    import psutil

    monkeypatch.setattr(G, "_ram", lambda: (2.0, 12.7))
    before = {p.pid for p in psutil.Process().children(recursive=True)}

    def check():
        raise KeyboardInterrupt                # what the guard's hard limit delivers

    with pytest.raises(KeyboardInterrupt):
        G.build(cmd=[sys.executable, "-c", "import time; time.sleep(60)"], check=check, poll_s=0.1)
    left = {p.pid for p in psutil.Process().children(recursive=True)} - before
    assert not left


def test_peak_ram_is_measured_for_real():
    """The watcher sees the child's memory: a child holding 400 MB raises the recorded peak."""
    used0, _ = G._ram()
    child = [sys.executable, "-c", "import time; b = bytearray(400_000_000); b[::4096] = b'x' * len(b[::4096]); time.sleep(1.5)"]
    r = G.build(cmd=child, poll_s=0.2)
    assert r["ok"] and r["peak_ram_gb"] >= used0 + 0.25
