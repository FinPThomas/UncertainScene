"""What changed, in words (Fin, 2026-10-16): distance, turn, deformation with the Rigidity
Copilot, coverage, names by size, second copies from off pose groups, scale check."""
from types import SimpleNamespace

import numpy as np

UP = np.array([0.0, 0.0, 1.0])


def _box(lo, hi, n=1500, seed=0):
    return np.random.default_rng(seed).uniform(lo, hi, (n, 3))


def _o(i, rec, label, status, X, src="detector", prob=0.9, rig=None, views=20, top3=None):
    c = np.median(X, 0)
    e = np.sort(X.max(0) - X.min(0))[::-1]
    return SimpleNamespace(id=i, recording=rec, label=label, label_prob=prob, status=status, partner=None, X=X,
                           centroid=c, extent=e, structure=False, desc=None, stats={}, complete=True,
                           label_top3=top3 or [], masks={},
                           extra={"label_source": src, "n_views": views, "clip_top3": top3 or [],
                                  "rigidity": None if rig is None else {"rigidity": rig, "materials": [("fabric or cloth", 0.6)]},
                                  "position_cm": {"radius90_cm": 2.0}})


def _turn(X, deg, c=None):
    from uncertainscene.change.movement import _rot_about

    c = X.mean(0) if c is None else c
    return (X - c) @ _rot_about(UP, deg).T + c


def test_turn_is_measured_on_a_long_object_and_not_on_a_round_one():
    from uncertainscene.change.movement import shape_fit

    bat = _box([0, 0, 0], [0.30, 0.08, 0.02], seed=1)
    bat2 = _box([0, 0, 0], [0.30, 0.08, 0.02], seed=21)               # another sampling of the same bat
    f = shape_fit(bat, _turn(bat2, 50) + [0.4, 0, 0], UP)
    assert f["turn_helps"] and abs(abs(f["turn_deg"]) - 50) < 8 and f["tilt_deg"] < 5
    rng = np.random.default_rng(2)
    a = rng.uniform(0, 2 * np.pi, 1500)
    cup = np.c_[0.04 * np.cos(a), 0.04 * np.sin(a), rng.uniform(0, 0.1, 1500)]
    g = shape_fit(cup, _turn(cup, 50), UP)
    assert not g["turn_helps"]                    # a round cup has no measurable turn


def _scene(cushion_b, rig):
    """Four unchanged rigid objects (the noise null) plus a cushion that is reshaped in place."""
    from uncertainscene.change.movement import describe_all

    objs_a, objs_b = [], []
    for k in range(4):
        X = _box([k, 2, 0], [k + 0.3, 2.2, 0.15], seed=10 + k)
        Y = X + np.random.default_rng(20 + k).normal(0, 0.003, X.shape)
        a, b = _o(k, "A", "book", "unchanged", X), _o(k, "B", "book", "unchanged", Y)
        a.partner, b.partner = k, k
        objs_a.append(a)
        objs_b.append(b)
    cush_a = _box([0, 0, 0], [0.45, 0.45, 0.15], seed=3)
    ca, cb = _o(4, "A", "cushion", "unchanged", cush_a, rig=rig), _o(4, "B", "cushion", "unchanged", cushion_b, rig=rig)
    ca.partner, cb.partner = 4, 4
    for o in (ca, cb):
        o.extra["clip"] = np.ones(4) / 2
    objs_a.append(ca)
    objs_b.append(cb)
    E = np.tile(np.c_[np.eye(3), np.zeros(3)], (2, 1, 1))
    summ = describe_all(objs_a, objs_b, 1.0, UP, E, {"gone": 0.3}, align_cm=0.5)
    return ca, cb, summ


def test_squashed_cushion_is_deformed_not_moved_but_a_stiff_one_is_not():
    rng = np.random.default_rng(4)
    squashed = _box([0, 0, 0], [0.45, 0.45, 0.15], seed=5)
    squashed[:, 2] *= np.where(squashed[:, 0] > 0.22, 0.35, 1.0)     # one half pressed down
    squashed += rng.normal(0, 0.002, squashed.shape)
    ca, cb, summ = _scene(squashed, rig=0.2)
    assert ca.status == cb.status == "deformed" and summ["deformed"] == 1
    assert "deformed but did not move away" in ca.extra["change_sentence"]
    assert "Rigidity Copilot" in ca.extra["change_sentence"]
    ca2, _, _ = _scene(squashed.copy(), rig=0.95)                    # ceramic-stiff: not deformation
    assert ca2.status == "unchanged" and "too stiff to deform" in ca2.extra["change_sentence"]


