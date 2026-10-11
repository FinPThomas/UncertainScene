# UncertainScene results: scene01

One file for every run so far. *Findings so far* is written by hand; everything after it is regenerated from the run folders (`python -m uncertainscene.eval.collate results/change`).

**Interactive viewer:** https://claude.ai/artifact/7Ys2rRdQCHmw1cEJZeZjSy (pick a run; view one recording alone or the changes between them; heat maps, object boxes and position ranges).

<!-- notes -->
## Findings so far

Last updated 2026-10-11. No answer key yet, so nothing below is scored against ground truth; no-change pairs (static, lighting only) give the false-change rate.

**Version 1 (run `2026-10-10_scene01-heatmaps`, 28 frames per recording, point-level).**

1. It cannot tell change from noise yet. No-change pairs flag about as much of the scene (2.2%) as real-change pairs (2.3%).
2. Of the changes visible in the light pairs it finds the hat and cup (`a` takes) and the glass vase, table tennis bat and cap (`b` takes). It misses the guitar, neck pillow and book stack.
3. Labels are per changed cluster only (CLIP on a crop), so most objects in the scene have no label, and some clusters get nonsense labels (e.g. "sofa removed").
4. Dark recordings are about twice as noisy as light ones (median surface uncertainty 21 vs 10 cm). Lighting alone does not add false changes.
5. Floor and scale failed in the dark pairs (259 and 69 m per unit, uncertainty in metres), so heights, reachability and sizes there are meaningless. In light pairs scale is plausible (2.2-2.5 m per unit) but reachable share is 1-14%.
6. Cause: the change threshold comes from how much one recording disagrees with itself (about 13 cm at 28 frames), so thin objects fall under it, and the floor fit used too few points.

**Version 2, first attempt (run `2026-10-10_scene01-objects`, 11 steps, 13:38-19:07, then Colab disconnected). Object results are invalid.**

1. Every step lost its objects: CLIP labelling crashed on the newer transformers library on Colab, so each step fell
   back and reports 0 changed objects. Ignore the object numbers of this run. Fixed.
2. Views of an object were almost never merged (1107 of 1122 objects seen in a single view): two views of the same
   surface disagree in depth by several cm, more than the 1.2 cm grid used to match them. Merging now uses a grid
   sized from VGGT's own depth uncertainty plus a colour check. Fixed.
3. The *Where objects are* clips steps did not start (their RAM estimate was too high for what was free). Lowered.
4. **Still valid from this run** (8 pair steps, every frame, ~370 per recording):
   - **Point-level change still can't separate change from noise:** pairs where nothing moved flag 3.5-4.8% of the
     surface, pairs with real changes 4.2-5.8%. The ranges overlap, so object-level decisions are still needed.
   - **Surface uncertainty:** 8-12 cm median in light recordings, 13-14 cm when both are dark.
   - **Robot reach (mobile base, 1.0 m arm):** 47-54% of the surface is reachable, against 1-14% with the old fixed
     robot. Approachable 9-17%; interactable stays under 1%.
   - **Floor and scale:** only about 3% of points fit the floor, and scale comes from the phone height alone (the
     object-size cue needs labels), so metric sizes are rough.
5. **Gaussian splats (valid):** both splats rendered held-out frames at only ~17.7 dB (blurry), so before vs after
   from the same camera flagged 2.0% of pixels against 1% when nothing changed. The comparison still highlights the
   table tennis bat cushion and a card added on the sofa. Next run trains 20 000 steps instead of 7 000 and compares
   only views the other recording also saw.

**Version 2 (run `2026-10-11_scene01-objects-fixed`, next).** Same steps with the fixes above. What version 2 adds:

- Every frame of each recording (~370 at 6 fps), reconstructed in GPU-sized chunks.
- Floor and metric scale from gravity, the phone height and the sizes of recognised objects.
- SAM 2 outlines and a CLIP label for every object, not only changed ones; changes decided per object.
- **Where each object is, and how sure.** Each view's measurement of an object's centre carries VGGT's own depth
  confidence and SAM 2's mask wobble; a Kalman filter fuses the views, so the 90% range narrows with every
  informative view and every extra clip. A consistency check (3 = honest) says whether to trust it, and the range
  from views disagreeing is shown alongside. Phase *Where objects are* measures this with 1, 2 and 3 clips.
