# UncertainScene research outline and progress

This is the single living document for the project. Update the **Progress log** and the
**Status** column of each research path in the same commit as the work it describes
(see [docs/COMMITS.md](docs/COMMITS.md)). Decisions with lasting consequences also get a short
entry in [docs/decisions/](docs/decisions/).

Last updated: 2026-10-09

## 1. Goal

Compare two phone recordings of the same small indoor area (a shelf and the things around it)
and say, in 3D, **what changed, where, by how much and how sure we are**, while telling real
changes apart from differences in viewpoint, lighting, occlusion or areas that simply were not
seen in one of the recordings.

The end product of every scene pair is a **scene report** (`docs/templates/scene_report.md`):
a list of changes with semantic labels and uncertainties, plus concrete recommendations for
what a simulator should randomise, and over what range, to narrow the sim-to-real gap
(for example "cup radius 4.1 cm ± 0.6 cm, randomise 3.5 to 4.7 cm").

### Minimum deliverables (from the task brief)

| # | Deliverable | Where it lives |
|---|---|---|
| D1 | A shared 3D representation of the scene built from both recordings | Path P1 |
| D2 | Objects or regions that were moved, added or removed, identified and visualised in 3D | Path P2 |
| D3 | Each change described with a semantic label or natural language | Paths P2 and P6 |

### Stretch goals (Fin's extensions)

| # | Goal | Path |
|---|---|---|
| S1 | Robust to lighting changes between recordings | P1, P2 |
| S2 | Re-identify moved objects and measure how far they moved, with uncertainty | P3 |
| S3 | Deformable objects (for example a blanket): heat map of how they deformed | P4 |
| S4 | Object boundary uncertainty (for example cup radius ± 1 cm) | P3 |
| S5 | Robot interactability heat map, using the floor plane to decide what is in reach | P5 |
| S6 | Simulator randomisation report built from all of the above | P6 |
| S7 | Export changed objects as simulation assets (meshes plus uncertainty) | P6 |

## 2. Research questions

1. **Shared frame.** How accurately can two handheld recordings, taken from different
   viewpoints and possibly in different lighting, be placed in one 3D frame using only the
   parts of the scene that did not change?
2. **Change versus not-seen.** Can a visibility-aware comparison separate "removed" from
   "occluded" or "never observed", so that the system says "unknown" rather than guessing?
3. **Identity.** When an object disappears in one place and something similar appears
   elsewhere, can appearance features (DINOv2, CLIP, V-JEPA, or a lightweight descriptor) decide
   that it is the same object that moved, and how confident is that match?
4. **Uncertainty.** How large is the uncertainty on positions, displacements and object sizes,
   where does it come from (reconstruction noise, segmentation boundary, registration error),
   and does it shrink sensibly with more views?
5. **Deformation.** For non-rigid objects, can per-point displacement fields give a readable
   deformation heat map without being swamped by reconstruction noise?
6. **Actionability.** Can the change and uncertainty estimates be turned into simulator
   randomisation ranges that a robotics user would actually adopt?

## 3. Research paths

Each path has its own long-lived branch `path/<name>` (see [docs/BRANCHING.md](docs/BRANCHING.md)).
Status values: `not started`, `exploring`, `working`, `done`, `parked`.

| Path | Branch | Question | Status |
|---|---|---|---|
| P0 Capture protocol | `path/capture` | How to record so the rest works | exploring |
| P1 Shared 3D reconstruction | `path/recon` | RQ1 | exploring (VGGT-1B joint, in the P2 proof of concept) |
| P2 Change detection | `path/change` | RQ2 | exploring (`exp/change/scene01-heatmaps`) |
| P3 Re-identification and uncertainty | `path/reid` | RQ3, RQ4 | not started |
| P4 Deformable objects | `path/deform` | RQ5 | not started |
| P5 Robot interactability and reach | `path/reach` | RQ6 | not started |
| P6 Scene report, sim randomisation, asset export | `path/report` | RQ6 | not started |

### P0 Capture protocol

Full protocol: [docs/CAPTURE.md](docs/CAPTURE.md) (phone settings, 60 to 90 s sweeps, camera
path, scene contents, what stays the same, ground truth, checklist).

- Test matrix: `pair00_static` (nothing changes), `pair01` (mixed changes), `pair02` (changes
  plus lighting change and a swap of similar objects) are the minimum; `pair03` to `pair07`
  cover small displacements, deformables, occlusion, lighting-only and floor obstacles.
- Ground truth is measured and written before each "after" recording, for **scoring only**.
- Storage: frames committed under `data/frames/`, originals on the laptop, manifest in
  `data/manifest.json` ([data/README.md](data/README.md)).
