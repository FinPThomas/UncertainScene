"""Node-structured object assets (Fin, 2026-10-11): every SAM 2 object as a small graph of nodes
with a convex hull, edge strain and relative stiffness, and contacts between objects.

Per object (from its SAM 2 points, metres, world frame with the floor's up vector given):
- nodes: farthest-point samples of the object's points; each node's weight is the share of
  the object's points nearest to it (a mass proxy, not a mass);
- edges: each node to its nearest neighbours (symmetric), with rest length (before);
- hull: convex hull of the points (vertices, faces, volume, area), also written as .obj;
- stiffness from the Rigidity Copilot (rigidity.py: CLIP asked what the object is made of and
  how stiff it is): expected Young's modulus with a range, and per edge a spring constant
  k ~ E * L in N/m;
- observed strain, measured, as a check on the Copilot: where the object has matched feature
  points (change/motion3d.py), every node gets the displacement of its nearby points with
  their uncertainty, the object's rigid motion is removed, and each edge's strain is the
  change of its length over its rest length, with the uncertainty carried through. An edge is
  strained when |strain| is beyond twice its uncertainty. The object's measured behaviour
  (deformable / rigid at this precision) is compared with the Copilot's expectation.
  No matched points -> strain unknown (null).
Between objects: contacts (points of two objects closer than `contact_m`), with which object rests
on which (from the up vector).

The geometry and strain never depend on an object's label; the Copilot looks at its image.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def save_objects(objs: list, path: Path, voxel: float | None = None, max_points: int = 4000) -> None:
    """Objects of a step (both recordings) for later steps of the same reconstruction (the
    splat step cuts per-object splats with them)."""
    rng = np.random.default_rng(0)
    meta, arrays = [], {}
    for k, o in enumerate(o for o in objs if not o.structure):
        X = o.X if len(o.X) <= max_points else o.X[rng.choice(len(o.X), max_points, replace=False)]
        arrays[f"X{k}"] = X.astype(np.float32)
        meta.append({"recording": o.recording, "id": int(o.id), "label": o.label, "status": o.status,
                     "partner": None if o.partner is None else int(o.partner), "motion": o.extra.get("motion"),
                     "voxel": o.extra.get("voxel", voxel)})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + "_tmp.npz")
    np.savez_compressed(tmp, meta=json.dumps(meta, default=float), **arrays)
    tmp.replace(path)


def load_objects(path: Path) -> list[dict]:
    z = np.load(path)
    meta = json.loads(str(z["meta"]))
    for k, m in enumerate(meta):
        m["X"] = z[f"X{k}"]
    return meta


CHANGED = ("moved", "added", "removed", "changed in place", "shifted")


def fps(X: np.ndarray, n: int, seed: int = 0) -> np.ndarray:
    """Indices of n farthest-point samples (spread evenly over the object)."""
    n = min(n, len(X))
    idx = np.zeros(n, int)
    idx[0] = int(np.random.default_rng(seed).integers(len(X)))
    d = np.linalg.norm(X - X[idx[0]], axis=1)
    for i in range(1, n):
        idx[i] = int(np.argmax(d))
        d = np.minimum(d, np.linalg.norm(X - X[idx[i]], axis=1))
    return idx


def node_graph(X: np.ndarray, n_nodes: int = 96, k: int = 6) -> dict:
    """Nodes, weights and symmetric k-nearest-neighbour edges of a point set."""
    from scipy.spatial import cKDTree

    N = X[fps(X, n_nodes)]
    owner = cKDTree(N).query(X)[1]
    w = np.bincount(owner, minlength=len(N)) / len(X)
    kk = min(k + 1, len(N))
    _, nn = cKDTree(N).query(N, k=kk)
    edges = sorted({(min(i, j), max(i, j)) for i in range(len(N)) for j in np.atleast_1d(nn[i])[1:] if i != j})
    return {"nodes": N, "weights": w, "edges": np.array(edges, int).reshape(-1, 2)}


def hull(X: np.ndarray) -> dict | None:
    from scipy.spatial import ConvexHull

    try:
        h = ConvexHull(X)
    except Exception:  # noqa: BLE001 - flat or tiny objects have no 3D hull
        return None
    used = np.unique(h.simplices)
    remap = -np.ones(len(X), int)
    remap[used] = np.arange(len(used))
    return {"vertices": X[used], "faces": remap[h.simplices], "volume": float(h.volume), "area": float(h.area)}


def node_strain(nodes: np.ndarray, edges: np.ndarray, links: dict | None, radius: float, min_links: int = 3) -> dict:
    """Edge strain between the recordings from matched feature points near each node.

    links: {"XA" [n, 3], "XB" [n, 3], "C" [n, 3, 3]} in the same units as nodes (the object's
    links, before -> after, with displacement covariances). Each node takes the inverse-
    variance mean displacement of the links within `radius` (and its covariance); the rigid
    motion of the nodes is removed; strain = (after length - rest length) / rest length."""
    from scipy.spatial import cKDTree

    E = len(edges)
    out = {"strain": np.full(E, np.nan), "strain_sigma": np.full(E, np.nan), "node_disp": None}
    if links is None or len(links["XA"]) < min_links or not E:
        return out
    tree = cKDTree(links["XA"])
    d = links["XB"] - links["XA"]
    var = np.trace(links["C"], axis1=1, axis2=2) / 3
    nd = np.full((len(nodes), 3), np.nan)
    nv = np.full(len(nodes), np.nan)
    for i, p in enumerate(nodes):
        k = tree.query_ball_point(p, radius)
        if len(k) < min_links:
            continue
        w = 1 / var[k]
        nd[i] = (d[k] * w[:, None]).sum(0) / w.sum()
        nv[i] = 1 / w.sum()
    ok = np.isfinite(nv)
    if ok.sum() < 3:
        return out
    from .motion3d import kabsch

    R, t = kabsch(nodes[ok], nodes[ok] + nd[ok])
    after = nodes @ R.T + t                                         # rigidly moved nodes
    after[ok] = nodes[ok] + nd[ok]                                  # measured where possible
    i, j = edges[:, 0], edges[:, 1]
    L0 = np.linalg.norm(nodes[j] - nodes[i], axis=1)
    L1 = np.linalg.norm(after[j] - after[i], axis=1)
    both = ok[i] & ok[j]
    s = (L1 - L0) / np.maximum(L0, 1e-9)
    sig = np.sqrt(np.nan_to_num(nv[i]) + np.nan_to_num(nv[j])) / np.maximum(L0, 1e-9)
    out["strain"][both], out["strain_sigma"][both] = s[both], sig[both]
    out["node_disp"] = nd
    return out


def contacts(graphs: list[dict], up: np.ndarray, contact: float) -> list[dict]:
    """Pairs of objects whose points come within `contact`; 'rests on' from the up vector."""
    from scipy.spatial import cKDTree

    out = []
    trees = [cKDTree(g["points"]) for g in graphs]
    lo = [np.quantile(g["points"] @ up, 0.05) for g in graphs]
    hi = [np.quantile(g["points"] @ up, 0.95) for g in graphs]
    for a in range(len(graphs)):
        for b in range(a + 1, len(graphs)):
            if graphs[a]["recording"] != graphs[b]["recording"]:
                continue
            if (np.any(graphs[a]["points"].min(0) > graphs[b]["points"].max(0) + contact) or
                    np.any(graphs[b]["points"].min(0) > graphs[a]["points"].max(0) + contact)):
                continue
            pairs = trees[a].query_ball_tree(trees[b], contact)
            n = sum(len(p) for p in pairs)
            if not n:
                continue
            rel = "touching"
            if lo[a] > lo[b] and abs(lo[a] - hi[b]) < 3 * contact:
                rel = f"{graphs[a]['name']} rests on {graphs[b]['name']}"
            elif lo[b] > lo[a] and abs(lo[b] - hi[a]) < 3 * contact:
                rel = f"{graphs[b]['name']} rests on {graphs[a]['name']}"
            out.append({"a": graphs[a]["name"], "b": graphs[b]["name"], "point_pairs": int(n), "relation": rel})
    return out


def build(objs: list, scale_m: float, up: np.ndarray, out_dir: Path, motion: dict | None = None,
          motion_objs: list | None = None, n_nodes: int = 96, max_objects: int = 60, contact_m: float = 0.015) -> dict:
    """Write out_dir/<name>.json (+ .obj hull) per object and out_dir/scene_graph.json.
    Exported: every changed object and its partner, then the largest others up to max_objects."""
    from scipy.spatial import cKDTree

    out_dir.mkdir(parents=True, exist_ok=True)
    real = [o for o in objs if not o.structure and len(o.X) >= 20]
    by = {(o.recording, o.id): o for o in real}
    pick = [o for o in real if o.status in CHANGED]
    for o in list(pick):
        p = by.get(("B" if o.recording == "A" else "A", o.partner)) if o.partner is not None else None
        if p is not None and p not in pick:
            pick.append(p)
    rest = sorted((o for o in real if o not in pick), key=lambda o: -len(o.X))
    pick = (pick + rest)[:max(max_objects, len(pick))]
    # links of each object (motion3d works on the before recording's objects)
    links_of = {}
    if motion is not None and len(motion.get("links_XA", [])) and "C" in motion:
        for j in np.unique(motion["obj_of"][motion["obj_of"] >= 0]):
            sel = motion["obj_of"] == j
            links_of[int(j)] = {"XA": motion["links_XA"][sel], "XB": motion["links_XB"][sel], "C": motion["C"][sel]}
    index = {id(o): i for i, o in enumerate(motion_objs if motion_objs is not None else objs)}
    graphs, files = [], []
    for o in pick:
        name = f"{o.recording}{o.id:03d}_{(o.label or 'object').replace(' ', '_')}"
        g = node_graph(o.X, n_nodes)
        nn_d = cKDTree(g["nodes"]).query(g["nodes"], k=2)[0][:, 1] if len(g["nodes"]) > 1 else np.ones(1)
        st = node_strain(g["nodes"], g["edges"], links_of.get(index.get(id(o))), radius=1.5 * float(np.median(nn_d)))
        h = hull(o.X)
        L0 = np.linalg.norm(g["nodes"][g["edges"][:, 1]] - g["nodes"][g["edges"][:, 0]], axis=1) * scale_m
        strained = np.abs(st["strain"]) > 2 * st["strain_sigma"]
        measured = np.isfinite(st["strain"])
        rig = o.extra.get("rigidity")
        if rig is not None:
            from .rigidity import edge_stiffness

            k, klo, khi = edge_stiffness(L0, rig)
        else:
            k = klo = khi = np.full(len(L0), np.nan)
        edges = [{"i": int(a), "j": int(b), "rest_m": round(float(l), 4),
                  "stiffness_n_per_m": None if not np.isfinite(kk) else float(f"{kk:.3g}"),
                  "stiffness_range_n_per_m": None if not np.isfinite(kk) else [float(f"{x:.3g}") for x in (kl, kh)],
                  "observed_strain": None if not m else round(float(s), 4),
                  "observed_strain_sigma": None if not m else round(float(sg), 4),
                  "strained": None if not m else bool(x)}
                 for (a, b), l, kk, kl, kh, s, sg, x, m in zip(g["edges"], L0, k, klo, khi, st["strain"],
                                                              st["strain_sigma"], strained, measured)]
        doc = {"name": name, "label": o.label, "label_top3": o.label_top3, "recording": o.recording, "id": int(o.id),
               "status": o.status, "partner": None if o.partner is None else f"{'B' if o.recording == 'A' else 'A'}{o.partner}",
               "units": "metres, world frame of the reconstruction", "up": np.round(up, 4).tolist(),
               "nodes": [{"xyz": np.round(p * scale_m, 4).tolist(), "weight": round(float(w), 4)}
                         for p, w in zip(g["nodes"], g["weights"])],
               "edges": edges,
               "hull": None if h is None else {"volume_m3": round(h["volume"] * scale_m**3, 6),
                                               "area_m2": round(h["area"] * scale_m**2, 5),
                                               "vertices": np.round(h["vertices"] * scale_m, 4).tolist(),
                                               "faces": h["faces"].tolist()},
               "deformation": {"edges_measured": int(measured.sum()), "edges_strained": int((strained & measured).sum()),
                               "strain_abs_p90": None if not measured.any() else round(float(np.quantile(np.abs(st["strain"][measured]), 0.9)), 4),
                               "behaviour": None if not measured.any() else
                               ("deformable" if (strained & measured).sum() >= max(3, 0.1 * measured.sum()) else
                                "rigid at this precision")},
               "rigidity_copilot": rig,
               "motion": o.extra.get("motion")}
        beh = doc["deformation"]["behaviour"]
        if rig is not None and beh is not None:          # does the measured strain agree with the expectation?
            expect_soft = rig["rigidity"] < 0.5
            doc["deformation"]["copilot_check"] = ("agrees" if expect_soft == (beh == "deformable") else
                                                   "disagrees (expected " + ("soft" if expect_soft else "rigid") +
                                                   f", measured {beh})")
        (out_dir / f"{name}.json").write_text(json.dumps(doc, default=float, separators=(",", ":")))
        if h is not None:
            obj = [f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in h["vertices"] * scale_m]
            obj += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in h["faces"]]
            (out_dir / f"{name}_hull.obj").write_text("\n".join(obj) + "\n")
        sub = o.X[:: max(1, len(o.X) // 1500)]
        graphs.append({"name": name, "recording": o.recording, "points": sub * scale_m})
        files.append({"name": name, "label": o.label, "status": o.status, "nodes": len(g["nodes"]),
                      "edges": len(edges), "behaviour": doc["deformation"]["behaviour"],
                      "rigidity": None if rig is None else rig["rigidity"],
                      "material": None if rig is None else rig["materials"][0][0],
                      "youngs_modulus_pa": None if rig is None else float(f"{rig['youngs_modulus_pa']:.3g}"),
                      "copilot_check": doc["deformation"].get("copilot_check"),
                      "edges_strained": doc["deformation"]["edges_strained"]})
    con = contacts(graphs, np.asarray(up, float), contact_m) if graphs else []
    scene = {"objects": files, "contacts": con, "scale_m_per_unit": scale_m, "up": np.round(up, 4).tolist()}
    (out_dir / "scene_graph.json").write_text(json.dumps(scene, indent=1, default=float))
    log.info("assets: %d objects exported (%d deformable), %d contacts", len(files),
             sum(f["behaviour"] == "deformable" for f in files), len(con))
    return scene
