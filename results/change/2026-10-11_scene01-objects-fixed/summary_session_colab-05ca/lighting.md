# Lighting comparison

![lighting](lighting.png)

| Group | Pairs | changed objects (SAM 2) | % of compared objects changed | % observed points flagged as changed | alignment residual across / within recordings | % points seen by both recordings | surface uncertainty, median cm |
|---|---|---|---|---|---|---|---|
| static-light | 1 | 13.00 (13.00-13.00) | 33.33 (33.33-33.33) | 5.01 (5.01-5.01) | 1.15 (1.15-1.15) | 99.33 (99.33-99.33) | 8.04 (8.04-8.04) |
| change-light | 1 | 12.00 (12.00-12.00) | 38.71 (38.71-38.71) | 5.72 (5.72-5.72) | 1.25 (1.25-1.25) | 98.87 (98.87-98.87) | 7.46 (7.46-7.46) |

Median (range). **lighting** vs **static-light/static-dark** isolates what the lighting change alone flags as change; **change+lighting** vs **change-light** shows how much the real changes survive it.

## Same changes found through a lighting change?

For each after recording: changes found from a light 'before' vs from a dark 'before' (base settings, first frame pick). Labels exclude background classes.

| After | Light-before pair: changes / labels | Dark-before pair: changes / labels | Shared labels | Label overlap (Jaccard) |
|---|---|---|---|---|

Ablation `no_appearance` (in the main summary) shows whether appearance features are what lighting trips; geometry-only detection should be nearly lighting-invariant.
