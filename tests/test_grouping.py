"""Object identity, clean object clouds, camera groups and the robot maps (Fin, 15 Oct 17:27-17:34)."""
from types import SimpleNamespace

import numpy as np


def _box(lo, hi, n=1500, seed=0):
    return np.random.default_rng(seed).uniform(lo, hi, (n, 3))


def _o(i, rec, label, status, X, src="clip", prob=0.95, clip=None):
    c, e = X.mean(0), X.max(0) - X.min(0)
    return SimpleNamespace(id=i, recording=rec, label=label, label_prob=prob, status=status, partner=None, X=X,
                           centroid=c, extent=np.sort(e)[::-1], structure=False, desc=None, stats={},
                           extra={"label_source": src, "clip": np.ones(4) / 2 if clip is None else clip})


def test_one_neck_pillow_one_verdict():
    """Run 10: the neck pillow was "removed" in A and "unchanged" (no partner) in B, 3 cm apart:
    two clouds, opposite answers. Both become one unchanged pair; a far one becomes moved."""
    from uncertainscene.change.inventory import pair_by_identity

    a = [_o(0, "A", "neck pillow", "removed", _box([0, 0, 0], [0.42, 0.23, 0.09], seed=1)),
         _o(1, "A", "cup", "removed", _box([2, 0, 0], [2.08, 0.08, 0.1], seed=2))]
    b = [_o(0, "B", "neck pillow", "unchanged", _box([0.03, 0, 0], [0.47, 0.25, 0.1], seed=3)),
         _o(1, "B", "cup", "added", _box([3, 1, 0], [3.08, 1.08, 0.1], seed=4)),
         _o(2, "B", "book", "added", _box([0, 1, 0], [0.3, 1.2, 0.04], seed=5))]
    assert pair_by_identity(a, b) == 2
    assert (a[0].status, a[0].partner, b[0].status, b[0].partner) == ("unchanged", 0, "unchanged", 0)
    assert (a[1].status, b[1].status, a[1].partner) == ("moved", "moved", 1)
    assert b[2].status == "added" and b[2].partner is None


def test_unsure_or_different_names_are_not_paired():
    from uncertainscene.change.inventory import pair_by_identity

    a = [_o(0, "A", "neck pillow", "removed", _box([0, 0, 0], [0.4, 0.2, 0.1]), prob=0.2)]
    b = [_o(0, "B", "neck pillow", "unchanged", _box([0, 0, 0], [0.4, 0.2, 0.1]))]
    assert pair_by_identity(a, b) == 0
    a = [_o(0, "A", "cushion", "removed", _box([0, 0, 0], [0.4, 0.2, 0.1]))]
    assert pair_by_identity(a, b) == 0


def test_names_that_cannot_fit_are_replaced():
    """A 1.2 m "neck pillow" (run 10: a sofa seat) loses the name."""
    from uncertainscene.change.inventory import check_label_sizes

    seat = _o(0, "A", "neck pillow", "unchanged", _box([0, 0, 0], [1.2, 0.5, 0.2]), src="detector")
    seat.extra["clip_top3"] = [("neck pillow", 0.3), ("cushion", 0.2), ("sofa", 0.1)]
    pillow = _o(1, "A", "neck pillow", "unchanged", _box([0, 0, 0], [0.42, 0.23, 0.09]))
    assert check_label_sizes([seat, pillow], 1.0) == 1
    assert seat.label == "sofa" and seat.extra["background"] and pillow.label == "neck pillow"


