# Running on Colab (one or many accounts)

All compute happens on Colab (or Kaggle). GitHub is the only place anything persists:
code, run state, small results, and (through release assets) big files. A Colab session is
disposable; any account can pick up where another stopped.

```
 Colab account A ─┐                       GitHub: FinPThomas/UncertainScene
 Colab account B ─┼─ clone branch ──►  code + runs/<run_id>/state.json + results/<path>/<run_id>/
 Kaggle ──────────┘   run steps   ──►  release assets (videos, point clouds, meshes, checkpoints)
                      push per step
          local disk only: /content/work/<run_id>/<step>/  (deleted after each step)
```

## One-time setup (per GitHub account, not per Colab account)

1. On GitHub: Settings → Developer settings → Fine-grained personal access tokens → Generate.
   Repository access: only `FinPThomas/UncertainScene`. Permissions: **Contents: Read and write**
   (this also covers release assets). Pick an expiry you are happy with.
2. Never paste the token into a notebook cell, a chat or a file.

## Per Colab account (once)

1. Open the experiment notebook from GitHub in Colab (File → Open notebook → GitHub tab, pick its
   branch), e.g. [scene01_change_heatmaps.ipynb](https://colab.research.google.com/github/FinPThomas/UncertainScene/blob/exp/change/scene01-heatmaps/notebooks/scene01_change_heatmaps.ipynb).
2. Left sidebar → key icon (Secrets) → add `GITHUB_TOKEN` with the token, and turn on
   **Notebook access** for it.
3. Optional: add a secret `UNCERTAINSCENE_ACCOUNT` with a short label (`fin-1`, `fin-2`) so the
   run state shows which account ran which step.

## Every session

1. Set the config cell: `BRANCH`, `RUN_ID`, `PATH_NAME`, and optionally `EXPECTED_COMMIT`.
2. Runtime → Run all. The setup cell:
   - clones `BRANCH` into `/content/UncertainScene` (local disk, never Drive);
   - prints the commit it is on and stops if it does not match `EXPECTED_COMMIT`
     (stale code was the most common failure in the previous project);
   - does a dry-run push, so a bad token fails in the first minute, not after the run;
   - prints RAM, disk and GPU, and starts the guard.
3. Steps already done (in `runs/<run_id>/state.json`) are skipped. Steps another account is
   running are skipped. Steps that would not fit in the remaining RAM, disk or session time are
   not started; the next session does them.
4. When a session dies, open the same notebook in any account and Run all again.

## Guardrails (configs/colab_budget.toml)

| Resource | Soft limit (warn, clean scratch, free caches) | Hard limit (stop step cleanly, publish) |
|---|---|---|
| RAM | 80 % used | 92 % used |
| Disk on /content | 12 GB free | 5 GB free |
| GPU memory | 90 % (warn only; torch raises OOM itself) | none |
| Session time | none | no step starts unless it can finish 20 min before the 11 h mark |

- Each step declares what it needs: `needs=dict(disk_gb=8, ram_gb=6, minutes=40)`.
- A background monitor checks every 15 s. At a hard limit it interrupts the step, records it
  as `interrupted` with the reason, publishes state, and stops the run. Rerunning resumes it.
- Long loops can also call `run.guard.check()` themselves.
- Scratch files go in the `work` folder each step is given; it is deleted when the step ends,
  and soft limits delete leftover scratch of other steps.

## What goes where

| Output | Where | Size rule |
|---|---|---|
| Metrics, JSON, small figures, logs, scene reports | `results/<path>/<run_id>/<step>/` (committed by the runner) | each file ≤ 5 MB, ≤ 50 MB per publish |
| Point clouds, splats, meshes, checkpoints, videos | `Store.put(...)` → release `run-<run_id>`, manifest committed in the step folder | ≤ 2 GB per file |
| Raw recordings | release `data`, manifest in `data/manifest.json` | ≤ 2 GB per file |
| Temporary working files | `work` folder on local disk | deleted after the step |

## Several accounts at once

Safe. Claims are pushed before a step starts, so two accounts never run the same step; if two
push a claim at the same instant, git rejects one and that account re-reads the state and
moves on. Results live in per-run, per-step folders, so pushes do not conflict. If a push
still conflicts, the results go to the branch `results/<run_id>` rather than being lost.

Each session adds a random suffix to its label (`fin-2-a3f9`), so even two sessions on the same
account keep out of each other's way. A session that resumes after a crash gets a new suffix;
its old claim expires on its own (1.5 × the step's expected minutes, plus 30 min).

## Kaggle

The same notebook works; set the token as a Kaggle secret named `GITHUB_TOKEN` (tick
"Attached" next to it) and turn Internet on. Paths switch to `/kaggle/working` and
`/kaggle/temp` automatically.
