"""Chunked reconstruction, floor/scale, SAM-style objects and object-level changes on the
synthetic scene from test_change (numpy only)."""

from __future__ import annotations

import numpy as np

from uncertainscene.recon.chunked import plan_chunks, reconstruct_chunked, transform_cameras, umeyama

from test_change import make_recon


def _rand_sim3(rng):
    q, _ = np.linalg.qr(rng.standard_normal((3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] *= -1
    return float(rng.uniform(0.5, 2.0)), q, rng.standard_normal(3)


def test_umeyama_recovers_sim3():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((500, 3))
    s, R, t = _rand_sim3(rng)
    Y = s * X @ R.T + t
    Y[:50] += 5                                 # outliers
    s2, R2, t2, res = umeyama(X, Y)
    assert abs(s2 - s) < 1e-6 and np.allclose(R2, R, atol=1e-6) and res < 1e-6


def test_chunked_matches_joint():
    """Each chunk is returned in its own random similarity frame (as a feed-forward model
    would); after alignment every camera and depth must match the ground truth."""
    gt = make_recon([0.2, 0.8, 0], [0.7, 0.8, 0], n=20, noise=0.0)
    rng = np.random.default_rng(1)
    calls = []

    def run_chunk(idx):
        s, R, t = _rand_sim3(rng)
        E, D = transform_cameras(gt.E[idx], gt.depth[idx], s, R, t)
        calls.append(len(idx))
        return {"depth": D, "conf": gt.conf[idx], "K": gt.K[idx], "E": E}

    anchors, chunks = plan_chunks(gt.video, 16, 6)
    assert len(anchors) == 6 and sorted(anchors + sum(chunks, [])) == list(range(40))
    rec, info = reconstruct_chunked(gt.images, gt.video, run_chunk, chunk_size=16, n_anchor=6)
    assert info["chunks"] == len(chunks) > 1 and max(calls) <= 16 and not info["chunks_dropped"]
    # compare up to the global similarity of chunk 0: camera centres relative to each other
    from uncertainscene.change.geometry import cam_centers
    c_gt, c = cam_centers(gt.E), cam_centers(rec.E)
    s, R, t, res = umeyama(c, c_gt)
    assert res < 1e-6
    np.testing.assert_allclose(rec.depth * s, gt.depth, rtol=1e-4, atol=1e-5)


def test_bad_chunk_is_dropped():
    gt = make_recon([0.2, 0.8, 0], [0.7, 0.8, 0], n=20, noise=0.0)
    rng = np.random.default_rng(2)
    n = {"c": 0}

    def run_chunk(idx):
        D = gt.depth[idx].copy()
        if n["c"] == 2:                         # third chunk: depth of anchors corrupted
            D[:6] *= 1 + 0.3 * rng.standard_normal(D[:6].shape)
        n["c"] += 1
        return {"depth": D, "conf": gt.conf[idx], "K": gt.K[idx], "E": gt.E[idx]}

    rec, info = reconstruct_chunked(gt.images, gt.video, run_chunk, chunk_size=16, n_anchor=6)
    assert info["chunks_dropped"] == [2] and len(rec.images) < 40


# ---- floor and scale ------------------------------------------------------------------------
def test_floor_scale_and_object_cue():
    from uncertainscene.change.floor import combine_scale, fit_floor, object_scale

    rec = make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0), n=10, noise=0.001)
    from uncertainscene.change.geometry import unproject
    X = np.concatenate([unproject(rec.depth[s], rec.K[s], rec.E[s], rec.depth[s] > 0)[0][::3] for s in range(len(rec.E))])
    fl = fit_floor(X * 0.4, rec.E * np.array([1, 1, 1, 0.4]))      # scene in arbitrary units (x0.4)
    assert abs(fl.scale_m_per_unit - 2.5) < 0.15 and fl.floor_points > 100
    obj = object_scale([{"label": "mug", "label_prob": 0.9, "extent_units": 0.12 / 2.4},
                        {"label": "guitar", "label_prob": 0.8, "extent_units": 1.0 / 2.45},
                        {"label": "sofa", "label_prob": 0.9, "extent_units": 1.0}])      # no prior: ignored
    assert obj["n"] == 2 and 2.3 < obj["scale"] < 2.6
    fl = combine_scale(fl, obj)
    assert 2.35 < fl.scale_m_per_unit < 2.6 and fl.scale_range[0] < fl.scale_m_per_unit < fl.scale_range[1]


