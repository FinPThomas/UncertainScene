"""Collect every step's metrics.json under results/<path>/<run_id>/ into one comparison.

    python -m uncertainscene.eval.summarize results/change/<run_id>

Writes summary.md (tables grouped by pair type, the key proof-of-concept contrast, and the
detected changes per pair), summary.csv and summary.png into <run_dir>/summary/.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

TYPE_ORDER = ["static", "lighting", "change", "change+lighting"]
TYPE_COL = {"static": "#9e9e9e", "lighting": "#9ecae1", "change": "#d62728", "change+lighting": "#ff7f0e"}


def load(run_dir: Path) -> list[dict]:
    rows = []
    for f in sorted(run_dir.glob("*/metrics.json")):
        m = json.loads(f.read_text())
        m["step"] = f.parent.name
        rows.append(m)
    return rows


def write(run_dir: Path, out_dir: Path | None = None) -> Path:
    run_dir = Path(run_dir)
    out_dir = Path(out_dir or run_dir / "summary")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load(run_dir)
    if not rows:
        (out_dir / "summary.md").write_text("# Summary\n\nNo finished steps yet.\n")
        return out_dir / "summary.md"
    keys = ["step", "pair_type", "pair", "a", "b", "n_per_video", "variant", "changed_frac", "A_observed_frac",
            "B_observed_frac", "alignment_residual_cross", "alignment_residual_within", "n_changes", "n_moved",
            "n_removed", "n_added", "n_appearance", "n_background_labelled", "recon_seconds", "gpu_peak_gb",
            "field_scale_m_per_unit", "field_uncertainty_cm_median", "field_uncertainty_cm_p90",
            "field_shift_cm_p99_abs", "field_reachable_frac", "field_approachable_frac", "field_interactable_frac"]
    with open(out_dir / "summary.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # ---- figure: changed fraction by pair type (the headline proof-of-concept contrast) ----
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    main = [r for r in rows if r.get("variant", "base") == "base"]
    for t in TYPE_ORDER:
        xs = [r["changed_frac"] * 100 for r in main if r.get("pair_type") == t]
        axes[0].scatter([TYPE_ORDER.index(t)] * len(xs), xs, s=40, color=TYPE_COL[t], alpha=0.8)
        if xs:
            axes[0].hlines(np.median(xs), TYPE_ORDER.index(t) - 0.25, TYPE_ORDER.index(t) + 0.25, color="k")
    axes[0].set_xticks(range(len(TYPE_ORDER)), TYPE_ORDER)
    axes[0].set_ylabel("% of observed points flagged as changed")
    axes[0].set_title("False changes (static, lighting) vs real changes")
    variants = sorted({r.get("variant", "base") for r in rows})
    for i, v in enumerate(variants):
        for t in TYPE_ORDER:
            xs = [r["changed_frac"] * 100 for r in rows if r.get("variant", "base") == v and r.get("pair_type") == t]
            if xs:
                axes[1].scatter([i + 0.15 * (TYPE_ORDER.index(t) - 1.5)] * len(xs), xs, s=25, color=TYPE_COL[t])
    axes[1].set_xticks(range(len(variants)), variants, rotation=45, ha="right")
    axes[1].set_title("By variant (colour = pair type)")
    fig.tight_layout()
    fig.savefig(out_dir / "summary.png", dpi=100)
    plt.close(fig)

    # ---- markdown ----------------------------------------------------------------------
    lines = [f"# Summary of {run_dir.name}", "", f"{len(rows)} finished steps.", "",
             "![summary](summary.png)", "", "## Headline: changed fraction by pair type (base settings)", "",
             "| Pair type | Steps | Median % changed | Range |", "|---|---|---|---|"]
    for t in TYPE_ORDER:
        xs = [r["changed_frac"] * 100 for r in main if r.get("pair_type") == t]
        if xs:
            lines.append(f"| {t} | {len(xs)} | {np.median(xs):.2f} | {min(xs):.2f} to {max(xs):.2f} |")
    lines += ["", "Static and lighting pairs have no real change, so their numbers are the false-change "
              "rate. A working detector shows change pairs clearly above both.", "",
              "## All steps", "", "| Step | Type | A vs B | Frames/video | Variant | % changed | Align resid (x/within) "
              "| Changes (mv/rm/add/app) | Recon s |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| [{r['step']}]({r['step']}/report.md) | {r.get('pair_type')} | {r.get('a')} vs {r.get('b')} | "
                     f"{r.get('n_per_video')} | {r.get('variant')} | {r['changed_frac'] * 100:.2f} | "
                     f"{r.get('alignment_residual_cross')}/{r.get('alignment_residual_within')} | "
                     f"{r.get('n_moved')}/{r.get('n_removed')}/{r.get('n_added')}/{r.get('n_appearance')} | "
                     f"{r.get('recon_seconds')} |")
    lines += ["", "## Detected changes (base settings)", ""]
    for r in main:
        ch = json.loads((run_dir / r["step"] / "changes.json").read_text()) if (run_dir / r["step"] / "changes.json").exists() else []
        if not ch:
            continue
        lines.append(f"**{r['step']}** ({r.get('pair_type')})")
        for c in ch[:8]:
            lines.append(f"- {c.get('description')}")
        lines.append("")
    fr = [r for r in rows if "field_reachable_frac" in r]
    if fr:
        lines += ["## Heat maps", "", "Each step's `heatmaps.png` shows the maps; `viewer.html` has them as 3D layers.", "",
                  "| Step | Variant | Robot (reach m / mount m / base offset m) | Scale m/unit | Uncertainty cm (median / p90) "
                  "| Shift cm p99 | Reachable | Approachable | Interactable |", "|---|---|---|---|---|---|---|---|---|"]
        for r in fr:
            rb = r.get("robot") or {}
            lines.append(f"| [{r['step']}]({r['step']}/heatmaps.png) | {r.get('variant')} | {rb.get('reach_m')} / "
                         f"{rb.get('shoulder_height_m')} / {rb.get('base_offset_m')} | {r['field_scale_m_per_unit']} | "
                         f"{r['field_uncertainty_cm_median']} / {r['field_uncertainty_cm_p90']} | "
                         f"{r['field_shift_cm_p99_abs']} | {r['field_reachable_frac']:.0%} | "
                         f"{r['field_approachable_frac']:.0%} | {r['field_interactable_frac']:.0%} |")
        lines.append("")
    groups: dict = {}
    for r in rows:
        if r.get("variant", "").startswith("offset") or r.get("variant") == "base":
            groups.setdefault(r.get("pair"), []).append(r)
    stab = {k: v for k, v in groups.items() if len(v) >= 3}
    if stab:
        lines += ["## Stability across different frame picks", "",
                  "Same pair, different frames chosen from the videos. Small spread = the result is a property of "
                  "the scene, not of which frames were used.", "",
                  "| Pair | Repeats | % changed (mean +/- sd) | Changes (mean +/- sd) | Interactable (mean +/- sd) |",
                  "|---|---|---|---|---|"]
        for k, v in stab.items():
            cf = np.array([r["changed_frac"] * 100 for r in v])
            nc = np.array([r["n_changes"] for r in v])
            it = np.array([r["field_interactable_frac"] * 100 for r in v if "field_interactable_frac" in r])
            its = f"{it.mean():.0f}% +/- {it.std():.0f}" if len(it) else "-"
            lines.append(f"| {k} | {len(v)} | {cf.mean():.2f} +/- {cf.std():.2f} | {nc.mean():.1f} +/- {nc.std():.1f} | {its} |")
        lines.append("")
    if any("gt_recall" in r for r in rows):
        lines += ["## Ground-truth scores", "", "| Step | Recall | Precision |", "|---|---|---|"]
        for r in rows:
            if "gt_recall" in r:
                lines.append(f"| {r['step']} | {r['gt_recall']} | {r['gt_precision']} |")
    from . import lighting

    lighting.write(run_dir, out_dir)
    lines[3:3] = ["Lighting (dark vs light) comparison: [lighting.md](lighting.md)", ""]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return out_dir / "summary.md"


if __name__ == "__main__":
    print(write(Path(sys.argv[1])))
