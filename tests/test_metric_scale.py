"""Metric scale from a metric depth model, fused with the other scale cues."""
from types import SimpleNamespace

import numpy as np

from uncertainscene.change.floor import Floor, combine_scale, object_scale
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


def _obj(label, true_m, scale, p=0.9, top3=None):
    return {"label": label, "label_prob": p, "label_top3": top3, "extent": [0.9 * true_m / scale], "complete": True}


def test_phone_height_is_never_used_and_most_likely_scale_wins():
    true = 2.7
    objs = [_obj("mug", 0.12, true), _obj("guitar", 1.0, true), _obj("table tennis bat", 0.26, true),
            _obj("baseball cap", 0.28, true),
            _obj("mug", 0.45, true)]                       # a cushion CLIP called a mug: an outlier
    o = object_scale(objs)
    f = combine_scale(_floor(3.6, 0.12), obj=o, metric={"scale": 2.8, "sd_log": 0.10})
    assert abs(np.log(f.scale_m_per_unit / true)) < 0.06           # phone height said 3.6
    assert f.scale_range[0] < true < f.scale_range[1]
    assert f.scale_cues["combined"]["cues"] == ["metric_depth", "objects"]
    # the wrong label does not drag the mode: same answer without it
    o4 = object_scale(objs[:4])
    assert abs(np.log(o["scale"] / o4["scale"])) < 0.03


def test_label_uncertainty_spreads_a_vote():
    # an object that is either a guitar (1.0 m) or a ukulele (0.55 m), 50/50: alone it cannot
    # tell the two scales apart; the metric depth cue picks the right one
    v = object_scale([_obj("guitar", 1.0, 2.0, top3=[("guitar", 0.5), ("ukulele", 0.5)])])
    assert v["range"][1] / v["range"][0] > 1.8
    f = combine_scale(_floor(9.9, 0.1), obj=v, metric={"scale": 2.0, "sd_log": 0.15})
    assert abs(np.log(f.scale_m_per_unit / 2.0)) < 0.08


def test_without_predictions_phone_height_stays_flagged():
    f = combine_scale(_floor(3.6, 0.12))
    assert f.scale_m_per_unit == 3.6 and "warning" in f.scale_cues["combined"]
