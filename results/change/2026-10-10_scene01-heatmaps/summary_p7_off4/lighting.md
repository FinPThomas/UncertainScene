# Lighting comparison

![lighting](lighting.png)

| Group | Pairs | % observed points flagged as changed | % flagged as appearance change | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm | change objects found |
|---|---|---|---|---|---|---|---|
| static-light | 6 | 1.70 (0.73-2.49) | 1.02 (0.73-1.23) | 1.01 (0.99-1.09) | 86.38 (85.56-86.94) | 8.90 (5.92-11.44) | 1.50 (0.00-4.00) |
| static-dark | 9 | 2.10 (1.22-2.67) | 0.95 (0.63-1.57) | 1.05 (0.97-1.10) | 87.09 (84.97-90.00) | 19.92 (9.62-534.08) | 3.00 (0.00-6.00) |
| lighting | 16 | 1.77 (0.96-3.13) | 1.00 (0.75-1.52) | 1.01 (0.99-1.10) | 87.49 (81.53-90.64) | 10.93 (4.40-32.31) | 2.00 (0.00-7.00) |
| change-light | 14 | 2.22 (1.00-3.64) | 1.22 (0.92-2.16) | 1.05 (0.96-1.15) | 86.00 (82.70-88.63) | 7.04 (5.75-29.97) | 2.00 (0.00-6.00) |
| change+lighting | 16 | 2.57 (1.59-3.90) | 1.59 (0.79-3.06) | 1.04 (0.95-1.13) | 86.91 (80.29-88.53) | 13.88 (6.24-1154.60) | 4.00 (0.00-8.00) |

Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.

## Reconstruction noise per recording

| Recording lighting | Samples | Surface uncertainty, median cm |
|---|---|---|
| dark | 50 | 11.86 |
| light | 72 | 8.12 |

## Same changes found through a lighting change?

For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, first frame pick). Labels exclude background classes.

| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | Label overlap (Jaccard) |
|---|---|---|---|---|
| scene01_light_after_a | 1.5 / cup, guitar, hat | 3.0 / blanket, coaster, guitar, hat, jacket, keys, mug, towel | guitar, hat | 0.22 |
| scene01_light_after_b | 4.0 / baseball cap, cup, glass vase, jacket, table tennis bat | 6.7 / bag, baseball cap, book, glass vase, guitar, hat, jacket, mug, pillow, table tennis bat, vase | baseball cap, glass vase, jacket, table tennis bat | 0.33 |

Ablation `no_appearance` (in the main summary) shows whether appearance features are what lighting trips; geometry-only detection should be nearly lighting-invariant.
