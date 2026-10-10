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
MAIN = ("p1_",)


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


def run_section(run: Path, rel: str) -> list[str]:
    rows = _rows(run)
    if not rows:
        return []
    v2 = any("obj_changed" in r for r in rows)
    base = [r for r in rows if r.get("variant", "base") == "base"]
    L = [f"## Run `{run.name}` ({'version 2: SAM 2 objects, all frames' if v2 else 'version 1: point-level, 28 frames per recording'})",
         "", f"{len(rows)} finished steps. Folder: [{rel}]({rel}/).", ""]
    if v2:
        L += ["### Changed objects by pair type", "",
              "| Pair type | Pairs | Changed objects, median (range) | Share of compared objects |", "|---|---|---|---|"]
        for t in TYPE_ORDER:
            rs = [r for r in base if r.get("pair_type") == t]
            if rs:
                n = [r["obj_changed"] for r in rs]
                L.append(f"| {t} | {len(rs)} | {np.median(n):.0f} ({min(n)}-{max(n)}) | "
                         f"{np.median([r['obj_changed_frac'] for r in rs]):.0%} |")
        a = auroc([r["obj_changed"] for r in base if str(r.get("pair_type")).startswith("change")],
                  [r["obj_changed"] for r in base if r.get("pair_type") in ("static", "lighting")])
        if a is not None:
            L += ["", f"**Separation of real-change pairs from no-change pairs: AUROC {a:.2f}** (0.5 = chance, 1 = perfect).", ""]
    L += ["### Point-level changed share by pair type", "", "| Pair type | Pairs | Median | Range |", "|---|---|---|---|"]
    for t in TYPE_ORDER:
        xs = [r["changed_frac"] for r in base if r.get("pair_type") == t]
        if xs:
            L.append(f"| {t} | {len(xs)} | {_fmt(np.median(xs), 2, True)} | {_fmt(min(xs), 2, True)} to {_fmt(max(xs), 2, True)} |")
    L += ["", "### Main pairs", "",
          "| Pair | Type | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Reachable | Figures |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r["step"].startswith(MAIN):
            continue
        if v2 and r["_objects"]:
            ch = [f"{o['label']} {o['status']}" for o in r["_changes"]][:6]
        else:
            ch = [f"{c.get('label')} {c.get('kind')}" for c in r["_changes"]][:6]
        figs = " · ".join(f"[{n.split('.')[0]}]({rel}/{r['step']}/{n})" for n in
                          ("objects.png", "heatmaps.png", "overlays.png", "object_overlays.png", "scene_report.md")
                          if (run / r["step"] / n).exists())
        L.append(f"| {r['step']} | {r.get('pair_type')} | {', '.join(ch) or 'none'} | {_fmt(r.get('field_scale_m_per_unit'), 3)} | "
                 f"{_fmt(r.get('field_uncertainty_cm_median'), 1)} | {_fmt(r.get('field_reachable_frac'), 0, True)} | {figs} |")
    light = run / next((d.name for d in sorted(run.iterdir(), reverse=True)
                        if d.name.startswith("summary_") and (d / "lighting.md").exists()), "none")
    if (light / "lighting.md").exists():
        txt = (light / "lighting.md").read_text()
        tab = [ln for ln in txt.splitlines() if ln.startswith("|")]
        L += ["", "### Light vs dark", ""] + tab[:7] + ["", f"Full lighting report: [{rel}/{light.name}/lighting.md]({rel}/{light.name}/lighting.md)"]
    ex = next((r for r in rows if r["step"].startswith("p1_") and r.get("pair_type") == "change"), None)
    if ex:
        L += ["", f"### Example: {ex['step']}", ""]
        for n, cap in (("objects.png", "every object"), ("heatmaps.png", "heat maps"), ("overlays.png", "changes in the frames"),
                       ("object_overlays.png", "object boundaries")):
            if (run / ex["step"] / n).exists():
                L += [f"![{cap}]({rel}/{ex['step']}/{n})", ""]
    return L


def write(root: Path, viewer_url: str | None = None) -> Path:
    root = Path(root)
    out = root / "RESULTS.md"
    old = out.read_text() if out.exists() else ""
    m = NOTES_RE.search(old)
    notes = m.group(0) if m else "<!-- notes -->\n## Findings so far\n\n(none yet)\n<!-- /notes -->"
    L = ["# UncertainScene results: scene01", "",
         "One file for every run so far. The block under *Findings so far* is written by hand; everything below it "
         "is regenerated from the run folders (`python -m uncertainscene.eval.collate results/change`).", ""]
    if viewer_url:
        L += [f"**Interactive viewer** (3D scene, heat maps, object boundaries, per-pair findings): {viewer_url}", ""]
    L += [notes, ""]
    runs = sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*/metrics.json"))), reverse=True)
    for r in runs:
        L += run_section(r, r.name) + [""]
    out.write_text("\n".join(L) + "\n")
    return out


if __name__ == "__main__":
    print(write(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None))
