"""Phase 2: where objects are, and how that improves with more clips (Fin, 2026-10-10).

Several clips are reconstructed in one shared frame. Every clip gets its own SAM 2 objects
with a 90% position region (change.position). The same object in different clips of the same
scene state is matched, and the views of 1, 2, ... clips are pooled, which shows how much
each extra clip narrows down where it is. With two states (before / after) the step also
reports the smallest movement that could be told apart from noise with 1 or 2 clips per state.
"""

from __future__ import annotations

import json
import logging
import time
from itertools import combinations, product
from pathlib import Path

import numpy as np

log = logging.getLogger("uncertainscene.clips")


def _recon(spec, cache: Path):
    from ..recon.frames import Recon
    from ..recon.vggt import reconstruct_many
    from .scene01 import _key

    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"recon2_{_key(spec)}.npz"
    info_f = f.with_suffix(".json")
    if f.exists() and info_f.exists():
        try:
            return Recon.load(f), json.loads(info_f.read_text()) | {"cached": True}
        except Exception as e:  # noqa: BLE001
            log.warning("cache %s unreadable (%s); rebuilding", f.name, e)
    rec, info = reconstruct_many(list(spec.clips), spec.n_per_video)
    tmp = f.with_name(f.stem + "_tmp.npz")
    rec.save(tmp)
    tmp.replace(f)
    info_f.write_text(json.dumps(info))
    return rec, info


def scene_points(rec, stride: int = 6, conf_q: float = 0.3, max_points: int = 400000, seed: int = 0):
    """Coloured points of every frame (subsampled), with their clip index."""
    from ..change.geometry import unproject

    X, C, V = [], [], []
    H, W = rec.depth.shape[1:]
    grid = np.zeros((H, W), bool)
    grid[::stride, ::stride] = True
    for s in range(len(rec.depth)):
        ok = grid & (rec.depth[s] > 0) & (rec.conf[s] >= np.quantile(rec.conf[s], conf_q))
        x, v, u = unproject(rec.depth[s], rec.K[s], rec.E[s], ok)
        X.append(x)
        C.append(rec.images[s][v, u])
        V.append(np.full(len(x), rec.video[s]))
    X, C, V = np.concatenate(X), np.concatenate(C), np.concatenate(V)
    if len(X) > max_points:
        k = np.random.default_rng(seed).choice(len(X), max_points, replace=False)
        X, C, V = X[k], C[k], V[k]
    return X, C, V


def _est(objs: list) -> dict:
    """Main estimate for one physical object over the given clips: the model-uncertainty
    filter over all their views (views disagreeing if no view measurements exist)."""
    from ..change import position as pos

    e = pos.ekf(pos.all_views(objs))
    return e if e else pos.pooled(objs)


def movement_floor(groups: list[dict], states: list[str], scale: float) -> dict:
    """For objects matched across both states: the 90% radius of their before-to-after
    displacement (the smallest movement distinguishable from noise) and how many look moved
    (Mahalanobis beyond the 99% level), with k clips per state, averaged over clip choices."""
    from ..change import position as pos

    a_idx = [i for i, st in enumerate(states) if st == states[0]]
    b_idx = [i for i, st in enumerate(states) if st != states[0]]
    out = {}
    for k in range(1, min(len(a_idx), len(b_idx)) + 1):
        radii, flagged, dist = [], [], []
        for g in groups:
            ga = [i for i in a_idx if i in g]
            gb = [i for i in b_idx if i in g]
            if len(ga) < k or len(gb) < k:
                continue
            r, fl, dd = [], [], []
            for sa, sb in product(combinations(ga, k), combinations(gb, k)):
                ea = _est([g[i] for i in sa])
                eb = _est([g[i] for i in sb])
                r.append(pos.radius90(ea["cov"] + eb["cov"]) * scale * 100)
                fl.append(pos.mahalanobis2(ea, eb) > pos.CHI2_3_99)
                dd.append(float(np.linalg.norm(ea["mean"] - eb["mean"])) * scale * 100)
            radii.append(np.mean(r))
            flagged.append(np.mean(fl))
            dist.append(np.mean(dd))
        if radii:
            out[k] = {"min_detectable_move_cm": round(float(np.median(radii)), 2),
                      "share_flagged_as_moved": round(float(np.mean(flagged)), 3),
                      "median_apparent_move_cm": round(float(np.median(dist)), 2), "objects": len(radii)}
    return out


