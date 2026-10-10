"""Phase 1b: Gaussian splats of one before/after pair (Fin chose "one pair first", 2026-10-10).

Questions:
1. Do splats started from the VGGT reconstruction render the scene well? Measured on frames
   the splat never saw (held out): PSNR of the render against the real photo.
2. Does comparing the before and after splats from exactly the same camera separate real
   changes from noise? For each held-out frame of the after recording, the after splat's
   render shows how far a render differs from the photo when nothing changed (noise); the
   before splat's render from the same camera shows the change plus that noise. A pixel counts
   as changed when its difference is above the 99th percentile of the no-change differences,
   so the threshold comes from the data itself, never from the answer key.

Outputs: splat_compare.png (per view: photo, before render, after render, difference, changed
pixels), splat_orbit.gif (before and after splats side by side along the after camera path),
splat_before.splat / splat_after.splat (the most visible 140 000 Gaussians each, standard
.splat format any web splat viewer opens), scene3d.json (splat centres as dense point clouds for
the results viewer), metrics.json, report.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("uncertainscene.splats")


@dataclass
class SplatSpec:
    name: str
    a: str
    b: str
    iters: int = 7000
    n_per_video: int | None = None
    offset: int = 0
    variant: str = "splat"
    outputs: str = "full"
    title: str = ""
    question: str = ""

    @property
    def needs(self) -> dict:
        # reconstruction (cached when the 1-main step of the same pair ran first) + 2 splats
        # + first-time gsplat CUDA compile (~10 min)
        return {"disk_gb": 6, "ram_gb": 6, "minutes": round(12 + 2 * self.iters / 7000 * 8 + 6)}


def analyse_splat(spec: SplatSpec, out: Path, cache: Path, check=None, store=None) -> str:
    from PIL import Image

    from ..change.fields import RobotModel, fit_floor
    from ..recon import splat as S
    from ..viz import scene3d
    from .catalog import pair_type, rec_name
    from .scene01 import Spec, _recon

    t0 = time.time()
    stages = []

    def stage(name):
        stages.append({"stage": name, "seconds": round(time.time() - t0)})
        log.info("stage done: %s (%.0f s)", name, time.time() - t0)
        if check is not None:
            check()

    rec, rinfo = _recon(Spec(spec.name, spec.a, spec.b, n_per_video=spec.n_per_video, offset=spec.offset), cache)
    stage(f"reconstruction ({len(rec.video)} frames{', cached' if rinfo.get('cached') else ''})")
    cfg = S.SplatConfig(iters=spec.iters)
    splats, infos = [], []
    for v in (0, 1):
        sp, info = S.train(rec, v, cfg, check)
        splats.append(sp)
        infos.append(info)
        stage(f"splat of {rec_name([spec.a, spec.b][v])}: {info['gaussians']} Gaussians, {info['seconds']} s")

    X0, _ = S.init_points(rec, np.nonzero(rec.video == 0)[0], 60000)
    floor = fit_floor(X0, rec.E, RobotModel())
    cm = floor.scale_m_per_unit * 100

    # ---- held-out views: quality and same-view comparison (both directions) ----
    h0, w0 = rec.depth.shape[1:]
    res = {}
    for v in (0, 1):
        views = S.test_views(rec, v, cfg.hold_every)
        own, other = splats[v], splats[1 - v]
        rows = []
        for s in views:
            real, wh = S._images([rec.files[s]], cfg.long_side)
            real = real[0].astype(np.float32) / 255
            K = rec.K[s].copy()
            K[0] *= wh[0] / w0
            K[1] *= wh[1] / h0
            ro, do, ao = own.render(rec.E[s], K, wh)
            rx, dx, ax = other.render(rec.E[s], K, wh)
            ok = (ao > 0.9) & (ax > 0.9)
            rows.append({"frame": int(s), "real": real, "own": ro, "other": rx, "ok": ok,
                         "noise": np.abs(ro - real).mean(-1), "diff": np.abs(rx - real).mean(-1),
                         "depth_cm": np.abs(do - dx) * cm, "psnr": S.psnr(ro, real)})
        noise = np.concatenate([r["noise"][r["ok"]] for r in rows]) if rows else np.zeros(1)
        thr = float(np.quantile(noise, 0.99)) if len(noise) else 1.0
        for r in rows:
            r["changed"] = (r["diff"] > thr) & r["ok"]
        res[v] = {"rows": rows, "thr": thr,
                  "psnr": float(np.mean([r["psnr"] for r in rows])) if rows else None,
                  "noise_median": float(np.median(noise)),
                  "diff_median": float(np.median(np.concatenate([r["diff"][r["ok"]] for r in rows]))) if rows else None,
                  "changed_share": float(np.mean(np.concatenate([r["changed"][r["ok"]] for r in rows]))) if rows else None,
                  "depth_changed_share": float(np.mean(np.concatenate([r["depth_cm"][r["ok"]] > 5 for r in rows])))
                  if rows else None}
    stage("held-out renders and same-view comparison")

    # ---- figures ----
    _compare_figure(res[1]["rows"], res[1]["thr"], out / "splat_compare.png",
                    f"{rec_name(spec.a)} vs {rec_name(spec.b)}: same camera, held-out frames of the after recording")
    _orbit_gif(rec, splats, cfg, out / "splat_orbit.gif", (w0, h0))
    stage("figures")

    # ---- exports: .splat files and dense points for the results viewer ----
    exported = {}
    for v, nm in ((0, "before"), (1, "after")):
        exported[nm] = splats[v].export_splat(out / f"splat_{nm}.splat", cfg.export_max)
        if store is not None:
            try:
                full = cache / f"splat_{nm}_{spec.name}.splat"
                splats[v].export_splat(full, 10**9)
                store.put(full, f"run-splats", name=full.name, overwrite=True)
            except Exception as e:  # noqa: BLE001 - the full export is a bonus
                log.warning("full splat upload failed: %s", e)
    pts = scene3d.Points()
    layers = []
    for v, lab, rk in ((0, rec_name(spec.a), "A"), (1, rec_name(spec.b), "B")):
        P, C = _centres(splats[v], 150_000)
        layers.append({"name": f"{lab} (splat centres)", "kind": "colour", "rec": rk, "visible": v == 0, "size": 2,
                       "pts": pts.add(P), "rgb": scene3d._b64(C)})
    scene3d.save({"points": pts.tables, "layers": layers, "objects": []}, out)
    stage("exports")

    a, b = res[1], res[0]
    m = {"step": spec.name, "step_kind": "splat", "version": 2, "a": spec.a, "b": spec.b,
         "pair_type": pair_type(spec.a, spec.b), "variant": spec.variant, "title": spec.title, "question": spec.question,
         "phase_title": "Gaussian splats", "splat_iters": spec.iters, "splats": infos,
         "splat_psnr_before": None if b["psnr"] is None else round(b["psnr"], 2),
         "splat_psnr_after": None if a["psnr"] is None else round(a["psnr"], 2),
         "same_view_noise_median": round(a["noise_median"], 4), "same_view_diff_median": a["diff_median"],
         "same_view_threshold": round(a["thr"], 4), "same_view_changed_share": a["changed_share"],
         "same_view_changed_share_reverse": b["changed_share"], "depth_changed_share_5cm": a["depth_changed_share"],
         "scale_m_per_unit": round(floor.scale_m_per_unit, 4), "exported_gaussians": exported,
         "frames_total": int(len(rec.video)), "stages": stages, "step_seconds": round(time.time() - t0)}
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float))
    write_report(spec, m, out)
    from .poc import enforce_size

    enforce_size(out)
    return (f"splats: PSNR before {m['splat_psnr_before']}, after {m['splat_psnr_after']} dB on held-out frames; "
            f"same-view changed {a['changed_share']:.1%} of pixels (noise level 1% by construction); "
            f"{m['step_seconds']} s")


def _centres(sp, n: int):
    import torch

    with torch.no_grad():
        op = torch.sigmoid(sp.p["opacities"]).cpu().numpy()
        P = sp.p["means"].cpu().numpy()
        C = (torch.sigmoid(sp.p["colors"]).cpu().numpy() * 255).astype(np.uint8)
    k = np.argsort(-op)[:n]
    return P[k], C[k]


def _compare_figure(rows: list, thr: float, out: Path, title: str, n: int = 5) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not rows:
        return
    pick = [rows[i] for i in np.linspace(0, len(rows) - 1, min(n, len(rows))).round().astype(int)]
    fig, axes = plt.subplots(len(pick), 5, figsize=(15, 3.6 * len(pick) * pick[0]["real"].shape[0]
                                                   / pick[0]["real"].shape[1] * 0.75 + 0.8), squeeze=False)
    heads = ["photo (after, not trained on)", "before splat, same camera", "after splat, same camera",
             "difference before render vs photo", "changed pixels (above no-change 99%)"]
    for i, r in enumerate(pick):
        ov = r["real"].copy()
        ov[r["changed"]] = 0.4 * ov[r["changed"]] + 0.6 * np.array([1.0, 0.1, 0.1])
        for j, im in enumerate([r["real"], r["other"], r["own"], r["diff"], ov]):
            ax = axes[i][j]
            ax.imshow(im, cmap="magma" if j == 3 else None, vmin=0, vmax=max(3 * thr, 0.05) if j == 3 else None)
            ax.set_xticks([]), ax.set_yticks([])
            if i == 0:
                ax.set_title(heads[j], fontsize=9)
        axes[i][0].set_ylabel(f"frame {r['frame']}\nafter PSNR {r['psnr']:.1f} dB", fontsize=8)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=80)
    plt.close(fig)


def _orbit_gif(rec, splats, cfg, out: Path, wh0, n: int = 36, long_side: int = 320, max_mb: float = 4.0) -> None:
    """Before | after splats rendered side by side along the after recording's camera path."""
    from PIL import Image

    frames = np.nonzero(rec.video == 1)[0]
    frames = frames[np.linspace(0, len(frames) - 1, min(n, len(frames))).round().astype(int)]
    s = long_side / max(wh0)
    wh = (int(round(wh0[0] * s)), int(round(wh0[1] * s)))
    imgs = []
    for f in frames:
        K = rec.K[f].copy()
        K[:2] *= s
        pair = [(sp.render(rec.E[f], K, wh)[0] * 255).astype(np.uint8) for sp in splats]
        imgs.append(Image.fromarray(np.concatenate([pair[0], np.full((wh[1], 6, 3), 255, np.uint8), pair[1]], 1)))
    while imgs:
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=150, loop=0, optimize=True)
        if out.stat().st_size < max_mb * 1024**2 or len(imgs) < 8:
            break
        imgs = imgs[::2]


