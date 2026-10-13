"""Commit and push run outputs from Colab (or anywhere) to the run's branch.

- The token is passed per command as an HTTP header, never written to .git/config, and is
  redacted from any error text.
- Only paths handed in are staged, so a stray file in the clone is never committed.
- Size guardrail: no file over `max_file_mb`, no publish over `max_run_mb`; big outputs belong
  in the artifact store (store.py).
- Concurrent pushes from several accounts: on a rejected push the publisher rebases onto the
  remote and retries with backoff. Results live in per-run folders, so rebases do not conflict;
  if one still does, it pushes to `results/<run_id>` instead of losing the work.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)
MB = 1024**2


class PublishError(RuntimeError):
    pass


class PushConflict(PublishError):
    """The rebase onto the remote branch conflicted."""


class Publisher:
    def __init__(self, repo_dir: str | Path, branch: str, token: str | None = None,
                 max_file_mb: float = 5, max_run_mb: float = 50, remote: str = "origin",
                 retries: int = 4):
        self.repo = Path(repo_dir)
        self.branch = branch
        self.token = token
        self.max_file_mb, self.max_run_mb = max_file_mb, max_run_mb
        self.remote = remote
        self.retries = retries
        # git is not safe to drive from two threads at once (the live-progress heartbeat
        # publishes while a step runs), so every repository-changing call takes this lock
        self.lock = threading.RLock()

    # ---- git plumbing ------------------------------------------------------------------
    def _auth(self) -> list[str]:
        if not self.token:
            return []
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    def _redact(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "***")
            text = text.replace(base64.b64encode(f"x-access-token:{self.token}".encode()).decode(), "***")
        return text

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", *self._auth(), *args], cwd=self.repo,
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise PublishError(self._redact(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}"))
        return proc

    def head(self) -> str:
        return self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def ensure_identity(self, name: str = "UncertainScene Colab", email: str = "colab@uncertainscene.invalid") -> None:
        if not self.git("config", "user.email", check=False).stdout.strip():
            self.git("config", "user.email", email)
            self.git("config", "user.name", name)

    def check_push_access(self) -> None:
        """Dry-run push; fails fast at setup instead of after hours of compute."""
        self.git("push", "--dry-run", self.remote, f"HEAD:refs/heads/{self.branch}")

    def sync(self) -> None:
        """Bring in what other sessions pushed (claims, results) before reading state."""
        with self.lock:
            self._sync()

    def _sync(self) -> None:
        self.git("fetch", self.remote, self.branch)
        proc = self.git("rebase", f"{self.remote}/{self.branch}", check=False)
        if proc.returncode != 0:
            self.git("rebase", "--abort", check=False)
            raise PushConflict(self._redact(proc.stderr.strip()))

    def reset_to_remote(self) -> None:
        """Drop local commits that lost a race (used for claims only, never for results)."""
        with self.lock:
            self.git("fetch", self.remote, self.branch)
            self.git("reset", "--hard", f"{self.remote}/{self.branch}")

    # ---- guardrails --------------------------------------------------------------------
    def check_sizes(self, paths: list[str | Path]) -> float:
        total = 0
        for p in paths:
            p = self.repo / p
            files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
            for f in files:
                size = f.stat().st_size
                if size > self.max_file_mb * MB:
                    raise PublishError(
                        f"{f.relative_to(self.repo)} is {size / MB:.1f} MB (limit {self.max_file_mb} MB); "
                        "put it in the artifact store (uncertainscene.infra.store) and commit its manifest")
                total += size
        if total > self.max_run_mb * MB:
            raise PublishError(f"publish is {total / MB:.1f} MB (limit {self.max_run_mb} MB)")
        return total / MB

    # ---- publish -----------------------------------------------------------------------
    def publish(self, paths: list[str | Path], message: str, fallback_branch: str | None = None) -> bool:
        """Commit `paths` and push. Returns False if there was nothing to commit.

        On a rebase conflict: push to `fallback_branch` if given, else raise PushConflict.
        """
        with self.lock:
            return self._publish(paths, message, fallback_branch)

    def _publish(self, paths: list[str | Path], message: str, fallback_branch: str | None = None) -> bool:
        paths = [str(p) for p in paths if (self.repo / p).exists()]
        if not paths:
            return False
        self.check_sizes(paths)
        self.git("add", "--", *paths)
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.git("commit", "-m", message)
        delay = 2
        for attempt in range(self.retries + 1):
            proc = self.git("push", self.remote, f"HEAD:refs/heads/{self.branch}", check=False)
            if proc.returncode == 0:
                return True
            log.warning("push rejected (attempt %d): %s", attempt + 1, self._redact(proc.stderr.strip()))
            try:
                self._sync()
            except PushConflict:
                if fallback_branch:
                    self.git("push", self.remote, f"HEAD:refs/heads/{fallback_branch}")
                    log.warning("pushed to fallback branch %s instead of %s", fallback_branch, self.branch)
                    return True
                raise
            time.sleep(delay)
            delay *= 2
        raise PublishError(f"push to {self.branch} failed after {self.retries + 1} attempts")
