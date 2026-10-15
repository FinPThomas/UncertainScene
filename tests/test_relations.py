"""Object context from point clouds: a cup standing on a sofa seat is "on top of" it, a book
beside the cup is "next to" it, and objects of different recordings are never related."""
from types import SimpleNamespace

import numpy as np

from uncertainscene.change.relations import relations


def _box(lo, hi, n=1500, seed=0):
    rng = np.random.default_rng(seed)
    return rng.uniform(lo, hi, (n, 3))


def _surface(lo, hi, n=4000, seed=0):
    """Points on the top and sides of a box (a reconstruction sees surfaces, not volumes)."""
    X = _box(lo, hi, n, seed)
    face = np.random.default_rng(seed + 9).integers(0, 3, n)
    X[face == 0, 2] = hi[2]                                     # top
    X[face == 1, 0] = np.where(X[face == 1, 0] > (lo[0] + hi[0]) / 2, hi[0], lo[0])
    X[face == 2, 1] = np.where(X[face == 2, 1] > (lo[1] + hi[1]) / 2, hi[1], lo[1])
    return X


def _obj(i, label, X, rec="A"):
    return SimpleNamespace(id=i, label=label, recording=rec, X=X, extra={})


def test_on_top_of_and_next_to():
    up = np.array([0, 0, 1.0])
    sofa = _obj(0, "sofa", _surface([0, 0, 0.0], [2, 0.8, 0.45], n=8000))
    cup = _obj(1, "cup", _box([1.0, 0.3, 0.46], [1.08, 0.38, 0.56], seed=1))
    book = _obj(2, "book", _box([1.15, 0.3, 0.46], [1.35, 0.45, 0.5], seed=2))
    other = _obj(0, "cup", _box([1.0, 0.3, 0.46], [1.08, 0.38, 0.56], seed=3), rec="B")
    rels = relations([sofa, cup, book, other], 1.0, up)
    assert cup.extra["context"]["on_top_of"]["object"] == "sofa A0"
    assert book.extra["context"]["on_top_of"]["object"] == "sofa A0"
    assert [n["object"] for n in cup.extra["context"]["next_to"]] == ["book A2"]
    assert set(sofa.extra["context"]["supports"]) == {"cup A1", "book A2"}
    assert sofa.extra["context"]["on_top_of"] is None
    assert not any("B0" in (r["a"], r["b"]) for r in rels)       # never across recordings
    assert "on top of the sofa" in cup.extra["context"]["sentence"] and "next to the book" in cup.extra["context"]["sentence"]


def test_far_apart_objects_are_unrelated():
    up = np.array([0, 0, 1.0])
    a = _obj(0, "cup", _box([0, 0, 0], [0.1, 0.1, 0.1]))
    b = _obj(1, "book", _box([1, 1, 0], [1.2, 1.2, 0.05]))
    assert relations([a, b], 1.0, up) == []
    assert a.extra["context"]["sentence"] == ""


def test_affordance_maps_from_objects():
    """A small rigid cup on a sofa is graspable, a big cushion is pushed, the free seat is a
    place to put things; the sofa itself is never grasped."""
    from uncertainscene.change.fields import object_affordance

    sofa_X = _surface([0, 0, 0.0], [2, 0.8, 0.45], n=8000)
    cup_X = _box([1.0, 0.3, 0.46], [1.08, 0.38, 0.56], seed=1)
    cush_X = _box([0.1, 0.1, 0.46], [0.6, 0.6, 0.6], seed=2)
    mk = lambda i, lab, X, ext, bg: SimpleNamespace(id=i, label=lab, recording="B", X=X, extent=np.array(ext),
                                                    structure=False, extra={"background": bg, "rigidity": {"rigidity": 0.9}})
    objs = [mk(0, "sofa", sofa_X, [2, 0.8, 0.45], True), mk(1, "cup", cup_X, [0.1, 0.08, 0.08], False),
            mk(2, "cushion", cush_X, [0.5, 0.5, 0.14], False)]
    relations(objs, 1.0, np.array([0, 0, 1.0]))
    X = np.concatenate([sofa_X, cup_X, cush_X])
    n = len(X)
    up_dot = np.where(np.isclose(X[:, 2], 0.45), 1.0, 0.0)
    f = {"which": "B", "X": X, "floor": {"scale_m_per_unit": 1.0}, "interact": np.ones(n), "reach": np.ones(n),
         "up_dot": up_dot}
    stats = object_affordance(f, objs)
    assert objs[1].extra["affordance"]["best"] == "grasp"
    assert objs[2].extra["affordance"]["best"] == "push"
    assert objs[0].extra["affordance"]["grasp"] == 0
    free_seat = (np.isclose(X[:, 2], 0.45)) & (X[:, 0] > 1.5)
    assert f["place"][free_seat].mean() > 0.9 and stats["place_frac"] > 0


def test_partner_links_agree_both_ways():
    """Run 6: the neck pillow was "removed" in A while B's neck pillow was "unchanged" with A's as
    its partner. A strong one-sided link makes both unchanged partners; a weak one is dropped."""
    from uncertainscene.change.inventory import consistent_partners

    def o(i, status, partner, iou, sim, label="neck pillow"):
        return SimpleNamespace(id=i, status=status, partner=partner, label=label,
                               stats={"counterpart_iou": iou, "counterpart_sim": sim}, extra={})
    a = [o(0, "removed", None, 0.6, 0.9), o(1, "removed", None, 0.1, 0.5)]
    b = [o(0, "unchanged", 0, 0.6, 0.9), o(1, "unchanged", 1, 0.2, 0.9)]
    assert consistent_partners(a, b) == 1
    assert (a[0].status, a[0].partner) == ("unchanged", 0)
    assert b[1].partner is None and a[1].status == "removed"
