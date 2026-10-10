# Lighting comparison

![lighting](lighting.png)

| Group | Pairs | % observed points flagged as changed | % flagged as appearance change | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm | change objects found |
|---|---|---|---|---|---|---|---|
| static-light | 9 | 1.82 (0.73-2.49) | 1.05 (0.73-1.27) | 1.04 (0.99-1.09) | 86.67 (85.56-86.95) | 9.61 (5.92-11.44) | 2.00 (0.00-4.00) |
| static-dark | 15 | 2.17 (1.22-3.18) | 0.98 (0.61-2.24) | 1.04 (0.93-1.10) | 86.83 (84.97-90.00) | 21.41 (9.62-534.08) | 3.00 (0.00-6.00) |
| lighting | 28 | 1.84 (0.70-3.13) | 1.14 (0.70-1.52) | 1.02 (0.99-1.10) | 86.66 (81.53-90.64) | 12.46 (4.40-32.31) | 2.00 (0.00-7.00) |
| change-light | 23 | 2.36 (0.97-4.47) | 1.28 (0.92-2.35) | 1.06 (0.96-1.18) | 85.66 (82.70-88.63) | 7.22 (5.20-29.97) | 2.00 (0.00-6.00) |
| change+lighting | 28 | 2.47 (1.08-3.90) | 1.64 (0.79-3.06) | 1.04 (0.95-1.13) | 84.85 (80.29-88.53) | 14.82 (5.93-1154.60) | 3.00 (0.00-8.00) |

Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.

## Reconstruction noise per recording

| Recording lighting | Samples | Surface uncertainty, median cm |
|---|---|---|
| dark | 86 | 16.58 |
| light | 120 | 8.27 |

## Same changes found through a lighting change?

For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, first frame pick). Labels exclude background classes.

| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | Label overlap (Jaccard) |
|---|---|---|---|---|
| scene01_light_after_a | 1.5 / cup, guitar, hat | 3.0 / blanket, coaster, guitar, hat, jacket, keys, mug, towel | guitar, hat | 0.22 |
| scene01_light_after_b | 4.0 / baseball cap, cup, glass vase, jacket, table tennis bat | 6.7 / bag, baseball cap, book, glass vase, guitar, hat, jacket, mug, pillow, table tennis bat, vase | baseball cap, glass vase, jacket, table tennis bat | 0.33 |

Ablation `no_appearance` (in the main summary) shows whether appearance features are what lighting trips; geometry-only detection should be nearly lighting-invariant.
