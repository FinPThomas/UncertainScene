# Data

Raw recordings are **not** in git. They live as GitHub release assets in the release `data`
(uploaded with `uncertainscene.infra.store.Store.put`), and `data/manifest.json` lists them
with size and sha256 so every Colab account downloads the same bytes.

## Naming

```
<pair>_<state>_<take>.<ext>      e.g. pair01_before_a.mp4, pair01_after_a.mp4, pair01_after_b.mp4
```

- `pair`: one scene and one set of changes (`pair01`, `pair02`, ...). A pair where nothing
  changed is named `pairNN_static` and measures the false-change rate.
- `state`: `before` or `after`.
- `take`: `a`, `b`, ... for extra sweeps of the same state.

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
