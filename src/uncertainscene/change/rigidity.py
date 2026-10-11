"""Rigidity Copilot (Fin, 2026-10-11): how stiff each object is expected to be, asked of CLIP.

Stiffness cannot be seen in two recordings without forces, so it is queried instead. CLIP
looks at the object's own masked best view (the same image embedding the labels come from) and
is asked:
1. what it is made of: zero-shot over common household materials, each with a typical
   Young's modulus range from engineering tables;
2. how it behaves: "soft, squishy, deformable" vs "hard, rigid".
The expected Young's modulus is the probability-weighted geometric mean over materials, with
a range spanning the likely materials; the rigidity score (0 soft .. 1 rigid) averages the
material-based and the soft/hard answers. Each edge of an object's node graph then gets a
spring constant k ~ E * L (a cubic lattice of node spacing L), in N/m, with its range.

It is a prior from appearance, not a measurement: the measured strain between the
recordings (assets.py) is kept beside it, and the two are compared ("agrees" / "disagrees").
"""

from __future__ import annotations

import numpy as np

# typical Young's modulus ranges (Pa) of whole household objects made of each material
MATERIALS = {
    "soft foam or stuffing": (1e4, 1e6),
    "fabric or cloth": (1e5, 1e7),
    "rubber": (1e6, 1e8),
    "leather": (1e7, 5e8),
    "paper or cardboard": (1e8, 4e9),
    "plastic": (1e9, 4e9),
    "wood": (5e9, 2e10),
    "ceramic or porcelain": (5e10, 3e11),
    "glass": (5e10, 9e10),
    "metal": (4.5e10, 2.1e11),
}
SOFT_HARD = ("a photo of a soft, squishy, deformable object", "a photo of a hard, rigid, stiff object")
LOG_LO, LOG_HI = 4.0, 11.5            # log10 Pa spanning foam .. steel, mapped to rigidity 0 .. 1


def score(p_material: np.ndarray, p_hard: float) -> dict:
    """Expected Young's modulus (Pa, geometric mean and 90% range over the materials) and the
    rigidity score from material probabilities (in MATERIALS order) and P(hard)."""
    p = np.asarray(p_material, float)
    p = p / p.sum()
    lo = np.log10([v[0] for v in MATERIALS.values()])
    hi = np.log10([v[1] for v in MATERIALS.values()])
    mid = (lo + hi) / 2
    e_log = float((p * mid).sum())
    # range: the materials holding 90% of the probability, their whole ranges
    order = np.argsort(-p)
    keep = order[: int(np.searchsorted(np.cumsum(p[order]), 0.9) + 1)]
    r_mat = float(np.clip((e_log - LOG_LO) / (LOG_HI - LOG_LO), 0, 1))
    names = list(MATERIALS)
    return {"youngs_modulus_pa": float(10**e_log),
            "youngs_modulus_range_pa": [float(10 ** min(lo[keep].min(), e_log)), float(10 ** max(hi[keep].max(), e_log))],
            "materials": [(names[i], round(float(p[i]), 3)) for i in order[:3]],
            "p_hard": round(float(p_hard), 3), "rigidity_from_material": round(r_mat, 3),
            "rigidity": round(0.5 * r_mat + 0.5 * float(p_hard), 3)}


def copilot(objs: list, model_name: str = "openai/clip-vit-large-patch14") -> int:
    """Sets o.extra["rigidity"] for every object with a CLIP image embedding (o.extra["clip"],
    from inventory.label_all). Returns how many objects were scored."""
    from .features import clip_text

    have = [o for o in objs if o.extra.get("clip") is not None]
    if not have:
        return 0
    tm = clip_text([f"a photo of an object made of {m}" for m in MATERIALS], model_name)
    th = clip_text(list(SOFT_HARD), model_name)
    I = np.stack([np.asarray(o.extra["clip"], np.float32) for o in have])
    I /= np.linalg.norm(I, axis=1, keepdims=True) + 1e-9

    def soft(x):
        x = 100 * x
        x = np.exp(x - x.max(1, keepdims=True))
        return x / x.sum(1, keepdims=True)

    pm, ph = soft(I @ tm.T), soft(I @ th.T)[:, 1]
    for o, a, b in zip(have, pm, ph):
        o.extra["rigidity"] = score(a, b) | {"source": "Rigidity Copilot: CLIP zero-shot on the object's best view"}
    return len(have)


def edge_stiffness(rest_m: np.ndarray, rig: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Spring constant (N/m) of each edge and its range: k ~ E * L for node spacing L."""
    L = np.asarray(rest_m, float)
    lo, hi = rig["youngs_modulus_range_pa"]
    return rig["youngs_modulus_pa"] * L, lo * L, hi * L
