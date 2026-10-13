"""Change detection, objects, heat maps and outputs on a synthetic scene (numpy only, no GPU).

Scene: floor (z = 0, metres), back wall at y = 2, and a 30 cm red box that is moved 50 cm
between recordings. Cameras are held at 1.35 m, so the floor fit should recover scale ~1."""

from __future__ import annotations

import json

import numpy as np
import pytest

from uncertainscene.change.detect import GONE, DetectConfig, detect
from uncertainscene.change.fields import RobotModel, base_placement, compute_fields, field_stats, object_fields
from uncertainscene.change.objects import find_objects, summarise
from uncertainscene.recon.frames import Recon

H, W, F = 60, 80, 60.0
BOX = 0.3


def look_at(c, target):
    fwd = target - c
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    R = np.stack([right, down, fwd])          # camera-from-world rotation (x right, y down, z fwd)
    return np.concatenate([R, (-R @ c)[:, None]], 1)


def render(E, K, box_min):
    v, u = np.mgrid[0:H, 0:W]
    d_cam = np.stack([(u + 0.5 - K[0, 2]) / K[0, 0], (v + 0.5 - K[1, 2]) / K[1, 1], np.ones_like(u, float)], -1)
    R, t = E[:, :3], E[:, 3]
    c = -R.T @ t
    d = d_cam @ R                             # world ray directions with camera z = 1
    t_hit = np.full((H, W), np.inf)
    col = np.zeros((H, W, 3), np.uint8)
    with np.errstate(divide="ignore", invalid="ignore"):
        tf = -c[2] / d[..., 2]                # floor
        hit = (tf > 0) & (tf < t_hit)
        t_hit[hit] = tf[hit]
        p = c + tf[..., None] * d
        chk = ((np.floor(p[..., 0] * 4) + np.floor(p[..., 1] * 4)) % 2).astype(bool)
        col[hit] = np.where(chk[hit, None], [120, 120, 120], [170, 170, 170])
        tw = (2.0 - c[1]) / d[..., 1]          # wall
        hit = (tw > 0) & (tw < t_hit)
        t_hit[hit] = tw[hit]
        col[hit] = [200, 180, 140]
        bmax = box_min + BOX                  # box (slab test)
        t0 = (box_min - c) / d
        t1 = (bmax - c) / d
        tn = np.nanmax(np.minimum(t0, t1), -1)
        tx = np.nanmin(np.maximum(t0, t1), -1)
        hit = (tx >= tn) & (tn > 0) & (tn < t_hit)
        t_hit[hit] = tn[hit]
        col[hit] = [200, 30, 30]
    return t_hit.astype(np.float32), col


def make_recon(pos_a, pos_b, n=8, noise=0.002, seed=0):
    rng = np.random.default_rng(seed)
    K = np.array([[F, 0, W / 2], [0, F, H / 2], [0, 0, 1.0]])
    Es, Ds, Is, vids = [], [], [], []
    for vid, pos in enumerate((pos_a, pos_b)):
        for i in range(n):
            ang = np.radians(-35 + 70 * i / (n - 1) + 4 * vid)
            c = np.array([1.6 * np.sin(ang), 0.9 - 1.6 * np.cos(ang), 1.35])
            E = look_at(c, np.array([0.0, 0.9, 0.3]))
            dep, col = render(E, K, np.array(pos, float))
            dep = dep * (1 + noise * rng.standard_normal(dep.shape)).astype(np.float32)
            Es.append(E), Ds.append(dep), Is.append(col), vids.append(vid)
    D = np.stack(Ds)
    D[~np.isfinite(D)] = 0
    S = len(Es)
    return Recon(np.stack(Is), D, np.ones_like(D) + 0.01 * rng.random(D.shape).astype(np.float32),
                 np.repeat(K[None], S, 0), np.stack(Es), np.array(vids))


def colour_feats(rec):
    f = rec.images.astype(np.float32) / 255 + 0.05
    return (f / np.linalg.norm(f, axis=-1, keepdims=True)).astype(np.float16)


CFG = DetectConfig(stride=1, conf_quantile=0.0)


@pytest.fixture(scope="module")
def moved():
    rec = make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0))
    res = detect(rec, colour_feats(rec), CFG)
    return rec, res, find_objects(res, min_points=60)