def test_moved_object_sentence_has_distance_direction_context_and_how_sure():
    from uncertainscene.change.movement import describe_all

    X = _box([0, 0, 0], [0.30, 0.08, 0.02], seed=6)
    Y = _box([0, 0, 0], [0.30, 0.08, 0.02], seed=26)
    a, b = _o(0, "A", "table tennis bat", "moved", X), _o(0, "B", "table tennis bat", "moved", _turn(Y, 60) + [0.5, 0, 0])
    a.partner = b.partner = 0
    a.stats["match_similarity"] = 0.9
    a.extra["context"] = {"on_top_of": {"object": "sofa A3"}, "next_to": [{"object": "cushion A2"}]}
    b.extra["context"] = {"on_top_of": {"object": "table B5"}, "next_to": [{"object": "book B1"}]}
    E = np.tile(np.c_[np.eye(3), np.zeros(3)], (2, 1, 1))
    describe_all([a], [b], 1.0, UP, E, {"gone": 0.3}, align_cm=1.0)
    s = a.extra["change_sentence"]
    assert "moved 50 cm" in s and "from on top of the sofa, next to the cushion to on top of the table" in s
    import re
    assert abs(int(re.search(r"turned about (\d+) deg", s).group(1)) - 60) <= 3
    assert "sure it moved" in s and "SAM 2 masks" in s
    assert a.extra["change"]["p_change"] > 0.8


def test_removed_only_where_the_other_video_looked():
    from uncertainscene.change.movement import guard_coverage

    H, W = 60, 80
    K = np.array([[60.0, 0, 40], [0, 60.0, 30], [0, 0, 1]])
    E0 = np.c_[np.eye(3), [0, 0, 0]]
    E_away = np.c_[np.eye(3) @ np.diag([1, 1, 1]), [0, 0, -10]]      # B looks the other way (object behind it)
    rec = SimpleNamespace(K=np.stack([K] * 4), E=np.stack([E0, E0, E_away, E_away]), video=np.array([0, 0, 1, 1]),
                          depth=np.full((4, H, W), 5.0, np.float32))
    o = _o(0, "A", "hat", "removed", _box([-0.1, -0.1, 2.0], [0.1, 0.1, 2.2], n=300))
    out = guard_coverage([o], [], rec, tau=0.1)
    assert o.status == "not seen in other recording" and out
    o2 = _o(1, "A", "hat", "removed", _box([-0.1, -0.1, 2.0], [0.1, 0.1, 2.2], n=300))
    rec.E = np.stack([E0] * 4)
    rec.E = np.concatenate([rec.E[:2], np.stack([E0] * 6)])
    rec.K = np.stack([K] * 8)
    rec.video = np.array([0, 0, 1, 1, 1, 1, 1, 1])
    rec.depth = np.full((8, H, W), 5.0, np.float32)                  # the wall behind: B sees through the hat
    guard_coverage([o2], [], rec, tau=0.1)
    assert o2.status == "removed" and o2.extra["coverage"]["views"] == 6


def test_names_follow_measured_size():
    """Run 11: a 30 cm glass vase called "cup" by the detector; a bat called "lamp" in one
    recording and "table tennis bat" in the other."""
    from uncertainscene.change.inventory import names_by_size

    vase = _o(0, "A", "cup", "moved", _box([0, 0, 0], [0.30, 0.11, 0.11], seed=7),
              top3=[("glass vase", 0.3), ("cup", 0.25), ("vase", 0.2)])
    lamp = _o(1, "A", "lamp", "moved", _box([0, 0, 0], [0.27, 0.15, 0.02], seed=8))
    bat = _o(1, "B", "table tennis bat", "moved", _box([0, 0, 0], [0.27, 0.15, 0.02], seed=9), src="clip", prob=0.45,
             top3=[("table tennis bat", 0.45), ("lamp", 0.1)])
    lamp.partner, bat.partner = 1, 1
    mug = _o(2, "A", "cup", "unchanged", _box([0, 0, 0], [0.10, 0.08, 0.08], seed=10))
    out = names_by_size([vase, lamp, mug], [bat], 1.0)
    assert vase.label == "glass vase" and lamp.label == bat.label == "table tennis bat" and mug.label == "cup"
    assert len(out) == 2