# ---- SAM-style objects on the synthetic scene ----------------------------------------------
def _fake_masks(rec, frames):
    """Stand-in for SAM 2: masks from the synthetic colours (box, wall, floor tiles)."""
    out = {}
    H, W = rec.depth.shape[1:]
    for s in frames:
        img = rec.images[s].astype(int)
        ms = []
        for col in ([200, 30, 30], [200, 180, 140]):
            m = np.all(img == col, -1)
            if m.sum() > 20:
                v, u = np.nonzero(m)
                ms.append({"mask": m, "full_bbox": (int(u.min()), int(v.min()), int(u.max()) + 1, int(v.max()) + 1),
                           "score": 0.9, "full_hw": (H, W)})
        out[s] = ms
    return out


def _with_files(rec, tmp_path):
    from PIL import Image
    files = []
    for s, im in enumerate(rec.images):
        p = tmp_path / f"f{s:03d}.png"
        Image.fromarray(im).save(p)
        files.append(str(p))
    rec.files = files
    return rec


def _fake_clip(monkeypatch):
    from uncertainscene.change import features

    def fake(crops, *a, **k):
        tops, embs = [], []
        for c in crops:
            a_ = np.asarray(c).reshape(-1, 3).astype(float)
            red = np.mean((a_[:, 0] > 150) & (a_[:, 1] < 80))
            tops.append([("box", 0.9), ("cushion", 0.05), ("bag", 0.02)] if red > 0.08 else
                        [("wall", 0.8), ("door", 0.1), ("shelf", 0.05)])
            e = np.array([red, 1 - red, 0.1])
            embs.append(e / np.linalg.norm(e))
        return tops, np.array(embs, np.float32)
    monkeypatch.setattr(features, "clip_inventory", fake)


def _objects(rec, feats):
    from test_change import CFG
    from uncertainscene.change import inventory as inv
    from uncertainscene.change.detect import detect
    from uncertainscene.change.segment import build_objects, key_frames

    res = detect(rec, feats, CFG)
    masks = _fake_masks(rec, key_frames(rec, 0, 8) + key_frames(rec, 1, 8))
    A = {s: m for s, m in masks.items() if rec.video[s] == 0}
    B = {s: m for s, m in masks.items() if rec.video[s] == 1}
    oa = build_objects(rec, A, "A", voxel=0.03, structure_units=1.2, min_voxels=3)
    ob = build_objects(rec, B, "B", voxel=0.03, structure_units=1.2, min_voxels=3)
    inv.label_all(oa + ob, rec)
    thr = inv.classify(oa, ob, rec, feats, res, CFG)
    return res, oa, ob, thr


def test_sam_objects_moved_box(tmp_path, monkeypatch):
    from test_change import colour_feats
    _fake_clip(monkeypatch)
    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0)), tmp_path)
    res, oa, ob, thr = _objects(rec, colour_feats(rec))
    boxes_a = [o for o in oa if o.label == "box"]
    boxes_b = [o for o in ob if o.label == "box"]
    assert len(boxes_a) == 1 and len(boxes_b) == 1, ([o.label for o in oa], [o.label for o in ob])
    assert boxes_a[0].structure is False and [o.structure for o in oa if o.label == "wall"] == [True]
    assert boxes_a[0].status == boxes_b[0].status == "moved", (boxes_a[0].stats, boxes_b[0].stats, thr)
    assert 0.35 < boxes_a[0].stats["distance"] < 0.65
    walls = [o for o in oa + ob if o.label == "wall"]
    assert all(o.status in ("unchanged", "not seen in other recording") for o in walls), [o.status for o in walls]


