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
        if m.get("step_kind") == "clips":      # multi-clip position steps have their own report
            continue
        m["step"] = f.parent.name
        rows.append(m)
    return rows


def auroc(pos: list, neg: list) -> float | None:
    """Probability that a pair with real changes scores above a pair without (ties count half)."""
    if not pos or not neg:
        return None
    p, n = np.array(pos, float)[:, None], np.array(neg, float)[None, :]
    return float(((p > n) + 0.5 * (p == n)).mean())


def object_headline(rows: list[dict], out_dir: Path) -> list[str]:
    """Version-2 runs: changed objects per pair type, and how well that separates pairs with
    real changes from pairs without (AUROC over pairs, base settings)."""
    obj = [r for r in rows if "obj_changed" in r and r.get("variant", "base") == "base"]
    if not obj:
        return []
    L = ["## Headline: changed objects by pair type (SAM 2 objects, base settings)", "",
         "| Pair type | Pairs | Changed objects, median (range) | Share of compared objects | Moved / added / removed / in place / shifted (total) |",
         "|---|---|---|---|---|"]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for i, t in enumerate(TYPE_ORDER):
        rs = [r for r in obj if r.get("pair_type") == t]
        if not rs:
            continue
        n = [r["obj_changed"] for r in rs]
        fr = [100 * r["obj_changed_frac"] for r in rs]
        tot = [sum(r.get(f"obj_{k}", 0) for r in rs) for k in ("moved", "added", "removed", "changed_in_place", "shifted")]
        L.append(f"| {t} | {len(rs)} | {np.median(n):.0f} ({min(n)}-{max(n)}) | {np.median(fr):.0f}% | "
                 f"{' / '.join(map(str, tot))} |")
        ax.scatter(np.full(len(n), i) + np.random.default_rng(i).uniform(-0.1, 0.1, len(n)), n, s=40,
                   color=TYPE_COL[t], alpha=0.85)
        ax.hlines(np.median(n), i - 0.3, i + 0.3, color="k")
    ax.set_xticks(range(len(TYPE_ORDER)), TYPE_ORDER)
    ax.set_ylabel("changed objects per pair")
    ax.set_title("False alarms (static, lighting) vs real changes")
    fig.tight_layout()
    fig.savefig(out_dir / "objects_by_type.png", dpi=100)
    plt.close(fig)
    pos = [r["obj_changed"] for r in obj if r.get("pair_type", "").startswith("change")]
    neg = [r["obj_changed"] for r in obj if r.get("pair_type") in ("static", "lighting")]
    a = auroc(pos, neg)
    L += ["", "![changed objects by pair type](objects_by_type.png)", ""]
    if a is not None:
        L += [f"**Separation: AUROC {a:.2f}** (chance of a real-change pair having more changed objects than a "
              f"no-change pair; 1.0 = perfect, 0.5 = no better than chance; {len(pos)} vs {len(neg)} pairs).", ""]
    labs: dict = {}
    for r in obj:
        if not r.get("pair_type", "").startswith("change"):
            continue
        f = out_dir.parent / r["step"] / "changes.json"
        for c in (json.loads(f.read_text()) if f.exists() else []):
            labs[(c.get("label"), c.get("status"))] = labs.get((c.get("label"), c.get("status")), 0) + 1
    if labs:
        L += ["Most frequent changes on real-change pairs (label, status: pairs):", "",
              ", ".join(f"{k[0]} {k[1]}: {v}" for k, v in sorted(labs.items(), key=lambda kv: -kv[1])[:15]), ""]
    return L


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
    lines = [f"# Summary of {run_dir.name}", "", f"{len(rows)} finished steps.", ""]
    lines += object_headline(rows, out_dir)
    lines += ["![summary](summary.png)", "", "## Point-level changed fraction by pair type (base settings)", "",
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
