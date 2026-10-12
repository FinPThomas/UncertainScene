# Objects moved, same lighting: light, before, take a vs light, after, take a

**What this run tests:** Does it find the objects that really moved, were added or removed?

Before: `scene01_light_before_a`. After: `scene01_light_after_a`. 364 frames per recording, 44 SAM 2 views each. Step folder `1-main_moved_light-before-a_vs_light-after-a` (part of *Main comparisons*).

SAM 2 failed (UnboundLocalError: cannot access local variable 'frames' where it is not associated with a value); objects below come from point clusters.

| Measure | Value |
|---|---|
| Objects found (A / B) | 0 / 0 |
| Objects compared / changed | 0 / 0 (0%) |
| Moved / added / removed / changed in place / shifted | 0 / 0 / 0 / 0 / 0 |
| Point-level changed share (old method) | 5.67% |
| Scale | 3.6092 m/unit (phone height cue 3.6092, objects cue None from 0 objects) |
| Reconstruction | 735 frames in 19 chunks, 530.4 s, worst anchor residual 0.0061 |
| Where objects are (90% range, median over objects) | ±- cm from VGGT confidence and SAM 2 mask wobble over - views (one view alone: ±- cm); check from views disagreeing: ±- cm |
| Consistency check (3 = honest, higher = overconfident) | - |
| Camera poses (bundle adjustment from VGGT) | reprojection error 8.3 -> 0.66 px median (1.98 px p90; worst frame 2.33 px), 117 sparse frames resected, 42 interpolated, cameras turned 1.15 deg; pose uncertainty 0.45 px median, 36 unconstrained frames count for less in positions, motion, change votes and splats |

## Object assets (node graphs)

`assets/` holds 60 objects as node graphs (nodes with weights, edges with rest length, spring constant from the Rigidity Copilot and measured strain, convex hull also as `_hull.obj`) and `scene_graph.json` with contacts.

Rigidity Copilot (CLIP asked what each object is made of and how stiff it is; an expectation from appearance, checked against the measured strain where there is any):

| Object | Status | Material (CLIP) | Rigidity 0-1 | Young's modulus | Measured | Check |
|---|---|---|---|---|---|---|
| baseball cap | moved | fabric or cloth | 0.65 | 3.2e+06 Pa | - | - |
| guitar | removed | wood | 0.86 | 5.4e+09 Pa | - | - |
| guitar | removed | wood | 0.77 | 2e+09 Pa | - | - |
| baseball cap | moved | fabric or cloth | 0.43 | 1.3e+07 Pa | - | - |
| jumper | removed | fabric or cloth | 0.52 | 5.6e+06 Pa | - | - |
| guitar | removed | wood | 0.85 | 6.6e+09 Pa | - | - |
| baseball cap | moved | fabric or cloth | 0.43 | 1.1e+07 Pa | - | - |
| paper bag | removed | plastic | 0.80 | 1.2e+09 Pa | - | - |
| skirting board | shifted | rubber | 0.74 | 9.2e+08 Pa | - | - |
| cup | removed | metal | 0.92 | 2.6e+10 Pa | - | - |
| cushion | removed | rubber | 0.68 | 9.1e+07 Pa | - | - |
| phone | removed | wood | 0.77 | 2.1e+09 Pa | - | - |
| coaster | shifted | rubber | 0.75 | 9.2e+08 Pa | - | - |
| skirting board | removed | metal | 0.93 | 2.9e+10 Pa | - | - |
| skirting board | removed | wood | 0.77 | 6.2e+08 Pa | - | - |

Contacts: A031_baseball_cap rests on A096_baseball_cap; A025_neck_pillow rests on A102_paper_bag; A047_mug rests on A142_cup; B006_cushion rests on B022_wooden_panel; B055_book rests on B007_tissue_box; B112_table_tennis_bat rests on B173_skirting_board; B020_sofa rests on B218_skirting_board; B009_cushion rests on B002_sofa; B002_sofa rests on B013_guitar; B020_sofa rests on B002_sofa.

![every object, labelled, border = status](objects.png)

![object masks in the frames](object_overlays.png)

![heat maps](heatmaps.png)

![where each object is: 90% regions, top-down](position_map.png)

![how position precision grows with more views](position_precision.png)

![where a robot could stand](base_placement.png)

![change map](change_map.png)

## Changes

- none

Scene report with the full object inventory and simulator ranges: [scene_report.md](scene_report.md)

Interactive 3D: download `viewer.html` and open it in a browser (objects are labelled; heat maps are layers in the legend).