def test_second_copy_from_off_poses_is_dropped_but_a_different_side_is_kept():
    from uncertainscene.change.segment import split_copies

    rng = np.random.default_rng(11)

    def surface(lo, hi, n, seed):                   # points on a box's faces (what depth maps see)
        P = _box(lo, hi, n, seed)
        ax = np.random.default_rng(seed + 1).integers(0, 3, n)
        side = np.random.default_rng(seed + 2).integers(0, 2, n)
        P[np.arange(n), ax] = np.where(side, np.asarray(hi)[ax], np.asarray(lo)[ax])
        return P

    obj = surface([0, 0, 0], [0.4, 0.25, 0.1], 6000, 12)
    X, fr = [], []
    for f in range(12):                             # 8 good frames, 4 frames whose poses sit 12 cm off
        P = obj[rng.choice(len(obj), 300, replace=False)] + rng.normal(0, 0.003, (300, 3))
        X.append(P + ([0.12, 0.05, 0] if f >= 8 else 0))
        fr += [f] * 300
    X, fr = np.concatenate(X), np.array(fr)
    look = np.array([0.0, 0.0, -1.0])               # every frame looks down from along one path
    cams = {f: (np.array([0.05 * f, 0.0, 1.0]), look) for f in range(12)}
    keep, info = split_copies(X, fr, r=0.015, cams=cams)
    assert info["copies_dropped"] == 1 and info["frames_dropped"] == 4 and set(fr[keep]) == set(range(8))
    # a long object seen in two halves by two groups of frames: nothing is a copy
    sofa = surface([0, 0, 0], [2.0, 0.8, 0.8], 12000, 13)
    X, fr = [], []
    for f in range(10):
        half = sofa[sofa[:, 0] < 0.9] if f < 5 else sofa[sofa[:, 0] > 1.1]
        X.append(half[rng.choice(len(half), 300, replace=False)])
        fr += [f] * 300
    cams = {f: (np.array([0.45 if f < 5 else 1.55, -1.0, 0.5]) + 0.02 * f, np.array([0.0, 1.0, 0.0])) for f in range(10)}
    keep, info = split_copies(np.concatenate(X), np.array(fr), r=0.03, cams=cams)
    assert info["copies_dropped"] == 0 and keep.all()


def test_scale_check_flags_a_wrong_scale():
    from uncertainscene.change.inventory import scale_check

    objs = [_o(0, "A", "guitar", "unchanged", _box([0, 0, 0], [1.0, 0.38, 0.1], seed=14)),
            _o(1, "A", "book", "unchanged", _box([0, 0, 0], [0.23, 0.16, 0.03], seed=15)),
            _o(2, "A", "table tennis bat", "unchanged", _box([0, 0, 0], [0.26, 0.15, 0.02], seed=16))]
    ok = scale_check(objs, 1.0)
    assert ok["fit"] == 3 and "looks right" in ok["verdict"]
    bad = scale_check(objs, 2.0)
    assert bad["fit"] < 3 and "too big" in bad["verdict"]


def test_neighbour_pieces_isolate_one_copy():
    from uncertainscene.change.assets import neighbour_pieces

    obj = _box([0, 0, 0], [0.3, 0.2, 0.05], n=3000, seed=30)
    X = np.concatenate([obj, obj[:1500] + [0.5, 0, 0]])           # the object twice, the copy smaller
    pc = neighbour_pieces(X, 0.03)
    assert pc["pieces"] == 2 and pc["largest"][0]["share"] > 0.6 and (pc["labels"][:3000] == 0).all()
    assert neighbour_pieces(X, 0.6)["pieces"] == 1