def analyse_clips(spec, out: Path, cache: Path, check=None) -> str:
    from ..change import inventory as inv
    from ..change import position as pos
    from ..change.fields import RobotModel, fit_floor
    from ..change.floor import combine_scale, object_scale
    from ..change.segment import build_objects, effective_voxel
    from ..viz import position_plots, scene3d
    from .catalog import rec_name
    from .poc import _Stage, enforce_size, short
    from .scene01 import _masks

    t0 = time.time()
    stage = _Stage(spec.name, check)
    rec, rinfo = _recon(spec, cache)
    stage(f"reconstruction of {len(spec.clips)} clips ({len(rec.images)} frames, {rinfo.get('chunks')} chunks)")
    X, C, V = scene_points(rec)
    floor = fit_floor(X, rec.E, RobotModel())
    stage(f"floor ({floor.floor_points} floor points)")
    masks = _masks(rec, spec, cache)
    voxel = effective_voxel(rec, masks, 0.012 / floor.scale_m_per_unit)
    struct = 1.2 / floor.scale_m_per_unit
    clips = []
    for v in range(len(spec.clips)):
        mv = {s: m for s, m in masks.items() if rec.video[s] == v}
        clips.append(build_objects(rec, mv, str(v + 1), voxel, struct, adapt_voxel=False))
    stage("SAM 2 objects per clip (" + ", ".join(str(len(c)) for c in clips) + ")")
    allobjs = [o for c in clips for o in c]
    inv.label_all(allobjs, rec)
    floor = combine_scale(floor, object_scale([{"label": o.label, "label_prob": o.label_prob,
                                                "extent_units": float(o.extent[0]), "complete": o.complete}
                                               for o in allobjs if not o.structure]))
    s = floor.scale_m_per_unit
    pos.annotate(allobjs, s, np.array(floor.up))
    stage("labels and position regions")

    states = list(spec.groups)
    first = [i for i, st in enumerate(states) if st == states[0]]
    groups_first = pos.match_groups([clips[i] for i in first])
    groups_first = [{first[i]: o for i, o in g.items()} for g in groups_first]
    curve = pos.clips_curve(groups_first, s)
    views = {str(k): v for k, v in pos.views_curve([o for o in clips[0] if not o.structure], s).items()}
    ekfc = {str(k): v for k, v in pos.ekf_curve([o for o in clips[0] if not o.structure], s).items()}
    move = {}
    if len(set(states)) > 1:
        groups_all = pos.match_groups(clips)
        move = movement_floor(groups_all, states, s)

    m = {"step_kind": "clips", "title": spec.title, "question": spec.question,
         "phase_title": "Where objects are, and how sure", "pair_type": "clips", "clips": list(spec.clips),
         "states": states, "variant": spec.variant, "frames_total": int(len(rec.images)),
         "recon_chunks": rinfo.get("chunks"), "chunk_residual_max": rinfo.get("chunk_residual_max"),
         "scale_m_per_unit": round(s, 4), "scale_range": [round(x, 4) for x in floor.scale_range],
         "objects_per_clip": [sum(not o.structure for o in c) for c in clips],
         "matched_objects": len(groups_first), "pos_clips_curve": {str(k): v for k, v in curve["by_clips"].items()},
         "pos_clips_curve_views": {str(k): v for k, v in curve["by_clips_views"].items()},
         "pos_coverage90_between_clips": curve["coverage90"], "pos_coverage90_between_clips_views": curve["coverage90_views"],
         "pos_nis_mean": curve["nis_mean"], "pos_views_curve": views, "pos_model_curve": ekfc,
         "pos_movement": {str(k): v for k, v in move.items()},
         "step_seconds": round(time.time() - t0, 1)}
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float) + "\n")

    # per object: position from each clip and fused, in cm
    rows = []
    for g in groups_first:
        idx = sorted(g)
        lab = max((g[i] for i in idx), key=lambda o: o.label_prob).label
        fused = {k: pos.radius90(_est([g[i] for i in idx[:k]])["cov"]) * s * 100
                 for k in range(1, len(idx) + 1)}
        rows.append({"label": lab, "clips": [int(i) + 1 for i in idx],
                     "radius90_cm_per_clip": [g[i].extra["position_cm"]["radius90_cm"] for i in idx],
                     "radius90_cm_fused_first_k": {str(k): round(v, 2) for k, v in fused.items()},
                     "centre_cm_fused": (_est([g[i] for i in idx])["mean"] * s * 100).round(1).tolist()})
    (out / "positions.json").write_text(json.dumps(rows, indent=2, default=float) + "\n")

    # figures: the same objects with 1, 2, ... clips fused
    rng = np.random.default_rng(0)
    bgk = rng.choice(np.nonzero(V == first[0])[0], min(30000, int((V == first[0]).sum())), replace=False)
    panels = []
    for k in range(1, len(first) + 1):
        items = []
        for g in groups_first:
            idx = sorted(g)[:k]
            if len(idx) < k:
                continue
            e = _est([g[i] for i in idx])
            o = g[idx[0]]
            items.append({"label": o.label or "object", "mean": e["mean"], "cov": e["cov"], "color": (31, 119, 180),
                          "r90_cm": pos.radius90(e["cov"]) * s * 100})
        panels.append({"title": f"{k} clip{'s' if k > 1 else ''} combined ({len(items)} objects)", "bg": X[bgk],
                       "bg_rgb": C[bgk], "items": items})
    position_plots.floor_plan(panels, np.array(floor.up), s, out / "position_map.png", spec.title)
    grid = []
    for g in sorted(groups_first, key=lambda g: -g[sorted(g)[0]].extra["position_cm"]["radius90_cm"]):
        idx = sorted(g)
        grid.append({"label": max(g.values(), key=lambda o: o.label_prob).label or "object",
                     "ests": [_est([g[i] for i in idx[:kk]]) for kk in range(1, len(idx) + 1)]})
    position_plots.region_grid(grid, np.array(floor.up), s, out / "position_closeups.png", spec.title)
    position_plots.precision_curves({int(k): v for k, v in views.items()}, curve, out / "position_precision.png",
                                    spec.title, model={int(k): v for k, v in ekfc.items()})

    # 3D: colour cloud per clip, fused objects with their 90% region, and the regions per number of clips
    pts = scene3d.Points()
    layers = []
    for v, lab in enumerate(spec.clips):
        k = np.nonzero(V == v)[0]
        k = k[rng.choice(len(k), min(60000, len(k)), replace=False)]
        layers.append({"name": f"clip {v + 1}: {rec_name(lab)} (colour)", "kind": "colour", "rec": str(v + 1),
                       "visible": v == 0, "size": 2, "pts": pts.add(X[k]), "rgb": scene3d._b64(C[k].astype(np.uint8))})
    for kk in range(1, len(first) + 1):
        rings = [pos.ellipsoid_points(*(lambda e: (e["mean"], e["cov"]))(_est([g[i] for i in sorted(g)[:kk]])))
                 for g in groups_first if len(g) >= kk]
        if rings:
            layers.append({"name": f"90% regions, {kk} clip{'s' if kk > 1 else ''}", "kind": "line", "rec": None,
                           "visible": kk == 1, "pts": pts.add(np.concatenate(rings)),
                           "color": ["#d62728", "#ff7f0e", "#2ca02c", "#1f77b4"][min(kk - 1, 3)]})
    first_objs = [max(g.values(), key=lambda o: o.label_prob) for g in groups_first]
    layers += scene3d.object_layer(pts, first_objs, None, "all clips", 100000, rng)
    objects = []
    for g in groups_first:
        e = _est([g[i] for i in sorted(g)])
        o = max(g.values(), key=lambda o: o.label_prob)
        r = pos.radius90(e["cov"]) * s * 100
        objects.append({"center": np.round(e["mean"], 4).tolist(), "label": o.label or "object", "status": "matched",
                        "color": "#1f77b4", "rec": None, "corners": scene3d.box_corners(o.X),
                        "hover": f"{o.label}: centre within {r:.1f} cm (90%) using {len(g)} clips",
                        "pal": scene3d.object_colour(o)})
    scene3d.save({"points": pts.tables, "layers": layers, "objects": objects}, out)
    write_report(spec, m, rows, out)
    stage("figures, 3D and report")
    enforce_size(out)
    c = m["pos_clips_curve"]
    return (f"{len(groups_first)} objects matched across clips; 90% radius "
            + " -> ".join(f"{v['median_radius90_cm']:.1f} cm ({k} clip{'s' if int(k) > 1 else ''})" for k, v in c.items())
            + f"; consistency (NIS) {curve['nis_mean']}, coverage {curve['coverage90']}")


