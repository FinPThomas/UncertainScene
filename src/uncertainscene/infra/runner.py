"""Run a sequence of steps that resumes across Colab sessions and Google accounts.

Typical use in a notebook (experiment code goes in `fn`, which writes into `out`):

    run = Run.from_env(run_id="2026-10-12_recon-mast3r", path="recon", branch="exp/recon/mast3r")
    run.step("reconstruct_pair01", fn, needs=dict(disk_gb=8, ram_gb=6, minutes=40))
    run.close()

For each step the runner:
1. pulls the latest state from GitHub and skips the step if it is done, or if another account
   holds a live claim on it;
2. refuses to start it unless the guard says the RAM, disk and session time it needs are
   available (better to stop and let the next session do it than to be killed half-way);
3. claims it and pushes the claim;
4. runs `fn(out, work)`: `out` is results/<path>/<run_id>/<step>/ in the repo (small files,
   committed); `work` is a scratch folder on local disk (big files, deleted afterwards);
5. records done / failed / interrupted and publishes results and state together.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback
import uuid
from pathlib import Path
from typing import Callable

from .budget import Budget, load_budget, platform
from .guard import Guard, ResourceLimit
from .publish import Publisher, PublishError, PushConflict
from .state import DONE, FAILED, INTERRUPTED, RunState

log = logging.getLogger(__name__)

StepFn = Callable[[Path, Path], "str | None"]  # returns an optional one-line result note


class Run:
    def __init__(self, run_id: str, path: str, branch: str, publisher: Publisher | None,
                 budget: Budget, guard: Guard, account: str):
        self.run_id, self.path, self.branch = run_id, path, branch
        self.pub, self.budget, self.guard, self.account = publisher, budget, guard, account
        self.repo = Path(budget.repo_dir)
        self.work_root = Path(budget.work_dir) / run_id
        self.state = RunState.load_or_new(self.repo, run_id, path, branch)

    @classmethod
    def from_env(cls, run_id: str, path: str, branch: str, account: str | None = None,
                 token: str | None = None, budget: Budget | None = None, monitor: bool = True) -> "Run":
        """Build a run from the notebook environment. `account` labels which Google account ran
        a step (any short name, e.g. 'fin-2'; a random suffix is added per session); `token` defaults to $GITHUB_TOKEN."""
        budget = budget or load_budget()
        token = token or os.environ.get("GITHUB_TOKEN")
        pub = Publisher(budget.repo_dir, branch, token, budget.max_file_mb, budget.max_run_mb)
        guard = Guard(budget)
        if monitor:
            guard.start()
        # A random suffix makes every session distinct, even two sessions on the same account.
        label = account or os.environ.get("UNCERTAINSCENE_ACCOUNT") or platform()
        account = f"{label}-{uuid.uuid4().hex[:4]}"
        run = cls(run_id, path, branch, pub, budget, guard, account)
        guard.soft_callbacks.append(run.clean_work)
        return run

    # ---- helpers -----------------------------------------------------------------------
    def out_dir(self, step: str) -> Path:
        return self.repo / "results" / self.path / self.run_id / step

    def state_rel(self) -> Path:
        return RunState.file(self.repo, self.run_id).relative_to(self.repo)

    def clean_work(self) -> None:
        """Soft-limit callback: delete scratch files of steps that are not running."""
        if not self.work_root.exists():
            return
        for d in self.work_root.iterdir():
            if d.is_dir() and self.state.step(d.name).status != "running":
                shutil.rmtree(d, ignore_errors=True)

    def _refresh(self) -> None:
        if self.pub:
            try:
                self.pub.sync()
            except PushConflict:
                self.pub.reset_to_remote()
        self.state = RunState.load_or_new(self.repo, self.run_id, self.path, self.branch)

    def _publish(self, paths: list[Path], message: str, fallback: bool = True) -> None:
        self.state.save(self.repo)
        if not self.pub:
            return
        rel = [p.relative_to(self.repo) if p.is_absolute() else p for p in paths] + [self.state_rel()]
        self.pub.publish(rel, message, fallback_branch=f"results/{self.run_id}" if fallback else None)

    def _claim(self, name: str, minutes: float) -> bool:
        commit = self.pub.head() if self.pub else None
        for _ in range(3):
            if self.state.is_done(name) or self.state.claimed_by_other(name, self.account):
                return False
            self.state.claim(name, self.account, minutes, commit)
            try:
                self._publish([], f"results({self.path}): claim {self.run_id}/{name} ({self.account})",
                              fallback=False)
                return True
            except PushConflict:  # another account pushed a claim at the same moment
                self.pub.reset_to_remote()
                self.state = RunState.load_or_new(self.repo, self.run_id, self.path, self.branch)
        return False

    # ---- the step ----------------------------------------------------------------------
    def step(self, name: str, fn: StepFn, needs: dict | None = None, retry_failed: bool = False) -> str:
        """Run one step. Returns its final status ('done', 'skipped', 'not_started', ...).

        A step that failed with an error is not retried automatically (it would usually fail
        again and burn GPU time); fix the code and pass retry_failed=True. Interrupted steps
        (resource limit, session end) are always resumed."""
        needs = {"disk_gb": 0, "ram_gb": 0, "minutes": 0, **(needs or {})}
        self._refresh()
        if self.state.is_done(name):
            log.info("step %s already done, skipping", name)
            return "skipped"
        if self.state.step(name).status == FAILED and not retry_failed:
            log.info("step %s failed before (%s); pass retry_failed=True to rerun", name, self.state.step(name).note)
            return "skipped"
        if self.state.claimed_by_other(name, self.account):
            log.info("step %s is running on account %s, skipping", name, self.state.step(name).account)
            return "skipped"
        try:
            self.guard.ensure_room(**needs)
        except ResourceLimit as e:
            log.warning("not starting %s: %s", name, e)
            return "not_started"
        if not self._claim(name, needs["minutes"]):
            return "skipped"

        out, work = self.out_dir(name), self.work_root / name
        out.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        status, note = DONE, None
        try:
            note = fn(out, work)
        except (ResourceLimit, KeyboardInterrupt) as e:
            status, note = INTERRUPTED, f"stopped: {self.guard.tripped or type(e).__name__}"
        except Exception as e:
            status, note = FAILED, f"{type(e).__name__}: {e}"
            (out / "error.txt").write_text(traceback.format_exc())
        finally:
            shutil.rmtree(work, ignore_errors=True)

        minutes = (time.time() - t0) / 60
        (out / "step_info.json").write_text(json.dumps({
            "run_id": self.run_id, "step": name, "status": status, "account": self.account,
            "commit": self.state.step(name).commit, "minutes": round(minutes, 1),
            "usage_at_end": self.guard.read().line(), "note": note,
        }, indent=2) + "\n")
        self.state.finish(name, status, note)
        try:
            self._publish([out], f"results({self.path}): {self.run_id}/{name} {status}")
        except PublishError as e:
            log.error("publishing %s failed: %s (results are still in %s)", name, e, out)
            raise
        log.info("step %s %s in %.1f min", name, status, minutes)
        if status == INTERRUPTED:
            raise ResourceLimit(note)
        return status

    def close(self) -> None:
        self.guard.stop()
        log.info("run %s:\n%s", self.run_id, self.state.summary())
