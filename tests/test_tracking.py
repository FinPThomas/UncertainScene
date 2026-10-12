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
