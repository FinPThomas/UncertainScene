# How to commit

Clean history is part of the research record: anyone (or any Claude session) should be able to
read `git log --oneline` and see what was tried, in what order, and where its results are.

## Message format

```
<type>(<scope>): <summary in the imperative, lower case, no full stop>

<optional body: why, what changed, what the result was. Wrap at 72 characters.>

<optional trailers, e.g. Run: 2026-10-12_1430_recon-mast3r>
```

- Summary line at most 72 characters.
- Imperative mood: "add", "fix", "record", not "added" or "adds".
- One logical change per commit. If the summary needs "and", split it.

### Types

| Type | Use for |
|---|---|
| `feat` | New capability in `src/` (a new pipeline stage, a new measure) |
| `fix` | Bug fix |
| `exp` | Experiment code or configs that exist to test one idea |
| `results` | Outputs of a run (written by the Colab publisher, or by hand) |
| `docs` | RESEARCH.md, guides, decision notes, scene reports |
| `data` | Ground-truth notes and data manifests (never raw video) |
| `infra` | Colab, publishing, guardrails, CI, tooling |
| `refactor` | Code change with no behaviour change |
| `test` | Tests only |
| `chore` | Dependency bumps, formatting, housekeeping |

### Scopes

Use the research path name when the change belongs to one: `capture`, `recon`, `change`,
`reid`, `deform`, `reach`, `report`. Otherwise use the area: `colab`, `ci`, `outline`, `deps`.

### Examples

```
feat(recon): register two recordings on static background with icp
exp(change): try dinov2 features for lighting-robust change scores
results(recon): mast3r joint reconstruction on pair01
docs(outline): mark M2 done and log registration error
infra(colab): stop a step early when free disk drops below 5 GB
data(capture): add ground truth for pair02 (mug moved 30 cm)
```

## Rules

1. **Never commit raw data or large binaries.** Original videos, point clouds, splats, checkpoints and
   weights go to the artifact store (`uncertainscene.infra.store`, GitHub release assets). Git holds
   only a manifest pointing at them. The pre-commit hook and CI reject any file over 5 MB.
2. **Update RESEARCH.md with the work.** A commit that finishes a step also adds its line to
   the progress log and updates the path or milestone status.
3. **Results are committed by the publisher.** Colab runs write `results/<path>/<run_id>/`
   and commit it as `results(<path>): ...`. Do not hand-edit published results; re-run instead.
4. **Do not rewrite shared history.** No force-push to `main` or any `path/*` branch. On your
   own `exp/*` branch, tidy commits freely before merging.
5. **Merge into `main` with squash or a merge commit** that has a proper summary line.
   An experiment branch merged into its path branch keeps its commits (they are the record).
6. **Each commit should leave tests passing** (`python -m pytest tests/`).

## Enforcing it

Turn on the repo hooks once per clone:

```
git config core.hooksPath .githooks
```

- `.githooks/commit-msg` checks the message format.
- `.githooks/pre-commit` blocks files over 5 MB and anything under `data/raw/`.

CI (`.github/workflows/ci.yml`) runs the same checks on every push and pull request, so a
commit made without the hooks (for example on Colab) is still caught.

The Colab publisher writes messages in this format automatically.