def test_sam_objects_static_pair(tmp_path, monkeypatch):
    from test_change import colour_feats
    _fake_clip(monkeypatch)
    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (-0.5, 0.8, 0.0)), tmp_path)
    res, oa, ob, thr = _objects(rec, colour_feats(rec))
    assert all(o.status in ("unchanged", "not seen in other recording") for o in oa + ob), \
        [(o.label, o.status, o.stats) for o in oa + ob]


def test_full_step_end_to_end(tmp_path, monkeypatch):
    """analyse() with the GPU stages replaced by the synthetic scene: every output is written."""
    from test_change import colour_feats
    from uncertainscene.change.segment import key_frames
    from uncertainscene.experiments import scene01

    _fake_clip(monkeypatch)
    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0)), tmp_path)
    monkeypatch.setattr(scene01, "_recon", lambda spec, cache: (rec, {"seconds": 1.0, "chunks": 1, "used_per_video": 8,
                                                                      "chunk_residual_max": 0.0}))
    monkeypatch.setattr(scene01, "_feats", lambda rec_, spec, cache: colour_feats(rec_))
    monkeypatch.setattr(scene01, "_masks", lambda rec_, spec, cache: _fake_masks(
        rec_, key_frames(rec_, 0, 8) + key_frames(rec_, 1, 8)))
    spec = scene01.Spec("p1_test", "scene01_light_before_a", "scene01_light_after_a",
                        detect={"stride": 1, "conf_quantile": 0.0}, voxel_m=0.02)
    out = tmp_path / "out"
    out.mkdir()
    note = scene01.step_fn(spec, tmp_path / "cache")(out, tmp_path)
    assert "changed objects" in note
    for n in ("metrics.json", "objects.json", "changes.json", "objects.png", "object_overlays.png", "heatmaps.png",
              "viewer.html", "scene_report.md", "report.md", "step_log.txt"):
        assert (out / n).exists() and (out / n).stat().st_size > 100, n
    import json
    m = json.loads((out / "metrics.json").read_text())
    assert m["obj_moved"] == 1 and m["version"] == 2 and m["sam_error"] is None
    rep = (out / "scene_report.md").read_text()
    assert "Every object in the scene" in rep and "box" in rep


def test_v2_queue_and_summary(tmp_path):
    import json
    from uncertainscene.eval import summarize
    from uncertainscene.experiments.scene01 import Spec, queue

    q = queue()
    names = [s if isinstance(s, str) else s.name for s in q]
    assert len(names) == len(set(names)) and names[0] == "p1_lightBa_vs_lightAa"
    pairs = {(s.a, s.b) for s in q if isinstance(s, Spec) and s.name[:2] in ("p1", "p2")}
    assert len(pairs) == 21                      # every pair of the 7 recordings, once
    assert all(not (s.a.endswith(("after_a", "after_b")) and "before" in s.b) for s in q if isinstance(s, Spec) and s.variant != "reversed")
    for i, (t, n) in enumerate([("static", 1), ("lighting", 2), ("change", 6), ("change+lighting", 5)]):
        d = tmp_path / f"s{i}"
        d.mkdir()
        (d / "metrics.json").write_text(json.dumps({"pair_type": t, "variant": "base", "changed_frac": 0.02,
                                                    "obj_changed": n, "obj_changed_frac": n / 40, "obj_moved": 1,
                                                    "a": "scene01_light_before_a", "b": "scene01_light_after_a",
                                                    "n_changes": n, "n_moved": 1, "n_removed": 0, "n_added": 0,
                                                    "n_appearance": 0, "step_seconds": 1}))
        (d / "changes.json").write_text(json.dumps([{"label": "guitar", "status": "added"}]))
    md = summarize.write(tmp_path, tmp_path / "summary").read_text()
    assert "AUROC 1.00" in md and "guitar added" in md
