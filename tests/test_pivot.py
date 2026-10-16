"""Chunked reconstruction, floor/scale, SAM-style objects and object-level changes on the
synthetic scene from test_change (numpy only)."""

from __future__ import annotations

import json

import numpy as np
import pytest

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
    # extents are 5-95% spans (0.9 of the length); true scale 2.4 m/unit
    obj = object_scale([{"label": "mug", "label_prob": 0.9, "extent_units": 0.9 * 0.12 / 2.4},
                        {"label": "guitar", "label_prob": 0.8, "extent_units": 0.9 * 1.0 / 2.4},
                        {"label": "wall", "label_prob": 0.9, "extent_units": 1.0}])      # no size: ignored
    assert obj["n"] == 2 and 2.3 < obj["scale"] < 2.5
    fl = combine_scale(fl, obj)                     # phone height is reported, never used
    assert 2.3 < fl.scale_m_per_unit < 2.5 and fl.scale_range[0] < fl.scale_m_per_unit < fl.scale_range[1]


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

    def fake(crops, *a, groups=None, **k):
        reds = []
        for c in crops:
            a_ = np.asarray(c).reshape(-1, 3).astype(float)
            reds.append(np.mean((a_[:, 0] > 150) & (a_[:, 1] < 80)))
        tops, embs = [], []
        for g in (groups if groups is not None else [[i] for i in range(len(crops))]):
            red = float(np.mean([reds[i] for i in g]))
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
    monkeypatch.setattr(scene01, "_masks", lambda rec_, spec, cache, store=None: _fake_masks(
        rec_, key_frames(rec_, 0, 8) + key_frames(rec_, 1, 8)))
    from uncertainscene.change import motion3d
    from uncertainscene.recon import poses

    monkeypatch.setattr(poses, "refine_recon", lambda rec_, cache, key, cfg=None: (rec_, None, {
        "vggt": {"median_px": 8.0}, "final": {"median_px": 0.7, "points": 10}, "rotation_change_deg_median": 0.4}))

    def fake_measure(rec_, objs, scale, cfg=None):     # one object moved 5 cm, its corner bent 3 cm
        o = [x for x in objs if not x.structure][0]
        XA = o.X[:50]
        XB = XA + [0.05 / scale, 0, 0]
        resid = np.linspace(0, 3, len(XA))
        row = {"recording": o.recording, "id": o.id, "label": o.label, "status": o.status, "links": 50,
               "rigid_inlier_share": 0.9, "moved_cm": 5.0, "moved_ci_cm": [4.5, 5.5], "moved_beyond_noise": True,
               "rotation_deg": 3.0, "deform_p90_cm": 2.7, "deform_peak_cm": 3.0, "deform_peak_sigma_cm": 0.4, "deformed_share": 0.4,
               "deformed": True, "deform_peak_where": "at a corner", "deform_peak_axes": [0.9, 0.8],
               "deform_peak_xyz": XA[-1].tolist()}
        obj_of = np.full(len(XA), objs.index(o))
        return {"links_XA": XA, "links_XB": XB, "disp_cm": np.full(len(XA), 5.0), "resid_cm": resid, "obj_of": obj_of,
                "objects": [row], "sigma_cm": np.full(len(XA), 0.4), "z": np.full(len(XA), 12.0),
                "moved_pt": np.ones(len(XA), bool), "deform_sig": resid > 1, "level": 0.975,
                "seconds": 1, "summary": {"links": 50, "point_sigma_cm_median": 0.4, "misalignment_sigma_cm": 0.1,
                                          "links_moved_share": 1.0, "objects_measured": 1, "objects_moved": 1,
                                          "objects_deformed": 1}}

    monkeypatch.setattr(motion3d, "measure", fake_measure)
    spec = scene01.Spec("p1_test", "scene01_light_before_a", "scene01_light_after_a",
                        detect={"stride": 1, "conf_quantile": 0.0}, voxel_m=0.02)
    out = tmp_path / "out"
    out.mkdir()
    note = scene01.step_fn(spec, tmp_path / "cache")(out, tmp_path)
    assert "changed objects" in note
    assert (out / "motion_map.png").exists() and (out / "motion.json").exists()
    assert (out / "assets" / "scene_graph.json").exists() and len(list((out / "assets").glob("*_hull.obj"))) > 0
    assert (tmp_path / "cache" / "objects_scene01_light_before_a_scene01_light_after_a_nall_o0.npz").exists()
    rp = (out / "report.md").read_text()
    assert "at a corner" in rp and "8.0 -> 0.70 px" in rp
    for n in ("metrics.json", "objects.json", "changes.json", "objects.png", "object_overlays.png", "heatmaps.png",
              "viewer.html", "scene_report.md", "report.md", "step_log.txt", "position_map.png",
              "position_precision.png", "scene3d.json"):
        assert (out / n).exists() and (out / n).stat().st_size > 100, n
    import json
    m = json.loads((out / "metrics.json").read_text())
    assert m["obj_moved"] == 1 and m["version"] == 2 and m["sam_error"] is None
    assert m["title"].startswith("Objects moved, same lighting") and m["question"]
    # fused ranges count views from one stretch of video once, and never undercut how much the
    # views really disagree, so they can be wider than one view's model range (2026-10-15)
    assert m["pos_radius90_cm_median"] > 0 and m["pos_single_view_radius90_cm_median"] > 0
    from uncertainscene.viz import scene3d

    sc = scene3d.load(out)
    assert {l["kind"] for l in sc["layers"]} >= {"colour", "heat", "objects", "line"} and any("ring" in o for o in sc["objects"])
    assert any(l["name"].startswith("heat map: deformation") for l in sc["layers"])
    assert all(o.get("pal") for o in sc["objects"])
    assert "What this run tests" in (out / "report.md").read_text()
    rep = (out / "scene_report.md").read_text()
    assert "Every object in the scene" in rep and "box" in rep


