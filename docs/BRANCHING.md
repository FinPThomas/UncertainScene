# Branching for research paths

```
main ─────────────────────────────────────────────────────────►  stable: works, documented
  ├── path/recon ───────────────────────────────────────────►   one long-lived branch per research path
  │     ├── exp/recon/mast3r-joint ──┐                           short experiments
  │     └── exp/recon/colmap-gsplat ─┴─(merge winner)─►
  ├── path/change ──────────────────────────────────────────►
  └── infra/colab-guard ─(PR)─► main                            tooling and docs go straight to main
```

## Branch types

| Pattern | Lives | Branches from | Merges into | Purpose |
|---|---|---|---|---|
| `main` | forever | none | none | Working, documented state. RESEARCH.md here is the truth. |
| `path/<name>` | for the life of a research path | `main` | `main` (via PR, at milestones) | One research path from RESEARCH.md section 3 |
| `exp/<path>/<idea>` | days | its `path/<name>` | its `path/<name>`, or abandoned | One idea or comparison |
| `infra/<topic>`, `docs/<topic>` | hours to days | `main` | `main` (via PR) | Tooling, CI, larger guide changes. A single self-contained docs commit may go straight to `main`. |
| `results/<run_id>` | rare | any | its parent | Only if a publish push keeps conflicting (the publisher falls back to this) |

Path names: `capture`, `recon`, `change`, `reid`, `deform`, `reach`, `report`.

## How a research path moves

1. Create the path branch from `main` when the path starts: `git switch -c path/recon main`.
   Change its status in RESEARCH.md to `exploring` in the first commit.
2. Each idea gets an `exp/` branch. Run it on Colab with `BRANCH = "exp/recon/<idea>"`;
   results are pushed to that branch.
3. When the idea is judged (worked or not), merge it into the path branch with a merge commit
   and a progress-log line saying what was learnt. Failed ideas are merged too, so their
   results stay in the record, unless the code is useless; then delete the branch and
   record the outcome in the progress log only.
4. At a milestone, open a PR from `path/<name>` to `main`. Keep `main`'s RESEARCH.md as the
   combined view of all paths.
5. Keep a path branch current with `git merge main` (never rebase a shared branch).

## Dependencies between paths

P2 needs P1's shared frame; P3 needs P2's candidates; P6 needs everything. When a later path
needs an unmerged earlier one, branch from the earlier **path** branch, not from an `exp/`
branch, and note it in the PR description. Prefer merging the earlier path to `main` first.

## Abandoning a path

Set its status to `parked` in RESEARCH.md with one line on why, merge any useful results, and
leave the branch in place (do not delete `path/*` branches; they are cheap and they are the
history).
