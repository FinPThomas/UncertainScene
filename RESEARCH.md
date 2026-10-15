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
| 2026-10-11 | recon | Splat fixes after Fin found run 2's splats blurry with floaters (not yet run): camera poses refined by SIFT tracks + bundle adjustment from the VGGT poses, both recordings together (`recon/poses.py`; synthetic test 0.72 -> <0.2 deg); splats start from the adjusted points plus VGGT depth lined up to them, VGGT depth guide, opacity resets (gsplat 1.5.3 never resets), floater pruning at 55% and 80% of training; real-splat web viewer `splat_viewer/` per step (`viz/splat_site.py`) | [src/uncertainscene/recon/poses.py](src/uncertainscene/recon/poses.py) |
| 2026-10-11 | change | Fin: poses updating as we go, CLIP must work, distance-moved map from matched features with a deformation metric (e.g. a bent cushion corner). Done (not yet run): bundle-adjusted poses + depth aligned to them in every non-light step (cached per reconstruction); splats carry learnt pose corrections to held-out frames; CLIP `_embeds` checked against a real transformers 5.19 CLIPModel (projected embeddings match); `change/motion3d.py`: per-recording SIFT tracks triangulated, before/after links by descriptor votes, rigid RANSAC per SAM object -> distance moved (bootstrap 90%) + non-rigid residual = deformation map, peak location in words; judged against each point's own triangulation covariance (+ one recording-misalignment term from the static majority), not a two-halves null (Fin: halves cannot show deformation); `motion_map.png`, `motion.json`, viewer layers | [src/uncertainscene/change/motion3d.py](src/uncertainscene/change/motion3d.py) |
| 2026-10-11 | change | Fin: CLIP must work, splats better, moved-object splats separately (assets), node-structured objects (nodes, convex hull, stiffness/interaction) fitting with SAM. Done (not yet run): CLIP checked on transformers 4.57 and 5.19, SAM 2 (GitHub main) mask path run with a random-weight model; `change/assets.py`: per SAM 2 object nodes (farthest points, weights), kNN edges with measured strain (from motion3d links, uncertainty carried) -> relative stiffness, convex hull (.obj), contacts with 'rests on'; `assets/` + `scene_graph.json` per main step; objects saved per reconstruction so the splat step cuts per-object splats of changed objects (`object_splats/`, also in the splat viewer) | [src/uncertainscene/change/assets.py](src/uncertainscene/change/assets.py) |
| 2026-10-11 | change | Fin: stiffness should be queried (VLM or CLIP), named Rigidity Copilot. Done (not yet run): `change/rigidity.py` asks CLIP (the labels' own image embeddings) what each object is made of (10 materials with Young's modulus ranges) and soft vs rigid -> rigidity 0-1, expected E with range; asset edges get k ~ E*L (N/m) with range; measured strain kept as a check (`copilot_check` agrees/disagrees); report table | [src/uncertainscene/change/rigidity.py](src/uncertainscene/change/rigidity.py) |
| 2026-10-12 | change | Run 3 (`2026-10-11_scene01-objects-fixed`) partly ran: main light-a pair computed everything (SAM 2 177/221 objects, CLIP labels, Copilot, 32 changes incl. cap moved 43 cm, 60 assets) then crashed writing its report (0 feature-motion links); splat step never reported back. Overview, downsides and research directions in RESULTS.md. Fixed: report crash, bundle adjustment divergence (8 -> 341 px; now guarded, never worse than VGGT), motion gate follows the measured reprojection spread; notebook retries failed steps | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-12 | change | Fin: fix over-splitting urgently with the poses, Copilot on whole objects, deformation map working. Done (not yet run): SAM 2 video predictor tracks each object through each recording (seeded by the mask generator every 8th tracked frame, new objects added where they appear), tracks merged only when never visible together; CLIP labels and Rigidity Copilot use the mean embedding of up to 4 views; Copilot range only from materials above 10%; pose refinement starts with resection-intersection; feature motion keeps links with rough poses (test: old gate 2 links at 1.2 deg pose error, now 4449) | [src/uncertainscene/change/segment.py](src/uncertainscene/change/segment.py) |
| 2026-10-12 | change | Run 4 (Colab colab-fa00, 12:38-13:23): bundle adjustment worked on real frames (8.26 -> 0.91 px median, cameras turned 1.13 deg); SAM 2 tracking then hit the 92% RAM guard (it kept float logits of every object in every frame, ~2.4 MB each), step interrupted. Fixed: tracking keeps only packed masks on discovery frames and small masks + wobble on kept frames, batches of 15 (simulated 66 objects x 88 frames: +0.65 GB) | [src/uncertainscene/change/segment.py](src/uncertainscene/change/segment.py) |
| 2026-10-12 | recon | Fin: minimise reprojection error. Run 4 left 146 of 735 frames (fewer than 30 adjusted points) on VGGT's ~8 px poses. Now: those frames get a second, wider feature-matching pass; after the adjustment each is resected (PnP + RANSAC, then Levenberg-Marquardt) against points from the other frames, or, with under 8 matches, moved by its time neighbours' correction; then one joint adjustment over every frame. Report adds p90, per-frame worst and resected/interpolated counts; pose cache renamed so the rerun recomputes. Synthetic test: sparse frames 12-26 px before, 0.5-0.7 px after | [src/uncertainscene/recon/poses.py](src/uncertainscene/recon/poses.py) |
| 2026-10-12 | recon | Fin: if a pose is really uncertain it must not impact much. Each frame now gets a measured pose uncertainty from bundle adjustment (residual spread x sqrt(6 / points), plus any residual above the scene's; frames with under 8 points get VGGT's own error level) and it flows everywhere: object positions (sideways term in each view's covariance, so the Kalman fusion down-weights it), feature motion (weighted triangulation, per-observation covariance), change votes (weight 1 / (1 + sigma_px^2) for source and target frame) and splat training (shaky frames drawn less often). Tests: a 5 cm-off view from a shaky camera moves the fused position < 1 mm; a 6 px-off shaky frame moves a feature point a third as much | [src/uncertainscene/recon/poses.py](src/uncertainscene/recon/poses.py) |
| 2026-10-10 | change | Gaussian splats added as phase 1b (not yet run; Fin chose one pair first): gsplat splat of light before a and of light after a, started from the VGGT points with per-camera pose correction; render quality (PSNR) on held-out frames; before vs after rendered from the same held-out camera, pixels flagged above the 99th percentile of the no-change render error; `.splat` exports and splat centres as dense clouds in the viewer | [src/uncertainscene/experiments/splats.py](src/uncertainscene/experiments/splats.py) |
| 2026-10-10 | change | Denser v2 (not yet run): frames re-extracted at 6 fps (~370 per recording, `data/frames_6fps/`); SAM 2 on 44 views per recording with a 32x32 point grid and smaller minimum mask, so more and smaller objects get labelled; viewer: ~6x more points per recording, each object painted one colour, filled object boxes, 3D loaded per run; robot heat maps now assume a mobile base that drives to any free floor spot, 1.0 m reach from a 0.75 m mount (fixed base kept as a robot variant); viewer point clouds no longer offset from the objects (decoding bug) | [src/uncertainscene/change/fields.py](src/uncertainscene/change/fields.py), [src/uncertainscene/viz/site.py](src/uncertainscene/viz/site.py) |
| 2026-10-10 | change | Results made readable and object position uncertainty added (not yet run): every run named after what it compares and the question it answers (phases 1-main ... 6-repeat); one results file `results/change/RESULTS.md` and a Plotly-free 3D viewer (one recording alone or the changes, heat maps, object boxes, position ranges); per-object 90% position range from VGGT depth confidence + SAM 2 mask wobble fused per view (Kalman filter) with an NIS consistency check and the views-disagreeing range as a cross-check; new phase 2-position (1-3 clips of one scene; 2 clips per state before/after) | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-10 | change | First overnight run (run 2026-10-10_scene01-heatmaps, 179 steps, ~5 h): point-level detection could not separate real changes from noise (2.3% vs 2.2% changed; guitar, neck pillow, books missed); dark recordings ~2x noisier; floor fit unreliable. Pivot (v2, not yet run): every frame per recording via chunked VGGT, gravity + floor from horizontal surfaces, scale from phone height + known object sizes, SAM 2 objects merged across views in 3D, CLIP label for every object, object-level change decisions with label-free thresholds, AUROC separation in summaries | [summary v1](results/change/2026-10-10_scene01-heatmaps/summary_session_colab-dbb8/summary.md), [src/uncertainscene/experiments/scene01.py](src/uncertainscene/experiments/scene01.py) |
| 2026-10-13 | change | Run 5 (main light-a pair, colab-a68c): poses 8.26 -> 0.66 px median (p90 1.98, worst frame 2.33; 117 sparse frames resected, 42 interpolated). SAM 2 tracking worked in memory: 139 objects (81 + 58) instead of 398 pieces, 65 min. But saving the mask cache crashed, so objects, labels, assets and motion were lost (fell back to 0 point clusters); splat step cut off by Colab. Fixed the cache; failed objects now fail the step (retried) and each attempt clears stale outputs; main step marked failed for rerun | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-13 | change | Fin: how to fix the metric scale (3.0 m/unit in run 3, 3.6 in run 5, from the phone-height cue with 3% floor points). Added a third cue: Depth Anything V2 Metric (indoor) depth / VGGT depth on confident pixels of 80 frames, pose-weighted median; uncertainty = frames spread / sqrt(n) plus a 10% shared model-bias prior that more frames cannot shrink. All cues (phone height, metric depth, object sizes) fused by inverse variance; cues more than 2 sigma apart widen the range to cover them. Used in main, clips and splat steps; a missing model leaves the other cues | [src/uncertainscene/change/metric.py](src/uncertainscene/change/metric.py) |
| 2026-10-13 | change | Fin: phone height above the floor changes all the time, so drop it; no direct measurements; pick the most likely scale from metric depth plus object-size predictions. Scale is now the mode of the joint posterior over log scale: metric depth (Gaussian) times one vote per object of the before recording, each a mixture over its top-3 CLIP labels weighted by probability, with a 10% flat share for wrong labels. Sizes from a new database (change/sizes.py: largest and second dimension plus product-to-product spread for ~90 labels); extents corrected for the 5-95% span. Phone height only reported. Tests: a cushion mislabelled as a mug does not move the mode; a guitar-or-ukulele vote is resolved by metric depth | [src/uncertainscene/change/sizes.py](src/uncertainscene/change/sizes.py) |
| 2026-10-13 | infra | Fin: no more bugs, crashes or memory issues before the rerun. Static check (no undefined names), full review of every change since run 4, and a new end-to-end test with only the networks faked (real mask cache, tracking glue, poses, motion, scale). Fixed: cache folder missing when saving masks; pose-weighted votes made 2 views count as fewer than 2 (gates now count views, weights only set the gone share); per-frame fallback masks were cached under the tracked name (a retry would never track); np.trapezoid needs numpy 2 (fallback added, metric cue fully guarded); empty crop list crashed CLIP; wall-sized fragments voted on scale; report still credited phone height. Memory/time: depth model and SAM 2 freed from the GPU after use, new-object check at quarter resolution, step budgets raised to the measured 65 min of tracking (+1 GB RAM); transformers >= 4.45 for the metric depth head | [tests/test_pivot.py](tests/test_pivot.py) |
| 2026-10-13 | infra | Run 6 main step ended at 8.4 of 12.7 GB RAM, so the splat step and the next main pairs (6 GB each) were not started and the queue skipped to the position step. Every step now drops all loaded networks (VGGT, DINOv2, CLIP, SAM 2, depth model), collects garbage and returns freed heap to the system (malloc_trim) when it ends | [src/uncertainscene/experiments/poc.py](src/uncertainscene/experiments/poc.py) |
| 2026-10-13 | change | Run 6 (main light-a pair, colab-5f93, 94dfe8c) written up: poses still 0.66 px; objects whole (29 + 29, plus 44 structures); cap moved 42 cm, bat, cup and card added; 58 assets with Copilot stiffness; scale 2.05 m/unit from metric depth 2.49 and objects 1.45 (4 sigma apart). Bad: positions overconfident (NIS 29058), recordings misaligned ~14 cm so unchanged objects "move", duplicate tracks (guitar x8), some wrong labels; splat not started (RAM, fixed in 9ec002c). Each object viewable on its own in the Change Viewer; future runs save a 480 px picture per object in assets/ | [results/change/RESULTS.md](results/change/RESULTS.md) |
| 2026-10-13 | change | Fin: why are the recordings 14 cm apart, how are they matched, can it improve? They are put in one frame by VGGT chunks sharing anchor frames from both recordings, then one joint bundle adjustment (0.66 px on its kept points). The 14 cm is the motion step's catch-all term: it triangulated each recording on its own with a gate of 2.5x the median error (run 6: 11.7 px "noise" in the before recording against 0.66 px), and linked before/after points by appearance only, so noisy points and wrong links read as misalignment. Fixes: one focal length for both recordings in the adjustment (same phone and lens); motion gate = 3 px plus each frame's pose uncertainty; links checked against a fundamental matrix between the two frames (static); a similarity fitted to static links (RANSAC then uncertainty-weighted Cauchy refits, kept at identity unless it explains clearly more points) lines the after points up before measuring motion; the misalignment term is measured on static links only. New report rows: per-recording error, points seen in both recordings, share of observations kept, alignment correction | [src/uncertainscene/change/motion3d.py](src/uncertainscene/change/motion3d.py) |
| 2026-10-13 | infra | Colab session colab-7e0e (17:30, on 4f0a0dd) ran setup, then the splat step from 17:34; it never finished, failed or reported, and the session's final summary step never ran, so the session ended mid-splat (disconnect or killed kernel) without a trace. Now every running step pushes its log so far to logs/live_<session>.txt every 10 minutes (git calls locked against the main thread), so a dead session still shows how far it got. Released the splat and position claims of the two dead sessions so the next Run all starts them at once. Before/after feature matches use a stricter ratio test (0.75) | [src/uncertainscene/infra/runner.py](src/uncertainscene/infra/runner.py) |
| 2026-10-13 | infra | Correction to the row above: Fin's log shows the guard did stop colab-7e0e's splat step on RAM (82% at 17:48:37, 97% at 17:48:56) while gsplat compiled its CUDA code with its default 10 parallel compiler jobs, on top of ~6 GB still held from the reconstruction and pose refinement (the reconstruction alone held 4.7 GB in run 3). The guard's interrupt cannot land while the main thread waits on the compiler, so RAM kept climbing and the session died before it could record the step. Fix: gsplat is built in the notebook setup, before any network loads, in a separate process with jobs sized to free RAM (2.5 GB per job, at most 4; 3 on a fresh 12.7 GB Colab), watched every second and stopped and retried with half the jobs if RAM passes 88%; the main thread stays in Python so the guard can still interrupt and the build is killed with it. The splat step frees VGGT after the reconstruction and everything else before training, then calls the same build (a no-op when setup built it). recon/splat.py caps MAX_JOBS at 2 if gsplat ever compiles in-process | [src/uncertainscene/recon/gsplat_build.py](src/uncertainscene/recon/gsplat_build.py) |
| 2026-10-15 | change | Fin: splats parked; objects first (whole-object segmentation in the point cloud, labels, same-object matching, context, affordance heat maps, movable assets, continuity through the video). Done (not yet run): YOLOE (text-prompted household vocabulary, weights from the ultralytics GitHub release) finds whole objects; its instances seed SAM 2 tracks before the automatic masks, automatic masks inside a detected object are not seeded, every kept frame gives each track label votes, and a track that keeps lying inside another track's instance is joined to it (run 6 had the guitar in up to 8 pieces). Labels come from the detector votes over many views (CLIP stays as fallback and for the embedding); tracks the detector names the same rejoin across gaps in the video with a weaker 3D overlap, differently named ones never join; per-object continuity (tracks, unbroken runs) and `object_continuity.png`. Same-object links agree both ways (run 6: neck pillow removed in A yet B's partner). Context `relations.json` (on top of / next to from the point clouds); grasp / push / place heat maps from the objects (size vs gripper, nothing on top, rigidity); every asset now has its coloured point cloud (.ply, object frame) and a pose (world = R local + t) so it can be moved and turned. Runs as new step `1c-objects` on the pilot pair (first in the next session). Tests: tracker fakes for seeds/parts/rejoin, relations, affordance, partner consistency | this commit |
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