def write_report(spec, m: dict, rows: list, out: Path) -> None:
    from .catalog import rec_name

    L = [f"# {spec.title}", "", f"**What this run tests:** {spec.question}", "",
         "Clips: " + "; ".join(f"{i + 1} = {rec_name(c)} ({st})" for i, (c, st) in enumerate(zip(spec.clips, spec.groups)))
         + f". Step folder `{spec.name}`.", "",
         "**How the range is measured.** Every view that sees an object measures its centre. VGGT says how sure it "
         "is of each pixel's depth, and SAM 2 says how much the object's outline wobbles; together they give each "
         "view's uncertainty. A Kalman filter adds the views one by one, so the range narrows with every view that "
         "carries information; extra clips simply add more views. As a check, the range is also computed purely from "
         "how much the views disagree.", "",
         "| Clips combined | 90% range, model (median over objects) | 90% range, views disagreeing | Objects |",
         "|---|---|---|---|"]
    cv = m.get("pos_clips_curve_views", {})
    for k, v in m["pos_clips_curve"].items():
        e = cv.get(k)
        L.append(f"| {k} | ±{v['median_radius90_cm']:.1f} cm | {'±%.1f cm' % e['median_radius90_cm'] if e else '-'} | "
                 f"{v['objects']} |")
    nis, cov = m.get("pos_nis_mean"), m.get("pos_coverage90_between_clips")
    if nis is not None or cov is not None:
        verdict = ("not measurable" if nis is None else "honest" if nis < 4.5 else
                   "overconfident: views land further apart than VGGT and SAM 2 predict, so the model range is too small")
        L += ["", f"**Consistency check.** Each new view lands, on average, {nis if nis is not None else '-'} "
              f"(squared, in units of the predicted spread) from the running estimate; 3 means the stated "
              f"uncertainties are honest ({verdict})."
              + (f" Two clips of the same scene agree within their combined 90% range {cov:.0%} of the time "
                 "(90% expected)." if cov is not None else "")]
    if m.get("pos_movement"):
        L += ["", "## Telling a real move from noise", "",
              "For objects found in both states (most did not move), the 90% radius of their before-to-after "
              "difference is the smallest move that stands out from noise.", "",
              "| Clips per state | Smallest detectable move (median) | Objects that look moved (99% test) | Apparent move, median |",
              "|---|---|---|---|"]
        for k, v in m["pos_movement"].items():
            L.append(f"| {k} | {v['min_detectable_move_cm']:.1f} cm | {v['share_flagged_as_moved']:.0%} | "
                     f"{v['median_apparent_move_cm']:.1f} cm |")
    L += ["", "![Where each object is, with 1, 2, ... clips pooled](position_map.png)", "",
          "![Close-ups: each object's region as clips are added](position_closeups.png)", "",
          "![How precision grows with views and clips](position_precision.png)", "",
          "## Objects", "", "| Object | Seen in clips | 90% range per clip (cm) | Combined, first 1/2/3 clips (cm) |",
          "|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["radius90_cm_per_clip"][0]):
        L.append(f"| {r['label']} | {', '.join(map(str, r['clips']))} | "
                 f"{', '.join(f'{x:.1f}' for x in r['radius90_cm_per_clip'])} | "
                 f"{' / '.join(f'{v:.1f}' for v in r['radius90_cm_fused_first_k'].values())} |")
    (out / "report.md").write_text("\n".join(L) + "\n")
