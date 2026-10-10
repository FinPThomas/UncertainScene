# Gaussian splats: light, before, take a vs light, after, take a

**What this run tests:** Do splats of each recording render well, and does comparing before and after from the exact same camera separate real changes from noise?

Before: light, before, take a. After: light, after, take a. One splat per recording, 7000 training steps each, started from the VGGT points; every 8th frame held out for testing. Step folder `1b-splat_moved_light-before-a_vs_light-after-a`.

| | Before splat | After splat |
|---|---|---|
| Render quality on held-out frames (PSNR, higher is better; 25+ dB looks good) | 17.7 dB | 17.9 dB |
| Gaussians | 440085 | 500617 |
| Training time | 1208 s | 327 s |
| Camera pose correction learnt (median) | 0.755 deg | 0.668 deg |

## Same camera, before vs after

Each held-out after photo is compared with the after splat's render (nothing changed, so this is the noise) and with the before splat's render from the same camera (change plus noise). Pixels above the 99th percentile of the no-change differences count as changed, so about 1% would be flagged if nothing moved.

- Changed pixels, before splat vs after photos: **2.0%** (reverse direction 1.4%; 1% = no change).
- Median colour difference: 0.082 vs 0.052 when nothing changed.
- Pixels where the two splats' depth differs by more than 5 cm: 76.9%.

Figures: `splat_compare.png` (per view), `splat_orbit.gif` (both splats along the after camera path). The `.splat` files open in any web splat viewer (e.g. drag into antimatter15.com/splat).
