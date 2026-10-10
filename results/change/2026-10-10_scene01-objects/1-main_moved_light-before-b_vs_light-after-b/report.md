# Objects moved, same lighting: light, before, take b vs light, after, take b

**What this run tests:** Does it find the objects that really moved, were added or removed?

Before: `scene01_light_before_b`. After: `scene01_light_after_b`. 372 frames per recording, 44 SAM 2 views each. Step folder `1-main_moved_light-before-b_vs_light-after-b` (part of *Main comparisons*).

SAM 2 failed (AttributeError: 'BaseModelOutputWithPooling' object has no attribute 'float'); objects below come from point clusters.

| Measure | Value |
|---|---|
| Objects found (A / B) | 552 / 511 |
| Objects compared / changed | 0 / 0 (0%) |
| Moved / added / removed / changed in place / shifted | 0 / 0 / 0 / 0 / 0 |
| Point-level changed share (old method) | 5.76% |
| Scale | 2.1864 m/unit (phone height cue 2.1864, objects cue None from 0 objects) |
| Reconstruction | 744 frames in 19 chunks, 429.5 s, worst anchor residual 0.0133 |
| Where objects are (90% range, median over objects) | ±1.51 cm from VGGT confidence and SAM 2 mask wobble over 1 views (one view alone: ±1.55 cm); check from views disagreeing: ±None cm |
| Consistency check (3 = honest, higher = overconfident) | 40.38 |

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
