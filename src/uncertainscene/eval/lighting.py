"""How does lighting (dark vs light recordings) affect reconstruction, false changes and the
heat maps? Reads every step's metrics.json / changes.json under a run folder.

    python -m uncertainscene.eval.lighting results/change/<run_id>

Writes lighting.md and lighting.png. Groups:
- static-light, static-dark: same lighting, nothing changed (false-change floor);
- lighting: dark vs light, nothing changed (what lighting alone triggers);
- change-light: light vs light with real changes;
- change+lighting: dark before vs light after (real changes seen through a lighting change).
Change pairs are compared per after-take: do light-before and dark-before find the same changes?
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

GROUPS = ["static-light", "static-dark", "lighting", "change-light", "change+lighting"]
GCOL = {"static-light": "#bdbdbd", "static-dark": "#636363", "lighting": "#6baed6", "change-light": "#d62728",
        "change+lighting": "#ff7f0e"}


def light_of(label: str) -> str:
    return "dark" if "_dark_" in label else "light"


def group(m: dict) -> str | None:
    t, la, lb = m.get("pair_type"), light_of(m["a"]), light_of(m["b"])
    if t == "static":
        return f"static-{la}"
    if t == "change":
        return f"change-{la}"
    return t if t in ("lighting", "change+lighting") else None


MEASURES = [  # key, label, scale
    ("changed_frac", "% observed points flagged as changed", 100),
    ("appearance_frac", "% flagged as appearance change", 100),
    ("align_ratio", "alignment residual across / within recordings", 1),
    ("observed_frac", "% points seen by both recordings", 100),
    ("field_uncertainty_cm_median", "surface uncertainty, median cm", 1),
    ("n_changes", "change objects found", 1),
]
MEASURES_V2 = [("obj_changed", "changed objects (SAM 2)", 1), ("obj_changed_frac", "% of compared objects changed", 100)]


def _value(m: dict, key: str):
    if key == "appearance_frac":
        return (m.get("A_appearance_frac", 0) + m.get("B_appearance_frac", 0)) / 2
    if key == "observed_frac":
        return (m.get("A_observed_frac", 0) + m.get("B_observed_frac", 0)) / 2
    if key == "align_ratio":
        a, w = m.get("alignment_residual_cross"), m.get("alignment_residual_within")
        return a / w if a and w else None
    return m.get(key)


def load(run_dir: Path) -> list[dict]:
    rows = []
    for f in sorted(Path(run_dir).glob("*/metrics.json")):
        m = json.loads(f.read_text())
        if m.get("step_kind") in ("clips", "splat"):   # multi-clip and splat steps have their own report
            continue
        if m.get("variant", "base") not in ("base", "dense") and not str(m.get("variant", "")).startswith("offset"):
            continue          # robot scenarios and ablations would double count
        m["step"] = f.parent.name
        m["group"] = group(m)
        ch = f.parent / "changes.json"
        m["_changes"] = json.loads(ch.read_text()) if ch.exists() else []
        if m["group"]:
            rows.append(m)
    return rows


def _labels(changes: list[dict]) -> set:
    bg = {"sofa", "armchair", "rug", "floor", "wall", "door", "shelf", "power socket", "cable"}
    return {c.get("label") for c in changes if c.get("label") and c.get("label") not in bg}


def write(run_dir: Path, out_dir: Path | None = None) -> Path:
    run_dir = Path(run_dir)
    out_dir = Path(out_dir or run_dir / "summary")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load(run_dir)
    md = out_dir / "lighting.md"
    if not rows:
        md.write_text("# Lighting comparison\n\nNo finished steps yet.\n")
        return md

    global MEASURES
    if any("obj_changed" in r for r in rows):      # version 2: object-level measures first
        MEASURES = MEASURES_V2 + [x for x in MEASURES if x[0] not in ("n_changes", "appearance_frac")]
    fig, axes = plt.subplots(2, 3, figsize=(17, 9))
    for ax, (key, lab, sc) in zip(axes.ravel(), MEASURES):
        for i, g in enumerate(GROUPS):
            v = [_value(r, key) for r in rows if r["group"] == g]
            v = [x * sc for x in v if x is not None]
            if v:
                ax.scatter(np.full(len(v), i) + np.random.default_rng(i).uniform(-0.12, 0.12, len(v)), v, s=22,
                           color=GCOL[g], alpha=0.8)
                ax.hlines(np.median(v), i - 0.3, i + 0.3, color="k")
        ax.set_xticks(range(len(GROUPS)), GROUPS, rotation=25, ha="right")
        ax.set_title(lab, fontsize=10)
    fig.suptitle("Lighting: dark vs light recordings (each dot = one pair or repeat)")
    fig.tight_layout()
    fig.savefig(out_dir / "lighting.png", dpi=95)
    plt.close(fig)

    L = ["# Lighting comparison", "", "![lighting](lighting.png)", "",
         "| Group | Pairs | " + " | ".join(lab for _, lab, _ in MEASURES) + " |",
         "|---|---|" + "---|" * len(MEASURES)]
    for g in GROUPS:
        rs = [r for r in rows if r["group"] == g]
        if not rs:
            continue
        cells = []
        for key, _, sc in MEASURES:
            v = np.array([_value(r, key) for r in rs if _value(r, key) is not None], float) * sc
            cells.append(f"{np.median(v):.2f} ({v.min():.2f}-{v.max():.2f})" if len(v) else "-")
        L.append(f"| {g} | {len(rs)} | " + " | ".join(cells) + " |")
    L += ["", "Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone "
          "flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.", ""]

    # per-recording surface uncertainty: are dark recordings noisier?
    unc = {"dark": [], "light": []}
    for r in rows:
        for side in ("A", "B"):
            v = r.get(f"unc_cm_{side}_median")
            if v is not None:
                unc[light_of(r[side.lower()])].append(v)
    if unc["dark"] and unc["light"]:
        L += ["## Reconstruction noise per recording", "",
              "| Recording lighting | Samples | Surface uncertainty, median cm |", "|---|---|---|",
              f"| dark | {len(unc['dark'])} | {np.median(unc['dark']):.2f} |",
              f"| light | {len(unc['light'])} | {np.median(unc['light']):.2f} |", ""]

    # same after-take, light-before vs dark-before: do they find the same changes?
    L += ["## Same changes found through a lighting change?", "",
          "For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, "
          "first frame pick). Labels exclude background classes.", "",
          "| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | "
          "Label overlap (Jaccard) |", "|---|---|---|---|---|"]
    base = [r for r in rows if r.get("offset", 0) == 0 and r.get("variant", "base") == "base"]
    for after in sorted({r["b"] for r in base if r["group"] in ("change-light", "change+lighting")}):
        lt = [r for r in base if r["b"] == after and r["group"] == "change-light"]
        dk = [r for r in base if r["b"] == after and r["group"] == "change+lighting"]
        if not lt or not dk:
            continue
        sl = set().union(*(_labels(r["_changes"]) for r in lt))
        sd = set().union(*(_labels(r["_changes"]) for r in dk))
        jac = len(sl & sd) / len(sl | sd) if sl | sd else float("nan")
        L.append(f"| {after} | {np.mean([r['n_changes'] for r in lt]):.1f} / {', '.join(sorted(sl)) or '-'} | "
                 f"{np.mean([r['n_changes'] for r in dk]):.1f} / {', '.join(sorted(sd)) or '-'} | "
                 f"{', '.join(sorted(sl & sd)) or '-'} | {jac:.2f} |")
    L += ["", "Ablation `no_appearance` (in the main summary) shows whether appearance features are what "
          "lighting trips; geometry-only detection should be nearly lighting-invariant."]
    md.write_text("\n".join(L) + "\n")
    return md


if __name__ == "__main__":
    print(write(Path(sys.argv[1])))
