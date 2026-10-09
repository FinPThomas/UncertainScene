# UncertainScene

Compare two phone recordings of the same small indoor area and report, in 3D, what was moved,
added or removed, by how much, and how certain that is. The uncertainties feed a scene report
that tells a simulator what to randomise (and over what range) to narrow the sim-to-real gap.

**Start here:** [RESEARCH.md](RESEARCH.md) (outline, research paths, milestones, progress log).

## Run the experiments

One notebook does everything: [**scene01_change_heatmaps.ipynb** on branch `exp/change/scene01-heatmaps`](https://colab.research.google.com/github/FinPThomas/UncertainScene/blob/exp/change/scene01-heatmaps/notebooks/scene01_change_heatmaps.ipynb)
(Open in Colab, T4 GPU, Runtime -> Run all). It checks GitHub first (token, repo, a real test
push), then runs every experiment until the session ends, pushing results after each step.
In Colab's GitHub picker, choose that branch first: experiment notebooks live on their
experiment branch (docs/BRANCHING.md), not on `main`.

| Doc | What it covers |
|---|---|
| [RESEARCH.md](RESEARCH.md) | Research outline and progress, updated with every step |
| [docs/COMMITS.md](docs/COMMITS.md) | Commit message format and rules |
| [docs/BRANCHING.md](docs/BRANCHING.md) | One branch per research path, experiment branches under it |
| [docs/COLAB.md](docs/COLAB.md) | Running on Colab from several accounts, publishing to GitHub, resource guardrails |
| [docs/templates/scene_report.md](docs/templates/scene_report.md) | The per-scene output report |
| [docs/decisions/](docs/decisions/) | Short notes on decisions with lasting consequences |
| [docs/CAPTURE.md](docs/CAPTURE.md) | How to record the videos: settings, length, path, scene, test pairs |
| [data/README.md](data/README.md) | Capture protocol, naming, where raw video lives |

## Layout

```
RESEARCH.md              living outline + progress log
configs/                 colab_budget.toml (guardrails); per-experiment configs later
data/                    README, manifest.json, ground_truth/ (raw video is NOT in git)
docs/                    guides, decisions, templates
notebooks/                experiment notebooks live on their exp/ branch
results/<path>/<run_id>/ small outputs, committed by the runner
runs/<run_id>/state.json resumable run state (which steps are done, claimed, failed)
scripts/                 commit and size checks used by hooks and CI
src/uncertainscene/
  infra/                 guard (RAM/disk/time), state, publish (git), store (big files), runner
tests/                   infrastructure tests (python -m pytest)
```

## Local setup

```
git clone https://github.com/FinPThomas/UncertainScene && cd UncertainScene
python -m pip install -e ".[dev]"
git config core.hooksPath .githooks
python -m pytest
```

Experiment code lives on research-path and experiment branches until a milestone merges it to main.
