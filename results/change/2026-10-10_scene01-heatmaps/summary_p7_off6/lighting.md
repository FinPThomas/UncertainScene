# Lighting comparison

![lighting](lighting.png)

| Group | Pairs | % observed points flagged as changed | % flagged as appearance change | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm | change objects found |
|---|---|---|---|---|---|---|---|
| static-light | 8 | 1.76 (0.73-2.49) | 1.02 (0.73-1.27) | 1.02 (0.99-1.09) | 86.54 (85.56-86.95) | 9.00 (5.92-11.44) | 1.50 (0.00-4.00) |
| static-dark | 13 | 2.10 (1.22-2.89) | 0.95 (0.61-1.95) | 1.04 (0.93-1.10) | 86.83 (84.97-90.00) | 20.01 (9.62-534.08) | 3.00 (0.00-6.00) |
| lighting | 24 | 1.83 (0.70-3.13) | 1.07 (0.70-1.52) | 1.02 (0.99-1.10) | 86.66 (81.53-90.64) | 11.32 (4.40-32.31) | 2.00 (0.00-7.00) |
| change-light | 20 | 2.30 (0.97-4.47) | 1.25 (0.92-2.35) | 1.05 (0.96-1.18) | 85.75 (82.70-88.63) | 7.18 (5.20-29.97) | 2.00 (0.00-6.00) |
| change+lighting | 24 | 2.47 (1.08-3.90) | 1.61 (0.79-3.06) | 1.03 (0.95-1.13) | 84.94 (80.29-88.53) | 13.88 (5.93-1154.60) | 3.00 (0.00-8.00) |

Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.

## Reconstruction noise per recording

| Recording lighting | Samples | Surface uncertainty, median cm |
|---|---|---|
| dark | 74 | 14.50 |
| light | 104 | 8.12 |

## Same changes found through a lighting change?

For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, first frame pick). Labels exclude background classes.

| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | Label overlap (Jaccard) |
|---|---|---|---|---|
| scene01_light_after_a | 1.5 / cup, guitar, hat | 3.0 / blanket, coaster, guitar, hat, jacket, keys, mug, towel | guitar, hat | 0.22 |
| scene01_light_after_b | 4.0 / baseball cap, cup, glass vase, jacket, table tennis bat | 6.7 / bag, baseball cap, book, glass vase, guitar, hat, jacket, mug, pillow, table tennis bat, vase | baseball cap, glass vase, jacket, table tennis bat | 0.33 |

Ablation `no_appearance` (in the main summary) shows whether appearance features are what lighting trips; geometry-only detection should be nearly lighting-invariant.
