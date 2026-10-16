"""What happened to each object, in words, with how sure (Fin, 2026-10-16).

For every object the pipeline compared between the two recordings this gives one record and one
sentence:

- **how far it moved** (cm, 90% range) and which way (left / right, towards / away from the camera,
  up / down), from the two object clouds after the recordings were lined up;
- **how it is posed and how that changed**: standing upright / lying flat / tilted, turned about
  the vertical by how many degrees, tipped over. The turn comes from fitting the after cloud to
  the before cloud (trimmed rigid ICP from four starting turns), and is only reported when
  turning actually fits better than not turning (a round cup has no measurable turn);
- **where it was and where it is now**: "from on top of the cabinet, next to the lamp, to on
  top of the table, next to the books" (relations.py context of both recordings);
- **deformation, object by object**: the same object in the same place whose shape differs by
  more than unchanged objects in the same pair differ (label-free null from this pair's own
  unchanged objects), checked with the Rigidity Copilot: a cushion (fabric, soft) can be
  squashed, a ceramic mug cannot, so a shape difference on a stiff object is put down to the
  reconstruction or a side one video never saw, not called deformation;
- **removed / added only where the other video looked**: an object counts as gone only when the
  other recording saw its spot (not hidden behind something) in enough frames; otherwise it is
  "can't tell" and never reported as a change;
- **how sure**, as a probability built from named parts, each shown: the object is real (views),
  its name (detector or CLIP), the two clouds are the same object (appearance similarity and
  name), the move is beyond the position uncertainty of both clouds plus the residual
  misalignment of the recordings (chi-square), the other video covered the spot, the
  see-through evidence beyond this pair's own no-change threshold, and the shape evidence with
  the Copilot's stiffness prior. These are model probabilities from the measured uncertainties,
  not calibrated against an answer key.

Everything here is measured on the SAM 2 masks: object points come only from inside an object's
masks in the frames where SAM 2 tracked it, and the names come from masked crops.
Nothing is fitted to ground truth.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

SHAPE_FLOOR_CM = 1.5        # shape differences below this are never deformation
TURN_FLOOR_DEG = 12.0       # turns below this are never reported
TILT_FLOOR_DEG = 10.0


# ---- small geometry helpers ----------------------------------------------------------------
def _basis(up: np.ndarray) -> np.ndarray:
    up = np.asarray(up, float)
    up = up / np.linalg.norm(up)
    a = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(up, a)
    e1 /= np.linalg.norm(e1)
    return np.stack([e1, np.cross(up, e1), up])          # rows: two horizontal axes, then up


def _rot_about(axis: np.ndarray, deg: float) -> np.ndarray:
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    t = np.radians(deg)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(t) * K + (1 - np.cos(t)) * K @ K


def _kabsch(P: np.ndarray, Q: np.ndarray):
    """R, t with Q ~ R P + t."""
    mp, mq = P.mean(0), Q.mean(0)
    U, _, Vt = np.linalg.svd((Q - mq).T @ (P - mp))
    S = np.eye(3)
    S[2, 2] = np.sign(np.linalg.det(U @ Vt)) or 1.0
    R = U @ S @ Vt
    return R, mq - R @ mp


def _sub(X: np.ndarray, n: int, rng) -> np.ndarray:
    X = np.asarray(X, float)
    return X[rng.choice(len(X), n, replace=False)] if len(X) > n else X


def pose_words(X: np.ndarray, up: np.ndarray) -> dict:
    """How an object sits: its main axes against the vertical. Upright = longest axis within 30 deg
    of vertical (and clearly longest); lying flat = shortest axis within 30 deg of vertical."""
    Xc = np.asarray(X, float) - np.median(X, 0)
    _, S, Vt = np.linalg.svd(Xc - Xc.mean(0), full_matrices=False)
    up = np.asarray(up, float) / np.linalg.norm(up)
    ang = np.degrees(np.arccos(np.clip(np.abs(Vt @ up), 0, 1)))      # per axis, longest first
    s = S / max(S[0], 1e-12)
    if ang[0] < 30 and s[1] < 0.8:
        w = "standing upright"
    elif ang[2] < 30 and s[2] < 0.6:
        w = "lying flat"
    else:
        w = f"tilted about {min(ang[0], 90 - ang[0]) if s[1] < 0.8 else ang[2]:.0f} deg"
    return {"words": w, "long_axis_from_vertical_deg": round(float(ang[0]), 1),
            "short_axis_from_vertical_deg": round(float(ang[2]), 1), "shape_ratios": np.round(s, 2).tolist()}


# ---- shape comparison of one object's two clouds -------------------------------------------
def _icp(P: np.ndarray, Q: np.ndarray, tree, R0: np.ndarray, iters: int, keep: float, rotate: bool = True):
    """Trimmed rigid ICP of P onto Q (tree over Q), from rotation R0 about P's centre."""
    cp, cq = np.median(P, 0), np.median(Q, 0)
    R, t = R0, cq - R0 @ cp
    for _ in range(iters):
        Y = P @ R.T + t
        d, j = tree.query(Y, k=1)
        k = d <= np.quantile(d, keep)
        if k.sum() < 6:
            break
        if rotate:
            R2, t2 = _kabsch(P[k], Q[j[k]])
        else:
            R2, t2 = R, t + (Q[j[k]] - Y[k]).mean(0)
        step = np.linalg.norm(R2 - R) + np.linalg.norm(t2 - t)
        R, t = R2, t2
        if step < 1e-7:
            break
    d, _ = tree.query(P @ R.T + t, k=1)
    return R, t, float(np.mean(np.sort(d)[: max(1, int(keep * len(d)))]))


