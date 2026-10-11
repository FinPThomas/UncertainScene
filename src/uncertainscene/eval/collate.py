"""One results file across runs: results/change/RESULTS.md.

    python -m uncertainscene.eval.collate results/change

Keeps the hand-written block between `<!-- notes -->` and `<!-- /notes -->` and regenerates
everything else from each run's metrics.json / changes.json / objects.json.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np

from .summarize import TYPE_ORDER, auroc

NOTES_RE = re.compile(r"<!-- notes -->.*?<!-- /notes -->", re.S)


def _rows(run: Path) -> list[dict]:
    rows = []
    for f in sorted(run.glob("*/metrics.json")):
        m = json.loads(f.read_text())
        m["step"] = f.parent.name
        for name in ("changes.json", "objects.json"):
            p = f.parent / name
            m["_" + name.split(".")[0]] = json.loads(p.read_text()) if p.exists() else []
        rows.append(m)
    return rows


def _fmt(v, d=2, pct=False):
    if v is None:
        return "-"
    return f"{v * 100:.{d}f}%" if pct else f"{v:.{d}f}"


GOOD = {"static": "no changed objects", "lighting": "no changed objects",
        "change": "the real changes found, nothing else", "change+lighting": "the real changes found, nothing else"}


def how_to_read() -> list[str]:
    from ..experiments.catalog import KIND, REC, rec_name

    L = ["## How to read this", "",
         "Every run compares two recordings of the sofa scene (or, in *Where objects are*, several clips). What a "
         "run can tell us depends on whether anything really moved between its recordings:", "",
         "| Kind of run | What it compares | Question it answers | A good result |", "|---|---|---|---|"]
    what = {"static": "two takes of the same scene, same lighting", "lighting": "the same scene, light vs dark",
            "change": "before vs after objects were moved, same lighting",
            "change+lighting": "before vs after, and the lighting changed too"}
    for k, (t, q, _) in KIND.items():
        L.append(f"| **{t}** | {what[k]} | {q} | {GOOD[k]} |")
    L += ["", "Recordings: " + "; ".join(f"`{lab.replace('scene01_', '')}` = {rec_name(lab)}" for lab in REC) + ".",
          "", "Runs are grouped by phase (what they try to find out). Folder names say what they compare, e.g. "
          "`1-main_moved_light-before-a_vs_light-after-a`. The first overnight run used older short names "
          "(`p1_lightBa_vs_lightAa` = light before a vs light after a); its titles below are written out.", ""]
    return L


def _figs(run: Path, rel: str, step: str) -> str:
    names = ("report.md", "splat_compare.png", "splat_orbit.gif", "objects.png", "position_map.png", "heatmaps.png", "overlays.png", "object_overlays.png",
             "scene_report.md")
    return " · ".join(f"[{n.split('.')[0].replace('_', ' ')}]({rel}/{step}/{n})" for n in names
                      if (run / step / n).exists())


# runs whose object-level results must not be read (shown, but marked, never silently dropped)
INVALID_OBJECTS = {
    "2026-10-10_scene01-objects": "CLIP labelling crashed in every step and views of an object were never merged, so "
                                  "changed objects (all 0) and position ranges are not real results. Surface shares, "
                                  "scale, heat maps and the Gaussian splats are unaffected. Fixed for the next run.",
}


def run_section(run: Path, rel: str) -> list[str]:
    from ..experiments.catalog import describe

    rows = _rows(run)
    if not rows:
        return []
    v2 = any("obj_changed" in r or r.get("step_kind") == "clips" for r in rows)
    pairs = [r for r in rows if r.get("step_kind") not in ("clips", "splat") and r.get("pair_type")]
    base = [r for r in pairs if r.get("variant", "base") == "base"]
    L = [f"## Run `{run.name}` ({'version 2: SAM 2 objects, every frame' if v2 else 'version 1, first overnight run: point-level, 28 frames per recording'})",
         "", f"{len(rows)} finished runs. Folder: [{rel}]({rel}/).", ""]
    bad = INVALID_OBJECTS.get(run.name)
    if bad:
        L += [f"> **Object results of this run are invalid.** {bad}", ""]
    # headline across all pairs of each kind
    L += ["### Headline: does it tell real changes from noise?", ""]
    if bad:
        L += ["Changed objects: invalid in this run (see the note above).", ""]
    elif v2 and any("obj_changed" in r for r in base):
        L += ["| Kind of run | Runs | Changed objects, median (range) | Share of compared objects |", "|---|---|---|---|"]
        for t in TYPE_ORDER:
            rs = [r for r in base if r.get("pair_type") == t and "obj_changed" in r]
            if rs:
                n = [r["obj_changed"] for r in rs]
                L.append(f"| {KIND_TITLE[t]} | {len(rs)} | "
                         f"{np.median(n):.0f} ({min(n)}-{max(n)}) | {np.median([r['obj_changed_frac'] for r in rs]):.0%} |")
        a = auroc([r["obj_changed"] for r in base if str(r.get("pair_type")).startswith("change") and "obj_changed" in r],
                  [r["obj_changed"] for r in base if r.get("pair_type") in ("static", "lighting") and "obj_changed" in r])
        if a is not None:
            L += ["", f"**Separation of runs with real changes from runs without: AUROC {a:.2f}** (0.5 = chance, 1 = perfect).", ""]
    L += ["| Kind of run | Runs | Share of the surface flagged as changed, median (range) |", "|---|---|---|"]
    for t in TYPE_ORDER:
        xs = [r["changed_frac"] for r in base if r.get("pair_type") == t and r.get("changed_frac") is not None]
        if xs:
            L.append(f"| {KIND_TITLE[t]} | {len(xs)} | {_fmt(np.median(xs), 2, True)} ({_fmt(min(xs), 2, True)} to "
                     f"{_fmt(max(xs), 2, True)}) |")
    if not v2:
        L += ["", "Runs with real changes flag about as much surface as runs without, so version 1 could not tell "
              "real changes from noise.", ""]
    # per phase
    phases: dict = {}
    for r in rows:
        d = describe(r["step"], r)
        phases.setdefault((d["phase"], d["phase_title"], d["phase_question"]), []).append((r, d))
    for (ph, title, question), items in phases.items():
        if ph == "summary":
            continue
        L += ["", f"### {title}", "", f"*{question}* ({len(items)} runs)", ""]
        if len(items) > 24:
            by = {}
            for r, d in items:
                by.setdefault(r.get("pair_type"), []).append(r)
            L += ["Too many runs to list; by kind:", "", "| Kind of run | Runs | Changed surface, median |", "|---|---|---|"]
            for t, rs in by.items():
                xs = [r.get("changed_frac") for r in rs if r.get("changed_frac") is not None]
                L.append(f"| {KIND_TITLE.get(t, t)} | {len(rs)} | {_fmt(np.median(xs), 2, True) if xs else '-'} |")
            continue
        if any(r.get("step_kind") == "splat" for r, _ in items):
            L += ["| Run | Render quality, held-out frames (PSNR before / after) | Changed pixels, same camera (1% = no change) | Depth differs > 5 cm | Details |",
                  "|---|---|---|---|---|"]
            for r, d in items:
                L.append(f"| {d['title']} | {r.get('splat_psnr_before')} / {r.get('splat_psnr_after')} dB | "
                         f"{_fmt(r.get('same_view_changed_share'), 1, True)} | {_fmt(r.get('depth_changed_share_5cm'), 1, True)} | "
                         f"{_figs(run, rel, r['step'])} |")
            continue
        if any(r.get("step_kind") == "clips" for r, _ in items):
            L += ["| Run | Objects matched | 90% range with 1 / 2 / 3 clips | Consistency (3 = honest) | Smallest detectable move | Details |",
                  "|---|---|---|---|---|---|"]
            for r, d in items:
                c = r.get("pos_clips_curve", {})
                mv = r.get("pos_movement", {})
                L.append(f"| {d['title']} | {r.get('matched_objects')} | "
                         f"{' / '.join('±%.1f cm' % v['median_radius90_cm'] for v in c.values()) or '-'} | "
                         f"{r.get('pos_nis_mean', '-')} | "
                         f"{' / '.join('%.1f cm (%s per state)' % (v['min_detectable_move_cm'], k) for k, v in mv.items()) or '-'} | "
                         f"{_figs(run, rel, r['step'])} |")
            continue
        has_pos = any(r.get("pos_radius90_cm_median") for r, _ in items)
        L += ["| Run | Changes found |" + (" Objects located to (90%) |" if has_pos else "")
              + " Scale (m/unit) | Surface uncertainty (cm) | Details |",
              "|---|---|" + ("---|" if has_pos else "") + "---|---|---|"]
        for r, d in items:
            if r["_objects"] and "obj_changed" in r:
                ch = [f"{o['label']} {o['status']}" for o in r["_changes"]][:6]
            else:
                ch = [f"{c.get('label')} {c.get('kind')}" for c in r["_changes"]][:6]
            pos = r.get("pos_radius90_cm_median")
            L.append(f"| {d['title']} | {'invalid' if bad else ', '.join(ch) or 'none'} |"
                     + (f" {'invalid' if bad else '±%.1f cm' % pos if pos else '-'} |" if has_pos else "")
                     + " "
                     f"{_fmt(r.get('field_scale_m_per_unit'), 3)} | {_fmt(r.get('field_uncertainty_cm_median'), 1)} | "
                     f"{_figs(run, rel, r['step'])} |")
    light = run / next((d.name for d in sorted(run.iterdir(), reverse=True)
                        if d.name.startswith("summary") and (d / "lighting.md").exists()), "none")
    if (light / "lighting.md").exists():
        tab = [ln for ln in (light / "lighting.md").read_text().splitlines() if ln.startswith("|")]
        L += ["", "### Light vs dark (all runs)", ""] + tab[:7] + ["", f"Full lighting report: [{rel}/{light.name}/lighting.md]({rel}/{light.name}/lighting.md)"]
    return L


KIND_TITLE = {"static": "Nothing moved, same lighting", "lighting": "Nothing moved, light vs dark",
              "change": "Objects moved, same lighting", "change+lighting": "Objects moved and lighting changed"}


def write(root: Path, viewer_url: str | None = None) -> Path:
    root = Path(root)
    out = root / "RESULTS.md"
    old = out.read_text() if out.exists() else ""
    m = NOTES_RE.search(old)
    notes = m.group(0) if m else "<!-- notes -->\n## Findings so far\n\n(none yet)\n<!-- /notes -->"
    L = ["# UncertainScene results: scene01", "",
         "One file for every run so far. *Findings so far* is written by hand; everything after it is regenerated "
         "from the run folders (`python -m uncertainscene.eval.collate results/change`).", ""]
    if viewer_url:
        L += [f"**Interactive viewer:** {viewer_url} (pick a run; view one recording alone or the changes between "
              "them; heat maps, object boxes and position ranges).", ""]
    L += [notes, ""] + how_to_read()
    runs = sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*/metrics.json"))), reverse=True)
    for r in runs:
        L += run_section(r, r.name) + [""]
    out.write_text("\n".join(L) + "\n")
    return out


if __name__ == "__main__":
    print(write(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None))
