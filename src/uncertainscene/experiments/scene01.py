"""scene01 experiments, version 2 (after the first overnight run, 2026-10-10).

The first run (experiments/poc.py, 56 frames per pair, per-point change detection) could not
separate real changes from noise. This version:
- reconstructs every extracted frame of both recordings (~370 each at 6 fps) in GPU-sized chunks
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
    n_per_video: int | None = None     # None = every extracted frame (~370 per recording at 6 fps)
    offset: int = 0
    n_key: int = 44                    # SAM 2 key frames per recording
    sam_points: int = 32               # SAM 2 points per side (32x32 grid finds smaller objects than 24x24)
    variant: str = "base"
    detect: dict = field(default_factory=dict)
    robot: dict = field(default_factory=dict)
    fields_on: str = "B"
    voxel_m: float = 0.008
    outputs: str = "full"              # full | standard | light
    refine_poses: bool = True          # bundle adjustment of the VGGT poses (not for light steps)
    motion: bool | None = None         # feature-matched distance moved + deformation; None = full outputs only

    @property
    def frames_est(self) -> int:
        return 2 * (self.n_per_video or 370)

    @property
    def needs(self) -> dict:
        n = self.frames_est
        minutes = 4 + n / 40 * 0.8 + 2 * self.n_key * 4 / 60 + n / 60 + 4
        if self.refine_poses and self.outputs != "light":
            minutes += n / 75                  # SIFT + matching + bundle adjustment (cached per reconstruction)
        if self.motion or (self.motion is None and self.outputs == "full"):
            minutes += n / 120                 # feature motion on every 2nd frame
        return {"disk_gb": 8, "ram_gb": 3.0 + 0.003 * n, "minutes": round(minutes)}


@dataclass
class ClipsSpec:
    """Several clips (takes) reconstructed together, to measure how precisely each object is
    located and how that improves with more clips. `groups` names the scene state of each
    clip (clips with the same state show the same scene)."""
    name: str
    clips: tuple
    groups: tuple
    n_per_video: int | None = 240      # per clip; 4 clips x every frame would not fit in Colab RAM
    n_key: int = 44
    sam_points: int = 32
    variant: str = "clips"
    outputs: str = "full"
    title: str = ""
    question: str = ""

    @property
    def needs(self) -> dict:
        n = len(self.clips) * (self.n_per_video or 370)
        minutes = 4 + n / 40 * 0.8 + len(self.clips) * self.n_key * 4 / 60 + 6 + n / 75   # + pose refinement
        return {"disk_gb": 8, "ram_gb": 2.5 + 0.003 * n, "minutes": round(minutes)}   # 0.006/frame never fit beside the main steps


def queue() -> list:
    """Order (Fin, 2026-10-10): the light before/after pair first as the pilot, then the other
    main pairs, then where objects are and how that improves with more clips, then every
    remaining pair, how many frames are enough, robot variants and repeats.

    Step folders say what they compare: <phase>_<variant>_<kind>_<recording a>_vs_<recording b>,
    e.g. 1-main_moved_light-before-a_vs_light-after-a (see experiments/catalog.py)."""
    from .catalog import PHASES, rec_name, step_name

    q: list = []
    core = [(LB, LA), (LB, LBb), (DB, LB), (DB, LA), (LBb, LAb), (DB, DBb)]
    for i, (a, b) in enumerate(core):
        q.append(Spec(step_name("1-main", a, b), a, b))
        if i == 0:      # right after the pilot pair, reusing its reconstruction (Fin chose one pair first)
            from .splats import SplatSpec

            q.append(SplatSpec(step_name("1b-splat", a, b), a, b,
                               title=f"Gaussian splats: {rec_name(a)} vs {rec_name(b)}",
                               question=PHASES["1b-splat"][1]))
    q.append("1-main_summary")

    q.append(ClipsSpec("2-position_3-clips_no-change_dark-before-a-b-c", (DB, DBb, DBc), ("before",) * 3,
                       title="Position uncertainty with 1, 2 and 3 clips: dark, before, takes a, b and c",
                       question="How precisely is each object located, and how much does each extra clip of the "
                                "same scene shrink that range?"))
    q.append(ClipsSpec("2-position_2-clips-each_moved_light-before-a-b_vs_light-after-a-b", (LB, LBb, LA, LAb),
                       ("before", "before", "after", "after"),
                       title="Position uncertainty and change with 1 or 2 clips per state: light, before a+b vs "
                             "after a+b",
                       question="With two clips of each state instead of one, are object positions tighter and "
                                "are moved objects easier to tell from noise?"))
    q.append("2-position_summary")

    every = [(x, y) for i, x in enumerate(REC) for y in list(REC)[i + 1:]]
    rest = [(a, b) for a, b in every if (a, b) not in core and (b, a) not in core]
    rest = [(b, a) if REC[a][1] == "after" and REC[b][1] == "before" else (a, b) for a, b in rest]   # before first
    for a, b in rest:
        q.append(Spec(step_name("3-allpairs", a, b), a, b, outputs="standard"))
    q.append("3-allpairs_summary")

    for a, b in (core[0], core[1]):
        for n in (28, 100, 185):
            q.append(Spec(step_name("4-frames", a, b, f"{n}-frames"), a, b, n_per_video=n, variant=f"n{n}",
                          outputs="standard"))
    for k in (16, 72):
        q.append(Spec(step_name("4-frames", LB, LA, f"{k}-sam-views"), LB, LA, n_key=k, variant=f"key{k}",
                      outputs="standard"))
    q.append("4-frames_summary")

    robots = {"short_arm": {"reach_m": 0.7}, "long_arm": {"reach_m": 1.3},
              "low_mount": {"shoulder_height_m": 0.45}, "fixed_base": {"mobile": False},
              "big_gripper": {"gripper_clearance_m": 0.2, "gripper_radius_m": 0.05}}
    for v, r in robots.items():
        for a, b in (core[0], core[4]):
            q.append(Spec(step_name("5-robot", a, b, v.replace("_", "-")), a, b, variant=v, robot=r,
                          outputs="standard"))
    for a, b in (core[0], core[4]):
        q.append(Spec(step_name("5-robot", a, b, "maps-of-before-scene"), a, b, variant="fields_on_A",
                      fields_on="A", outputs="standard"))
    q.append("5-robot_summary")

    for off in (1, 2, 3):
        for a, b in core:
            q.append(Spec(step_name("6-repeat", a, b, f"frame-picks-{off}"), a, b, n_per_video=100, offset=off,
                          variant=f"offset{off}", outputs="light"))
        q.append(f"6-repeat_summary-{off}")
    q.append(Spec(step_name("6-repeat", LA, LB, "reversed"), LA, LB, variant="reversed", outputs="standard"))
    q.append("6-repeat_summary")
    return q


# ---- cached stages ------------------------------------------------------------------------
def _key(spec) -> str:
    if isinstance(spec, ClipsSpec):
        return "clips_" + "_".join(short(c) for c in spec.clips) + f"_n{spec.n_per_video or 'all'}"
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


def _posed(spec, cache: Path, rec, rinfo: dict, stage=None):
    """Refine the reconstruction's camera poses by bundle adjustment (recon.poses, cached per
    reconstruction), so every later stage works on the sharper poses. Light steps (repeats) keep
    VGGT's poses to save time. Returns rec, rinfo (with rinfo["poses"]) and the adjusted points."""
    if getattr(spec, "outputs", "full") == "light" or not getattr(spec, "refine_poses", True):
        return rec, rinfo, None
    from ..recon.poses import refine_recon

    try:
        rec, pts, rep = refine_recon(rec, cache, _key(spec))
        msg = (f"camera poses refined: {rep['vggt']['median_px']:.1f} px -> {rep['final']['median_px']:.2f} px "
               f"reprojection error, cameras turned {rep['rotation_change_deg_median']:.2f} deg (median)"
               f"{', cached' if rep.get('cached') else ''}")
        if stage is not None:
            stage(msg)
        else:
            log.info(msg)
        return rec, rinfo | {"poses": rep}, pts
    except Exception as e:  # noqa: BLE001 - VGGT poses still work
        if type(e).__name__ == "ResourceLimit":
            raise
        log.exception("pose refinement failed; keeping VGGT poses")
        return rec, rinfo | {"poses": {"failed": f"{type(e).__name__}: {e}"[:300]}}, None


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
    frames = [k for v in np.unique(rec.video) for k in key_frames(rec, int(v), spec.n_key)]
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
            from .splats import SplatSpec, analyse_splat

            if isinstance(spec, SplatSpec):
                log.info("step %s: Gaussian splats of %s and %s", spec.name, spec.a, spec.b)
                return analyse_splat(spec, out, cache, check, store)
            if isinstance(spec, ClipsSpec):
                from .clips import analyse_clips

                log.info("step %s: clips %s, %s frames/video", spec.name, ", ".join(spec.clips), spec.n_per_video or "all")
                return analyse_clips(spec, out, cache, check)
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
    from ..change import position as pos
    from ..change.segment import build_objects, effective_voxel, merge_voxel
    from ..viz import objects_plots, plots, viewer
    from .catalog import describe as describe_step

    t0 = time.time()
    stage = _Stage(spec.name, check)
    rec, rinfo = _recon(spec, cache)
    stage(f"reconstruction ({len(rec.images)} frames, {rinfo.get('chunks')} chunks, worst anchor residual "
          f"{rinfo.get('chunk_residual_max')})")
    rec, rinfo, _ = _posed(spec, cache, rec, rinfo, stage)
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
        voxel = effective_voxel(rec, masks, 0.012 / floor.scale_m_per_unit)   # one grid for both recordings
        struct = 1.2 / floor.scale_m_per_unit
        A = {s: m for s, m in masks.items() if rec.video[s] == 0}
        B = {s: m for s, m in masks.items() if rec.video[s] == 1}
        mvox = merge_voxel(rec, masks, voxel)                                 # one merge grid for both
        objs_a = build_objects(rec, A, "A", voxel, struct, adapt_voxel=False, merge_vox=mvox)
        objs_b = build_objects(rec, B, "B", voxel, struct, adapt_voxel=False, merge_vox=mvox)
        stage(f"SAM 2 objects ({len(objs_a)} in A, {len(objs_b)} in B)")
        inv.label_all(objs_a + objs_b, rec)
        stage("CLIP labels for every object")
        try:
            from ..change.rigidity import copilot

            n_rig = copilot(objs_a + objs_b)
            stage(f"Rigidity Copilot: expected stiffness of {n_rig} objects")
        except Exception:  # noqa: BLE001 - stiffness priors are a bonus
            log.exception("Rigidity Copilot failed; assets get no stiffness")
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
    pos.annotate(objs_a + objs_b, s, np.array(floor.up))
    for o in objs_a + objs_b:
        o.extra["description"] = inv.describe(o, rec.E, s, scale_range=floor.scale_range)
        pc = o.extra.get("position_cm")
        if pc and not o.structure and not pc["too_few_views"]:
            o.extra["description"] += (f" Its centre is known to within about {pc['radius90_cm']:.0f} cm (90%, from "
                                       f"{pc['n_views']} views; one view alone: {pc['single_view_radius90_cm']:.0f} cm).")

    # ---- distance moved and deformation from matched feature points ----
    mot = None
    if objs_a and (spec.motion or (spec.motion is None and spec.outputs == "full")):
        try:
            from ..change import motion3d

            mot = motion3d.measure(rec, objs_a, s)
            by = {(o.recording, o.id): o for o in objs_a}
            for r in mot["objects"]:
                o = by[(r["recording"], r["id"])]
                o.extra["motion"] = r
                if r["moved_beyond_noise"]:
                    o.extra["description"] += (f" Matched feature points: moved {r['moved_cm']:.0f} cm (90%: "
                                               f"{r['moved_ci_cm'][0]:.0f}-{r['moved_ci_cm'][1]:.0f} cm), turned "
                                               f"{r['rotation_deg']:.0f} deg.")
                if r["deformed"]:
                    o.extra["description"] += (f" Its shape changed: up to {r['deform_peak_cm']:.1f} cm beyond its rigid "
                                               f"motion, {r['deform_peak_where']} ({r['deformed_share']:.0%} of it).")
            sm = mot["summary"]
            stage(f"feature motion ({sm['links']} matched points, point uncertainty {sm['point_sigma_cm_median']} cm, "
                  f"{sm['objects_moved']} objects moved, {sm['objects_deformed']} deformed)")
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "ResourceLimit":
                raise
            log.exception("feature motion failed; continuing without it")
            (out / "motion_error.txt").write_text(f"{type(e).__name__}: {e}\n")
            mot = None

    # ---- node-structured object assets; objects kept for later steps (per-object splats) ----
    assets_scene = None
    if objs_a or objs_b:
        try:
            from ..change import assets

            assets.save_objects(objs_a + objs_b, cache / f"objects_{_key(spec)}.npz")
            if spec.outputs == "full":
                assets_scene = assets.build(objs_a + objs_b, s, np.array(floor.up), out / "assets", mot, motion_objs=objs_a)
                stage(f"object assets ({len(assets_scene['objects'])} objects, {len(assets_scene['contacts'])} contacts)")
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "ResourceLimit":
                raise
            log.exception("object assets failed; continuing without them")
            (out / "assets_error.txt").write_text(f"{type(e).__name__}: {e}\n")

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
    d = describe_step(spec.name, {"pair_type": ptype, "a": spec.a, "b": spec.b, "variant": spec.variant})
    m = {"title": d["title"], "question": d["question"], "phase_title": d["phase_title"], "pair_type": ptype, "a": spec.a, "b": spec.b, "pair": f"{short(spec.a)}_vs_{short(spec.b)}",
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
    m["poses"] = rinfo.get("poses")
    if assets_scene is not None:
        m["assets_objects"] = len(assets_scene["objects"])
        m["assets_deformable"] = sum(o["behaviour"] == "deformable" for o in assets_scene["objects"])
        m["assets_contacts"] = len(assets_scene["contacts"])
    if mot is not None:
        m.update({f"motion_{k}": v for k, v in mot["summary"].items()})
        (out / "motion.json").write_text(json.dumps({"summary": mot["summary"], "objects": mot["objects"]},
                                                    indent=2, default=float) + "\n")
    m.update(position_metrics(objs_a, objs_b, s))
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
    if objs_a or objs_b:
        _position_figures(objs_a, objs_b, res, floor, s, m, out, d["title"])
    if mot is not None:
        try:
            from ..change import motion3d

            motion3d.figure(mot, rec, objs_a, out / "motion_map.png", f"{d['title']}: distance moved and deformation")
        except Exception:  # noqa: BLE001
            log.exception("motion figure failed")
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
    try:
        _write_scene3d(res, objs_a, objs_b, f, out, (short(spec.a), short(spec.b)), motion=mot)
    except Exception:  # noqa: BLE001 - the viewer file must not lose the step
        log.exception("scene3d.json failed")
    stage("figures, viewer and reports")
    write_report(spec, m, rows, out)
    enforce_size(out)
    return (f"{ptype}: {m.get('obj_changed', 0)} changed objects of {m.get('obj_total', 0)} "
            f"({m.get('obj_changed_frac', 0):.0%}); labels: {', '.join(c['label'] or '?' for c in changes[:5])}; "
            f"scale {s:.3f} m/unit; {m['step_seconds']:.0f}s")


def _write_scene3d(res, objs_a, objs_b, f, out: Path, labels, max_mb: float = 13.0, motion=None) -> None:
    """Dense 3D scene for the viewer, split into files under the git limit (scene3d.save)."""
    from ..viz import scene3d

    n = 120000
    while True:
        sc = scene3d.from_analysis(res, objs_a, objs_b, f, n_colour=n, n_heat=int(0.4 * n), labels=labels)
        if motion is not None:
            from ..change.motion3d import scene_layers

            pts = scene3d.Points()
            pts.tables = sc["points"]
            sc["layers"] += scene_layers(motion, pts)
        txt = json.dumps(sc, separators=(",", ":"), default=float)
        if len(txt) < max_mb * 1024**2 or n < 3000:
            break
        n = int(n * 0.75)
    scene3d.save(sc, out)


def position_metrics(objs_a: list, objs_b: list, scale: float) -> dict:
    """How precisely objects are located, from one video each (label-free): the main range from
    the VGGT-confidence + SAM 2 wobble filter, the views-disagreeing range as a check, the
    filter's consistency (NIS, ~3 when honest), how the range shrinks with views, and, for
    objects unchanged between the two recordings, whether their positions agree within the
    stated ranges (only meaningful where nothing moved)."""
    from ..change import position as pos

    things = [o for o in objs_a + objs_b if not o.structure and "position" in o.extra
              and not o.extra["position_cm"]["too_few_views"]]
    out: dict = {}
    if not things:
        return out
    pc = [o.extra["position_cm"] for o in things]
    med = (lambda key: round(float(np.median([p[key] for p in pc if p.get(key) is not None])), 2)
           if any(p.get(key) is not None for p in pc) else None)
    out.update({"pos_method": pc[0]["method"], "pos_radius90_cm_median": med("radius90_cm"),
                "pos_radius90_cm_views_median": med("radius90_cm_views"),
                "pos_single_view_radius90_cm_median": med("single_view_radius90_cm"),
                "pos_depth_sd_cm_median": med("depth_sd_cm"), "pos_side_sd_cm_median": med("side_sd_cm"),
                "pos_nis_median": med("nis_mean"), "pos_views_median": int(np.median([p["n_views"] for p in pc])),
                "pos_wobble_share": round(float(np.mean([p.get("wobble_views", 0) / max(p["n_views"], 1) for p in pc])), 3),
                "pos_views_curve": {str(k): v for k, v in pos.views_curve(things, scale).items()},
                "pos_model_curve": {str(k): v for k, v in pos.ekf_curve(things, scale).items()}})
    d2 = []
    for o in objs_a:
        if o.status == "unchanged" and o.partner is not None and not o.structure and o.partner < len(objs_b):
            d2.append(pos.mahalanobis2(o.extra["position"], objs_b[o.partner].extra["position"]))
    if d2:
        out["pos_unchanged_pairs"] = len(d2)
        out["pos_coverage90_unchanged"] = round(float(np.mean(np.array(d2) <= pos.CHI2_3_90)), 3)
    return out


def _position_figures(objs_a, objs_b, res, floor, s, m, out: Path, title: str) -> None:
    from ..change.inventory import STATUS_COLOR
    from ..viz import position_plots

    rng = np.random.default_rng(0)
    panels = []
    for ps, objs, lab in ((res.A, objs_a, "before"), (res.B, objs_b, "after")):
        k = rng.choice(len(ps.X), min(30000, len(ps.X)), replace=False) if len(ps.X) else np.zeros(0, int)
        items = [{"label": o.label or "object", "mean": o.extra["position"]["mean"], "cov": o.extra["position"]["cov"],
                  "color": STATUS_COLOR.get(o.status, (90, 90, 90)), "r90_cm": o.extra["position_cm"]["radius90_cm"]}
                 for o in objs if not o.structure and "position" in o.extra]
        panels.append({"title": f"{lab}: {len(items)} objects", "bg": ps.X[k], "bg_rgb": ps.rgb[k], "items": items})
    position_plots.floor_plan(panels, np.array(floor.up), s, out / "position_map.png", title)
    position_plots.precision_curves({int(k): v for k, v in m.get("pos_views_curve", {}).items()}, None,
                                    out / "position_precision.png", title,
                                    model={int(k): v for k, v in m.get("pos_model_curve", {}).items()})


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
    L = [f"# {m.get('title', spec.name)}", "",
         f"**What this run tests:** {m.get('question', '')}", "",
         f"Before: `{spec.a}`. After: `{spec.b}`. {m['n_per_video']} frames per recording, {spec.n_key} SAM 2 views "
         f"each. Step folder `{spec.name}` (part of *{m.get('phase_title', '')}*).", ""]
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
          f"worst anchor residual {m.get('chunk_residual_max')} |",
          f"| Where objects are (90% range, median over objects) | ±{m.get('pos_radius90_cm_median', '-')} cm from "
          f"VGGT confidence and SAM 2 mask wobble over {m.get('pos_views_median', '-')} views (one view alone: "
          f"±{m.get('pos_single_view_radius90_cm_median', '-')} cm); check from views disagreeing: "
          f"±{m.get('pos_radius90_cm_views_median', '-')} cm |",
          f"| Consistency check (3 = honest, higher = overconfident) | {m.get('pos_nis_median', '-')} |"]
    if m.get("pos_coverage90_unchanged") is not None:
        L.append(f"| Do unchanged objects agree between the recordings? | {m['pos_coverage90_unchanged']:.0%} within "
                 f"their combined 90% ranges ({m['pos_unchanged_pairs']} objects; about 90% expected where nothing "
                 f"moved) |")
    p = m.get("poses") or {}
    if p.get("final"):
        L.append(f"| Camera poses (bundle adjustment from VGGT) | reprojection error {p['vggt']['median_px']:.1f} -> "
                 f"{p['final']['median_px']:.2f} px median, cameras turned {p['rotation_change_deg_median']:.2f} deg |")
    elif p.get("failed"):
        L.append(f"| Camera poses | refinement failed ({p['failed']}); VGGT poses used |")
    if m.get("motion_links") is not None:
        L.append(f"| Matched feature points before/after | {m['motion_links']}; uncertainty per point {m['motion_point_sigma_cm_median']} "
                 f"cm median (1 sigma, from the triangulation; of which {m['motion_misalignment_sigma_cm']} cm misalignment "
                 f"between the recordings); {m['motion_links_moved_share']:.0%} moved beyond their own uncertainty |")
        L.append(f"| Objects moved / deformed (feature points) | {m['motion_objects_moved']} / "
                 f"{m['motion_objects_deformed']} of {m['motion_objects_measured']} measured |")
    L += [""]
    mo = out / "motion.json"
    if mo.exists():
        rows_m = [r for r in json.loads(mo.read_text())["objects"] if r["moved_beyond_noise"] or r["deformed"]]
        if rows_m:
            L += ["## Distance moved and deformation (matched feature points)", "",
                  "Each object's feature points are matched between the recordings. Its rigid motion gives the distance "
                  "moved; what the rigid motion cannot explain is deformation (e.g. a bent cushion corner).", "",
                  "Every matched point carries its own uncertainty (from how many frames saw it, from what angles); "
                  "deformation counts only where it is beyond that.", "",
                  "| Object | Moved (90%) | Turned | Deformation p90 / peak (± point uncertainty) | Where | Share beyond uncertainty |",
                  "|---|---|---|---|---|---|"]
            for r in rows_m[:15]:
                ci = r["moved_ci_cm"] or [None, None]
                L.append(f"| {r['label'] or 'object'} {r['recording']}{r['id']} | {r['moved_cm']:.1f} cm "
                         f"({ci[0]}-{ci[1]}) | {r['rotation_deg']:.0f} deg | {r['deform_p90_cm']:.1f} / "
                         f"{r['deform_peak_cm']:.1f} ± {r.get('deform_peak_sigma_cm', '-')} cm | {r['deform_peak_where']} | {r['deformed_share']:.0%} |")
            L += ["", "![distance moved and deformation](motion_map.png)", ""]
    sg = out / "assets" / "scene_graph.json"
    if sg.exists():
        g = json.loads(sg.read_text())
        L += ["## Object assets (node graphs)", "",
              f"`assets/` holds {len(g['objects'])} objects as node graphs (nodes with weights, edges with rest length, "
              "spring constant from the Rigidity Copilot and measured strain, convex hull also as `_hull.obj`) and "
              "`scene_graph.json` with contacts.", ""]
        rg = [o for o in g["objects"] if o.get("rigidity") is not None]
        if rg:
            L += ["Rigidity Copilot (CLIP asked what each object is made of and how stiff it is; an expectation from "
                  "appearance, checked against the measured strain where there is any):", "",
                  "| Object | Status | Material (CLIP) | Rigidity 0-1 | Young's modulus | Measured | Check |",
                  "|---|---|---|---|---|---|---|"]
            for o in sorted(rg, key=lambda o: o["status"] == "unchanged")[:15]:
                L.append(f"| {o['label'] or 'object'} | {o['status']} | {o['material']} | {o['rigidity']:.2f} | "
                         f"{o['youngs_modulus_pa']:.2g} Pa | {o['behaviour'] or '-'} | {o.get('copilot_check') or '-'} |")
            L.append("")
        de = [o for o in g["objects"] if o["behaviour"] == "deformable"]
        if de:
            L.append("Deformable (strained edges): " + ", ".join(f"{o['label'] or 'object'} ({o['edges_strained']} edges)"
                                                               for o in de[:10]) + ".")
        rel = [c["relation"] for c in g["contacts"] if "rests on" in c["relation"]]
        if rel:
            L.append("Contacts: " + "; ".join(rel[:10]) + ".")
        L.append("")
    if m["pair_type"] in ("static", "lighting"):
        L += [f"Nothing was moved between these recordings, so every changed object is a false alarm "
              f"(**{m.get('obj_changed', 0)} of {m.get('obj_compared', 0)}**).", ""]
    for img, cap in (("motion_map.png", "distance moved and deformation from matched feature points"),
                     ("objects.png", "every object, labelled, border = status"),
                     ("object_overlays.png", "object masks in the frames"), ("heatmaps.png", "heat maps"),
                     ("position_map.png", "where each object is: 90% regions, top-down"),
                     ("position_precision.png", "how position precision grows with more views"),
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