def test_static_pair_has_few_false_changes():
    rec = make_recon((-0.5, 0.8, 0.0), (-0.5, 0.8, 0.0))
    res = detect(rec, colour_feats(rec), CFG)
    for ps in (res.A, res.B):
        obs = ps.cls != 3
        assert obs.mean() > 0.5
        assert (ps.cls == GONE).sum() / obs.sum() < 0.01


def test_moved_box_is_detected_and_measured(moved):
    rec, res, objs = moved
    gone_a = res.A.X[res.A.cls == GONE]
    assert len(gone_a) > 50
    assert np.abs(np.median(gone_a[:, 0]) - (-0.35)) < 0.1     # box A centre x = -0.35
    mv = [o for o in objs if o.kind == "moved"]
    assert mv, [o.kind for o in objs]
    assert 0.35 < mv[0].distance < 0.65                         # moved 0.5 m (visible faces only)
    lo, hi = mv[0].distance_ci
    assert lo <= mv[0].distance <= hi
    ch = summarise(objs)
    assert sum(c["kind"] == "moved" for c in ch) == 1
    json.dumps(ch, default=float)


def test_fields_scale_reach_and_object_metrics(moved):
    rec, res, objs = moved
    f = compute_fields(res, rec, RobotModel(), voxel_m=0.02)
    assert abs(f["floor"]["scale_m_per_unit"] - 1.0) < 0.1
    for k in ("reach", "approach", "interact"):
        assert np.all((f[k] >= 0) & (f[k] <= 1))
    floor_pts = f["height_m"] < 0.02
    assert floor_pts.mean() > 0.2
    assert np.nanmedian(np.abs(f["shift_cm"][floor_pts])) < 3
    fx = compute_fields(res, rec, RobotModel(mobile=False), voxel_m=0.02)   # base fixed at the operator
    near = np.linalg.norm(fx["X"][:, :2] - fx["base"][:2], axis=1) < 0.4
    far = np.linalg.norm(fx["X"][:, :2] - fx["base"][:2], axis=1) > 2.0
    if near.any() and far.any():
        assert fx["reach"][near].mean() > fx["reach"][far].mean()
    assert len(f["bases"]) > 0                                   # a mobile robot finds free floor to stand on
    assert f["reach"].mean() >= fx["reach"].mean()               # and reaches at least as much as a fixed one
    s = field_stats(f)
    assert 0 <= s["interactable_frac"] <= 1
    object_fields(objs, res, f)
    mt = [o for o in objs if o.kind == "moved"][0].extra["metric"]
    assert 35 < mt["distance_cm"] < 65
    assert max(mt["size_cm"]) < 60


def test_outputs_render(moved, tmp_path):
    pytest.importorskip("matplotlib")
    pytest.importorskip("plotly")
    from uncertainscene.change import scene_report
    from uncertainscene.eval import metrics as M
    from uncertainscene.viz import plots, viewer

    rec, res, objs = moved
    f = compute_fields(res, rec, RobotModel(), voxel_m=0.02)
    object_fields(objs, res, f)
    for o in objs:
        o.label, o.label_prob, o.description = "box", 0.9, "test"
    m = M.label_free(res, objs)
    plots.change_map(res, rec, objs, tmp_path / "change_map.png", "t")
    plots.calibration(res, tmp_path / "calibration.png")
    plots.field_maps(f, rec, tmp_path / "heatmaps.png", "t")
    bm = base_placement(f)
    assert bm and bm["best_score"] is not None and 0 < bm["best_score"] <= 1
    plots.base_map(bm, tmp_path / "base_placement.png", "t")
    viewer.write_html(res, objs, tmp_path / "viewer.html", "t", fields=f)
    viewer.write_ply(res, tmp_path / "cloud.ply")
    viewer.write_fields_ply(f, tmp_path / "fields.ply")
    scene_report.write(tmp_path / "scene_report.md", "t", "a", "b", m, objs, f, field_stats(f), "light in both")
    for n in ("change_map.png", "calibration.png", "heatmaps.png", "base_placement.png", "viewer.html", "cloud.ply", "fields.ply"):
        assert (tmp_path / n).stat().st_size > 1000, n
    rep = (tmp_path / "scene_report.md").read_text()
    assert "Simulator randomisation" in rep and "box" in rep


