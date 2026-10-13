"""Metric scale from a metric depth model, fused with the other scale cues."""
from types import SimpleNamespace

import numpy as np

from uncertainscene.change.floor import Floor, combine_scale
from uncertainscene.change.metric import metric_scale


def _rec(S=30, H=24, W=32, seed=0):
    rng = np.random.default_rng(seed)
    depth = rng.uniform(0.3, 1.2, (S, H, W)).astype(np.float32)
    return SimpleNamespace(files=[str(i) for i in range(S)], depth=depth, conf=np.ones((S, H, W), np.float32),
                           pose_weight=lambda: np.ones(S))


def test_metric_scale_recovers_true_scale_with_honest_range():
    rec = _rec()
    true = 2.7                                     # metres per scene unit
    rng = np.random.default_rng(1)
    noise = {str(i): np.exp(rng.normal(0, 0.05)) for i in range(30)}   # per-frame model error 5%

    def predict(f):
        return rec.depth[int(f)] * true * noise[f] * np.exp(rng.normal(0, 0.1, rec.depth.shape[1:]))

    m = metric_scale(rec, n_frames=30, predict=predict)
    assert abs(np.log(m["scale"] / true)) < 0.05
    assert m["n"] == 30
    # the shared model bias is never averaged away by more frames
    assert m["sd_log"] >= m["bias_sd_log"]


def test_metric_scale_none_without_usable_frames():
    rec = _rec()
    assert metric_scale(rec, predict=lambda f: np.zeros(rec.depth.shape[1:])) is None


def _floor(scale, sd):
    f = Floor(up=[0, 1, 0], height=0.0, inlier_frac=0.03, scale_m_per_unit=scale,
              scale_range=[scale * 0.8, scale * 1.2], camera_height_units=0.4)
    f.scale_cues = {"phone_height": {"scale": scale, "sd_log": sd}}
    return f


def test_three_cues_fuse_and_contradiction_widens_range():
    f = combine_scale(_floor(3.6, 0.12), metric={"scale": 3.0, "sd_log": 0.10})
    assert 3.0 < f.scale_m_per_unit < 3.6
    assert f.scale_cues["combined"]["cues"] == ["phone_height", "metric_depth"]
    f = combine_scale(f, obj={"scale": 3.1, "sd_log": 0.1, "n": 4, "labels": ["mug"]})
    assert len(f.scale_cues["combined"]["cues"]) == 3
    narrow = f.scale_range[1] / f.scale_range[0]
    g = combine_scale(_floor(6.0, 0.12), metric={"scale": 3.0, "sd_log": 0.10})   # 4+ sigma apart
    assert g.scale_cues["combined"]["disagreement_sigma"] > 2
    assert g.scale_range[0] <= 3.0 * 1.05 and g.scale_range[1] >= 6.0 * 0.95
    assert g.scale_range[1] / g.scale_range[0] > narrow