def test_strap_inside_object_box_joins_it():
    """A smaller CLIP piece with another name that keeps lying inside a detector object's box in
    the images joins it; one outside the box does not."""
    from uncertainscene.change.inventory import group_pieces

    def m(y0, y1, x0, x1):
        k = np.zeros((40, 40), bool)
        k[y0:y1, x0:x1] = True
        return k

    def o(i, label, X, src, masks):
        return SimpleNamespace(id=i, recording="A", label=label, X=X, frame=np.zeros(len(X), int),
                               pix=np.zeros((len(X), 2), int), voxels={(i, 0, 0)}, masks=masks, structure=False,
                               extra={"label_source": src, "tracks": [i]}, centroid=None, extent=None)
    bag = o(0, "bag", _box([0, 0, 0], [0.4, 0.3, 0.3], seed=1), "detector", {f: m(5, 30, 5, 30) for f in range(3)})
    strap = o(1, "belt", _box([0.1, 0.1, 0.31], [0.3, 0.12, 0.33], seed=2), "clip",
              {f: m(8, 12, 10, 25) for f in range(3)})
    book = o(2, "book", _box([0.41, 0, 0], [0.6, 0.15, 0.03], seed=3), "clip", {f: m(30, 38, 31, 39) for f in range(3)})
    objs = [bag, strap, book]
    assert group_pieces(objs, 1.0) == 1
    assert [x.label for x in objs] == ["bag", "book"]


def test_clean_points_drops_weak_view_shell_and_strays():
    """An object seen in 6 frames: one frame with an uncertain pose puts a shifted copy beside it,
    and a few points float far away. Only the surface the views agree on is kept."""
    from uncertainscene.change.segment import clean_points

    rng = np.random.default_rng(0)
    X, fr = [], []
    surf = rng.uniform([0, 0, 0], [0.3, 0.3, 0.0], (6000, 3))
    for f in range(6):
        P = surf[rng.choice(len(surf), 800, replace=False)] + rng.normal(0, 0.002, (800, 3))
        if f == 5:
            P = P + np.array([0, 0, 0.08])                # the off view
        X.append(P)
        fr.append(np.full(len(P), f))
    X.append(rng.uniform([1, 1, 1], [1.5, 1.5, 1.5], (10, 3)))
    fr.append(np.full(10, 2))
    X, fr = np.concatenate(X), np.concatenate(fr)
    pose_px = np.array([0.4, 0.5, 0.4, 0.45, 0.5, 3.0])
    keep = clean_points(X, fr, 0.02, pose_px)
    assert not keep[fr == 5].any() and not keep[-10:].any()
    assert keep[(fr < 5)[: len(keep)]][:-10].mean() > 0.9


def test_loose_camera_group_found_and_jumps_counted():
    from uncertainscene.recon.poses import loose_frames, trajectory_jumps

    # frames 0-5 share many tracks, 6-8 share many among themselves but only 3 with the rest
    t, fr = [], []
    for k in range(100):
        t += [k] * 6
        fr += list(range(6))
        t += [1000 + k] * 3
        fr += [6, 7, 8]
    t += [5000, 5000, 5001, 5001, 5002, 5002]
    fr += [5, 6, 5, 6, 5, 6]
    loose = loose_frames(np.array(t), np.array(fr), 9, 50)
    assert loose.tolist() == [False] * 6 + [True] * 3
    E = np.tile(np.eye(3, 4), (8, 1, 1))
    E[:, 0, 3] = -np.array([0, 0.1, 0.2, 0.3, 1.3, 1.4, 1.5, 1.6])     # one jump of 10 steps
    assert trajectory_jumps(E, np.zeros(8, int)) == [1]


def test_floating_points_do_not_score_max_approach():
    """A surface and a few stray points in mid-air: the strays have a free path in every
    direction but no surface, so their approach is near zero; the surface keeps a good score."""
    from uncertainscene.change.fields import _local_shape

    rng = np.random.default_rng(0)
    surf = np.c_[rng.uniform(0, 0.5, (5000, 2)), np.zeros(5000)]
    stray = rng.uniform([0, 0, 0.3], [0.5, 0.5, 0.6], (10, 3))
    handle = np.c_[np.full(200, 0.25), np.linspace(0.6, 0.8, 200), np.full(200, 0.1)]
    support, thin = _local_shape(np.r_[surf, stray, handle], 0.025)
    assert support[5000:5010].max() < 0.2 and np.median(support[:5000]) == 1.0
    assert np.median(thin[5010:]) > 0.8 > np.median(thin[:5000])
