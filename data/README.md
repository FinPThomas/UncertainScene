# Data

Raw recordings are **not** in git (too big). Extracted frames are, and `data/manifest.json`
lists every original with size and sha256, so frames can always be traced back to one file.

## Naming

```
<scene>_<lighting>_<state>_<take>      e.g. scene01_light_before_a, scene01_dark_after_a
```

- `scene`: one physical setup (`scene01` = living-room sofa).
- `lighting`: `light` or `dark` (or another short word for a new condition).
- `state`: `before` (original arrangement) or `after` (objects moved).
- `take`: `a`, `b`, ... for extra sweeps of the same state.

`data/recordings.json` maps each original phone file name to its label and defines the
**pairs** compared in experiments (e.g. `pair00_static_light` = two "before" sweeps, nothing
changed; `pair01` = before vs after in the same light).

## Frames and previews (committed)

`python -m uncertainscene.prep --raw <folder of originals>` writes, per labelled recording:

- `data/frames/<label>/00000.jpg ...`: the sharpest frame of every 1/9 s window, 3 per
  second, long side 1024 px, plus `frames.json` (source time, sharpness, brightness, video
  metadata). This is what experiments read; no video decoding needed on Colab.
- `data/previews/<label>.jpg`: contact sheet for a quick look.
- an entry in `data/manifest.json` (sha256 and metadata of the original).

The original videos stay on the laptop (`data/raw/` or wherever they were copied). They can be
attached to the GitHub release `data` later with `--upload api` from Colab.

## Ground truth (scoring only)

`data/ground_truth/<pair>.json`, written at capture time:

```json
{
  "pair": "pair01",
  "lighting": {"before": "daylight", "after": "lamp only"},
  "scale_reference": "A4 sheet on the shelf, 29.7 cm",
  "changes": [
    {"object": "mug", "change": "moved", "approx_cm": 30, "note": "middle shelf, left to right"},
    {"object": "book", "change": "removed"},
    {"object": "blanket", "change": "deformed", "note": "folded in half"}
  ]
}
```

## Local copies

Put local copies under `data/raw/` (gitignored). On Colab they are downloaded into the run's
work folder.