def shape_fit(XA: np.ndarray, XB: np.ndarray, up: np.ndarray, max_points: int = 1500, iters: int = 40,
              keep: float = 0.8, n_boot: int = 6, seed: int = 0) -> dict:
    """How the after cloud XB of one object relates to its before cloud XA (scene units):
    the rigid motion A -> B (turn about the vertical, tilt, move of the centre) and what that
    motion cannot explain (the shape difference). The shape difference is the smaller of the two
    one-sided 80th-percentile distances after the fit: a side that only one video saw raises one
    direction only, a real change of shape raises both."""
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(seed)
    A, B = _sub(XA, max_points, rng), _sub(XB, max_points, rng)
    up = np.asarray(up, float) / np.linalg.norm(up)
    tA, tB = cKDTree(A), cKDTree(B)

    def yaw(X):                                   # direction of the main horizontal axis (deg)
        H = (X - X.mean(0)) @ _basis(up)[:2].T
        w, V = np.linalg.eigh(H.T @ H)
        v = V[:, -1]
        return float(np.degrees(np.arctan2(v[1], v[0]))), float(w[-1] / max(w[0], 1e-12))

    def best_fit(P, Q, tree):
        starts = [0.0, 90.0, 180.0, 270.0]
        (yq, eq), (yp, ep) = yaw(Q), yaw(P)
        if min(eq, ep) > 1.5:                         # elongated: line the main axes up (both ways)
            starts += [yq - yp, yq - yp + 180]
        return [_icp(P, Q, tree, _rot_about(up, a), iters, keep) for a in starts]

    fits = best_fit(B, A, tA)
    R, t, res = min(fits, key=lambda f: f[2])                        # A ~ R B + t
    # a shape that looks the same turned half way round (a box, a book) cannot tell a turn from
    # that turn plus 180 deg: then the smaller of the two is reported
    t0 = _turn_about(R.T, up)
    flips = [f[2] for f in fits if abs(abs((_turn_about(f[0].T, up) - t0 + 180) % 360 - 180) - 180) < 25]
    symmetric = bool(flips) and min(flips) < 1.15 * res
    _, _, res_still = _icp(B, A, tA, np.eye(3), iters, keep, rotate=False)   # same place, no turn
    Bf = B @ R.T + t
    dA, _ = cKDTree(Bf).query(A, k=1)
    dB, _ = tA.query(Bf, k=1)
    shape = min(float(np.quantile(dA, 0.8)), float(np.quantile(dB, 0.8)))
    Rab = R.T                                                        # motion of the object A -> B
    turn = _turn_about(Rab, up)
    if symmetric and abs(turn) > 90:
        turn = turn - np.sign(turn) * 180
    tilt = float(np.degrees(np.arccos(np.clip(Rab @ up @ up, -1, 1))))
    turns, shapes = [], []
    for b in range(n_boot):                                          # spread from resampling the points
        r2 = np.random.default_rng(seed + 1 + b)
        Ab = A[r2.integers(0, len(A), len(A))]
        Bb = B[r2.integers(0, len(B), len(B))]
        tb = cKDTree(Ab)
        Rb, tt, _ = _icp(Bb, Ab, tb, R, iters // 2, keep)
        turns.append(_turn_about(Rb.T, up))
        dd, _ = tb.query(Bb @ Rb.T + tt, k=1)
        shapes.append(float(np.quantile(dd, 0.8)))
    turns = np.array(turns)
    dev = np.abs((turns - turn + 180) % 360 - 180)
    if symmetric:
        dev = np.minimum(dev, 180 - dev)
    return {"turn_deg": round(turn, 1), "turn_sd_deg": round(float(np.sqrt(np.mean(dev ** 2))) if len(dev) else 0.0, 1),
            "tilt_deg": round(tilt, 1), "shape": shape, "shape_sd": float(np.std(shapes)) if shapes else 0.0,
            "residual_turned": res, "residual_still": res_still,
            "turn_helps": bool(res < 0.85 * res_still),                # turning explains the clouds clearly better
            "turn_symmetric": symmetric,
            "extent_ratio": (np.sort(_extent(B))[::-1] / np.maximum(np.sort(_extent(A))[::-1], 1e-12)).round(3).tolist()}


def _extent(X: np.ndarray) -> np.ndarray:
    Xc = X - X.mean(0)
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    P = Xc @ Vt.T
    return np.quantile(P, 0.95, axis=0) - np.quantile(P, 0.05, axis=0)


def _turn_about(R: np.ndarray, up: np.ndarray) -> float:
    """Signed turn (deg) of rotation R about the vertical: how far a horizontal direction swings."""
    Bs = _basis(up)
    h = R @ Bs[0]
    h = h - (h @ up) * up
    if np.linalg.norm(h) < 1e-9:
        return 0.0
    return float(np.degrees(np.arctan2(h @ Bs[1], h @ Bs[0])))


# ---- did the other video look at the spot? -------------------------------------------------
def coverage(o, rec, frames: np.ndarray, tau: float, n: int = 150, min_share: float = 0.3, seed: int = 0) -> dict:
    """How well the frames of the other recording looked at where object o is: the number of
    frames in which at least `min_share` of its points fall inside the image, in front of the
    camera and not hidden behind something nearer (depth there not more than tau in front),
    and the share of its points seen that way in at least 2 frames."""
    from .geometry import project

    rng = np.random.default_rng(seed)
    X = _sub(o.X, n, rng)
    H, W = rec.depth.shape[1:]
    hits = np.zeros(len(X), int)
    views = 0
    for t in frames:
        u, v, z = project(X, rec.K[t], rec.E[t])
        ui, vi = np.round(u).astype(int), np.round(v).astype(int)
        ok = (z > 1e-6) & (ui >= 0) & (ui < W) & (vi >= 0) & (vi < H)
        if not ok.any():
            continue
        idx = np.nonzero(ok)[0]
        d = rec.depth[t, vi[idx], ui[idx]]
        vis = (d - z[idx]) / z[idx] > -tau
        seen = np.zeros(len(X), bool)
        seen[idx[vis]] = True
        hits += seen
        views += seen.mean() >= min_share
    return {"views": int(views), "share_seen_twice": round(float((hits >= 2).mean()), 3)}


def guard_coverage(objs_a: list, objs_b: list, rec, tau: float, min_views: int = 4, min_share: float = 0.5) -> list[str]:
    """No removed / added verdict where the other video did not look: such objects become
    "not seen in other recording" (can't tell). Fills o.extra["coverage"] for every compared
    object. Returns what was downgraded."""
    other = {"A": np.nonzero(rec.video == 1)[0], "B": np.nonzero(rec.video == 0)[0]}
    out = []
    for o in objs_a + objs_b:
        if o.structure or o.status in ("unknown",):
            continue
        cv = coverage(o, rec, other[o.recording], tau, seed=int(o.id))
        o.extra["coverage"] = cv
        if o.status in ("removed", "added") and (cv["views"] < min_views or cv["share_seen_twice"] < min_share):
            o.extra["status_before_coverage"] = o.status
            o.status = "not seen in other recording"
            out.append(f"{o.recording}{o.id} {o.label} (other video saw its spot in {cv['views']} frames, "
                       f"{cv['share_seen_twice']:.0%} of it twice)")
    if out:
        log.info("coverage: %d removed/added verdicts withdrawn (other video did not look): %s", len(out), "; ".join(out))
    return out


# ---- probabilities -------------------------------------------------------------------------
def p_real(o) -> float:
    """The object is a real, single thing: grows with the views SAM 2 tracked it in."""
    n = o.extra.get("n_views") or len(getattr(o, "masks", {}) or {}) or 1
    return round(1 - 0.5 ** (n / 4), 3)


def p_name(o) -> float:
    if o.extra.get("label_source") == "detector":
        votes = o.extra.get("det_votes") or {}
        tot = sum(votes.values()) if isinstance(votes, dict) else 0
        share = votes.get(o.label, 0) / tot if tot else 0.9
        return round(float(np.clip(max(share, 0.75), 0, 0.98)), 3)
    return round(float(o.label_prob or 0), 3)


def p_same_object(a, b, sim: float | None) -> float:
    s = 0.0 if sim is None else float(np.clip((sim - 0.5) / 0.4, 0, 1))
    if a.label is not None and a.label == b.label:
        s = min(1.0, 0.3 + 0.7 * s + 0.1)
    elif a.label is not None and b.label is not None:
        s *= 0.6
    return round(float(np.clip(s, 0, 0.99)), 3)


def _sigma_cm(o) -> float:
    pc = o.extra.get("position_cm") or {}
    r = pc.get("radius90_cm") or pc.get("radius90_cm_views") or 5.0
    return float(r) / 2.5                    # 90% radius in 3D = 2.5 sigma


def p_moved(d_cm: float, a, b, align_cm: float) -> float:
    from scipy.stats import chi2

    s2 = _sigma_cm(a) ** 2 + _sigma_cm(b) ** 2 + align_cm ** 2
    return round(float(chi2.cdf(d_cm ** 2 / max(s2, 1e-6), 3)), 3)


def p_gone(o, thr_gone: float) -> float:
    g = float(o.stats.get("gone") or 0)
    return round(float(1 / (1 + np.exp(-(g - thr_gone) / 0.05))), 3)


def p_covered(o) -> float:
    cv = o.extra.get("coverage") or {}
    v, s = cv.get("views", 0), cv.get("share_seen_twice", 0)
    return round(float(min(1, v / 8) * min(1, s / 0.7)), 3)


def p_deformed(shape_cm: float, thr_cm: float, rigidity: float | None) -> tuple[float, float]:
    """(evidence that the shape differs, probability it deformed given the Copilot's stiffness).
    Deformed objects show a shape difference 90% of the time, rigid ones 10% (reconstruction)."""
    pe = float(1 / (1 + np.exp(-(shape_cm - thr_cm) / max(0.25 * thr_cm, 0.3))))
    r = 0.5 if rigidity is None else float(np.clip(rigidity, 0.02, 0.98))
    num = (1 - r) * (0.9 * pe + 0.1 * (1 - pe))
    den = num + r * (0.1 * pe + 0.9 * (1 - pe))
    return round(pe, 3), round(num / den, 3)


# ---- the per-object records ----------------------------------------------------------------
def _ctx_words(o) -> str:
    c = o.extra.get("context") or {}
    parts = []
    if c.get("on_top_of"):
        parts.append("on top of the " + c["on_top_of"]["object"].rsplit(" ", 1)[0])
    if c.get("next_to"):
        parts.append("next to the " + " and the ".join(n["object"].rsplit(" ", 1)[0] for n in c["next_to"][:2]))
    return ", ".join(parts)


def _name(o) -> str:
    return o.label or "object"


def _sure(p: float) -> str:
    return f"{100 * p:.0f}% sure"


def describe_all(objs_a: list, objs_b: list, scale_m: float, up: np.ndarray, E: np.ndarray, thr: dict,
                 align_cm: float = 3.0, min_sim: float = 0.7) -> dict:
    """Movement record and sentence for every compared object (o.extra["change"],
    o.extra["change_sentence"]); objects found deformed get status "deformed". Returns a summary
    with this pair's own noise levels (from its unchanged objects)."""
    from .describe import direction_words

    up = np.asarray(up, float) / np.linalg.norm(up)
    cm = scale_m * 100
    by_b = {o.id: o for o in objs_b}
    by_a = {o.id: o for o in objs_a}
    for o in objs_a + objs_b:
        if not o.structure:
            o.extra["pose"] = pose_words(o.X, up)

    # the same object in both recordings: partners, or a look-alike in the same place
    pairs = []
    for a in objs_a:
        if a.structure or a.extra.get("background"):
            continue
        b = by_b.get(a.partner) if a.partner is not None else None
        if b is None and a.status in ("changed in place", "shifted", "unchanged"):
            cp = a.stats.get("counterpart")
            cand = by_b.get(cp) if cp is not None else None
            if (cand is not None and not cand.structure and (a.stats.get("counterpart_iou") or 0) >= 0.3
                    and (a.stats.get("counterpart_sim") or 0) >= min_sim
                    and (a.label == cand.label or a.label is None or cand.label is None)):
                b = cand
        if b is not None:
            pairs.append((a, b))
    fits = {}
    for a, b in pairs:
        try:
            fits[a.id] = shape_fit(a.X, b.X, up, seed=int(a.id))
        except Exception:  # noqa: BLE001 - one odd cloud must not lose the rest
            log.exception("shape fit failed for %s%d", a.recording, a.id)

    # this pair's noise levels, from the objects it calls unchanged (label-free)
    still = [fits[a.id] for a, b in pairs if a.id in fits and a.status == "unchanged"]
    shape_null = max(SHAPE_FLOOR_CM, float(np.quantile([f["shape"] * cm for f in still], 0.9))) if len(still) >= 3 \
        else 2 * SHAPE_FLOOR_CM
    turn_null = max(TURN_FLOOR_DEG, float(np.quantile([abs(f["turn_deg"]) for f in still if f["turn_helps"]] or [0], 0.9)))
    tilt_null = max(TILT_FLOOR_DEG, float(np.quantile([f["tilt_deg"] for f in still], 0.9))) if still else TILT_FLOOR_DEG

    n_def = 0
    for a, b in pairs:
        f = fits.get(a.id)
        d_vec = (np.asarray(b.centroid) - np.asarray(a.centroid))
        d_cm = float(np.linalg.norm(d_vec)) * cm
        sim = a.stats.get("match_similarity") or a.stats.get("counterpart_sim")
        pm = p_moved(d_cm, a, b, align_cm)
        ps = p_same_object(a, b, sim)
        size_cm = float(np.max(a.extent)) * cm
        rec = {"other": f"B{b.id}", "distance_cm": round(d_cm, 1),
               "distance_range_cm": [round(max(0.0, d_cm - 1.645 * np.hypot(_sigma_cm(a), _sigma_cm(b))), 1),
                                     round(d_cm + 1.645 * np.hypot(_sigma_cm(a), _sigma_cm(b)), 1)],
               "direction": direction_words(d_vec, E) if d_cm > 0 else "",
               "height_change_cm": round(float(d_vec @ up) * cm, 1),
               "pose_before": a.extra["pose"]["words"], "pose_after": b.extra["pose"]["words"],
               "context_before": _ctx_words(a), "context_after": _ctx_words(b),
               "p": {"real_before": p_real(a), "real_after": p_real(b), "name": p_name(a), "same_object": ps,
                     "beyond_position_noise": pm}}
        if f is not None:
            rigid = (a.extra.get("rigidity") or {}).get("rigidity")
            pe, pdef = p_deformed(f["shape"] * cm, shape_null, rigid)
            rec.update({"turn_deg": f["turn_deg"] if f["turn_helps"] and abs(f["turn_deg"]) >= turn_null else None,
                        "turn_sd_deg": f["turn_sd_deg"], "turn_measurable": f["turn_helps"],
                        "tilt_deg": f["tilt_deg"] if f["tilt_deg"] >= tilt_null else None,
                        "shape_diff_cm": round(f["shape"] * cm, 2), "shape_noise_cm": round(shape_null, 2),
                        "extent_ratio": f["extent_ratio"], "rigidity": rigid,
                        "materials": (a.extra.get("rigidity") or {}).get("materials")})
            rec["p"].update({"shape_differs": pe, "deformed": pdef})
            small_move = d_cm < max(10.0, 0.5 * size_cm) or pm < 0.9
            if pe >= 0.5 and pdef >= 0.6 and small_move and a.status in ("unchanged", "changed in place", "shifted"):
                a.status = b.status = "deformed"
                a.partner, b.partner = b.id, a.id
                n_def += 1
        moved_now = a.status == "moved"
        rec["p_change"] = round(rec["p"]["real_before"] * rec["p"]["real_after"] * ps *
                                (pm if moved_now or a.status != "deformed" else rec["p"].get("deformed", 0)), 3) \
            if a.status in ("moved", "deformed") else round(1 - pm * ps, 3)
        rec["p_change_means"] = {"moved": "moved", "deformed": "deformed"}.get(a.status, "unchanged (sure it did not move)")
        a.extra["change"] = rec
        b.extra["change"] = rec | {"other": f"A{a.id}"}
        a.extra["change_sentence"] = b.extra["change_sentence"] = sentence(a, rec)

    for o in objs_a + objs_b:                       # one-sided: removed / added / can't tell
        if o.structure or "change" in o.extra:
            continue
        rec = {"p": {"real": p_real(o), "name": p_name(o), "other_video_covered": p_covered(o),
                     "see_through": p_gone(o, thr.get("gone", 0.3))},
               "coverage": o.extra.get("coverage"), "context": _ctx_words(o), "pose": (o.extra.get("pose") or {}).get("words")}
        if o.status in ("removed", "added"):
            rec["p_change"] = round(rec["p"]["real"] * rec["p"]["other_video_covered"] * rec["p"]["see_through"], 3)
            rec["p_change_means"] = o.status
        o.extra["change"] = rec
        o.extra["change_sentence"] = sentence(o, rec)
    if n_def:
        log.info("movement: %d objects deformed (same object, same place, different shape, soft enough)", n_def)
    return {"pairs_compared": len(pairs), "shape_noise_cm": round(shape_null, 2), "turn_noise_deg": round(turn_null, 1),
            "tilt_noise_deg": round(tilt_null, 1), "deformed": n_def, "unchanged_pairs_for_noise": len(still)}


def sentence(o, rec: dict) -> str:
    """One informative sentence (or three) about what happened to object o."""
    n = _name(o)
    p = rec.get("p", {})
    named = f"{n} ({p.get('name', 0):.0%} sure of the name, {o.extra.get('label_source') or 'CLIP'})"
    views = o.extra.get("n_views")
    masks = f" Measured on its SAM 2 masks in {views} views." if views else ""
    st = o.status
    if st in ("moved", "unchanged", "deformed", "changed in place", "shifted") and "distance_cm" in rec:
        lo, hi = rec["distance_range_cm"]
        bits = []
        if st == "moved" or (st != "deformed" and p.get("beyond_position_noise", 0) >= 0.9):
            bits.append(f"moved {rec['distance_cm']:.0f} cm (90%: {lo:.0f} to {hi:.0f} cm) {rec['direction']}")
            cb, ca = rec["context_before"], rec["context_after"]
            if cb and ca and cb != ca:
                bits.append(f"from {cb} to {ca}")
            elif ca:
                bits.append(f"still {ca}")
        if rec.get("turn_deg") is not None:
            bits.append(f"turned about {abs(rec['turn_deg']):.0f} deg (+/- {rec['turn_sd_deg']:.0f}) about the vertical")
        if rec.get("tilt_deg") is not None:
            bits.append(f"tipped {rec['tilt_deg']:.0f} deg")
        if rec["pose_before"] != rec["pose_after"]:
            bits.append(f"was {rec['pose_before']}, now {rec['pose_after']}")
        if st == "deformed":
            mat = ", ".join(m for m, _ in (rec.get("materials") or [])[:2]) or "unknown material"
            s = (f"The {named} deformed but did not move away: same object in the same place (moved "
                 f"{rec['distance_cm']:.0f} cm, within its position noise), but its shape differs by "
                 f"{rec['shape_diff_cm']:.1f} cm where unchanged objects in this pair differ by up to "
                 f"{rec['shape_noise_cm']:.1f} cm. The Rigidity Copilot reads it as {mat} (rigidity "
                 f"{rec.get('rigidity') or 0:.2f}), so a change of shape is plausible. "
                 f"{_sure(rec['p_change'])} it deformed.")
            if bits:
                s += " Also: " + "; ".join(bits) + "."
            return s + masks
        if st == "moved":
            s = f"The {named} " + ", ".join(bits) + "."
            s += (f" {_sure(rec['p_change'])} it moved (beyond position noise {p['beyond_position_noise']:.0%}, "
                  f"same object {p['same_object']:.0%}, real object {min(p['real_before'], p['real_after']):.0%}).")
        else:
            s = f"The {named} did not move" if not bits else f"The {named}: " + ", ".join(bits)
            if not bits and rec.get("context_before"):
                s += f"; it is still {rec['context_before']}"
            s += f". {_sure(rec['p_change'])} it stayed where it was."
        if rec.get("shape_diff_cm") is not None and p.get("shape_differs", 0) >= 0.5 and st != "deformed":
            mat = ", ".join(m for m, _ in (rec.get("materials") or [])[:2]) or "its material"
            s += (f" Its two clouds differ in shape by {rec['shape_diff_cm']:.1f} cm (noise {rec['shape_noise_cm']:.1f} cm), "
                  f"but the Rigidity Copilot reads it as {mat} (rigidity {rec.get('rigidity') or 0:.2f}), too stiff to "
                  "deform: most likely the reconstruction or a side one video never saw.")
        return s + masks
    cv = rec.get("coverage") or {}
    where = f", {rec['context']}" if rec.get("context") else ""
    other = "after" if o.recording == "A" else "before"
    if st == "removed":
        return (f"The {named}{where} was removed: the after video looked at that spot in {cv.get('views', 0)} frames "
                f"and saw straight through where it stood. {_sure(rec['p_change'])} (real object {p['real']:.0%}, spot "
                f"covered {p['other_video_covered']:.0%}, see-through {p['see_through']:.0%}).{masks}")
    if st == "added":
        return (f"The {named}{where} was added: the before video looked at that spot in {cv.get('views', 0)} frames "
                f"and it was empty. {_sure(rec['p_change'])} (real object {p['real']:.0%}, spot covered "
                f"{p['other_video_covered']:.0%}, see-through {p['see_through']:.0%}).{masks}")
    if st == "not seen in other recording":
        was = o.extra.get("status_before_coverage")
        why = f" It looked {was}, but" if was else ""
        return (f"Can't tell what happened to the {named}{where}.{why} the {other} video saw its spot in only "
                f"{cv.get('views', 0)} frames ({cv.get('share_seen_twice', 0):.0%} of it twice), not enough to say it "
                f"is gone.{masks}")
    return ""
