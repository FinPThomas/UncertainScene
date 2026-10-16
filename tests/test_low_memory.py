"""The RAM guard steps a step down to lighter settings instead of stopping it (Fin, 2026-10-16)."""
from pathlib import Path

import pytest


class _Guard:
    def __init__(self):
        self.tripped, self.soft_callbacks, self.started = None, [], 0

    def check(self):
        return None

    def start(self):
        self.started += 1


def test_ram_stop_retries_lighter_then_finishes(tmp_path: Path, monkeypatch):
    from uncertainscene.experiments import poc, scene01

    seen = []

    def fake(spec, out, cache, store, check):
        seen.append(spec.low_memory)
        (out / "partial.txt").write_text("x")
        if spec.low_memory < 3:
            check.__self__.tripped = "RAM at 93% (hard limit 92%)"
            raise KeyboardInterrupt
        return "done"

    monkeypatch.setattr(scene01, "analyse", fake)
    monkeypatch.setattr(poc, "release_memory", lambda: {})
    g = _Guard()
    spec = scene01.Spec("s", "a", "b", low_memory=1)
    assert scene01._analyse_degrading(spec, tmp_path, tmp_path, None, g.check) == "done"
    assert seen == [1, 2, 3] and g.started == 2 and g.tripped is None


def test_person_stopping_or_lightest_level_still_stops(tmp_path: Path, monkeypatch):
    from uncertainscene.experiments import poc, scene01

    monkeypatch.setattr(poc, "release_memory", lambda: {})
    g = _Guard()

    def stop(spec, out, cache, store, check):
        raise KeyboardInterrupt                    # no guard trip: a person pressed stop

    monkeypatch.setattr(scene01, "analyse", stop)
    with pytest.raises(KeyboardInterrupt):
        scene01._analyse_degrading(scene01.Spec("s", "a", "b"), tmp_path, tmp_path, None, g.check)

    def ram(spec, out, cache, store, check):
        g.tripped = "RAM at 95%"
        raise KeyboardInterrupt

    monkeypatch.setattr(scene01, "analyse", ram)
    with pytest.raises(KeyboardInterrupt):
        scene01._analyse_degrading(scene01.Spec("s", "a", "b", low_memory=3), tmp_path, tmp_path, None, g.check)


def test_final_queue_starts_light():
    from uncertainscene.experiments.scene01 import Spec, queue

    first = [x for x in queue("light") if isinstance(x, Spec)][:2]
    assert all(x.low_memory == 1 for x in first)
