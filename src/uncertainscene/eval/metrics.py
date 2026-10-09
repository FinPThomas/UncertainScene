"""Evaluation of one run.

Label-free measures (always available):
- observed_frac: share of points seen well enough by the other recording to be judged;
- changed_frac: share of observed points flagged as changed (geometry or appearance).
  On a nothing-changed pair (`pair00_static_*`) this IS the false-change rate; on a
  lighting-only pair it is the false-change rate under lighting change;
- alignment_residual: median |relative depth residual| between recordings on unchanged
  points (how well the two recordings were put in one frame), vs the same within a recording;
- n_objects per kind, and how many were labelled as background (likely false positives).

Ground-truth scoring (only when data/ground_truth/<scene>.json exists; used to score, never to
fit anything): object-level precision / recall by change type with tolerant label matching,
and displacement error when the ground truth gives distances and a metric scale is known.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..change.detect import APPEARANCE, GONE, UNCHANGED, UNKNOWN, ChangeResult
from ..infra.budget import REPO_ROOT

SYNONYMS = {
    "cushion": {"cushion", "pillow", "throw"}, "pillow": {"cushion", "pillow"},
    "blanket": {"blanket", "throw", "towel"}, "book": {"book", "stack of books"},
    "books": {"book", "stack of books"}, "vase": {"vase", "glass vase", "bottle"},
    "mug": {"mug", "cup"}, "cup": {"mug", "cup"}, "cap": {"baseball cap", "hat"}, "hat": {"baseball cap", "hat"},
    "bat": {"table tennis bat"}, "coaster": {"coaster"}, "guitar": {"guitar"},
}


def label_free(res: ChangeResult, objs: list) -> dict:
    out = {}
    for name, ps in (("A", res.A), ("B", res.B)):
        n = len(ps.cls)
        obs = ps.cls != UNKNOWN
        out[f"{name}_points"] = int(n)
        out[f"{name}_observed_frac"] = round(float(obs.mean()), 4) if n else 0.0
        changed = (ps.cls == GONE) | (ps.cls == APPEARANCE)
        out[f"{name}_changed_frac"] = round(float(changed.sum() / max(obs.sum(), 1)), 5)
        out[f"{name}_gone_frac"] = round(float((ps.cls == GONE).sum() / max(obs.sum(), 1)), 5)
        out[f"{name}_appearance_frac"] = round(float((ps.cls == APPEARANCE).sum() / max(obs.sum(), 1)), 5)
    out["changed_frac"] = round((out["A_changed_frac"] + out["B_changed_frac"]) / 2, 5)
    r_cross = np.abs(res.null["r_cross"])
    r_in = np.abs(res.null["r_within"])
    out["alignment_residual_cross"] = round(float(np.median(r_cross[r_cross < 0.5])), 4) if len(r_cross) else None
    out["alignment_residual_within"] = round(float(np.median(r_in[r_in < 0.5])), 4) if len(r_in) else None
    kinds = [o.kind for o in objs]
    for k in ("removed", "added", "moved", "appearance"):
        out[f"n_{k}"] = kinds.count(k) if k != "moved" else kinds.count(k) // 2
    out["n_changes"] = out["n_removed"] + out["n_added"] + out["n_moved"] + out["n_appearance"]
    out["n_background_labelled"] = sum(bool(o.extra.get("is_background")) for o in objs)
    out.update({f"thr_{k}": (round(v, 5) if np.isfinite(v) else None) for k, v in res.thresholds.items()})
    return out


def load_ground_truth(scene: str) -> dict | None:
    f = REPO_ROOT / "data" / "ground_truth" / f"{scene}.json"
    return json.loads(f.read_text()) if f.exists() else None


def _match_label(pred: str | None, truth: str) -> bool:
    if not pred:
        return False
    t = truth.lower()
    ok = SYNONYMS.get(t, {t})
    return pred.lower() in ok or t in pred.lower() or pred.lower() in t


def score(changes: list[dict], gt: dict, scale_m: float | None = None) -> dict:
    """Object-level scoring. gt['changes'] items: {object, change, approx_cm?}.
    A predicted change matches a truth if labels match and the type is compatible
    (moved truth also accepts removed+added of that object, scored as half)."""
    truths = gt.get("changes", [])
    used, hits, disp_err = set(), 0.0, []
    for t in truths:
        best = None
        for i, c in enumerate(changes):
            if i in used or not _match_label(c.get("label"), t["object"]):
                continue
            if c["kind"] == t["change"] or (t["change"] == "deformed" and c["kind"] == "appearance"):
                best = (i, 1.0)
                break
            if t["change"] == "moved" and c["kind"] in ("removed", "added"):
                best = best or (i, 0.5)
        if best:
            used.add(best[0])
            hits += best[1]
            c = changes[best[0]]
            if t.get("approx_cm") and c.get("distance") and scale_m:
                disp_err.append(abs(c["distance"] * scale_m * 100 - t["approx_cm"]))
    fg = [c for c in changes if c.get("label") not in (None,)]
    return {"gt_changes": len(truths), "pred_changes": len(fg),
            "recall": round(hits / max(len(truths), 1), 3),
            "precision": round(hits / max(len(fg), 1), 3),
            "displacement_abs_err_cm": round(float(np.mean(disp_err)), 1) if disp_err else None}
