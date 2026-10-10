# Objects moved and lighting changed: dark, before, take a vs light, after, take a

**What this run tests:** Does it still find the real changes when the lighting also changes?

Before: `scene01_dark_before_a`. After: `scene01_light_after_a`. 382 frames per recording, 44 SAM 2 views each. Step folder `1-main_moved-and-light-changed_dark-before-a_vs_light-after-a` (part of *Main comparisons*).

SAM 2 failed (AttributeError: 'BaseModelOutputWithPooling' object has no attribute 'float'); objects below come from point clusters.

| Measure | Value |
|---|---|
| Objects found (A / B) | 462 / 553 |
| Objects compared / changed | 0 / 0 (0%) |
| Moved / added / removed / changed in place / shifted | 0 / 0 / 0 / 0 / 0 |
| Point-level changed share (old method) | 5.29% |
| Scale | 3.9504 m/unit (phone height cue 3.9504, objects cue None from 0 objects) |
| Reconstruction | 753 frames in 19 chunks, 434.7 s, worst anchor residual 0.0083 |
| Where objects are (90% range, median over objects) | ±2.22 cm from VGGT confidence and SAM 2 mask wobble over 1 views (one view alone: ±2.22 cm); check from views disagreeing: ±None cm |
| Consistency check (3 = honest, higher = overconfident) | 69.34 |

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