def test_v2_queue_and_summary(tmp_path):
    import json
    from uncertainscene.eval import summarize
    from uncertainscene.experiments.scene01 import Spec, queue

    # two streams (Fin, 2026-10-15): each starts with its re-aligned objects steps
    light = [s if isinstance(s, str) else s.name for s in queue("light")]
    dark = [s if isinstance(s, str) else s.name for s in queue("dark")]
    # (Fin, 2026-10-16): the light stream compares the after recording with the light and then the
    # dark before recording, so a change seen in the dark can be compared with the light
    assert light[:3] == ["1e-changes_moved_light-before-a_vs_light-after-a",
                         "1e-changes_moved-and-light-changed_dark-before-a_vs_light-after-a", "1e-changes_summary-light"]
    assert dark[0] == "1e-changes_no-change_light-before-a_vs_light-before-b"
    assert not any("dark" in n for n in light[3:]) and all("dark" in n or "summary" in n for n in dark[2:])
    assert all(queue("light")[i].align_rounds == 2 and queue("light")[i].variant == "objects3" for i in range(2))
    assert not any(n.startswith("1d-objects") for n in light + dark)
    q = queue()
    names = [s if isinstance(s, str) else s.name for s in q]
    assert len(names) == len(set(names)) and names[0] == light[0]
    i0 = names.index("1-main_moved_light-before-a_vs_light-after-a")
    assert names[i0 + 1] == "1b-splat_moved_light-before-a_vs_light-after-a"     # splats reuse the pilot's reconstruction
    assert names[i0 + 2] == "1c-objects_moved_light-before-a_vs_light-after-a"   # whole objects on the pilot pair
    assert names[i0 + 3] == "1-main_no-change_light-before-a_vs_light-before-b"
    assert any(n.startswith("2-position_") for n in names)
    pairs = {(s.a, s.b) for s in q if isinstance(s, Spec) and s.name.startswith(("1-main", "3-allpairs"))}
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


class _Obj:
    """Minimal stand-in for a SceneObject: points from n views, each view offset by noise."""

    def __init__(self, centre, n_views, sd, rng, structure=False):
        offs = rng.normal(0, sd, (n_views, 3))
        self.X = np.concatenate([centre + o + rng.normal(0, 0.001, (40, 3)) for o in offs])
        self.frame = np.repeat(np.arange(n_views), 40)
        self.centroid = np.asarray(centre, float)
        self.extent = np.array([0.2, 0.1, 0.05])
        self.structure = structure
        self.extra = {}
        self.desc = None
        self.label, self.label_prob = "cup", 0.9


