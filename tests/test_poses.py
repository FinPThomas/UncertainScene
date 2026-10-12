"""Bundle adjustment leaves no frame on its rough starting pose."""
import numpy as np
import pytest


def _scene(rng, S=24, sparse=(), n_sparse=14):
    from scipy.spatial.transform import Rotation as Rot

    N, W, H, f = 1200, 1024, 576, 800.0
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
        if s in sparse:                       # a blurry frame: only a handful of matched features
            i = rng.choice(i, n_sparse, replace=False)
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
    return E, Ep, K, Kp, video, X, t, fr, uv, bad


def _frame_px(E, K, X, t, fr, uv, s, bad):
    from uncertainscene.recon.poses import reproject

    sel = (fr == s) & ~bad
    return float(np.median(reproject(E, K, X, t[sel], fr[sel], uv[sel])[0]))


def test_sparse_frames_are_resected_not_left_on_rough_poses():
    """Frames with fewer than 30 adjusted points used to keep their VGGT pose (~8 px on the real
    video). They must now be resected: their true-point reprojection error ends near the noise."""
    pytest.importorskip("torch")
    pytest.importorskip("cv2")
    from uncertainscene.recon import poses as P

    rng = np.random.default_rng(1)
    sparse = (3, 9, 15, 20)
    E, Ep, K, Kp, video, X, t, fr, uv, bad = _scene(rng, sparse=sparse)
    cfg = P.PoseConfig(iters=150, polish_iters=100)
    Ec, Kc, pts, rep = P.refine_tracks(Ep, Kp, video, t, fr, uv, cfg)
    assert rep["frames_with_few_points"] >= len(sparse)
    assert rep["frames_resected"] >= len(sparse)
    Ec, Kc = Ec.astype(np.float64), Kc.astype(np.float64)
    # score each sparse frame on ALL its true matches (not just the ones the adjustment kept):
    # points triangulated from the other frames' clean observations, projected into it
    other = ~np.isin(fr, sparse) & ~bad
    Xr, _ = P.triangulate(Ec, Kc, t[other], fr[other], uv[other])
    old = Ec.copy()
    old[list(sparse)] = Ep[list(sparse)]                  # what the code did before: keep the rough pose
    for s in sparse:
        before = _frame_px(old, Kc, Xr, t, fr, uv, s, bad)
        after = _frame_px(Ec, Kc, Xr, t, fr, uv, s, bad)
        assert after < 1.0, (s, before, after)
        assert after < before / 5, (s, before, after)
    assert rep["final"]["median_px"] < 1.0
    assert rep["per_frame_median_px"]["worst"] < 1.5
