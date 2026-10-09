"""Resource guardrails: keep a run inside Colab's RAM, disk and session-time allowance.

Colab kills the kernel without warning when RAM runs out, and a full disk corrupts git and
checkpoints. The guard checks usage before a step starts (`ensure_room`), and a background
monitor checks it while the step runs:

- soft limit: log a warning and call the registered soft callbacks (free caches, delete temp
  files, publish state);
- hard limit: stop the step by raising `ResourceLimit` in the main thread, so the runner can
  record the step as interrupted and publish before Colab would have killed it.
"""

from __future__ import annotations

import _thread
import gc
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil

from .budget import Budget

log = logging.getLogger(__name__)
GB = 1024**3


class ResourceLimit(RuntimeError):
    """Raised when a hard limit is hit or a step would not fit."""


@dataclass
class Usage:
    ram_used_gb: float
    ram_total_gb: float
    disk_free_gb: float
    gpu_used_gb: float | None = None
    gpu_total_gb: float | None = None

    @property
    def ram_frac(self) -> float:
        return self.ram_used_gb / self.ram_total_gb

    @property
    def gpu_frac(self) -> float | None:
        if self.gpu_used_gb is None or not self.gpu_total_gb:
            return None
        return self.gpu_used_gb / self.gpu_total_gb

    def line(self) -> str:
        gpu = "" if self.gpu_frac is None else f", GPU {self.gpu_used_gb:.1f}/{self.gpu_total_gb:.1f} GB"
        return f"RAM {self.ram_used_gb:.1f}/{self.ram_total_gb:.1f} GB, disk free {self.disk_free_gb:.1f} GB{gpu}"


def read_usage(work_dir: str | Path) -> Usage:
    vm = psutil.virtual_memory()
    path = Path(work_dir)
    while not path.exists():
        path = path.parent
    usage = Usage(
        ram_used_gb=(vm.total - vm.available) / GB,
        ram_total_gb=vm.total / GB,
        disk_free_gb=shutil.disk_usage(path).free / GB,
    )
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.splitlines()[0]
            used, total = (float(x) / 1024 for x in out.split(","))
            usage.gpu_used_gb, usage.gpu_total_gb = used, total
        except (subprocess.SubprocessError, IndexError, ValueError):
            pass
    return usage


class Guard:
    def __init__(self, budget: Budget, reader: Callable[[], Usage] | None = None,
                 start_time: float | None = None):
        self.budget = budget
        self.read = reader or (lambda: read_usage(budget.work_dir))
        self.start_time = start_time if start_time is not None else time.time()
        self.soft_callbacks: list[Callable[[], None]] = [gc.collect]
        self.tripped: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- checks -------------------------------------------------------------------------
    def level(self, u: Usage) -> tuple[str, str]:
        """('ok' | 'soft' | 'hard', reason)."""
        b = self.budget
        if u.ram_frac >= b.ram_hard_frac:
            return "hard", f"RAM at {u.ram_frac:.0%} (hard limit {b.ram_hard_frac:.0%})"
        if u.disk_free_gb <= b.disk_hard_free_gb:
            return "hard", f"disk free {u.disk_free_gb:.1f} GB (hard limit {b.disk_hard_free_gb} GB)"
        if u.ram_frac >= b.ram_soft_frac:
            return "soft", f"RAM at {u.ram_frac:.0%}"
        if u.disk_free_gb <= b.disk_soft_free_gb:
            return "soft", f"disk free {u.disk_free_gb:.1f} GB"
        if u.gpu_frac is not None and u.gpu_frac >= b.gpu_soft_frac:
            return "soft", f"GPU memory at {u.gpu_frac:.0%}"
        return "ok", ""

    def minutes_left(self) -> float:
        end = self.start_time + self.budget.session_hours * 3600
        return (end - time.time()) / 60 - self.budget.safety_minutes

    def ensure_room(self, disk_gb: float = 0, ram_gb: float = 0, minutes: float = 0) -> None:
        """Raise ResourceLimit unless a step needing this much can finish safely."""
        u = self.read()
        b = self.budget
        problems = []
        if u.disk_free_gb - disk_gb < b.disk_hard_free_gb:
            problems.append(f"needs {disk_gb} GB disk, {u.disk_free_gb:.1f} GB free "
                            f"(keeps {b.disk_hard_free_gb} GB spare)")
        ram_cap = u.ram_total_gb * b.ram_hard_frac
        if u.ram_used_gb + ram_gb > ram_cap:
            problems.append(f"needs {ram_gb} GB RAM, {ram_cap - u.ram_used_gb:.1f} GB usable")
        if minutes and minutes > self.minutes_left():
            problems.append(f"needs {minutes:.0f} min, {self.minutes_left():.0f} min left in session")
        if problems:
            raise ResourceLimit("; ".join(problems))

    def check(self) -> Usage:
        """One check; runs soft callbacks or raises ResourceLimit. Safe to call inside loops."""
        u = self.read()
        lvl, why = self.level(u)
        if lvl == "soft":
            log.warning("guard soft limit: %s (%s)", why, u.line())
            for cb in self.soft_callbacks:
                try:
                    cb()
                except Exception:  # a cleanup failing must not kill the run
                    log.exception("soft callback failed")
        elif lvl == "hard":
            self.tripped = why
            raise ResourceLimit(why)
        return u

    # ---- background monitor -------------------------------------------------------------
    def start(self) -> "Guard":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="uncertainscene-guard", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.budget.monitor_interval_s):
            try:
                self.check()
            except ResourceLimit as e:
                log.error("guard hard limit: %s; interrupting the running step", e)
                _thread.interrupt_main()  # arrives as KeyboardInterrupt; Run converts it
                return
            except Exception:
                log.exception("guard check failed")