def test_position_regions_shrink_and_are_honest():
    from uncertainscene.change import position as pos

    rng = np.random.default_rng(0)
    up = np.array([0, -1.0, 0])
    hits, r1, r2 = [], [], []
    for t in range(300):
        c = rng.uniform(-1, 1, 3)
        a, b = _Obj(c, 12, 0.02, rng), _Obj(c, 12, 0.02, rng)
        pos.annotate([a, b], 1.0, up)
        ea, eb = a.extra["position"], b.extra["position"]
        hits.append(pos.mahalanobis2(ea, eb) <= pos.CHI2_3_90)
        r1.append(pos.radius90(ea["cov"]))
        r2.append(pos.radius90(pos.pooled([a, b])["cov"]))
    assert 0.8 < np.mean(hits) < 0.97                       # regions are about right
    assert 0.65 < np.median(r2) / np.median(r1) < 0.92     # two agreeing clips shrink (cautiously: between-clip spread has 1 dof)
    # two clips that disagree (offset far beyond their view noise): pooling must not shrink the region
    c = np.zeros(3)
    a, b = _Obj(c, 12, 0.02, rng), _Obj(c + 0.3, 12, 0.02, rng)
    pos.annotate([a, b], 1.0, up)
    assert pos.radius90(pos.pooled([a, b])["cov"]) > 3 * pos.radius90(a.extra["position"]["cov"])
    # more views -> smaller region, roughly 1/sqrt(n)
    objs = [_Obj(rng.uniform(-1, 1, 3), 32, 0.02, rng) for _ in range(20)]
    curve = pos.views_curve(objs, 1.0, ns=(2, 8))
    assert 1.6 < curve[2]["median_radius90_cm"] / curve[8]["median_radius90_cm"] < 2.5
    e = pos.floor_ellipse(np.zeros(3), np.eye(3), up)
    assert abs(e["a"] - np.sqrt(pos.CHI2_2_90)) < 1e-6


def test_clips_step_end_to_end(tmp_path, monkeypatch):
    """Phase 2 step on four synthetic clips (two before, two after; the box moves)."""
    import json
    from uncertainscene.change.segment import key_frames
    from uncertainscene.experiments import clips, scene01
    from uncertainscene.recon.frames import Recon

    _fake_clip(monkeypatch)
    r1 = make_recon((-0.5, 0.8, 0.0), (-0.5, 0.8, 0.0), seed=0)
    r2 = make_recon((0.0, 0.8, 0.0), (0.0, 0.8, 0.0), seed=1)
    rec = _with_files(Recon(np.concatenate([r1.images, r2.images]), np.concatenate([r1.depth, r2.depth]),
                            np.concatenate([r1.conf, r2.conf]), np.concatenate([r1.K, r2.K]),
                            np.concatenate([r1.E, r2.E]), np.concatenate([r1.video, r2.video + 2])), tmp_path)
    monkeypatch.setattr(clips, "_recon", lambda spec, cache: (rec, {"chunks": 1, "chunk_residuals": []}))
    monkeypatch.setattr(scene01, "_masks", lambda rec_, spec, cache, store=None: _fake_masks(
        rec_, [k for v in range(4) for k in key_frames(rec_, v, 8)]))
    spec = scene01.ClipsSpec("2-position_test", ("scene01_light_before_a", "scene01_light_before_b",
                                                 "scene01_light_after_a", "scene01_light_after_b"),
                             ("before", "before", "after", "after"), title="test clips", question="q?")
    out = tmp_path / "out"
    out.mkdir()
    note = scene01.step_fn(spec, tmp_path / "cache")(out, tmp_path)
    m = json.loads((out / "metrics.json").read_text())
    assert m["step_kind"] == "clips" and m["matched_objects"] >= 1, note
    c = m["pos_clips_curve"]
    assert c["2"]["median_radius90_cm"] < c["1"]["median_radius90_cm"]
    for n in ("position_map.png", "position_precision.png", "scene3d.json", "report.md", "positions.json"):
        assert (out / n).exists() and (out / n).stat().st_size > 100, n


