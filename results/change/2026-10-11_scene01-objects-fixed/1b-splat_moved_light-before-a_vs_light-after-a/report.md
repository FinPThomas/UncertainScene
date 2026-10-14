# Gaussian splats: light, before, take a vs light, after, take a

**What this run tests:** Do splats of each recording render well, and does comparing before and after from the exact same camera separate real changes from noise?

Before: light, before, take a. After: light, after, take a. One splat per recording, 20000 training steps each, started from the VGGT points; every 8th frame held out for testing. Step folder `1b-splat_moved_light-before-a_vs_light-after-a`.

| | Before splat | After splat |
|---|---|---|
| Camera poses: reprojection error VGGT -> refined (median, px at frame size) | 8.3 -> 0.59 px (54061 points, cameras turned 1.60 deg median) | |
| Render quality on held-out frames (PSNR, higher is better; 25+ dB looks good) | 18.2 dB | 18.9 dB |
| Gaussians | 456850 | 551847 |
| Training time | 634 s | 658 s |
| Camera pose correction learnt (median) | 0.849 deg | 0.696 deg |

## Same camera, before vs after

Each held-out after photo is compared with the after splat's render (nothing changed, so this is the noise) and with the before splat's render from the same camera (change plus noise). Pixels above the 99th percentile of the no-change differences count as changed, so about 1% would be flagged if nothing moved.

- Compared on 30 held-out after frames that the before recording also saw from a similar place (elsewhere the before splat would only be guessing).
- Changed pixels, before splat vs after photos: **1.5%** (reverse direction 0.4%; 1% = no change).
- Median colour difference: 0.107 vs 0.045 when nothing changed.
- Pixels where the two splats' depth differs by more than 5 cm: 89.5%.

Figures: `splat_compare.png` (per view), `splat_orbit.gif` (both splats along the after camera path). `splat_viewer/splat_viewer.html` shows the real splats (open it next to its `splats/` folder).
