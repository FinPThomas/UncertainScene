# UncertainScene: notes for Claude sessions

Read RESEARCH.md first. It is the single source of truth for what has been done and what is next.

## Rules

1. Do not start a step that RESEARCH.md marks done. Check the progress log and
   `runs/*/state.json` before building or running anything.
2. Every commit follows docs/COMMITS.md (`type(scope): summary`). Run
   `python scripts/check_commit_msg.py -m "<message>"` if unsure.
3. Work on the branch docs/BRANCHING.md prescribes (`path/<name>` or `exp/<path>/<idea>`);
   never commit experiment work straight to `main`.
4. A commit that finishes a step updates RESEARCH.md (progress log line and statuses) in the
   same commit.
5. Never commit raw data or any file over 5 MB. Big files go through
   `uncertainscene.infra.store` (GitHub release assets) with a manifest committed.
6. Compute runs on Colab through notebooks built from `notebooks/test_github_push.ipynb` (named
   after what they run, kept on their experiment branch) and `uncertainscene.infra.Run`.
   Every experiment is a `run.step(...)` with honest `needs` (disk_gb, ram_gb, minutes).
7. Ground truth in `data/ground_truth/` is for scoring only; never tune a threshold on the
   pair it is scored on (RESEARCH.md section 5).
8. Never write tokens or secrets into files, commits or notebooks. Colab reads `GITHUB_TOKEN`
   from Colab secrets.
9. Run `python -m pytest` before pushing.