- Gaussian splats of the light before/after pair, compared from the same cameras.

Results land in this file and the viewer as each phase finishes.
<!-- /notes -->

## How to read this

Every run compares two recordings of the sofa scene (or, in *Where objects are*, several clips). What a run can tell us depends on whether anything really moved between its recordings:

| Kind of run | What it compares | Question it answers | A good result |
|---|---|---|---|
| **Nothing moved, same lighting** | two takes of the same scene, same lighting | How many false alarms does it raise when nothing changed? | no changed objects |
| **Nothing moved, light vs dark** | the same scene, light vs dark | Does a lighting change alone cause false alarms? | no changed objects |
| **Objects moved, same lighting** | before vs after objects were moved, same lighting | Does it find the objects that really moved, were added or removed? | the real changes found, nothing else |
| **Objects moved and lighting changed** | before vs after, and the lighting changed too | Does it still find the real changes when the lighting also changes? | the real changes found, nothing else |

Recordings: `dark_before_a` = dark, before, take a; `dark_before_b` = dark, before, take b; `dark_before_c` = dark, before, take c; `light_before_a` = light, before, take a; `light_before_b` = light, before, take b; `light_after_a` = light, after, take a; `light_after_b` = light, after, take b.

Runs are grouped by phase (what they try to find out). Folder names say what they compare, e.g. `1-main_moved_light-before-a_vs_light-after-a`. The first overnight run used older short names (`p1_lightBa_vs_lightAa` = light before a vs light after a); its titles below are written out.

## Run `2026-10-10_scene01-objects` (version 2: SAM 2 objects, every frame)

9 finished runs. Folder: [2026-10-10_scene01-objects](2026-10-10_scene01-objects/).

> **Object results of this run are invalid.** CLIP labelling crashed in every step and views of an object were never merged, so changed objects (all 0) and position ranges are not real results. Surface shares, scale, heat maps and the Gaussian splats are unaffected. Fixed for the next run.

### Headline: does it tell real changes from noise?

Changed objects: invalid in this run (see the note above).

| Kind of run | Runs | Share of the surface flagged as changed, median (range) |
|---|---|---|
| Nothing moved, same lighting | 3 | 4.60% (3.55% to 4.82%) |
| Nothing moved, light vs dark | 2 | 3.75% (3.60% to 3.90%) |
| Objects moved, same lighting | 2 | 5.00% (4.24% to 5.76%) |
| Objects moved and lighting changed | 1 | 5.29% (5.29% to 5.29%) |

### Main comparisons

*One or two pairs of every kind, with every output (objects, heat maps, 3D, scene report).* (6 runs)

