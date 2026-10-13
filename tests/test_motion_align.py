"""Lining the two recordings up from static feature points only."""
import numpy as np
import pytest


def _rot(deg, axis=(0.3, 1.0, 0.2)):
    from scipy.spatial.transform import Rotation as Rot

    a = np.asarray(axis, float)
    return Rot.from_rotvec(np.radians(deg) * a / np.linalg.norm(a)).as_matrix()


def test_alignment_recovers_offset_despite_wrong_and_moved_links():
    from uncertainscene.change.motion3d import align_recordings, cross_term

    rng = np.random.default_rng(0)
    n = 600
    XA = rng.uniform(-1, 1, (n, 3)) * [0.8, 0.4, 0.3] + [0, 0, 1.5]
    C = np.tile(np.eye(3) * 0.003**2, (n, 1, 1))
    # after recording: same scene, but its frame is off by 6 cm, 1.5 deg and 2% scale
    R, sc, t = _rot(1.5), 1.02, np.array([0.06, -0.02, 0.03])
    XB = ((XA + rng.normal(0, 0.003, (n, 3))) - t) @ R / sc         # inverse map of XA = sc R XB + t
    bad = rng.random(n) < 0.15                                      # wrong matches and moved points
    XB[bad] += rng.normal(0, 0.3, (bad.sum(), 3))
    s2_before = cross_term(XB - XA, C)
    s, Rf, tf, info = align_recordings(XA, XB, C)
    XBa = s * XB @ Rf.T + tf
    res = np.linalg.norm(XBa - XA, axis=1)[~bad]
    assert abs(s - sc) < 0.002
    assert np.degrees(np.arccos(np.clip((np.trace(Rf @ R.T) - 1) / 2, -1, 1))) < 0.05
    assert np.median(res) < 0.006                                   # back at the 3 mm noise
    s2_after = cross_term(XBa - XA, C)
    assert np.sqrt(s2_after) < 0.1 * np.sqrt(s2_before)
    assert info["shift"] > 0.03


def test_aligned_recordings_are_left_alone():
    from uncertainscene.change.motion3d import align_recordings

    rng = np.random.default_rng(1)
    XA = rng.uniform(-1, 1, (300, 3)) + [0, 0, 2]
    XB = XA + rng.normal(0, 0.004, XA.shape)
    s, R, t, info = align_recordings(XA, XB, np.tile(np.eye(3) * 0.004**2, (300, 1, 1)))
    assert abs(s - 1) < 0.002 and info["rotation_deg"] < 0.05 and info["shift"] < 0.003


def test_static_check_rejects_a_moved_object():
    pytest.importorskip("cv2")
    from uncertainscene.change.motion3d import _static_matches

    rng = np.random.default_rng(2)
    X = np.c_[rng.uniform(-1, 1, 300), rng.uniform(-.6, .6, 300), rng.uniform(1.5, 3, 300)]
    moved = np.arange(300) >= 255
    K = np.array([[700, 0, 288], [0, 700, 512], [0, 0, 1.0]])

    def proj(R, c, P):
        Xc = (P - c) @ R.T
        return (Xc @ K.T)[:, :2] / Xc[:, 2:]

    Rb = _rot(6, (0, 1, 0))
    XB = X.copy()
    XB[moved] += [0.25, 0.0, -0.1]                                 # an object put somewhere else
    pa = proj(np.eye(3), np.zeros(3), X) + rng.normal(0, .4, (300, 2))
    pb = proj(Rb, np.array([0.3, 0.05, 0.0]), XB) + rng.normal(0, .4, (300, 2))
    inl = _static_matches(pa, pb, 2.0)
    assert inl[~moved].mean() > 0.9
    assert inl[moved].mean() < 0.3


def test_one_focal_for_both_recordings():
    pytest.importorskip("torch")
    from test_poses import _scene
    from uncertainscene.recon import poses as P

    rng = np.random.default_rng(3)
    E, Ep, K, Kp, video, X, t, fr, uv, bad = _scene(rng)
    Kp[video == 1, :2, :2] *= 1.04                                  # VGGT guessed different focals
    Ec, Kc, pts, rep = P.refine_tracks(Ep, Kp, video, t, fr, uv, P.PoseConfig(iters=150, polish_iters=100))
    f = Kc[:, 0, 0]
    assert np.ptp(f) < 1e-6 * f.mean()
    assert abs(f.mean() / K[0, 0, 0] - 1) < 0.03
    assert rep["points_in_both"] > 100
    assert len(rep["per_recording_median_px"]) == 2
