"""Feature-point motion and deformation (change/motion3d.py)."""

import numpy as np
import pytest


def _look(c, target=(0, 0, 1.1)):
    z = np.asarray(target, float) - c
    z /= np.linalg.norm(z)
    x = np.cross([0, 1, 0], z)
    x /= np.linalg.norm(x)
    R = np.stack([x, np.cross(z, x), z])
    return np.c_[R, -R @ c]


def _render(E, K, wh, planes):
    """Textured planes z = const, drawn back to front (homography warp)."""
    import cv2

    img = np.full((wh[1], wh[0], 3), 127, np.uint8)
    for tex, (x0, y0, z0), (sx, sy) in planes:
        R, t = E[:, :3], E[:, 3]
        H = K @ np.c_[R[:, 0] * sx, R[:, 1] * sy, R @ np.array([x0, y0, z0]) + t]
        warped = cv2.warpPerspective(tex, H, wh)
        mask = cv2.warpPerspective(np.full(tex.shape[:2], 255, np.uint8), H, wh) > 0
        img[mask] = warped[mask]
    return img


def test_object_motion_finds_rigid_move_and_bent_corner():
    from uncertainscene.change.motion3d import MotionConfig, object_motion

    rng = np.random.default_rng(0)
    P = np.c_[rng.uniform(-0.25, 0.25, 600), rng.uniform(-0.15, 0.15, 600), np.zeros(600)]   # a flat cushion
    th = np.radians(10)
    R = np.array([[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]])
    B = P @ R.T + [0.1, 0.02, 0] + rng.normal(0, 0.002, P.shape)
    corner = (P[:, 0] > 0.17) & (P[:, 1] > 0.08)                     # one corner pushed up by 3 cm
    B[corner] += [0, 0, 0.03]
    r = object_motion(P, B, P, thr=0.005, cfg=MotionConfig())
    assert abs(r["moved"] - np.linalg.norm([0.1, 0.02])) < 0.01
    assert abs(r["rotation_deg"] - 10) < 1
    assert r["smooth"][corner].mean() > 0.02 and np.median(r["smooth"][~corner]) < 0.005
    from uncertainscene.change.motion3d import where
    assert where(r["peak_axes"]) == "at a corner"


def test_measure_end_to_end(tmp_path):
    """Rendered scene: textured floor (static) and a textured box top that moves 6 cm between
    recordings. Links on the box must show the move; the floor must not."""
    pytest.importorskip("cv2")
    pytest.importorskip("torch")
    from PIL import Image

    from uncertainscene.change.motion3d import MotionConfig, measure
    from uncertainscene.change.segment import SceneObject, _keys
    from uncertainscene.recon.frames import Recon

    rng = np.random.default_rng(1)

    def tex(n):
        t = rng.integers(0, 255, (n // 8, n // 8, 3)).astype(np.uint8)
        import cv2
        return cv2.GaussianBlur(cv2.resize(t, (n, n), interpolation=cv2.INTER_CUBIC), (3, 3), 0)

    floor, box = tex(1024), tex(512)
    W, H, f = 640, 480, 520.0
    K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1.0]])
    E, files, video = [], [], []
    for v, shift in ((0, 0.0), (1, 0.06)):
        for k, a in enumerate(np.linspace(-0.5, 0.5, 14)):
            e = _look(np.array([0.5 * np.sin(a), 0.1 * np.cos(2 * a) - 0.05 * v, 0.25 * np.cos(a) - 0.1]))
            planes = [(floor, (-1.0, -1.0, 1.3), (2 / 1024, 2 / 1024)),
                      (box, (-0.15 + shift, -0.15, 1.05), (0.3 / 512, 0.3 / 512))]
            fn = tmp_path / f"{v}_{k:02d}.png"
            Image.fromarray(_render(e, K, (W, H), planes)).save(fn)
            E.append(e)
            files.append(str(fn))
            video.append(v)
    S = len(E)
    hm, wm = 120, 160
    Km = K.copy()
    Km[:2] *= wm / W
    rec = Recon(np.zeros((S, hm, wm, 3), np.uint8), np.ones((S, hm, wm), np.float32), np.ones((S, hm, wm), np.float32),
                np.tile(Km, (S, 1, 1)), np.array(E), np.array(video), files, ("A", "B"))
    g = np.mgrid[-0.15:0.15:0.01, -0.15:0.15:0.01].reshape(2, -1).T
    Xbox = np.c_[g, np.full(len(g), 1.05)]
    vox = 0.03
    o = SceneObject(id=0, recording="A", X=Xbox, frame=np.zeros(len(g), int), pix=np.zeros((len(g), 2)),
                    voxels=_keys(Xbox, vox) | _keys(Xbox + [0, 0, 0.01], vox) | _keys(Xbox - [0, 0, 0.01], vox),
                    masks={}, label="box", extra={"voxel": vox})
    m = measure(rec, [o], scale_m_per_unit=1.0, cfg=MotionConfig(frame_step=1, k_near=6, k_cross=5, block=3), device="cpu")
    assert m["summary"]["links"] > 200, m["summary"]
    assert m["threshold_cm"] < 1.5, m["summary"]                     # triangulated points are precise
    on_box = m["obj_of"] == 0
    assert on_box.sum() > 30
    assert abs(np.median(m["disp_cm"][on_box]) - 6) < 1.0           # the box moved 6 cm
    assert np.median(m["disp_cm"][~on_box]) < 1.0                    # the floor did not
    row = m["objects"][0]
    assert abs(row["moved_cm"] - 6) < 1.0 and row["moved_beyond_noise"] and not row["deformed"]


def test_clip_embeddings_on_this_transformers(monkeypatch):
    """clip_inventory with a real (tiny, random) CLIPModel: the embeddings must be the projected
    ones the full model uses (transformers 5 returns an output object, 4 a tensor)."""
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    from PIL import Image

    from uncertainscene.change import features as F

    cfg = transformers.CLIPConfig(
        text_config=dict(hidden_size=32, intermediate_size=64, num_hidden_layers=2, num_attention_heads=2,
                         vocab_size=100, max_position_embeddings=20, bos_token_id=0, eos_token_id=1, pad_token_id=1),
        vision_config=dict(hidden_size=32, intermediate_size=64, num_hidden_layers=2, num_attention_heads=2,
                           image_size=32, patch_size=8), projection_dim=16)
    model = transformers.CLIPModel(cfg).eval()

    class Proc:
        def __call__(self, text=None, images=None, return_tensors="pt", padding=True):
            if text is not None:
                ids = torch.tensor([[2 + (hash(t) + i) % 90 for i in range(6)] + [1] for t in text])
                return {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
            return {"pixel_values": torch.stack([torch.as_tensor(np.asarray(im.resize((32, 32))), dtype=torch.float32)
                                                 .permute(2, 0, 1) / 255 for im in images])}

    monkeypatch.setitem(F._CACHE, "tiny", (model, Proc()))
    monkeypatch.setattr(F, "_device", lambda: "cpu")
    crops = [Image.fromarray(np.random.default_rng(i).integers(0, 255, (40, 40, 3), dtype=np.uint8)) for i in range(3)]
    tops, emb = F.clip_inventory(crops, vocab=["cushion", "cup", "hat"], model_name="tiny")
    assert len(tops) == 3 and len(tops[0]) == 3 and emb.shape == (3, 16)
    with torch.no_grad():
        full = model(**Proc()(text=["a photo of a cushion"]), pixel_values=Proc()(images=crops)["pixel_values"])
    ref = torch.nn.functional.normalize(full.image_embeds, dim=-1).numpy()
    assert np.allclose(emb, ref, atol=1e-5)
