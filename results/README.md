# Results

Written by `uncertainscene.infra.Run`, one folder per step:

```
results/<path>/<run_id>/<step>/
  step_info.json     status, account, commit, minutes, resource usage at the end
  ...                whatever the step wrote (metrics.json, figures, report.md)
  artifacts.json     manifest of big files in the release run-<run_id>, if any
  error.txt          traceback, if the step failed
```

`run_id` format: `YYYY-MM-DD_<short-name>`, e.g. `2026-10-12_recon-mast3r`.
Do not hand-edit published results; rerun the step instead. Headline numbers go into the
RESEARCH.md progress log with a link to the folder.
