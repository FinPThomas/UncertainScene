"""Proof-of-concept experiment queue: joint VGGT reconstruction + visibility-aware change
detection on scene01, then ablations, sweeps and repeats.

Each queue entry is one `Run.step`: it reconstructs (or reuses a cached reconstruction of)
two recordings, detects and describes changes, evaluates them and writes figures, a 3D viewer
and a report into the step's results folder, which the runner pushes to GitHub.

Order matters: the proof-of-concept comparison (change vs nothing-changed vs lighting-only)
comes first, so even a short session produces the headline result. Later phases are designed
to keep a 12-hour session busy with useful, non-overlapping work.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np

from ..change.detect import DetectConfig, detect
from ..change.objects import find_objects, summarise
from ..eval import metrics as M

log = logging.getLogger("uncertainscene.poc")

REC = {  # label -> (lighting, state)
    "scene01_dark_before_a": ("dark", "before"), "scene01_dark_before_b": ("dark", "before"),
    "scene01_dark_before_c": ("dark", "before"), "scene01_light_before_a": ("light", "before"),
    "scene01_light_before_b": ("light", "before"), "scene01_light_after_a": ("light", "after"),
    "scene01_light_after_b": ("light", "after"),
}


def pair_type(a: str, b: str) -> str:
    (la, sa), (lb, sb) = REC[a], REC[b]
    if sa == sb:
        return "static" if la == lb else "lighting"
    return "change" if la == lb else "change+lighting"


def short(label: str) -> str:
    return label.replace("scene01_", "").replace("_before_", "B").replace("_after_", "A")


@dataclass
class Spec:
    name: str
    a: str
    b: str
    n_per_video: int = 28          # 56 frames: fits a T4 (VGGT ~0.2 GB per frame); capped per GPU anyway
    offset: int = 0
    variant: str = "base"
    detect: dict = field(default_factory=dict)
    robot: dict = field(default_factory=dict)     # RobotModel overrides for the heat maps
    fields_on: str = "B"                          # which recording the heat maps describe
    voxel_m: float = 0.01
    outputs: str = "full"          # full | standard | light
    gpu: bool = True

    @property
    def needs(self) -> dict:
        frames = 2 * self.n_per_video
        ram = 7 if self.detect.get("stride") == 1 else 6
        return {"disk_gb": 8 if self.gpu else 1, "ram_gb": ram, "minutes": 8 + frames / 10 if self.gpu else 5}


def queue() -> list:
    """Steps in order. A string entry means "write the run summary here" (a cheap CPU step).

    Emphasis (Fin, 2026-10-09): the creative outputs (uncertainty, deformation, reachability,
    approachability and interactability heat maps, scene reports with sim ranges) over long
    ablation grids. Only two ablations stay, as sanity checks of the method."""
    q: list = []
    LB, LBb, LA, LAb = ("scene01_light_before_a", "scene01_light_before_b",
                        "scene01_light_after_a", "scene01_light_after_b")
    DB, DBb, DBc = "scene01_dark_before_a", "scene01_dark_before_b", "scene01_dark_before_c"
    core = [(LB, LA), (LB, LBb), (DB, LB), (DB, LA), (DB, DBb), (LBb, LAb)]

    # Phase 1: proof of concept with every heat map and a scene report per pair
    for a, b in core:
        q.append(Spec(f"p1_{short(a)}_vs_{short(b)}", a, b))

    # Phase 1b: lighting (Fin, 2026-10-09): every light/dark contrast with other takes, so the
    # lighting report has repeats: lighting only, real changes through a lighting change, and
    # a nothing-changed pair in each lighting
    light = [(DBb, LAb), (DBc, LA), (DBb, LBb), (DBc, LB), (DBb, DBc), (LA, LAb), (LBb, LA)]
    for a, b in light:
        q.append(Spec(f"p1b_{short(a)}_vs_{short(b)}", a, b, variant="base", outputs="standard"))
    q.append("summary_p1")

    # Phase 2: dense versions of the main pairs (every pixel, as many frames as the GPU fits):
    # the best-looking heat maps and viewers
    for a, b in core[:4]:
        q.append(Spec(f"p2_dense_{short(a)}_vs_{short(b)}", a, b, n_per_video=48, variant="dense",
                      detect={"stride": 1, "max_points": 800000}, voxel_m=0.007))
    q.append("summary_p2")

    # Phase 3: robot scenarios on the change pairs (cheap: reuses cached reconstructions)
    robots = {"short_arm": {"reach_m": 0.6}, "long_arm": {"reach_m": 1.1},
              "table_mount": {"shoulder_height_m": 0.75}, "mobile_closer": {"base_offset_m": 0.6},
              "big_gripper": {"gripper_clearance_m": 0.2, "gripper_radius_m": 0.05},
              "tall_operator": {"phone_height_m": 1.55}, "short_operator": {"phone_height_m": 1.15}}
    for v, r in robots.items():
        for a, b in (core[0], core[3]):
            q.append(Spec(f"p3_{v}_{short(a)}_vs_{short(b)}", a, b, variant=v, robot=r, outputs="standard"))
    for a, b in (core[0], core[3]):    # heat maps of the scene before the change
        q.append(Spec(f"p3_before_fields_{short(a)}_vs_{short(b)}", a, b, variant="fields_on_A", fields_on="A",
                      outputs="standard"))
    q.append("summary_p3")

    # Phase 4: two sanity ablations (does visibility reasoning / appearance matter?)
    for v, d in {"naive_nn": {"use_visibility": False}, "no_appearance": {"use_appearance": False}}.items():
        for a, b in core[:3]:
            q.append(Spec(f"p4_{v}_{short(a)}_vs_{short(b)}", a, b, variant=v, detect=d, outputs="light"))
    q.append("summary_p4")

    # Phase 5: every remaining take combination, with heat maps (more evidence per pair type)
    done = {(a, b) for a, b in core + light}
    combos = ([(x, y) for x in (LB, LBb) for y in (LA, LAb)] + [(x, y) for x in (DB, DBb, DBc) for y in (LA, LAb)]
              + [(DB, DBc), (DBb, DBc)] + [(x, y) for x in (DB, DBb, DBc) for y in (LB, LBb)] + [(LA, LAb)])
    combos = [c for c in combos if c not in done]
    for a, b in combos:
        q.append(Spec(f"p5_{short(a)}_vs_{short(b)}", a, b, outputs="standard"))
    q.append(Spec(f"p5_reversed_{short(LA)}_vs_{short(LB)}", LA, LB, variant="reversed", outputs="standard"))
    q.append("summary_p5")

    # Phase 6: how few frames still give usable heat maps (GPU)
    for n in (16, 24, 44):
        for a, b in core[:2] + [core[3]]:
            q.append(Spec(f"p6_n{n}_{short(a)}_vs_{short(b)}", a, b, n_per_video=n, variant=f"n{n}",
                          outputs="standard"))
    q.append("summary_p6")

    # Phase 7: repeats with different frame picks: how stable are detections and heat maps?
    for off in range(1, 10):
        for a, b in core + (combos if off >= 3 else []):
            q.append(Spec(f"p7_off{off}_{short(a)}_vs_{short(b)}", a, b, offset=off, variant=f"offset{off}",
                          outputs="light"))
        q.append(f"summary_p7_off{off}")
    return q


# ---------------------------------------------------------------------------------------
def _recon(spec: Spec, cache: Path):
    from ..recon.frames import Recon
    from ..recon.vggt import reconstruct

    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"recon_{spec.a}_{spec.b}_n{spec.n_per_video}_o{spec.offset}.npz"
    info_f = f.with_suffix(".json")
    if f.exists() and info_f.exists():
        try:
            log.info("reusing cached reconstruction %s", f.name)
            return Recon.load(f), json.loads(info_f.read_text()) | {"cached": True}
        except Exception as e:  # noqa: BLE001 - a broken cache file is rebuilt
            log.warning("cache %s unreadable (%s); rebuilding", f.name, e)
    rec, info = reconstruct(spec.a, spec.b, spec.n_per_video, spec.offset)
    tmp = f.with_name(f.stem + "_tmp.npz")
    rec.save(tmp)
    tmp.replace(f)                     # never leave a half-written cache file
    info_f.write_text(json.dumps(info))
    return rec, info


def trim_cache(cache: Path, max_gb: float = 6.0) -> None:
    """Delete the least recently used cached reconstructions once the cache passes max_gb."""
    if not cache.exists():
        return
    files = sorted((f for f in cache.iterdir() if f.is_file()), key=lambda f: f.stat().st_atime)
    total = sum(f.stat().st_size for f in files)
    while files and total > max_gb * 1e9:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def _feats(rec, key: str, cache: Path):
    from ..change.features import dino_features

    f = cache / f"dino_{key}.npy"
    if f.exists():
        try:
            return np.load(f)
        except Exception:  # noqa: BLE001
            pass
    feats = dino_features(rec.images)
    np.save(f.with_name(f.stem + "_tmp.npy"), feats)
    f.with_name(f.stem + "_tmp.npy").replace(f)
    return feats


class _Stage:
    """Logs each stage with its duration and machine usage, and runs the guard check (RAM,
    disk) between stages so a step stops cleanly before Colab would kill the kernel."""

    def __init__(self, name: str, check=None):
        self.name, self.check, self.t = name, check, time.time()

    def __call__(self, what: str) -> None:
        import gc

        gc.collect()
        u = self.check() if self.check else None
        log.info("[%s] %s done in %.0f s%s", self.name, what, time.time() - self.t, f" | {u.line()}" if u else "")
        self.t = time.time()


def step_fn(spec: Spec, cache: Path, store=None, check=None):
    """The function `Run.step` calls: runs `analyse` with a per-step log file (step_log.txt in
    the results folder, pushed with them, also when the step fails)."""
    def fn(out: Path, work: Path) -> str:
        h = logging.FileHandler(out / "step_log.txt")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(h)
        if logging.getLogger("uncertainscene").getEffectiveLevel() > logging.INFO:
            logging.getLogger("uncertainscene").setLevel(logging.INFO)
        try:
            log.info("step %s: %s vs %s, %d frames/video, variant %s, outputs %s", spec.name, spec.a, spec.b,
                     spec.n_per_video, spec.variant, spec.outputs)
            return analyse(spec, out, cache, store, check)
        except Exception:
            log.exception("step %s failed", spec.name)
            raise
        finally:
            root.removeHandler(h)
            h.close()
    return fn


def enforce_size(out: Path, max_mb: float = 4.8) -> list[str]:
    """Last line of defence for the 5 MB git limit: drop any oversized file and say so."""
    dropped = []
    for f in out.rglob("*"):
        if f.is_file() and f.stat().st_size > max_mb * 1024**2:
            log.warning("dropping %s (%.1f MB > %.1f MB git limit)", f.name, f.stat().st_size / 1024**2, max_mb)
            dropped.append(f.name)
            f.unlink()
    if dropped:
        (out / "dropped_files.txt").write_text("Too big for git, not saved:\n" + "\n".join(dropped) + "\n")
    return dropped


def analyse(spec: Spec, out: Path, cache: Path, store=None, check=None) -> str:
    """One step. Returns a one-line note for the run state. `check` is the guard check
    (Run.guard.check), called between stages."""
    from ..change import scene_report
    from ..change.describe import label_objects
    from ..change.fields import RobotModel, base_placement, compute_fields, field_stats, object_fields
    from ..viz import plots, viewer

    t0 = time.time()
    stage = _Stage(spec.name, check)
    stage("start")
    rec, rinfo = _recon(spec, cache)
    stage(f"reconstruction ({len(rec.images)} frames)")
    feats = _feats(rec, f"{spec.a}_{spec.b}_n{spec.n_per_video}_o{spec.offset}", cache)
    stage("DINOv2 features")
    cfg = replace(DetectConfig(), **spec.detect)
    if spec.outputs == "light":
        cfg = replace(cfg, stride=max(cfg.stride, 3))
    res = detect(rec, feats, cfg)
    stage(f"change detection ({len(res.A.X) + len(res.B.X)} points, tau {res.thresholds['tau_rel_depth']:.3f})")
    objs = find_objects(res)
    stage(f"objects ({len(objs)} clusters)")

    # heat maps (metric scale from the floor) before labelling, so descriptions use centimetres
    f = fstats = None
    bm = {}
    unit, scale = "scene units", 1.0
    try:
        f = compute_fields(res, rec, replace(RobotModel(), **spec.robot), which=spec.fields_on,
                           voxel_m=spec.voxel_m if spec.outputs != "light" else max(spec.voxel_m, 0.015))
        fstats = field_stats(f)
        bm = base_placement(f) if spec.outputs != "light" else {}
        if bm:
            fstats.update({"base_standable_frac": round(bm["standable_frac"], 3),
                           "base_best_score": None if bm["best_score"] is None else round(bm["best_score"], 3),
                           "base_best_score_changed": None if bm["best_score_changed"] is None
                           else round(bm["best_score_changed"], 3),
                           "base_best_from_operator_m": None if bm["best_m"] is None
                           else round(float(np.linalg.norm(bm["best_m"] - bm["base_now_m"])), 2)})
        object_fields(objs, res, f)
        unit, scale = "cm", f["floor"]["scale_m_per_unit"] * 100
        stage(f"heat maps ({len(f['X'])} points, scale {scale / 100:.3f} m/unit)")
    except Exception as e:  # noqa: BLE001 - a failed floor fit must not lose the change results
        if type(e).__name__ == "ResourceLimit":
            raise
        log.exception("heat maps failed; continuing without them")
        (out / "fields_error.txt").write_text(f"{type(e).__name__}: {e}\n")
    label_objects(objs, res, rec, crop_dir=out / "crops" if spec.outputs != "light" else None, unit=unit, scale=scale)
    changes = summarise(objs)
    stage(f"labels ({', '.join(str(c.get('label')) for c in changes[:6])})")
    by_id = {o.id: o for o in objs}
    for c in changes:
        if "metric" in by_id[c["id"]].extra:
            c["metric"] = by_id[c["id"]].extra["metric"]

    ptype = pair_type(spec.a, spec.b)
    m = {"pair_type": ptype, "a": spec.a, "b": spec.b, "pair": f"{short(spec.a)}_vs_{short(spec.b)}",
         "n_per_video": spec.n_per_video, "offset": spec.offset, "variant": spec.variant,
         "detect_config": asdict(cfg), "robot": f["robot"] if f else None, "fields_on": spec.fields_on,
         "recon_seconds": rinfo.get("seconds"), "gpu_peak_gb": rinfo.get("gpu_peak_gb"),
         "recon_cached": rinfo.get("cached", False), "image_size_hw": rinfo.get("size_hw")}
    m.update(M.label_free(res, objs))
    if rinfo.get("used_per_video") is not None:
        m["used_per_video"], m["oom_retries"] = rinfo["used_per_video"], rinfo.get("oom_retries", 0)
    for side, ps in (("A", res.A), ("B", res.B)):   # reconstruction noise per recording (lighting report)
        u = ps.unc[np.isfinite(ps.unc)] * (scale if unit == "cm" else 1.0)
        m[f"unc_{'cm' if unit == 'cm' else 'units'}_{side}_median"] = round(float(np.median(u)), 3) if len(u) else None
    if fstats:
        m.update({f"field_{k}": v for k, v in fstats.items()})
    gt = M.load_ground_truth("scene01")
    if gt and ptype in ("change", "change+lighting"):
        s = M.score(changes, gt, scale_m=f["floor"]["scale_m_per_unit"] if f else None)
        m.update({f"gt_{k}": v for k, v in s.items()})
    m["step_seconds"] = round(time.time() - t0, 1)
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float) + "\n")
    (out / "changes.json").write_text(json.dumps(changes, indent=2, default=float) + "\n")

    title = f"{spec.name}: {spec.a} vs {spec.b} ({ptype}, {spec.n_per_video} frames/video, {spec.variant})"
    plots.change_map(res, rec, objs, out / "change_map.png", title)
    if f is not None:
        plots.field_maps(f, rec, out / "heatmaps.png", title)
        plots.base_map(bm, out / "base_placement.png", title)
    if spec.outputs in ("full", "standard"):
        plots.calibration(res, out / "calibration.png")
        plots.frame_overlays(res, rec, objs, out / "overlays.png")
        plots.crops_sheet(out / "crops", objs, out / "crops.png")
        viewer.write_html(res, objs, out / "viewer.html", title, fields=f)
        scene_report.write(out / "scene_report.md", spec.name, spec.a, spec.b, m, objs, f, fstats,
                           lighting_note(spec.a, spec.b))
    if spec.outputs == "full":
        viewer.write_ply(res, out / "cloud.ply")
        if f is not None:
            viewer.write_fields_ply(f, out / "fields.ply")
        if store is not None:   # dense full-resolution cloud to the release, best effort
            try:
                full = cache / f"{spec.name}_dense.ply"
                viewer.write_ply(res, full, max_points=10**8)
                entry = store.put(full, f"run-{out.parent.name}", name=full.name, overwrite=True)
                (out / "artifacts.json").write_text(json.dumps([entry], indent=2) + "\n")
            except Exception as e:  # noqa: BLE001 - optional extra
                (out / "artifacts.json").write_text(json.dumps({"error": str(e)[:300]}) + "\n")
    stage("figures, viewer and reports")
    write_report(spec, m, changes, out)
    enforce_size(out)
    extra = f", interactable {fstats['interactable_frac']:.0%}" if fstats else ""
    return (f"{ptype}: {m['changed_frac'] * 100:.2f}% changed, {m['n_changes']} changes "
            f"({m['n_moved']} moved){extra}, {m['step_seconds']:.0f}s")


def lighting_note(a: str, b: str) -> str:
    la, lb = REC[a][0], REC[b][0]
    return f"{la} in both" if la == lb else f"{la} (A) vs {lb} (B)"


def write_report(spec: Spec, m: dict, changes: list[dict], out: Path) -> None:
    lines = [f"# {spec.name}", "",
             f"**{spec.a}** (A, before) vs **{spec.b}** (B, after) - pair type **{m['pair_type']}**, "
             f"{spec.n_per_video} frames per video, variant `{spec.variant}`.", "",
             ("Distances are in cm, from a floor fit and an assumed phone height "
              f"(scale {m['field_scale_m_per_unit']} m/unit, range {m['field_scale_range']})."
              if "field_scale_m_per_unit" in m else "Distances are in scene units (floor fit failed)."), "",
             "| Measure | Value |", "|---|---|",
             f"| Observed share (A / B) | {m['A_observed_frac']:.1%} / {m['B_observed_frac']:.1%} |",
             f"| Changed share of observed points (A / B) | {m['A_changed_frac']:.2%} / {m['B_changed_frac']:.2%} |",
             f"| Alignment residual across / within recordings | {m['alignment_residual_cross']} / {m['alignment_residual_within']} |",
             f"| Thresholds (depth tau, see-through fraction, appearance) | {m['thr_tau_rel_depth']} / {m['thr_gone_frac']} / {m['thr_appearance']} |",
             f"| Changes (moved / removed / added / appearance) | {m['n_moved']} / {m['n_removed']} / {m['n_added']} / {m['n_appearance']} |",
             f"| Reconstruction time | {m['recon_seconds']} s (GPU peak {m['gpu_peak_gb']} GB) |", ""]
    if m["pair_type"] in ("static", "lighting"):
        lines += [f"Nothing was moved between these recordings, so every flagged point is a false change: "
                  f"**false-change rate {m['changed_frac']:.2%}**.", ""]
    if "field_reachable_frac" in m:
        lines += [f"Heat maps (recording {m['fields_on']}): reachable {m['field_reachable_frac']:.0%}, approachable "
                  f"{m['field_approachable_frac']:.0%}, interactable {m['field_interactable_frac']:.0%}; surface "
                  f"uncertainty median {m['field_uncertainty_cm_median']} cm (90th pct {m['field_uncertainty_cm_p90']}); "
                  f"surface shift 99th pct {m['field_shift_cm_p99_abs']} cm.", ""]
    lines += ["![change map](change_map.png)", ""]
    if (out / "heatmaps.png").exists():
        lines += ["![heat maps](heatmaps.png)", ""]
    if (out / "base_placement.png").exists():
        lines += ["![where the robot could stand](base_placement.png)", ""]
    if (out / "scene_report.md").exists():
        lines += ["Scene report with simulator randomisation ranges: [scene_report.md](scene_report.md)", ""]
    if (out / "overlays.png").exists():
        lines += ["![overlays](overlays.png)", ""]
    if (out / "crops.png").exists():
        lines += ["![crops](crops.png)", ""]
    if (out / "calibration.png").exists():
        lines += ["![calibration](calibration.png)", ""]
    lines += ["## Changes", ""]
    for c in changes:
        ci = f", 90% interval {c['distance_ci'][0]:.2f}-{c['distance_ci'][1]:.2f}" if c.get("distance_ci") else ""
        lines.append(f"- **{c['id']} {c['kind']}** {c.get('label')} (p={c.get('label_prob')}, {c['n_points']} pts, "
                     f"centroid +/- {c['centroid_std']:.3f}{ci}): {c.get('description')}")
    if not changes:
        lines.append("- none detected")
    if (out / "viewer.html").exists():
        lines += ["", "Interactive 3D: download `viewer.html` and open it in a browser. "
                  "Dense cloud: `cloud.ply` (colour by the `change` field); heat maps: `fields.ply`."]
    if "gt_recall" in m:
        lines += ["", f"Ground truth: recall {m['gt_recall']}, precision {m['gt_precision']}."]
    (out / "report.md").write_text("\n".join(lines) + "\n")
