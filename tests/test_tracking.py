"""SAM 2 video tracking (change.segment.sam_tracks) with a stand-in tracker and mask generator:
one physical object must keep one track id across every frame (run 3 split one object into
2-9 pieces when masks were matched per frame), and objects that appear later are found."""

import numpy as np
import pytest
from PIL import Image

from uncertainscene.change import segment as S

H, W, N = 64, 96, 12
# objects: (first frame visible, top-left x at frame 0, y, size); x moves 2 px per frame
OBJ = [(0, 10, 10, 12), (0, 40, 30, 16), (5, 60, 8, 10)]


def _mask(k, f):
    start, x0, y0, s = OBJ[k]
    m = np.zeros((H, W), bool)
    if f >= start:
        x = x0 + 2 * f
        m[y0:y0 + s, x:x + s] = True
    return m


class FakeTracker:
    """Follows a seeded mask exactly (stands in for SAM 2's video predictor)."""

    def init_state(self, video_path, **kw):
        import os

        return {"n": len(os.listdir(video_path)), "seeds": {}}

    def reset_state(self, st):
        st["seeds"] = {}

    def add_new_mask(self, st, frame_idx, obj_id, mask):
        # which true object is this? (largest overlap)
        k = max(range(len(OBJ)), key=lambda k: (np.asarray(mask) & _mask(k, frame_idx)).sum())
        st["seeds"][obj_id] = k

    def propagate_in_video(self, st, start_frame_idx=0, reverse=False):
        import torch

        order = range(start_frame_idx, -1, -1) if reverse else range(start_frame_idx, st["n"])
        ids = list(st["seeds"])
        for f in order:
            lg = np.stack([np.where(_mask(st["seeds"][i], f), 5.0, -5.0) for i in ids])[:, None]
            yield f, ids, torch.as_tensor(lg)


def _gen_for(frames_files):
    def gen(img):
        f = next(i for i, p in enumerate(frames_files) if np.array_equal(np.asarray(Image.open(p)), img))
        out = []
        for k in range(len(OBJ)):
            m = _mask(k, f)
            if m.any():
                out.append({"segmentation": m, "area": int(m.sum()), "predicted_iou": 0.9, "stability_score": 0.95})
                part = m.copy()
                part[:, : np.nonzero(m.any(0))[0][0] + 3] = False   # a part of the same object
                out.append({"segmentation": part, "area": int(part.sum()), "predicted_iou": 0.9,
                            "stability_score": 0.9})
        return out
    return gen


