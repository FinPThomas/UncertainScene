"""scene01 experiments, version 2 (after the first overnight run, 2026-10-10).

The first run (experiments/poc.py, 56 frames per pair, per-point change detection) could not
separate real changes from noise. This version:
- reconstructs every extracted frame of both recordings (~185 each) in GPU-sized chunks
  aligned through shared anchor frames (recon.chunked);
- fits gravity and the floor from horizontal surfaces and combines two scale cues (phone
  height, sizes of recognised objects) (change.floor);
- segments every object with SAM 2 on key frames, merges masks across views in 3D, labels
  every object with CLIP (change.segment, change.inventory);
- decides changes per object, pooling the visibility test over the object's points, with
  thresholds from the object-level within-recording null (change.inventory).
The per-point detection and the heat maps stay (they are the uncertainty and deformation
maps); old-style point clusters are kept as a fallback when SAM 2 is not available.

Each queue entry is one `Run.step`; results land in results/change/<run_id>/<step>/.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np

from ..change.detect import DetectConfig, detect
from ..eval import metrics as M
from .poc import REC, _Stage, enforce_size, lighting_note, pair_type, short

log = logging.getLogger("uncertainscene.scene01")

LB, LBb, LA, LAb = "scene01_light_before_a", "scene01_light_before_b", "scene01_light_after_a", "scene01_light_after_b"
DB, DBb, DBc = "scene01_dark_before_a", "scene01_dark_before_b", "scene01_dark_before_c"


@dataclass
class Spec:
    name: str
    a: str
    b: str
    n_per_video: int | None = None     # None = every extracted frame (~185 per recording)
    offset: int = 0
    n_key: int = 36                    # SAM 2 key frames per recording
    sam_points: int = 24               # SAM 2 points per side
    variant: str = "base"
    detect: dict = field(default_factory=dict)
    robot: dict = field(default_factory=dict)
    fields_on: str = "B"
    voxel_m: float = 0.008
    outputs: str = "full"              # full | standard | light

    @property
    def frames_est(self) -> int:
        return 2 * (self.n_per_video or 185)

    @property
    def needs(self) -> dict:
        n = self.frames_est
        minutes = 4 + n / 40 * 0.8 + 2 * self.n_key * 2.5 / 60 + n / 60 + 4
        return {"disk_gb": 6, "ram_gb": 3.5, "minutes": round(minutes)}


def queue() -> list:
    """Order (Fin, 2026-10-10): the light before/after pair first as the pilot, then the other
    main pairs, then every remaining pair, then how many frames are enough, then robot
    scenarios (cheap, cached), then stability repeats."""
    q: list = []
    core = [(LB, LA), (LB, LBb), (DB, LB), (DB, LA), (LBb, LAb), (DB, DBb)]
    for a, b in core:
        q.append(Spec(f"p1_{short(a)}_vs_{short(b)}", a, b))
    q.append("summary_p1")

    every = [(x, y) for i, x in enumerate(REC) for y in list(REC)[i + 1:]]
    rest = [(a, b) for a, b in every if (a, b) not in core and (b, a) not in core]
    rest = [(b, a) if REC[a][1] == "after" and REC[b][1] == "before" else (a, b) for a, b in rest]   # before first
    for a, b in rest:
        q.append(Spec(f"p2_{short(a)}_vs_{short(b)}", a, b, outputs="standard"))
    q.append("summary_p2")

    # how many frames / key frames are enough (proper evaluation of the frame count)
    for a, b in (core[0], core[1]):
        for n in (28, 56, 100):
            q.append(Spec(f"p3_n{n}_{short(a)}_vs_{short(b)}", a, b, n_per_video=n, variant=f"n{n}",
                          outputs="standard"))
    for k in (16, 72):
        q.append(Spec(f"p3_key{k}_{short(LB)}_vs_{short(LA)}", LB, LA, n_key=k, variant=f"key{k}", outputs="standard"))
    q.append("summary_p3")

    robots = {"short_arm": {"reach_m": 0.6}, "long_arm": {"reach_m": 1.1},
              "table_mount": {"shoulder_height_m": 0.75}, "mobile_closer": {"base_offset_m": 0.6},
              "big_gripper": {"gripper_clearance_m": 0.2, "gripper_radius_m": 0.05}}
    for v, r in robots.items():
        for a, b in (core[0], core[4]):
            q.append(Spec(f"p4_{v}_{short(a)}_vs_{short(b)}", a, b, variant=v, robot=r, outputs="standard"))
    for a, b in (core[0], core[4]):
        q.append(Spec(f"p4_before_fields_{short(a)}_vs_{short(b)}", a, b, variant="fields_on_A", fields_on="A",
                      outputs="standard"))
    q.append("summary_p4")

    # stability: 100 frames per recording from different picks, and the reversed direction
    for off in (1, 2, 3):
        for a, b in core:
            q.append(Spec(f"p5_off{off}_{short(a)}_vs_{short(b)}", a, b, n_per_video=100, offset=off,
                          variant=f"offset{off}", outputs="light"))
        q.append(f"summary_p5_off{off}")
    q.append(Spec(f"p5_reversed_{short(LA)}_vs_{short(LB)}", LA, LB, variant="reversed", outputs="standard"))
    q.append("summary_p5")
    return q


# ---- cached stages ------------------------------------------------------------------------
def _key(spec: Spec) -> str:
    return f"{spec.a}_{spec.b}_n{spec.n_per_video or 'all'}_o{spec.offset}"


def _recon(spec: Spec, cache: Path):
    from ..recon.frames import Recon
    from ..recon.vggt import reconstruct_all

    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"recon2_{_key(spec)}.npz"
    info_f = f.with_suffix(".json")
    if f.exists() and info_f.exists():
        try:
            log.info("reusing cached reconstruction %s", f.name)
            return Recon.load(f), json.loads(info_f.read_text()) | {"cached": True}
        except Exception as e:  # noqa: BLE001
            log.warning("cache %s unreadable (%s); rebuilding", f.name, e)
    rec, info = reconstruct_all(spec.a, spec.b, spec.n_per_video, spec.offset)
    tmp = f.with_name(f.stem + "_tmp.npz")
    rec.save(tmp)
    tmp.replace(f)
    info_f.write_text(json.dumps(info))
    return rec, info


def _feats(rec, spec: Spec, cache: Path):
    from ..change.features import dino_features

    f = cache / f"dino2_{_key(spec)}.npy"
    if f.exists():
        try:
            return np.load(f)
        except Exception:  # noqa: BLE001
            pass
    feats = dino_features(rec.images)
    np.save(f.with_name(f.stem + "_tmp.npy"), feats)
    f.with_name(f.stem + "_tmp.npy").replace(f)
    return feats


def _masks(rec, spec: Spec, cache: Path) -> dict:
    """SAM 2 masks on key frames of both recordings, cached (packed bits)."""
    from ..change.segment import key_frames, sam_masks

    f = cache / f"sam2_{_key(spec)}_k{spec.n_key}_p{spec.sam_points}.npz"
    H, W = rec.depth.shape[1:]
    if f.exists():
        try:
            z = np.load(f, allow_pickle=True)
            meta = z["meta"].tolist()
            bits = np.unpackbits(z["bits"])[: len(meta) * H * W].reshape(len(meta), H, W).astype(bool)
            out: dict = {}
            for m, b in zip(meta, bits):
                out.setdefault(m["frame"], []).append({**m, "mask": b})
            for k in z["frames"].tolist():
                out.setdefault(k, [])
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("SAM cache unreadable (%s); recomputing", e)
    frames = key_frames(rec, 0, spec.n_key) + key_frames(rec, 1, spec.n_key)
    t0 = time.time()
    out = sam_masks(rec.files, frames, (H, W), points_per_side=spec.sam_points)
    log.info("SAM 2: %d masks on %d frames in %.0f s", sum(len(v) for v in out.values()), len(frames), time.time() - t0)
    meta, bits = [], []
    for s, ms in out.items():
        for m in ms:
            meta.append({k: v for k, v in m.items() if k != "mask"} | {"frame": s})
            bits.append(m["mask"])
    np.savez_compressed(f, meta=np.array(meta, dtype=object),
                        bits=np.packbits(np.stack(bits)) if bits else np.zeros(0, np.uint8), frames=np.array(frames))
    return out


# ---- the step -----------------------------------------------------------------------------
def step_fn(spec: Spec, cache: Path, store=None, check=None):
    def fn(out: Path, work: Path) -> str:
        h = logging.FileHandler(out / "step_log.txt")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(h)
        for name in ("uncertainscene", "httpx", "huggingface_hub"):
            logging.getLogger(name).setLevel(logging.INFO if name == "uncertainscene" else logging.WARNING)
        try:
            log.info("step %s: %s vs %s, %s frames/video, %d SAM key frames, variant %s", spec.name, spec.a, spec.b,
                     spec.n_per_video or "all", spec.n_key, spec.variant)
            return analyse(spec, out, cache, store, check)
        except Exception:
            log.exception("step %s failed", spec.name)
            raise
        finally:
            root.removeHandler(h)
            h.close()
    return fn


def analyse(spec: Spec, out: Path, cache: Path, store=None, check=None) -> str:
    from ..change import inventory as inv
    from ..change import scene_report
    from ..change.fields import RobotModel, base_placement, compute_fields, field_stats, fit_floor, object_fields
    from ..change.floor import combine_scale, object_scale
    from ..change.segment import build_objects
    from ..viz import objects_plots, plots, viewer

    t0 = time.time()
    stage = _Stage(spec.name, check)
    rec, rinfo = _recon(spec, cache)
    stage(f"reconstruction ({len(rec.images)} frames, {rinfo.get('chunks')} chunks, worst anchor residual "
          f"{rinfo.get('chunk_residual_max')})")
    feats = _feats(rec, spec, cache)
    stage("DINOv2 features")
    cfg = replace(DetectConfig(max_points=600000), **spec.detect)
    if spec.outputs == "light":
        cfg = replace(cfg, stride=max(cfg.stride, 3))
    res = detect(rec, feats, cfg)
    stage(f"point-level change detection ({len(res.A.X) + len(res.B.X)} points, tau {res.thresholds['tau_rel_depth']:.3f})")

    robot = replace(RobotModel(), **spec.robot)
    floor = fit_floor(np.concatenate([res.A.X, res.B.X]), rec.E, robot)
    stage(f"floor ({floor.floor_points} floor points, gravity refined {floor.gravity_change_deg} deg)")

    # ---- objects: SAM 2 + CLIP, or point clusters as a fallback ----
    objs_a = objs_b = []
    obj_thr, sam_error = {}, None
    try:
        masks = _masks(rec, spec, cache)
        voxel = 0.012 / floor.scale_m_per_unit
        struct = 1.2 / floor.scale_m_per_unit
        A = {s: m for s, m in masks.items() if rec.video[s] == 0}
        B = {s: m for s, m in masks.items() if rec.video[s] == 1}
        objs_a = build_objects(rec, A, "A", voxel, struct)
        objs_b = build_objects(rec, B, "B", voxel, struct)
        stage(f"SAM 2 objects ({len(objs_a)} in A, {len(objs_b)} in B)")
        inv.label_all(objs_a + objs_b, rec)
        stage("CLIP labels for every object")
        floor = combine_scale(floor, object_scale([{"label": o.label, "label_prob": o.label_prob,
                                                    "extent_units": float(o.extent[0]), "complete": o.complete}
                                                   for o in objs_a + objs_b if not o.structure]))
        obj_thr = inv.classify(objs_a, objs_b, rec, feats, res, cfg)
        stage("object-level changes")
    except Exception as e:  # noqa: BLE001 - SAM 2 missing or failing must not lose the rest
        if type(e).__name__ == "ResourceLimit":
            raise
        sam_error = f"{type(e).__name__}: {e}"
        log.exception("SAM 2 objects failed; falling back to point clusters")
        (out / "sam_error.txt").write_text(sam_error + "\n")

    s = floor.scale_m_per_unit
    for o in objs_a + objs_b:
        o.extra["description"] = inv.describe(o, rec.E, s, scale_range=floor.scale_range)

    # ---- heat maps ----
    f = fstats = None
    bm = {}
    try:
        f = compute_fields(res, rec, robot, which=spec.fields_on,
                           voxel_m=spec.voxel_m if spec.outputs != "light" else max(spec.voxel_m, 0.015), floor=floor)
        fstats = field_stats(f)
        bm = base_placement(f) if spec.outputs != "light" else {}
        if bm:
            fstats.update({"base_standable_frac": round(bm["standable_frac"], 3),
                           "base_best_score": None if bm["best_score"] is None else round(bm["best_score"], 3),
                           "base_best_score_changed": None if bm["best_score_changed"] is None
                           else round(bm["best_score_changed"], 3),
                           "base_best_from_operator_m": None if bm["best_m"] is None
                           else round(float(np.linalg.norm(bm["best_m"] - bm["base_now_m"])), 2)})
        stage(f"heat maps ({len(f['X'])} points, scale {s:.3f} m/unit)")
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "ResourceLimit":
            raise
        log.exception("heat maps failed; continuing without them")
        (out / "fields_error.txt").write_text(f"{type(e).__name__}: {e}\n")

    # ---- change objects for the report (SAM objects, else point clusters) ----
    if objs_a or objs_b:
        changes_objs = inv.to_change_objects(objs_a, objs_b, res)
        by_sam = {(o.recording, o.id): o for o in objs_a + objs_b}
        for co in changes_objs:
            so = by_sam[(co.recording, co.extra["sam_id"])]
            co.description = so.extra["description"]
            co.kind = {"changed in place": "appearance", "shifted": "appearance"}.get(so.status, so.status)
    else:
        from ..change.describe import label_objects
        from ..change.objects import find_objects
        changes_objs = find_objects(res)
        label_objects(changes_objs, res, rec, unit="cm", scale=s * 100)
    if f is not None:
        good = [o for o in changes_objs if o.extra["mask"].any()]
        object_fields(good, res, f)

    # ---- metrics ----
    ptype = pair_type(spec.a, spec.b)
    m = {"pair_type": ptype, "a": spec.a, "b": spec.b, "pair": f"{short(spec.a)}_vs_{short(spec.b)}",
         "n_per_video": rinfo.get("used_per_video"), "requested_per_video": spec.n_per_video, "offset": spec.offset,
         "n_key": spec.n_key, "variant": spec.variant, "version": 2, "detect_config": asdict(cfg),
         "robot": asdict(robot), "fields_on": spec.fields_on, "recon_seconds": rinfo.get("seconds"),
         "recon_chunks": rinfo.get("chunks"), "chunk_residual_max": rinfo.get("chunk_residual_max"),
         "chunks_dropped": rinfo.get("chunks_dropped"), "gpu_peak_gb": rinfo.get("gpu_peak_gb"),
         "recon_cached": rinfo.get("cached", False), "frames_total": int(len(rec.images)),
         "sam_error": sam_error, "scale_cues": floor.scale_cues}
    m.update(M.label_free(res, changes_objs))
    for side, ps in (("A", res.A), ("B", res.B)):
        u = ps.unc[np.isfinite(ps.unc)] * s * 100
        m[f"unc_cm_{side}_median"] = round(float(np.median(u)), 3) if len(u) else None
    if fstats:
        m.update({f"field_{k}": v for k, v in fstats.items()})
    m.update(object_metrics(objs_a, objs_b, obj_thr))
    rows = inv.object_rows(objs_a + objs_b, s, np.array(floor.up), floor.height)
    changes = [r for r in rows if r["status"] not in ("unchanged", "not seen in other recording", "unknown")
               and not r["structure"]]
    gt = M.load_ground_truth("scene01")
    if gt and ptype in ("change", "change+lighting"):
        sc = M.score([{"kind": c["status"], "label": c["label"], "description": c["description"]} for c in changes],
                     gt, scale_m=s)
        m.update({f"gt_{k}": v for k, v in sc.items()})
    m["step_seconds"] = round(time.time() - t0, 1)
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float) + "\n")
    (out / "objects.json").write_text(json.dumps(rows, indent=2, default=float) + "\n")
    (out / "changes.json").write_text(json.dumps(changes, indent=2, default=float) + "\n")

    # ---- figures, viewer, reports ----
    title = (f"{spec.name}: {spec.a} vs {spec.b} ({ptype}, {m['n_per_video']} frames/video, {spec.variant})")
    if objs_a or objs_b:
        objects_plots.inventory_sheet(objs_b + objs_a, rec, out / "objects.png", title)
        objects_plots.mask_overlays(objs_a, objs_b, rec, out / "object_overlays.png")
    if f is not None:
        plots.field_maps(f, rec, out / "heatmaps.png", title)
        plots.base_map(bm, out / "base_placement.png", title)
    if spec.outputs in ("full", "standard"):
        plots.change_map(res, rec, changes_objs, out / "change_map.png", title)
        plots.calibration(res, out / "calibration.png")
        markers = [{"x": float(o.centroid[0]), "y": float(o.centroid[1]), "z": float(o.centroid[2]),
                    "text": f"{o.recording}{o.id} {o.label}", "group": o.status,
                    "color": "#%02x%02x%02x" % inv.STATUS_COLOR.get(o.status, (0, 0, 0)),
                    "hover": o.extra.get("description", "")} for o in objs_a + objs_b if not o.structure]
        viewer.write_html(res, changes_objs, out / "viewer.html", title, fields=f, markers=markers)
        scene_report.write(out / "scene_report.md", spec.name, spec.a, spec.b, m, changes_objs, f, fstats,
                           lighting_note(spec.a, spec.b), inventory=rows)
    if spec.outputs == "full":
        viewer.write_ply(res, out / "cloud.ply")
        if f is not None:
            viewer.write_fields_ply(f, out / "fields.ply")
    stage("figures, viewer and reports")
    write_report(spec, m, rows, out)
    enforce_size(out)
    return (f"{ptype}: {m.get('obj_changed', 0)} changed objects of {m.get('obj_total', 0)} "
            f"({m.get('obj_changed_frac', 0):.0%}); labels: {', '.join(c['label'] or '?' for c in changes[:5])}; "
            f"scale {s:.3f} m/unit; {m['step_seconds']:.0f}s")


def physical(objs_a: list, objs_b: list) -> list:
    """One entry per physical object: every object of the after recording, plus objects of
    the before recording that have no counterpart there (removed, or only seen before)."""
    return [o for o in objs_b if not o.structure] + [
        o for o in objs_a if not o.structure and o.status in ("removed", "not seen in other recording")]


def object_metrics(objs_a: list, objs_b: list, thr: dict) -> dict:
    objs = physical(objs_a, objs_b)
    seen = [o for o in objs if o.status not in ("not seen in other recording", "unknown")]
    changed = [o for o in seen if o.status != "unchanged"]
    out = {"obj_total": len(objs), "obj_A": sum(not o.structure for o in objs_a),
           "obj_B": sum(not o.structure for o in objs_b), "obj_compared": len(seen), "obj_changed": len(changed),
           "obj_changed_frac": round(len(changed) / max(len(seen), 1), 4),
           "obj_labelled_confident": sum(o.label_prob >= 0.35 for o in objs)}
    for st in ("moved", "added", "removed", "changed in place", "shifted"):
        out[f"obj_{st.replace(' ', '_')}"] = sum(o.status == st for o in objs)
    out.update({f"obj_thr_{k}": v for k, v in thr.items()})
    return out


def write_report(spec: Spec, m: dict, rows: list[dict], out: Path) -> None:
    L = [f"# {spec.name}", "",
         f"**{spec.a}** (A, before) vs **{spec.b}** (B, after): pair type **{m['pair_type']}**, "
         f"{m['n_per_video']} frames per recording, {spec.n_key} SAM 2 key frames each, variant `{spec.variant}`.", ""]
    if m.get("sam_error"):
        L += [f"SAM 2 failed ({m['sam_error']}); objects below come from point clusters.", ""]
    L += ["| Measure | Value |", "|---|---|",
          f"| Objects found (A / B) | {m.get('obj_A', 0)} / {m.get('obj_B', 0)} |",
          f"| Objects compared / changed | {m.get('obj_compared', 0)} / {m.get('obj_changed', 0)} "
          f"({m.get('obj_changed_frac', 0):.0%}) |",
          f"| Moved / added / removed / changed in place / shifted | {m.get('obj_moved', 0)} / {m.get('obj_added', 0)} / "
          f"{m.get('obj_removed', 0)} / {m.get('obj_changed_in_place', 0)} / {m.get('obj_shifted', 0)} |",
          f"| Point-level changed share (old method) | {m['changed_frac']:.2%} |",
          f"| Scale | {m.get('field_scale_m_per_unit')} m/unit (phone height cue {m.get('field_scale_phone_height')}, "
          f"objects cue {m.get('field_scale_objects')} from {m.get('field_scale_objects_n')} objects) |",
          f"| Reconstruction | {m['frames_total']} frames in {m.get('recon_chunks')} chunks, {m['recon_seconds']} s, "
          f"worst anchor residual {m.get('chunk_residual_max')} |", ""]
    if m["pair_type"] in ("static", "lighting"):
        L += [f"Nothing was moved between these recordings, so every changed object is a false alarm "
              f"(**{m.get('obj_changed', 0)} of {m.get('obj_compared', 0)}**).", ""]
    for img, cap in (("objects.png", "every object, labelled, border = status"),
                     ("object_overlays.png", "object masks in the frames"), ("heatmaps.png", "heat maps"),
                     ("base_placement.png", "where a robot could stand"), ("change_map.png", "change map")):
        if (out / img).exists():
            L += [f"![{cap}]({img})", ""]
    L += ["## Changes", ""]
    ch = [r for r in rows if r["status"] not in ("unchanged", "not seen in other recording", "unknown") and not r["structure"]]
    L += [f"- **{r['id']} {r['label']}** ({r['status']}, {' x '.join(f'{x:.0f}' for x in r['size_cm'])} cm): "
          f"{r['description']}" for r in ch] or ["- none"]
    if (out / "scene_report.md").exists():
        L += ["", "Scene report with the full object inventory and simulator ranges: [scene_report.md](scene_report.md)"]
    if (out / "viewer.html").exists():
        L += ["", "Interactive 3D: download `viewer.html` and open it in a browser (objects are labelled; heat maps "
              "are layers in the legend)."]
    (out / "report.md").write_text("\n".join(L) + "\n")