def write_report(spec: SplatSpec, m: dict, out: Path) -> None:
    from .catalog import rec_name

    f = lambda v, d=1, pct=False: "-" if v is None else (f"{v:.{d}%}" if pct else f"{v:.{d}f}")  # noqa: E731
    sp = {s["recording"]: s for s in m["splats"]}
    L = [f"# {spec.title}", "", f"**What this run tests:** {spec.question}", "",
         f"Before: {rec_name(spec.a)}. After: {rec_name(spec.b)}. One splat per recording, {spec.iters} training "
         f"steps each, started from the VGGT points; every 8th frame held out for testing. Step folder `{spec.name}`.", "",
         "| | Before splat | After splat |", "|---|---|---|",
         f"| Render quality on held-out frames (PSNR, higher is better; 25+ dB looks good) | "
         f"{f(m['splat_psnr_before'])} dB | {f(m['splat_psnr_after'])} dB |",
         "| Gaussians | " + " | ".join(str(s["gaussians"]) for s in m["splats"]) + " |",
         "| Training time | " + " | ".join(f"{s['seconds']} s" for s in m["splats"]) + " |",
         "| Camera pose correction learnt (median) | " + " | ".join(f"{s['pose_correction_deg_median']} deg"
                                                             for s in m["splats"]) + " |",
         "", "## Same camera, before vs after", "",
         "Each held-out after photo is compared with the after splat's render (nothing changed, so this is the "
         "noise) and with the before splat's render from the same camera (change plus noise). Pixels above the 99th "
         "percentile of the no-change differences count as changed, so about 1% would be flagged if nothing moved.", "",
         f"- Changed pixels, before splat vs after photos: **{f(m['same_view_changed_share'], 1, True)}** "
         f"(reverse direction {f(m['same_view_changed_share_reverse'], 1, True)}; 1% = no change).",
         f"- Median colour difference: {f(m['same_view_diff_median'], 3)} vs {f(m['same_view_noise_median'], 3)} "
         "when nothing changed.",
         f"- Pixels where the two splats' depth differs by more than 5 cm: {f(m['depth_changed_share_5cm'], 1, True)}.",
         "", "Figures: `splat_compare.png` (per view), `splat_orbit.gif` (both splats along the after camera path). "
         "The `.splat` files open in any web splat viewer (e.g. drag into antimatter15.com/splat)."]
    (out / "report.md").write_text("\n".join(L) + "\n")
