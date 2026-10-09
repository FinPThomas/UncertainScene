# UncertainScene

Compare two phone recordings of the same small indoor area and report, in 3D, what was moved,
added or removed, by how much, and how certain that is. The uncertainties feed a scene report
that tells a simulator what to randomise (and over what range) to narrow the sim-to-real gap.

**Start here:** [RESEARCH.md](RESEARCH.md) (outline, research paths, milestones, progress log).

| Doc | What it covers |
|---|---|
| [RESEARCH.md](RESEARCH.md) | Research outline and progress, updated with every step |
| [docs/COMMITS.md](docs/COMMITS.md) | Commit message format and rules |
| [docs/BRANCHING.md](docs/BRANCHING.md) | One branch per research path, experiment branches under it |
| [docs/COLAB.md](docs/COLAB.md) | Running on Colab from several accounts, publishing to GitHub, resource guardrails |
| [docs/templates/scene_report.md](docs/templates/scene_report.md) | The per-scene output report |
| [docs/decisions/](docs/decisions/) | Short notes on decisions with lasting consequences |
| [data/README.md](data/README.md) | Capture protocol, naming, where raw video lives |

## Layout

```
RESEARCH.md              living outline + progress log
configs/                 colab_budget.toml (guardrails); per-experiment configs later
data/                    README, manifest.json, ground_truth/ (raw video is NOT in git)
docs/                    guides, decisions, templates
notebooks/colab_run.ipynb  the one notebook every Colab/Kaggle session runs
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

No experiment code yet: research paths start from RESEARCH.md section 3.