- Recorded so far (2026-10-09): scene01 (living-room sofa), dark and light lighting, before and
  after. Missing: the dark "after" sweep (not yet copied off the phone). Ground truth of what
  moved still to be written.

### P1 Shared 3D reconstruction

Candidate approaches (pick by quick trial, record the choice in a decision note):

- Feed-forward multi-view models (MASt3R, VGGT, DUSt3R) run jointly on frames from both
  recordings, so both land in one frame directly.
- COLMAP (or GLOMAP) structure-from-motion on both recordings, then a dense model
  (3D Gaussian splatting via gsplat or nerfstudio, or MVS point cloud).
- Fallback: reconstruct each recording separately and register them on the static background
  (feature matching plus robust ICP), estimating the registration error as an explicit
  uncertainty term.

Measures: registration error on static anchors, scale error against the known-size object,
coverage (fraction of the scene seen in both recordings).

### P2 Change detection (moved, added, removed)

- Per-point or per-voxel comparison in the shared frame, **only where both recordings observed
  the region** (visibility from camera poses and depth). Unobserved regions are reported as
  `unknown`, never as changes.
- Group changed points into object candidates with 2D masks lifted to 3D (SAM 2 masks across
  frames, or open-vocabulary detectors such as Grounding DINO / OWL-ViT).
- Lighting robustness: compare geometry and illumination-invariant features, not raw colour.
- Labels: open-vocabulary detector labels first, a vision-language model for a natural-language
  sentence per change.
- Visualisation: coloured 3D point cloud or splat (red removed, green added, blue moved,
  grey unknown), exported to an HTML viewer or `.ply`.

Measures: precision/recall of changes against `data/ground_truth`, false-change rate on the
nothing-changed pairs.

### P3 Re-identification, displacement and boundary uncertainty

- Match "removed" candidates to "added" candidates with appearance features (DINOv2, CLIP,
  V-JEPA patch features, or a lightweight colour/shape descriptor). A match turns a
  remove-plus-add into one **moved** object with a 6-DoF displacement.
- Uncertainty: combine registration error, reconstruction noise and mask boundary spread
  (for example by resampling masks or bootstrapping points) into a displacement covariance and
  a size interval such as "radius 4.1 ± 0.6 cm".
- Measures: match accuracy, displacement error against tape-measured ground truth,
  calibration of the stated uncertainty (do 90 % intervals contain the truth about 90 % of
  the time?).

### P4 Deformable objects

- For objects flagged as deformable (label or poor rigid fit), compute a dense displacement
  field between the two reconstructions (nearest-neighbour with outlier rejection, or scene
  flow) and render it as a heat map on the object surface.
- Measure noise floor on the nothing-changed pairs so the heat map shows real deformation only.

### P5 Robot interactability and reach

- Fit the floor plane; given a robot base position and arm reach (configurable), mark what is
  reachable.
- Interactability heat map: reachable, free space around the object for a gripper, object size
  within gripper range, and how certain the geometry is there.
- Changes since the last recording raise attention in the map (the robot should re-check
  those areas).

### P6 Scene report, sim randomisation, asset export

- One script turns the outputs of P1 to P5 into `docs/templates/scene_report.md` format.
- Randomisation ranges come straight from P3/P4 uncertainties (pose, size, deformation) and
  P1 (lighting, registration).
- Stretch: export each changed object as a mesh (`.obj`/`.glb`) with a sidecar JSON of its
  uncertainty, ready for a simulator such as Isaac Sim, MuJoCo or PyBullet.

## 4. Milestones

| # | Milestone | Needs | Status |
|---|---|---|---|
| M0 | Repo, branching, Colab publishing and guardrails in place | none | done (2026-10-09) |
| M1 | First scene pair recorded with ground truth notes | P0 | recorded; ground truth pending |
| M2 | Both recordings in one 3D frame, registration error measured | P1 | not started |
| M3 | Moved/added/removed changes visualised with labels (minimum deliverables) | P2 | not started |
| M4 | Moved objects re-identified, displacement and size with uncertainty | P3 | not started |
| M5 | First full scene report with sim randomisation ranges | P6 | not started |
| M6 | Stretch: deformation heat map, reach map, asset export | P4, P5, P6 | not started |

## 5. Evaluation rules

- Ground truth (`data/ground_truth/`) is used to **score** outputs, never to tune thresholds
  on the same pair it is scored on. If a threshold must be chosen, choose it on a separate
  pair and say which.
- Every reported number names the scene pair(s), the commit and the run id it came from.
- Always report the nothing-changed pair alongside the changed pairs, so false changes are
  visible.

## 6. Progress log

Newest first. One line per meaningful step: date, path, what was done, link to the result.