def test_filter_shrinks_with_views_and_nis_flags_overconfidence():
    from uncertainscene.change import position as pos

    rng = np.random.default_rng(1)
    truth = np.array([0.3, 0.8, 1.0])

    def views(n, sd_true, sd_claimed):
        out = []
        for _ in range(n):
            ray = rng.normal(size=3)
            ray /= np.linalg.norm(ray)
            out.append({"c": truth + rng.normal(0, sd_true, 3), "ray": ray, "sd_depth": sd_claimed,
                        "sd_side": sd_claimed, "pixel": 0.001})
        return out

    honest = [pos.ekf(views(20, 0.02, 0.02)) for _ in range(50)]
    nis = np.mean([np.mean(e["nis"]) for e in honest])
    assert 2.4 < nis < 3.6                                    # honest uncertainties: NIS ~ 3
    tr = np.median([e["trace"] for e in honest], 0)
    assert tr[0] > tr[3] > tr[15]                             # every view narrows the range
    cover = np.mean([(e["mean"] - truth) @ np.linalg.solve(e["cov"], e["mean"] - truth) <= pos.CHI2_3_90
                     for e in honest])
    assert cover > 0.8
    over = pos.ekf(views(20, 0.06, 0.02))                     # true noise 3x the claimed noise
    assert np.mean(over["nis"]) > 15


def test_viewer_point_encoding_round_trip():
    """The viewer decodes x = lo + sc * (q + 32500); clouds must land where the objects are."""
    import base64
    from uncertainscene.viz.scene3d import Points

    X = np.random.default_rng(0).uniform(-1, 3, (500, 3))
    X[7] = np.nan
    p = Points()
    t = p.tables[p.add(X)]
    q = np.frombuffer(base64.b64decode(t["q"]), np.int16).reshape(-1, 3)
    dec = np.array(t["lo"]) + np.array(t["sc"]) * (q + 32500.0)
    ok = np.isfinite(X).all(1)
    assert np.abs(dec[ok] - X[ok]).max() < 1e-3 and (q[~ok] == -32768).all()


