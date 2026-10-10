# Lighting comparison

![lighting](lighting.png)

| Group | Pairs | % observed points flagged as changed | % flagged as appearance change | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm | change objects found |
|---|---|---|---|---|---|---|---|
| static-light | 3 | 2.04 (1.82-2.49) | 1.17 (0.99-1.23) | 1.04 (1.00-1.09) | 86.34 (85.73-86.41) | 7.25 (5.92-10.95) | 2.00 (2.00-4.00) |
| static-dark | 4 | 2.43 (2.17-2.67) | 1.29 (0.95-1.57) | 1.05 (0.97-1.10) | 87.82 (84.97-90.00) | 32.02 (9.62-534.08) | 4.00 (4.00-6.00) |
| lighting | 7 | 2.08 (1.69-3.13) | 1.11 (0.85-1.52) | 1.02 (1.01-1.10) | 88.50 (84.66-89.73) | 9.46 (4.40-14.27) | 4.00 (1.00-7.00) |
| change-light | 6 | 2.29 (1.16-3.07) | 1.20 (0.92-1.41) | 1.07 (0.98-1.14) | 86.25 (82.70-88.63) | 6.83 (5.75-7.50) | 2.00 (1.00-5.00) |
| change+lighting | 7 | 2.75 (1.59-3.90) | 1.64 (1.26-2.25) | 1.05 (1.03-1.13) | 87.70 (83.76-88.52) | 14.03 (6.24-172.52) | 4.00 (1.00-8.00) |

Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.

## Reconstruction noise per recording

| Recording lighting | Samples | Surface uncertainty, median cm |
|---|---|---|
| dark | 22 | 11.25 |
| light | 32 | 6.59 |

## Same changes found through a lighting change?

For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, first frame pick). Labels exclude background classes.

| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | Label overlap (Jaccard) |
|---|---|---|---|---|
| scene01_light_after_a | 1.5 / cup, guitar, hat | 3.0 / blanket, coaster, guitar, hat, jacket, keys, mug, towel | guitar, hat | 0.22 |
| scene01_light_after_b | 4.0 / baseball cap, cup, glass vase, jacket, table tennis bat | 6.7 / bag, baseball cap, book, glass vase, guitar, hat, jacket, mug, pillow, table tennis bat, vase | baseball cap, glass vase, jacket, table tennis bat | 0.33 |

Ablation `no_appearance` (in the main summary) shows whether appearance features are what lighting trips; geometry-only detection should be nearly lighting-invariant.