| Run | Changes found | Objects located to (90%) | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|---|
| Objects moved and lighting changed: dark, before, take a vs light, after, take a | invalid | invalid | 3.950 | 11.0 | [report](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/report.md) · [objects](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/objects.png) · [position map](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_moved-and-light-changed_dark-before-a_vs_light-after-a/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a | invalid | invalid | 3.206 | 9.6 | [report](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/report.md) · [objects](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/objects.png) · [position map](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_moved_light-before-a_vs_light-after-a/scene_report.md) |
| Objects moved, same lighting: light, before, take b vs light, after, take b | invalid | invalid | 2.186 | 9.8 | [report](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/report.md) · [objects](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/objects.png) · [position map](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_moved_light-before-b_vs_light-after-b/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take a vs light, before, take a | invalid | invalid | 3.361 | 8.2 | [report](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/report.md) · [objects](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/objects.png) · [position map](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_no-change-light-vs-dark_dark-before-a_vs_light-before-a/scene_report.md) |
| Nothing moved, same lighting: dark, before, take a vs dark, before, take b | invalid | invalid | 5.104 | 13.1 | [report](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/report.md) · [objects](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/objects.png) · [position map](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_no-change_dark-before-a_vs_dark-before-b/scene_report.md) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b | invalid | invalid | 2.862 | 11.7 | [report](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/report.md) · [objects](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/objects.png) · [position map](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/position_map.png) · [heatmaps](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/object_overlays.png) · [scene report](2026-10-10_scene01-objects/1-main_no-change_light-before-a_vs_light-before-b/scene_report.md) |

### Gaussian splats

*Do splats of each recording render well, and does comparing before and after from the exact same camera separate real changes from noise?* (1 runs)

| Run | Render quality, held-out frames (PSNR before / after) | Changed pixels, same camera (1% = no change) | Depth differs > 5 cm | Details |
|---|---|---|---|---|
| Gaussian splats: light, before, take a vs light, after, take a | 17.74 / 17.85 dB | 2.0% | 76.9% | [report](2026-10-10_scene01-objects/1b-splat_moved_light-before-a_vs_light-after-a/report.md) · [splat compare](2026-10-10_scene01-objects/1b-splat_moved_light-before-a_vs_light-after-a/splat_compare.png) · [splat orbit](2026-10-10_scene01-objects/1b-splat_moved_light-before-a_vs_light-after-a/splat_orbit.gif) |

### Every other pair

*More pairs of each kind, so false alarms and real detections can be counted properly.* (2 runs)

| Run | Changes found | Objects located to (90%) | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|---|
| Nothing moved, light vs dark: dark, before, take a vs light, before, take b | invalid | invalid | 2.731 | 10.3 | [report](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/report.md) · [objects](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/objects.png) · [position map](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/position_map.png) · [heatmaps](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/object_overlays.png) · [scene report](2026-10-10_scene01-objects/3-allpairs_no-change-light-vs-dark_dark-before-a_vs_light-before-b/scene_report.md) |
| Nothing moved, same lighting: dark, before, take a vs dark, before, take c | invalid | invalid | 5.248 | 14.4 | [report](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/report.md) · [objects](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/objects.png) · [position map](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/position_map.png) · [heatmaps](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/heatmaps.png) · [object overlays](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/object_overlays.png) · [scene report](2026-10-10_scene01-objects/3-allpairs_no-change_dark-before-a_vs_dark-before-c/scene_report.md) |

## Run `2026-10-10_scene01-heatmaps` (version 1, first overnight run: point-level, 28 frames per recording)

163 finished runs. Folder: [2026-10-10_scene01-heatmaps](2026-10-10_scene01-heatmaps/).

### Headline: does it tell real changes from noise?

| Kind of run | Runs | Share of the surface flagged as changed, median (range) |
|---|---|---|
| Nothing moved, same lighting | 5 | 2.17% (1.82% to 2.44%) |
| Nothing moved, light vs dark | 6 | 2.24% (1.81% to 3.13%) |
| Objects moved, same lighting | 4 | 2.29% (1.16% to 3.07%) |
| Objects moved and lighting changed | 6 | 2.87% (1.59% to 3.90%) |

Runs with real changes flag about as much surface as runs without, so version 1 could not tell real changes from noise.


### Main comparisons

*The six main pairs with every output.* (6 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Nothing moved, same lighting: dark, before, take a vs dark, before, take b | blanket added, towel appearance, stack of books removed, blanket appearance | 259.440 | 534.1 | [report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_darkBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_darkBa_vs_darkBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_darkBa_vs_darkBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_darkBb/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a | keys added, coaster removed, towel removed, jacket appearance | 68.742 | 172.5 | [report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightAa/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take a vs light, before, take a | cushion added, towel removed, baseball cap appearance | 5.959 | 11.3 | [report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightBa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightBa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_darkBa_vs_lightBa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a | hat appearance, cup appearance | 2.524 | 6.2 | [report](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightAa/scene_report.md) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b | hat appearance, guitar removed | 2.459 | 5.9 | [report](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_lightBa_vs_lightBb/scene_report.md) |
| Objects moved, same lighting: light, before, take b vs light, after, take b | sofa removed, baseball cap removed, table tennis bat appearance, glass vase appearance, rug added | 2.152 | 7.2 | [report](2026-10-10_scene01-heatmaps/p1_lightBb_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1_lightBb_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1_lightBb_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1_lightBb_vs_lightAb/scene_report.md) |

### Light vs dark

*Every light/dark contrast, for the lighting report.* (7 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Nothing moved, same lighting: dark, before, take b vs dark, before, take c | jacket appearance, cup added, cup appearance, book appearance | 20.587 | 54.0 | [report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_darkBc/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_darkBc/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_darkBc/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_darkBc/scene_report.md) |
| Objects moved and lighting changed: dark, before, take b vs light, after, take b | pillow removed, hat appearance, hat removed, guitar removed, vase added | 16.469 | 59.9 | [report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightAb/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take b vs light, before, take b | hat appearance, baseball cap added, blanket removed, book appearance, towel appearance, towel added | 2.012 | 8.0 | [report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_darkBb_vs_lightBb/scene_report.md) |
| Objects moved and lighting changed: dark, before, take c vs light, after, take a | hat appearance, mug appearance, guitar added, guitar added | 3.943 | 12.2 | [report](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightAa/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take c vs light, before, take a | guitar added, baseball cap removed, mug appearance, baseball cap appearance | 6.637 | 14.3 | [report](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightBa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightBa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_darkBc_vs_lightBa/scene_report.md) |
| Nothing moved, same lighting: light, after, take a vs light, after, take b | blanket appearance, shoe appearance | 2.379 | 7.2 | [report](2026-10-10_scene01-heatmaps/p1b_lightAa_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_lightAa_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_lightAa_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_lightAa_vs_lightAb/scene_report.md) |
| Objects moved, same lighting: light, before, take b vs light, after, take a | guitar added | 2.013 | 5.8 | [report](2026-10-10_scene01-heatmaps/p1b_lightBb_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p1b_lightBb_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p1b_lightBb_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p1b_lightBb_vs_lightAa/scene_report.md) |

### Robot variants

*Reach and approach maps for different robots.* (16 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (heat maps of the scene before the change) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 8.9 | [report](2026-10-10_scene01-heatmaps/p3_before_fields_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_before_fields_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_before_fields_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_before_fields_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (heat maps of the scene before the change) | hat appearance, cup appearance | 2.527 | 4.4 | [report](2026-10-10_scene01-heatmaps/p3_before_fields_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_before_fields_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_before_fields_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_before_fields_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (bigger gripper) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 11.3 | [report](2026-10-10_scene01-heatmaps/p3_big_gripper_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_big_gripper_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_big_gripper_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_big_gripper_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (bigger gripper) | hat appearance, cup appearance | 2.527 | 6.3 | [report](2026-10-10_scene01-heatmaps/p3_big_gripper_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_big_gripper_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_big_gripper_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_big_gripper_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (long arm (1.3 m reach)) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 11.3 | [report](2026-10-10_scene01-heatmaps/p3_long_arm_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_long_arm_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_long_arm_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_long_arm_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (long arm (1.3 m reach)) | hat appearance, cup appearance | 2.527 | 6.3 | [report](2026-10-10_scene01-heatmaps/p3_long_arm_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_long_arm_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_long_arm_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_long_arm_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (robot base 0.6 m closer) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 11.3 | [report](2026-10-10_scene01-heatmaps/p3_mobile_closer_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_mobile_closer_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_mobile_closer_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_mobile_closer_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (robot base 0.6 m closer) | hat appearance, cup appearance | 2.527 | 6.3 | [report](2026-10-10_scene01-heatmaps/p3_mobile_closer_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_mobile_closer_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_mobile_closer_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_mobile_closer_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (short arm (0.7 m reach)) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 11.3 | [report](2026-10-10_scene01-heatmaps/p3_short_arm_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_short_arm_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_short_arm_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_short_arm_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (short arm (0.7 m reach)) | hat appearance, cup appearance | 2.527 | 6.3 | [report](2026-10-10_scene01-heatmaps/p3_short_arm_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_short_arm_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_short_arm_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_short_arm_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (assuming the phone was held lower) | keys added, coaster removed, towel removed, baseball cap appearance | 3.850 | 9.7 | [report](2026-10-10_scene01-heatmaps/p3_short_operator_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_short_operator_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_short_operator_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_short_operator_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (assuming the phone was held lower) | hat appearance, cup appearance | 2.153 | 5.5 | [report](2026-10-10_scene01-heatmaps/p3_short_operator_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_short_operator_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_short_operator_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_short_operator_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (arm mounted at table height) | keys added, coaster removed, towel removed, baseball cap appearance | 4.519 | 11.3 | [report](2026-10-10_scene01-heatmaps/p3_table_mount_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_table_mount_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_table_mount_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_table_mount_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (arm mounted at table height) | hat appearance, cup appearance | 2.527 | 6.3 | [report](2026-10-10_scene01-heatmaps/p3_table_mount_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_table_mount_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_table_mount_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_table_mount_lightBa_vs_lightAa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (assuming the phone was held higher) | keys added, coaster removed, towel removed, baseball cap appearance | 5.189 | 13.0 | [report](2026-10-10_scene01-heatmaps/p3_tall_operator_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_tall_operator_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_tall_operator_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_tall_operator_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (assuming the phone was held higher) | hat appearance, cup appearance | 2.901 | 7.0 | [report](2026-10-10_scene01-heatmaps/p3_tall_operator_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p3_tall_operator_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p3_tall_operator_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p3_tall_operator_lightBa_vs_lightAa/scene_report.md) |

### Ablations

*Does the visibility test or the appearance test matter?* (6 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Nothing moved, light vs dark: dark, before, take a vs light, before, take a (plain nearest-point comparison, no visibility test) | none | 52.777 | 100.0 | [report](2026-10-10_scene01-heatmaps/p4_naive_nn_darkBa_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_naive_nn_darkBa_vs_lightBa/heatmaps.png) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (plain nearest-point comparison, no visibility test) | none | 2.517 | 6.6 | [report](2026-10-10_scene01-heatmaps/p4_naive_nn_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_naive_nn_lightBa_vs_lightAa/heatmaps.png) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b (plain nearest-point comparison, no visibility test) | none | 2.347 | 5.7 | [report](2026-10-10_scene01-heatmaps/p4_naive_nn_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_naive_nn_lightBa_vs_lightBb/heatmaps.png) |
| Nothing moved, light vs dark: dark, before, take a vs light, before, take a (without the appearance test) | baseball cap added, coaster removed | 52.777 | 100.0 | [report](2026-10-10_scene01-heatmaps/p4_no_appearance_darkBa_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_no_appearance_darkBa_vs_lightBa/heatmaps.png) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (without the appearance test) | none | 2.517 | 6.6 | [report](2026-10-10_scene01-heatmaps/p4_no_appearance_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_no_appearance_lightBa_vs_lightAa/heatmaps.png) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b (without the appearance test) | guitar removed | 2.347 | 5.7 | [report](2026-10-10_scene01-heatmaps/p4_no_appearance_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p4_no_appearance_lightBa_vs_lightBb/heatmaps.png) |

### Every other pair

*Remaining pairs for more evidence per kind.* (9 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Nothing moved, same lighting: dark, before, take a vs dark, before, take c | throw added, mug appearance, bag removed, towel appearance | 5.533 | 10.1 | [report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_darkBc/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBa_vs_darkBc/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBa_vs_darkBc/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_darkBc/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take b | bag added, table tennis bat appearance, floor appearance, baseball cap appearance, bag appearance, book added | 5.378 | 13.6 | [report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightAb/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take a vs light, before, take b | blanket added | 4.088 | 9.5 | [report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBa_vs_lightBb/scene_report.md) |
| Objects moved and lighting changed: dark, before, take b vs light, after, take a | blanket appearance | 1.920 | 6.2 | [report](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightAa/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take b vs light, before, take a | throw appearance, guitar added, cup appearance, sofa added | 2.033 | 4.4 | [report](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightBa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightBa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBb_vs_lightBa/scene_report.md) |
| Objects moved and lighting changed: dark, before, take c vs light, after, take b | pillow removed, table tennis bat appearance, mug appearance, jacket removed, guitar removed, floor appearance | 6.262 | 21.1 | [report](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightAb/scene_report.md) |
| Nothing moved, light vs dark: dark, before, take c vs light, before, take b | towel appearance, blanket appearance, guitar added, mug appearance, towel added | 3.737 | 10.6 | [report](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_darkBc_vs_lightBb/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take b | cup appearance, baseball cap appearance, jacket removed | 2.480 | 6.8 | [report](2026-10-10_scene01-heatmaps/p5_lightBa_vs_lightAb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_lightBa_vs_lightAb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_lightBa_vs_lightAb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_lightBa_vs_lightAb/scene_report.md) |
| Objects moved, same lighting: light, after, take a vs light, before, take a (reversed direction (after compared with before)) | baseball cap appearance, cup appearance | 2.375 | 4.4 | [report](2026-10-10_scene01-heatmaps/p5_reversed_lightAa_vs_lightBa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p5_reversed_lightAa_vs_lightBa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p5_reversed_lightAa_vs_lightBa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p5_reversed_lightAa_vs_lightBa/scene_report.md) |

### How many frames are enough

*Fewer frames per recording.* (9 runs)

| Run | Changes found | Scale (m/unit) | Surface uncertainty (cm) | Details |
|---|---|---|---|---|
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (16 frames per recording) | none | 11.656 | 21.2 | [report](2026-10-10_scene01-heatmaps/p6_n16_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n16_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n16_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n16_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (16 frames per recording) | cup appearance, coaster appearance | 2.864 | 4.8 | [report](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightAa/scene_report.md) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b (16 frames per recording) | shoe appearance | 2.399 | 6.2 | [report](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n16_lightBa_vs_lightBb/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (24 frames per recording) | rug added, baseball cap appearance, coaster removed, table tennis bat appearance, floor removed | 7.288 | 17.5 | [report](2026-10-10_scene01-heatmaps/p6_n24_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n24_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n24_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n24_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (24 frames per recording) | coaster appearance, baseball cap appearance | 2.468 | 5.7 | [report](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightAa/scene_report.md) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b (24 frames per recording) | sofa added | 2.318 | 6.0 | [report](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n24_lightBa_vs_lightBb/scene_report.md) |
| Objects moved and lighting changed: dark, before, take a vs light, after, take a (44 frames per recording) | keys added, coaster removed, towel removed, jacket appearance | 68.742 | 172.5 | [report](2026-10-10_scene01-heatmaps/p6_n44_darkBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n44_darkBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n44_darkBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n44_darkBa_vs_lightAa/scene_report.md) |
| Objects moved, same lighting: light, before, take a vs light, after, take a (44 frames per recording) | hat appearance, cup appearance | 2.524 | 6.2 | [report](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightAa/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightAa/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightAa/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightAa/scene_report.md) |
| Nothing moved, same lighting: light, before, take a vs light, before, take b (44 frames per recording) | hat appearance, guitar removed | 2.459 | 5.9 | [report](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightBb/report.md) · [heatmaps](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightBb/heatmaps.png) · [overlays](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightBb/overlays.png) · [scene report](2026-10-10_scene01-heatmaps/p6_n44_lightBa_vs_lightBb/scene_report.md) |

### Repeatability

*Same pairs with different frame picks.* (110 runs)

Too many runs to list; by kind:

| Kind of run | Runs | Changed surface, median |
|---|---|---|
| Nothing moved, same lighting | 25 | 2.47% |
| Objects moved and lighting changed | 30 | 2.38% |
| Nothing moved, light vs dark | 30 | 1.84% |
| Objects moved, same lighting | 25 | 2.44% |

### Light vs dark (all runs)

| Group | Pairs | % observed points flagged as changed | % flagged as appearance change | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm | change objects found |
|---|---|---|---|---|---|---|---|
| static-light | 11 | 2.04 (0.73-2.49) | 1.17 (0.73-1.27) | 1.07 (0.99-1.09) | 86.69 (85.56-86.95) | 9.79 (5.92-11.44) | 2.00 (0.00-4.00) |
| static-dark | 19 | 2.44 (1.22-3.18) | 1.31 (0.61-2.24) | 1.04 (0.93-1.10) | 86.83 (84.97-90.00) | 21.41 (9.62-534.08) | 4.00 (0.00-6.00) |
| lighting | 36 | 1.84 (0.70-3.13) | 1.15 (0.70-1.52) | 1.02 (0.99-1.10) | 86.66 (81.53-90.64) | 14.67 (4.40-32.31) | 1.00 (0.00-7.00) |
| change-light | 29 | 2.44 (0.97-4.47) | 1.28 (0.92-2.35) | 1.07 (0.96-1.18) | 85.66 (82.70-88.63) | 7.39 (5.20-29.97) | 2.00 (0.00-6.00) |
| change+lighting | 36 | 2.47 (1.08-3.90) | 1.64 (0.79-3.06) | 1.04 (0.95-1.13) | 84.36 (80.29-88.53) | 16.68 (5.93-1154.60) | 3.00 (0.00-8.00) |

Full lighting report: [2026-10-10_scene01-heatmaps/summary_session_colab-dbb8/lighting.md](2026-10-10_scene01-heatmaps/summary_session_colab-dbb8/lighting.md)