def test_splat_step_end_to_end(tmp_path, monkeypatch):
    """analyse_splat with gsplat replaced by a fake splat that renders the photo (own recording)
    or the photo with a changed patch (other recording): outputs written, change found, noise ~1%."""
    torch = pytest.importorskip("torch")
    from uncertainscene.experiments import scene01, splats
    from uncertainscene.recon import splat as S

    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0)), tmp_path)
    monkeypatch.setattr(scene01, "_recon", lambda spec, cache: (rec, {"cached": True}))
    rng = np.random.default_rng(0)

    class Fake(S.Splat):
        def __init__(self, v):
            n = 500
            super().__init__({"means": torch.randn(n, 3), "scales": torch.full((n, 3), -3.0), "opacities": torch.zeros(n),
                              "quats": torch.randn(n, 4), "colors": torch.zeros(n, 3)})
            self.v = v

        def render(self, E, K, wh):
            s = int(np.argmin([np.abs(e - E).sum() for e in rec.E]))
            img = S._images([rec.files[s]], max(wh))[0][0].astype(np.float32) / 255
            img = np.clip(img + rng.normal(0, 0.01, img.shape), 0, 1)
            if rec.video[s] != self.v:              # the other recording's splat: a patch changed
                img[: wh[1] // 4, : wh[0] // 4] = 1 - img[: wh[1] // 4, : wh[0] // 4]
            return img, np.ones(img.shape[:2]), np.ones(img.shape[:2])

    from uncertainscene.recon import poses

    got = {}

    def fake_refine(rec_, cache, key, cfg=None):
        X = np.random.default_rng(1).normal(0, 0.3, (40, 3)).astype(np.float32)
        seen = np.zeros((40, 2), bool)
        seen[:30, 0] = seen[10:, 1] = True
        return rec_, {"X": X, "rgb": np.zeros((40, 3), np.uint8), "seen": seen}, {
            "vggt": {"median_px": 9.0}, "final": {"median_px": 0.6, "points": 40}, "rotation_change_deg_median": 0.5}

    monkeypatch.setattr(poses, "refine_recon", fake_refine)

    def fake_train(rec_, v, cfg, check=None, points=None):
        got[v] = len(points["X"])
        return Fake(v), {
        "recording": rec_.labels[v], "gaussians": 500, "seconds": 1, "pose_correction_deg_median": 0.0,
        "train_frames": 7, "held_out_frames": 1, "iters": cfg.iters, "image_size": [64, 48], "final_l1": 0.01}

    monkeypatch.setattr(S, "train", fake_train)
    from uncertainscene.recon import gsplat_build

    monkeypatch.setattr(gsplat_build, "build", lambda check=None: {"ok": True, "already_built": True})
    from uncertainscene.change import assets
    from uncertainscene.change.segment import SceneObject

    Xo = np.random.default_rng(2).normal(0, 1, (300, 3))
    so = [SceneObject(id=i, recording=r, X=Xo, frame=np.zeros(300, int), pix=np.zeros((300, 2)), voxels=set(), masks={},
                      label="cushion", status=st, partner=1 - i, extra={"voxel": 0.6})
          for i, (r, st) in enumerate((("A", "moved"), ("B", "moved")))]
    assets.save_objects(so, tmp_path / "cache" / f"objects_{scene01._key(scene01.Spec('x', 'scene01_light_before_a', 'scene01_light_after_a'))}.npz")
    spec = splats.SplatSpec("1b-splat_test", "scene01_light_before_a", "scene01_light_after_a", iters=10,
                            title="Gaussian splats: test", question="q?")
    out = tmp_path / "out"
    out.mkdir()
    note = scene01.step_fn(spec, tmp_path / "cache")(out, tmp_path)
    m = json.loads((out / "metrics.json").read_text())
    assert m["step_kind"] == "splat", note
    assert m["same_view_changed_share"] > 0.03                      # the changed patch is found
    for n in ("splat_compare.png", "splat_orbit.gif", "scene3d.json", "report.md", "splat_viewer/splat_viewer.html"):
        assert (out / n).exists() and (out / n).stat().st_size > 100, n
    assert len(list((out / "object_splats").glob("*.splat"))) == 2         # the moved cushion, before and after
    assert len(list((out / "splat_viewer" / "splats").iterdir())) == 4
    assert got == {0: 30, 1: 30}                                    # each splat starts from the points its recording saw
    assert m["poses"]["final"]["median_px"] == 0.6 and "0.60 px" in (out / "report.md").read_text()


MV = None     # default merge grid (set to 0.01, the fine grid, the old behaviour fails this test)


def test_views_merge_despite_depth_noise(tmp_path, monkeypatch):
    """Real VGGT depth is off by several cm per view; views of one object must still merge into
    one multi-view object (run 2 found 1107 of 1122 objects seen in a single view)."""
    from test_change import colour_feats
    from uncertainscene.change.segment import build_objects, key_frames

    _fake_clip(monkeypatch)
    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (-0.5, 0.8, 0.0)), tmp_path)
    rng = np.random.default_rng(0)
    rec.depth = rec.depth * (1 + rng.normal(0, 0.04, (len(rec.depth), 1, 1))) \
        * (1 + rng.normal(0, 0.01, rec.depth.shape))           # per-view offset + per-pixel noise
    rec.conf = np.full_like(rec.depth, 0.2 / 0.05)             # VGGT-style confidence: ~5 cm depth error
    masks = _fake_masks(rec, key_frames(rec, 0, 8))
    objs = build_objects(rec, masks, "A", voxel=0.01, structure_units=1.2, min_voxels=3, merge_vox=MV)
    box = [o for o in objs if np.all(rec.images[o.best_frame][o.masks[o.best_frame]][:1] == [200, 30, 30])]
    assert box and box[0].extra["n_views"] >= 6, [o.extra["n_views"] for o in objs]