| Date | Path | What happened | Result |
|---|---|---|---|
| 2026-10-10 | change | v2 first attempt (run 2026-10-10_scene01-objects, 11 steps, Colab disconnected 19:07): object stage failed in every step (CLIP labelling crash on transformers 5), so 0 changes everywhere; views almost never merged (1107/1122 objects single-view, depth error >> 1.2 cm grid); clips steps never started (RAM estimate). Splats: 17.7 dB held-out PSNR, same-view 2.0% changed vs 1% noise. Fixed all three; splats now 20k steps, compared only where the other recording saw; rerun as 2026-10-11_scene01-objects-fixed | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-10 | change | Gaussian splats added as phase 1b (not yet run; Fin chose one pair first): gsplat splat of light before a and of light after a, started from the VGGT points with per-camera pose correction; render quality (PSNR) on held-out frames; before vs after rendered from the same held-out camera, pixels flagged above the 99th percentile of the no-change render error; `.splat` exports and splat centres as dense clouds in the viewer | [src/uncertainscene/experiments/splats.py](src/uncertainscene/experiments/splats.py) |
| 2026-10-10 | change | Denser v2 (not yet run): frames re-extracted at 6 fps (~370 per recording, `data/frames_6fps/`); SAM 2 on 44 views per recording with a 32x32 point grid and smaller minimum mask, so more and smaller objects get labelled; viewer: ~6x more points per recording, each object painted one colour, filled object boxes, 3D loaded per run; robot heat maps now assume a mobile base that drives to any free floor spot, 1.0 m reach from a 0.75 m mount (fixed base kept as a robot variant); viewer point clouds no longer offset from the objects (decoding bug) | [src/uncertainscene/change/fields.py](src/uncertainscene/change/fields.py), [src/uncertainscene/viz/site.py](src/uncertainscene/viz/site.py) |
| 2026-10-10 | change | Results made readable and object position uncertainty added (not yet run): every run named after what it compares and the question it answers (phases 1-main ... 6-repeat); one results file `results/change/RESULTS.md` and a Plotly-free 3D viewer (one recording alone or the changes, heat maps, object boxes, position ranges); per-object 90% position range from VGGT depth confidence + SAM 2 mask wobble fused per view (Kalman filter) with an NIS consistency check and the views-disagreeing range as a cross-check; new phase 2-position (1-3 clips of one scene; 2 clips per state before/after) | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-10 | change | First overnight run (run 2026-10-10_scene01-heatmaps, 179 steps, ~5 h): point-level detection could not separate real changes from noise (2.3% vs 2.2% changed; guitar, neck pillow, books missed); dark recordings ~2x noisier; floor fit unreliable. Pivot (v2, not yet run): every frame per recording via chunked VGGT, gravity + floor from horizontal surfaces, scale from phone height + known object sizes, SAM 2 objects merged across views in 3D, CLIP label for every object, object-level change decisions with label-free thresholds, AUROC separation in summaries | [summary v1](results/change/2026-10-10_scene01-heatmaps/summary_session_colab-dbb8/summary.md), [src/uncertainscene/experiments/scene01.py](src/uncertainscene/experiments/scene01.py) |
| 2026-10-09 | change | Low-compute proof of concept ready to run on Colab (not yet run): VGGT-1B joint reconstruction of both recordings, visibility-aware change detection with label-free thresholds, DINOv2 re-ID, CLIP labels; heat maps (surface uncertainty, deformation/shift, height, reach, approach, interactability), robot base-placement map, scene report with sim randomisation ranges, 3D viewer; ~167-step queue (creative outputs first, light vs dark contrasts and lighting report, 2 sanity ablations) that fills a 12 h session; logged token/setup checks, GPU frame cap with out-of-memory retry, RAM/disk guard between stages | [notebooks/scene01_change_heatmaps.ipynb](notebooks/scene01_change_heatmaps.ipynb), [src/uncertainscene/experiments/poc.py](src/uncertainscene/experiments/poc.py) |
| 2026-10-09 | capture | First recordings: scene01 (sofa), 7 sweeps labelled (3 dark before, 2 light before, 2 light after); frames extracted (3 fps, 1024 px) | [data/recordings.json](data/recordings.json), [data/previews/](data/previews/) |
| 2026-10-09 | capture | Capture protocol and recording test matrix written | [docs/CAPTURE.md](docs/CAPTURE.md) |
| 2026-10-09 | infra | Repo scaffold: outline, commit and branching guides, Colab publishing with resource guardrails | this commit |

## 7. Open questions

- Which reconstruction approach gives the best registration on phone video in a small space?
  (decide in P1 after a quick trial)
- What robot (reach, gripper width) should the interactability map assume?
- Is a vision-language model API acceptable for the natural-language descriptions, or should
  it be an open model that runs on Colab?
