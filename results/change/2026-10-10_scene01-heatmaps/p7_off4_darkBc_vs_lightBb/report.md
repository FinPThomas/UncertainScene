# p7_off4_darkBc_vs_lightBb

**scene01_dark_before_c** (A, before) vs **scene01_light_before_b** (B, after) - pair type **lighting**, 28 frames per video, variant `offset4`.

Distances are in cm, from a floor fit and an assumed phone height (scale 3.0331 m/unit, range [2.4714, 3.5948]).

| Measure | Value |
|---|---|
| Observed share (A / B) | 86.5% / 77.4% |
| Changed share of observed points (A / B) | 1.23% / 1.64% |
| Alignment residual across / within recordings | 0.0461 / 0.046 |
| Thresholds (depth tau, see-through fraction, appearance) | 0.13748 / 0.75 / 0.91162 |
| Changes (moved / removed / added / appearance) | 0 / 0 / 0 / 0 |
| Reconstruction time | 27.2 s (GPU peak 9.57 GB) |

Nothing was moved between these recordings, so every flagged point is a false change: **false-change rate 1.43%**.

Heat maps (recording B): reachable 0%, approachable 36%, interactable 0%; surface uncertainty median 11.384 cm (90th pct 32.276); surface shift 99th pct 56.461 cm.

![change map](change_map.png)

![heat maps](heatmaps.png)

## Changes

- none detected