def test_one_track_per_object(tmp_path):
    pytest.importorskip("torch")                  # sam_tracks runs SAM 2 under torch (not installed in CI)
    files = []
    rng = np.random.default_rng(0)
    for f in range(N):
        p = tmp_path / f"{f}.jpg"
        Image.fromarray(rng.integers(0, 255, (H, W, 3), dtype=np.uint8)).save(p, quality=100)
        files.append(str(p))
    # decode once more so the generator can recognise frames by their pixels
    out = S.sam_tracks(files, list(range(N)), (H // 2, W // 2), gen=_gen_for(files), predictor=FakeTracker(),
                       discover_every=4, min_area=0.005, workdir=str(tmp_path))
    assert set(out) == set(range(N))
    tracks = {}
    for f, ms in out.items():
        for m in ms:
            k = max(range(len(OBJ)), key=lambda k: (np.asarray(Image.fromarray(m["mask"]).resize((W, H))) & _mask(k, f)).sum())
            tracks.setdefault(k, set()).add(m["track"])
            assert m["wobble_px"] is not None and m["mask"].shape == (H // 2, W // 2)
    assert {k: len(v) for k, v in tracks.items()} == {0: 1, 1: 1, 2: 1}       # one id each, late object found
    late = [f for f, ms in out.items() if any(m["track"] in tracks[2] for m in ms)]
    assert min(late) == OBJ[2][0]                                            # tracked from when it appears


def test_merge_keeps_tracks_and_rejoins_disjoint():
    # fragments: track 0 in frames 0,1; track 1 (same object found again later) in frames 5,6;
    # track 2 visible together with track 0 in frame 1 (a different object, overlapping in 3D)
    vox = {(0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1)}
    fr = lambda f, t: {"frame": f, "track": t, "vox": vox, "voxc": vox, "rgb": np.zeros(3)}
    frags = [fr(0, 0), fr(1, 0), fr(5, 1), fr(6, 1), fr(1, 2)]
    groups = sorted(sorted(g) for g in S.merge(frags))
    assert [0, 1, 2, 3] in groups and [4] in groups


def test_tracked_masks_are_cached_and_reloaded(tmp_path, monkeypatch):
    """Run 5 tracked 139 objects for an hour, then lost them all to a bug in saving the cache.
    The tracked path must save, and a second call must reload instead of tracking again."""
    from types import SimpleNamespace

    from uncertainscene.experiments import scene01 as X

    rec = SimpleNamespace(video=np.repeat([0, 1], 6), depth=np.zeros((12, H, W), np.float32),
                          files=[f"{i}.jpg" for i in range(12)])
    calls = []

    def fake_tracks(files, frames, size_hw, **kw):
        calls.append(list(frames))
        return {f: [{"mask": _mask(0, 0), "full_bbox": [0, 0, 1, 1], "score": 0.9, "full_hw": (H, W),
                     "wobble_px": None, "track": 1}] for f in kw["keep_frames"]}

    monkeypatch.setattr(S, "sam_tracks", fake_tracks)
    monkeypatch.setattr("uncertainscene.change.detect2d.Detector", lambda **kw: object())   # no weights download
    spec = X.Spec("t", "a", "b", n_key=3)
    out1 = X._masks(rec, spec, tmp_path)
    out2 = X._masks(rec, spec, tmp_path)
    assert len(calls) == 2                                       # once per recording, not again
    assert sorted(out1) == sorted(out2) and len(out1) == 6
    assert {m["track"] for v in out1.values() for m in v} == {1, 100001}
    assert all((a["mask"] == b["mask"]).all() for k in out1 for a, b in zip(out1[k], out2[k]))


# ---- whole-object detector (YOLOE) seeds, part suppression, label votes, part joining ----
LABELS = ["cup", "book", "baseball cap"]


class ShiftTracker(FakeTracker):
    """Follows the seeded mask itself (a part stays a part), moving 2 px per frame like OBJ."""

    def add_new_mask(self, st, frame_idx, obj_id, mask):
        k = max(range(len(OBJ)), key=lambda k: (np.asarray(mask) & _mask(k, frame_idx)).sum())
        st["seeds"][obj_id] = (np.asarray(mask, bool), frame_idx, OBJ[k][0])

    def propagate_in_video(self, st, start_frame_idx=0, reverse=False):
        import torch

        order = range(start_frame_idx, -1, -1) if reverse else range(start_frame_idx, st["n"])
        ids = list(st["seeds"])
        for f in order:
            lg = []
            for i in ids:
                m, f0, start = st["seeds"][i]
                lg.append(np.where(np.roll(m, 2 * (f - f0), axis=1) & (f >= start), 5.0, -5.0))
            yield f, ids, torch.as_tensor(np.stack(lg)[:, None])


def _frames(tmp_path):
    files = []
    rng = np.random.default_rng(0)
    for f in range(N):
        p = tmp_path / f"{f}.png"
        Image.fromarray(rng.integers(0, 255, (H, W, 3), dtype=np.uint8)).save(p)
        files.append(str(p))
    return files


def _frame_of(files, img):
    return next(i for i, p in enumerate(files) if np.array_equal(np.asarray(Image.open(p).convert("RGB")), img))


def _det_for(files, first=0):
    def det(img):
        f = _frame_of(files, img)
        return [{"mask": _mask(k, f), "label": LABELS[k], "conf": 0.8, "object": True}
                for k in range(len(OBJ)) if _mask(k, f).any() and f >= first]
    return det


def _which(m, f):
    big = np.asarray(Image.fromarray(m["mask"]).resize((W, H)))
    return max(range(len(OBJ)), key=lambda k: (big & _mask(k, f)).sum())


def test_detector_seeds_whole_objects_and_votes_labels(tmp_path):
    pytest.importorskip("torch")
    files = _frames(tmp_path)
    out = S.sam_tracks(files, list(range(N)), (H // 2, W // 2), gen=_gen_for(files), predictor=ShiftTracker(),
                       discover_every=4, min_area=0.005, workdir=str(tmp_path), det=_det_for(files))
    tracks = {}
    for f, ms in out.items():
        for m in ms:
            k = _which(m, f)
            tracks.setdefault(k, set()).add(m["track"])
            assert max(m["det_votes"], key=m["det_votes"].get) == LABELS[k]
    # the generator's part masks (inside a detected object) never become tracks of their own
    assert {k: len(v) for k, v in tracks.items()} == {0: 1, 1: 1, 2: 1}


def test_parts_found_before_the_detector_are_joined(tmp_path):
    """Run 6 had the guitar in up to 8 tracked pieces. A piece seeded before the whole object
    is detected must end up in the whole object's track, with its masks added to it."""
    pytest.importorskip("torch")
    files = _frames(tmp_path)

    def gen(img):                                  # only a quarter of object 1, every discovery frame
        f = _frame_of(files, img)
        m = _mask(1, f)
        q = np.zeros_like(m)
        ys, xs = np.nonzero(m)
        q[ys.min():ys.min() + 8, xs.min():xs.min() + 8] = True
        return [{"segmentation": q, "area": int(q.sum()), "predicted_iou": 0.9, "stability_score": 0.9}]

    def det(img):                                  # the whole object, only from frame 4
        f = _frame_of(files, img)
        return [{"mask": _mask(1, f), "label": "guitar", "conf": 0.7, "object": True}] if f >= 4 else []

    out = S.sam_tracks(files, list(range(N)), (H // 2, W // 2), gen=gen, predictor=ShiftTracker(),
                       discover_every=4, min_area=0.002, workdir=str(tmp_path), det=det)
    ids = {m["track"] for ms in out.values() for m in ms}
    assert len(ids) == 1                                          # piece joined to the whole object
    m0 = out[0][0]
    assert m0["parts"] and m0["det_votes"].get("guitar", 0) > 0
    assert m0["mask"].sum() > 0                                   # frame 0: the piece's mask, now in the object


def test_detector_label_needs_enough_agreeing_votes():
    from uncertainscene.change.inventory import detector_label

    assert detector_label({}) is None
    assert detector_label({"cup": 0.3}) is None                   # one weak view
    assert detector_label({"cup": 0.6, "glass": 0.7}) is None     # split
    lab, share, top = detector_label({"guitar": 2.4, "table tennis bat": 0.3})
    assert lab == "guitar" and share > 0.85 and top[0][0] == "guitar"


def test_tracks_of_one_named_object_rejoin_after_leaving_view():
    """An object that leaves the view and comes back gets a new SAM 2 track. If the detector
    names both tracks the same, a weaker 3D overlap is enough to join them (continuity); two
    tracks named differently are never joined, however much they overlap."""
    big = {(0, 0, k) for k in range(10)}
    part = {(0, 0, k) for k in range(4)} | {(5, 5, k) for k in range(6)}       # 4 of 10 cells shared
    fr = lambda f, t, vox, lab: {"frame": f, "track": t, "vox": vox, "voxc": vox, "rgb": np.zeros(3),
                                 "det_votes": {lab: 2.0} if lab else {}}
    same = [fr(0, 0, big, "cup"), fr(8, 1, part, "cup")]
    assert len(S.merge(same)) == 1
    unnamed = [fr(0, 0, big, None), fr(8, 1, part, None)]
    assert len(S.merge(unnamed)) == 2
    differ = [fr(0, 0, big, "cup"), fr(8, 1, big, "book")]
    assert len(S.merge(differ)) == 2


def test_continuity_counts_tracks_and_breaks():
    fs = [{"frame": f, "track": 7} for f in (10, 12, 14)] + [{"frame": f, "track": 9} for f in (20, 22)]
    c = S.continuity(fs, [10, 12, 14, 16, 18, 20, 22])
    assert c["tracks"] == 2 and c["runs"] == 2 and c["frames_seen"] == 5 and c["positions"] == [0, 1, 2, 5, 6]


def test_pieces_inside_one_unmatched_instance_are_joined(tmp_path):
    """1c-objects run: the guitar stayed in pieces because no track covered the whole instance,
    so there was no parent to join them to. Pieces lying inside one detected instance that no
    track matches are joined to each other."""
    pytest.importorskip("torch")
    files = _frames(tmp_path)

    def gen(img):                                  # two separate quarters of object 1
        f = _frame_of(files, img)
        m = _mask(1, f)
        ys, xs = np.nonzero(m)
        out = []
        for dy, dx in ((0, 0), (8, 8)):
            q = np.zeros_like(m)
            q[ys.min() + dy:ys.min() + dy + 8, xs.min() + dx:xs.min() + dx + 8] = True
            out.append({"segmentation": q, "area": int(q.sum()), "predicted_iou": 0.9, "stability_score": 0.9})
        return out

    def det(img):                                  # the whole object, never on a discovery frame
        f = _frame_of(files, img)
        return [{"mask": _mask(1, f), "label": "guitar", "conf": 0.7, "object": True}] if f % 4 else []

    out = S.sam_tracks(files, list(range(N)), (H // 2, W // 2), gen=gen, predictor=ShiftTracker(),
                       discover_every=4, min_area=0.002, workdir=str(tmp_path), det=det)
    assert len({m["track"] for ms in out.values() for m in ms}) == 1
    assert all(len(ms) == 1 for ms in out.values())               # one mask per frame: the pieces together
