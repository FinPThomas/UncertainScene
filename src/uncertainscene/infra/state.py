"""Resumable run state, stored in git at runs/<run_id>/state.json.

Every Colab session (on any Google account) clones the branch, reads this file and skips
steps that are already done. Before starting a step a session *claims* it and pushes the
claim; another account that sees a live claim skips that step. A claim expires (so a
session that died does not block the step forever) at `expires`, which the runner sets from
the step's expected duration.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PENDING, RUNNING, DONE, FAILED, INTERRUPTED = "pending", "running", "done", "failed", "interrupted"


def now() -> float:
    return time.time()


def stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t or now()))


@dataclass
class StepState:
    status: str = PENDING
    account: str | None = None
    started: str | None = None
    finished: str | None = None
    expires: float | None = None   # epoch seconds; a RUNNING claim past this is stale
    attempts: int = 0
    commit: str | None = None      # code commit the step ran on
    note: str | None = None        # error, interruption reason, or a one-line result


@dataclass
class RunState:
    run_id: str
    path: str
    branch: str
    steps: dict[str, StepState] = field(default_factory=dict)

    # ---- persistence -------------------------------------------------------------------
    @staticmethod
    def file(repo_dir: str | Path, run_id: str) -> Path:
        return Path(repo_dir) / "runs" / run_id / "state.json"

    @classmethod
    def load_or_new(cls, repo_dir: str | Path, run_id: str, path: str, branch: str) -> "RunState":
        f = cls.file(repo_dir, run_id)
        if not f.exists():
            return cls(run_id=run_id, path=path, branch=branch)
        raw = json.loads(f.read_text())
        steps = {k: StepState(**v) for k, v in raw.pop("steps", {}).items()}
        return cls(**raw, steps=steps)

    def save(self, repo_dir: str | Path) -> Path:
        f = self.file(repo_dir, self.run_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
        return f

    # ---- step logic --------------------------------------------------------------------
    def step(self, name: str) -> StepState:
        return self.steps.setdefault(name, StepState())

    def is_done(self, name: str) -> bool:
        return self.step(name).status == DONE

    def claimed_by_other(self, name: str, account: str) -> bool:
        s = self.step(name)
        return (s.status == RUNNING and s.account != account
                and s.expires is not None and s.expires > now())

    def claim(self, name: str, account: str, expected_minutes: float, commit: str | None) -> None:
        s = self.step(name)
        s.status, s.account, s.started, s.finished = RUNNING, account, stamp(), None
        s.expires = now() + (expected_minutes * 1.5 + 30) * 60
        s.attempts += 1
        s.commit, s.note = commit, None

    def finish(self, name: str, status: str, note: str | None = None) -> None:
        s = self.step(name)
        s.status, s.finished, s.expires, s.note = status, stamp(), None, note

    def summary(self) -> str:
        rows = [f"{n}: {s.status}" + (f" ({s.note})" if s.note else "") for n, s in self.steps.items()]
        return "\n".join(rows) or "(no steps yet)"