def test_bundle_adjustment_recovers_poses():
    """Synthetic scene: VGGT-like pose errors (0.7 deg, 1 cm, focal 3% off) and 3% outlier
    matches; triangulation + bundle adjustment must bring relative rotations back near truth."""
    pytest.importorskip("torch")
    from scipy.spatial.transform import Rotation as Rot

    from uncertainscene.recon import poses as P

    rng = np.random.default_rng(0)
    N, S, W, H, f = 1200, 24, 1024, 576, 800.0
    X = np.c_[rng.uniform(-.6, .6, N), rng.uniform(-.4, .4, N), 1.0 + 0.15 * np.sin(3 * rng.uniform(-1, 1, N))]

    def look(c):
        z = np.array([0, 0, 1.0]) - c
        z /= np.linalg.norm(z)
        x = np.cross([0, 1, 0], z)
        x /= np.linalg.norm(x)
        R = np.stack([x, np.cross(z, x), z])
        return np.c_[R, -R @ c]

    E = np.stack([look(np.array([0.5 * np.sin(a), 0.05 * np.cos(3 * a), 0.2 * np.cos(a) - 0.1]))
                  for a in np.linspace(-1, 1, S)])
    video = np.repeat([0, 1], S // 2)
    K = np.tile(np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1.0]]), (S, 1, 1))
    rows = []
    for s in range(S):
        Xc = X @ E[s, :, :3].T + E[s, :, 3]
        u, v = f * Xc[:, 0] / Xc[:, 2] + W / 2, f * Xc[:, 1] / Xc[:, 2] + H / 2
        i = np.nonzero((u > 0) & (u < W) & (v > 0) & (v < H) & (rng.random(N) < 0.7))[0]
        rows.append(np.c_[i, np.full(len(i), s), u[i] + rng.normal(0, .5, len(i)), v[i] + rng.normal(0, .5, len(i))])
    O = np.concatenate(rows)
    t, fr, uv = O[:, 0].astype(int), O[:, 1].astype(int), O[:, 2:]
    bad = rng.random(len(uv)) < 0.03
    uv[bad] += rng.normal(0, 40, (bad.sum(), 2))
    Ep = E.copy()
    for s in range(S):
        dR = Rot.from_rotvec(rng.normal(0, np.radians(0.7) / np.sqrt(3), 3)).as_matrix()
        Ep[s, :, :3], Ep[s, :, 3] = dR @ E[s, :, :3], dR @ E[s, :, 3] + rng.normal(0, 0.01, 3)
    Kp = K.copy()
    Kp[:, :2, :2] *= 1.03

    def rel_err(Ea):
        e = [np.degrees(np.arccos(np.clip((np.trace((E[i, :, :3] @ E[j, :, :3].T).T @ Ea[i, :, :3] @ Ea[j, :, :3].T)
                                            - 1) / 2, -1, 1))) for i in range(S) for j in range(i + 1, S)]
        return float(np.median(e))

    cfg = P.PoseConfig(iters=150)
    Xt, tn, fn, uvn = P._filter(Ep, Kp, t, fr, uv, cfg.first_px, cfg.min_angle_deg, cfg.max_points)
    Ec, Kc, Xb = P.bundle_adjust(Ep, Kp, video, Xt, tn, fn, uvn, cfg)
    Xt, tn, fn, uvn = P._filter(Ec, Kc, t, fr, uv, 6.0, cfg.min_angle_deg, cfg.max_points)
    Ec, Kc, Xb = P.bundle_adjust(Ec, Kc, video, Xt, tn, fn, uvn, cfg)
    E2, X2 = P._to_frame(Ec, Xb, Ep)
    assert rel_err(Ep) > 0.5
    assert rel_err(E2) < 0.2
    assert abs(Kc[0, 0, 0] / f - 1) < 0.02                         # started 3% off
    assert np.median(P.reproject(E2, Kc, X2, tn, fn, uvn)[0]) < 1.0

    # points that sit almost on a camera (z ~ 0) made the first real run diverge (8 -> 341 px);
    # with them added the adjustment must still end no worse than it started, and finite
    Xt, tn, fn, uvn = P._filter(Ep, Kp, t, fr, uv, cfg.first_px, cfg.min_angle_deg, cfg.max_points)
    c = -np.einsum("sji,sj->si", Ep[:, :, :3], Ep[:, :, 3])
    n0 = len(Xt)
    Xt = np.r_[Xt, c[:8] + 0.002 * (Ep[:8, 2, :3])]
    tn = np.r_[tn, np.repeat(np.arange(n0, n0 + 8), 2)]
    fn = np.r_[fn, np.c_[np.arange(8), (np.arange(8) + 5) % S].ravel()]
    uvn = np.r_[uvn, np.tile([W / 2, H / 2], (16, 1))]
    e0 = np.median(P.reproject(Ep, Kp, Xt, tn, fn, uvn)[0])
    Ec, Kc, Xb = P.bundle_adjust(Ep, Kp, video, Xt, tn, fn, uvn, cfg)
    e1 = np.median(P.reproject(Ec, Kc, Xb, tn, fn, uvn)[0])
    assert np.isfinite(e1) and e1 <= e0 * 1.05 + 0.05


