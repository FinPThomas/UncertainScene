"""Node-structured object assets (change/assets.py)."""

import json

import numpy as np


def _obj(X, rec="A", oid=0, label="thing", status="unchanged", partner=None):
    from uncertainscene.change.segment import SceneObject

    return SceneObject(id=oid, recording=rec, X=X, frame=np.zeros(len(X), int), pix=np.zeros((len(X), 2)),
                       voxels=set(), masks={}, label=label, status=status, partner=partner, extra={"voxel": 0.02})


def test_assets_strain_hull_and_contacts(tmp_path):
    from uncertainscene.change import assets

    rng = np.random.default_rng(0)
    # cushion: a slab on the floor (y up is -y in the camera-style frame; use up = (0, 0, 1) here)
    cush = np.c_[rng.uniform(-0.25, 0.25, 3000), rng.uniform(-0.15, 0.15, 3000), rng.uniform(0, 0.1, 3000)]
    box = np.c_[rng.uniform(-0.05, 0.05, 800), rng.uniform(-0.05, 0.05, 800), rng.uniform(0.1, 0.2, 800)]
    objs = [_obj(cush, oid=0, label="cushion", status="changed in place"), _obj(box, oid=1, label="box", status="moved")]
    # matched feature points: cushion corner stretched outwards by 4 cm, box moved rigidly 5 cm
    XA = np.r_[cush[::6], box[::4]]
    XB = XA.copy()
    cn = (XA[:, 0] > 0.15) & (XA[:, 1] > 0.05) & (XA[:, 2] < 0.1001)
    XB[cn, 0] += 0.04 * (XA[cn, 0] - 0.15) / 0.1
    isbox = np.r_[np.zeros(len(cush[::6]), bool), np.ones(len(box[::4]), bool)]
    XB[isbox] += [0.05, 0, 0]
    C = np.tile(np.eye(3) * 0.001**2, (len(XA), 1, 1))
    motion = {"links_XA": XA, "links_XB": XB, "C": C, "obj_of": np.where(isbox, 1, 0)}
    scene = assets.build(objs, 1.0, np.array([0, 0, 1.0]), tmp_path, motion, motion_objs=objs, n_nodes=64)
    d = {o["name"]: o for o in scene["objects"]}
    assert d["A000_cushion"]["behaviour"] == "deformable"
    assert d["A001_box"]["behaviour"] == "rigid at this precision"
    c = json.loads((tmp_path / "A000_cushion.json").read_text())
    assert len(c["nodes"]) == 64 and c["hull"]["volume_m3"] > 0.01 and (tmp_path / "A000_cushion_hull.obj").exists()
    strained = [e for e in c["edges"] if e["strained"]]
    nodes = np.array([n["xyz"] for n in c["nodes"]])
    mids = np.array([(nodes[e["i"]] + nodes[e["j"]]) / 2 for e in strained])
    assert len(strained) >= 3 and (mids[:, 0] > 0.1).mean() > 0.8          # the strain is at the stretched corner
    print(scene["contacts"])
    assert any("box" in x["relation"] and "rests on" in x["relation"] for x in scene["contacts"])


def test_objects_round_trip(tmp_path):
    from uncertainscene.change import assets

    objs = [_obj(np.random.default_rng(1).normal(size=(50, 3)), status="moved", partner=3)]
    assets.save_objects(objs, tmp_path / "o.npz")
    back = assets.load_objects(tmp_path / "o.npz")
    assert back[0]["status"] == "moved" and back[0]["partner"] == 3 and back[0]["X"].shape == (50, 3)