def test_queue_names_unique_and_creative_first():
    from uncertainscene.experiments.poc import Spec, queue

    q = queue()
    names = [s if isinstance(s, str) else s.name for s in q]
    assert len(names) == len(set(names))
    specs = [s for s in q if isinstance(s, Spec)]
    assert all(s.outputs == "full" for s in specs[:6])
    assert sum(s.needs["minutes"] for s in specs) > 12 * 60 * 0.6   # enough work to fill a session
    assert sum(1 for s in specs if s.variant in ("naive_nn", "no_appearance")) <= 6


def test_summary_and_lighting_reports(tmp_path):
    from uncertainscene.eval import lighting, summarize

    rng = np.random.default_rng(0)
    pairs = [("scene01_light_before_a", "scene01_light_before_b", "static"),
             ("scene01_dark_before_a", "scene01_dark_before_b", "static"),
             ("scene01_dark_before_a", "scene01_light_before_a", "lighting"),
             ("scene01_light_before_a", "scene01_light_after_a", "change"),
             ("scene01_dark_before_a", "scene01_light_after_a", "change+lighting")]
    for i, (a, b, t) in enumerate(pairs):
        for off in range(3):
            d = tmp_path / f"s{i}_{off}"
            d.mkdir()
            m = {"pair_type": t, "a": a, "b": b, "pair": f"{a}_{b}", "variant": "base" if off == 0 else f"offset{off}",
                 "offset": off, "n_per_video": 28, "changed_frac": float(rng.uniform(0, 0.05)), "n_changes": 2,
                 "n_moved": 1, "n_removed": 0, "n_added": 0, "n_appearance": 1, "A_observed_frac": 0.7,
                 "B_observed_frac": 0.7, "A_appearance_frac": 0.01, "B_appearance_frac": 0.02,
                 "alignment_residual_cross": 0.02, "alignment_residual_within": 0.01, "field_uncertainty_cm_median": 1.2,
                 "field_reachable_frac": 0.3, "field_approachable_frac": 0.8, "field_interactable_frac": 0.2,
                 "field_scale_m_per_unit": 1.0, "field_uncertainty_cm_p90": 3.0, "field_shift_cm_p99_abs": 9.0,
                 "unc_cm_A_median": 1.5, "unc_cm_B_median": 1.0, "robot": {"reach_m": 0.85}}
            (d / "metrics.json").write_text(json.dumps(m))
            (d / "changes.json").write_text(json.dumps([{"id": 0, "kind": "moved", "label": "vase",
                                                         "description": "The vase moved."}]))
    out = tmp_path / "summary"
    summarize.write(tmp_path, out)
    lt = (out / "lighting.md").read_text()
    assert "static-dark" in lt and "change+lighting" in lt
    assert "vase" in lt and "dark" in lt
    assert (out / "lighting.png").stat().st_size > 1000
    assert "lighting.md" in (out / "summary.md").read_text()
    assert lighting.group({"pair_type": "static", "a": "scene01_dark_before_a", "b": "x_dark_y"}) == "static-dark"


def test_step_log_and_size_guard(tmp_path):
    import logging

    from uncertainscene.experiments import poc

    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * int(5.1 * 1024**2))
    assert poc.enforce_size(tmp_path) == ["big.bin"] and not big.exists()
    spec = poc.Spec("t", "scene01_light_before_a", "scene01_light_after_a")
    orig = poc.analyse
    poc.analyse = lambda *a, **k: (logging.getLogger("uncertainscene.poc").info("inside"), "ok")[1]
    try:
        assert poc.step_fn(spec, tmp_path)(tmp_path, tmp_path) == "ok"
    finally:
        poc.analyse = orig
    assert "inside" in (tmp_path / "step_log.txt").read_text()


def test_small_pose_uncertainty_does_not_change_what_is_observed():
    """Pose weights scale votes; they must not turn 2 views into 'not observed' (review finding:
    weights just under 1 made min_obs=2 behave like 3)."""
    rec = make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0))
    base = detect(rec, colour_feats(rec), CFG)
    rec.pose_px = np.full(len(rec.E), 0.1, np.float32)
    w = detect(rec, colour_feats(rec), CFG)
    for a, b in ((base.A, w.A), (base.B, w.B)):
        assert np.mean(a.cls == b.cls) > 0.995