def test_full_step_with_real_tracking_poses_motion_and_scale_paths(tmp_path, monkeypatch):
    """Like test_full_step_end_to_end but with far fewer stand-ins: the real mask cache and
    tracking glue (only SAM 2 itself faked), the real pose refinement, feature motion and metric
    scale code (only the depth network faked), and per-frame pose uncertainty set. Run 5 crashed
    in glue code the other test replaced; this one must reach the end with objects."""
    from test_change import colour_feats
    from uncertainscene.change import metric
    from uncertainscene.change import segment as S
    from uncertainscene.experiments import scene01

    _fake_clip(monkeypatch)
    rec = _with_files(make_recon((-0.5, 0.8, 0.0), (0.0, 0.8, 0.0)), tmp_path)
    rec.pose_px = np.linspace(0.2, 6.0, len(rec.E)).astype(np.float32)     # a few shaky frames
    monkeypatch.setattr(scene01, "_recon", lambda spec, cache: (rec, {"seconds": 1.0, "chunks": 1, "used_per_video": 8,
                                                                      "chunk_residual_max": 0.0}))
    monkeypatch.setattr(scene01, "_feats", lambda rec_, spec, cache: colour_feats(rec_))

    def fake_tracks(files, frames, size_hw, keep_frames=None, **kw):
        out = _fake_masks(rec, keep_frames)
        for ms in out.values():
            for k, m in enumerate(ms):
                m.update(track=k, wobble_px=(0.5, 0.3))
        return out

    monkeypatch.setattr(S, "sam_tracks", fake_tracks)
    monkeypatch.setattr("uncertainscene.change.detect2d.Detector", lambda **kw: object())   # no weights download
    true_scale = 2.5
    monkeypatch.setattr(metric, "_predictor", lambda model_id: (
        lambda f: rec.depth[rec.files.index(f)] * true_scale))
    spec = scene01.Spec("p1_test2", "scene01_light_before_a", "scene01_light_after_a", n_key=8,
                        detect={"stride": 1, "conf_quantile": 0.0}, voxel_m=0.02)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale_from_last_attempt.png").write_bytes(b"x" * 200)
    note = scene01.step_fn(spec, tmp_path / "cache")(out, tmp_path)
    assert "changed objects" in note
    assert not (out / "stale_from_last_attempt.png").exists()
    import json
    m = json.loads((out / "metrics.json").read_text())
    assert m["sam_error"] is None and m["obj_total"] > 0
    assert m.get("field_scale_metric_depth") and abs(m["field_scale_metric_depth"] / true_scale - 1) < 0.05
    assert (out / "assets" / "scene_graph.json").exists()
    assert list((tmp_path / "cache").glob("sam2td_*.npz"))      # tracked masks (with the detector) cached
    # a second attempt reuses the cached masks and still finishes
    out2 = tmp_path / "out2"
    out2.mkdir()
    monkeypatch.setattr(S, "sam_tracks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-tracked")))
    # ... also with the re-alignment on unchanged objects (objects rebuilt on the moved recording)
    from dataclasses import replace as _replace
    assert "changed objects" in scene01.step_fn(_replace(spec, align_rounds=2), tmp_path / "cache")(out2, tmp_path)
    m2 = json.loads((out2 / "metrics.json").read_text())
    assert isinstance(m2["recording_alignment"], list) and m2["sam_error"] is None
    # what changed, in words (objects3): every recording's tracks cached on their own, so a pair
    # that shares a recording (and a new session, via the release store) does not track it again
    assert len(list((tmp_path / "cache").glob("sam2rec_*_det.npz"))) == 2
    for f in (tmp_path / "cache").glob("sam2td_*.npz"):
        f.unlink()
    out3 = tmp_path / "out3"
    out3.mkdir()
    note3 = scene01.step_fn(_replace(spec, align_rounds=1, variant="objects3", motion=False), tmp_path / "cache")(out3, tmp_path)
    assert "changed objects" in note3
    m3 = json.loads((out3 / "metrics.json").read_text())
    assert m3["sam_error"] is None and "movement" in m3 and "scale_check" in m3
    rep = (out3 / "report.md").read_text()
    assert "## What changed, in words" in rep and "## Where the SAM 2 masks are used" in rep
    rows = json.loads((out3 / "objects.json").read_text())
    assert any(r.get("change_sentence") for r in rows if not r["structure"])
    assert not (out3 / "movement_error.txt").exists()
