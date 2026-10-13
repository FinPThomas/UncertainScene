"""Build gsplat's CUDA code once per session, without running the machine out of RAM.

gsplat compiles its CUDA kernels the first time it is imported, with MAX_JOBS=10 parallel
compiler jobs unless told otherwise. In run 7 that build started inside the splat step, on top
of the ~6 GB the reconstruction and pose refinement already held: RAM went from 82% to 97% in
20 s and the session was lost. The compiler runs as child processes, so the guard's interrupt
could not land until the build returned.

`build()` therefore compiles in a separate process, with the number of jobs chosen from the RAM
that is actually free (`GB_PER_JOB` each), while this process watches system RAM every second.
If RAM still passes `kill_frac`, the build is stopped and retried with half the jobs (down to
one). The notebook calls it during setup, before any network is loaded; the splat step calls it
again (a no-op when the build exists) after freeing the reconstruction.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "gsplat_cuda"
GB_PER_JOB = 2.5          # a gsplat .cu file can take ~2 GB in nvcc/cicc; keep a margin
MAX_JOBS = 4
PROBE = "from gsplat.cuda._backend import _C; import sys; sys.exit(0 if _C is not None else 3)"


def _ram() -> tuple[float, float]:
    """(used GB, total GB) for the whole machine, child processes included."""
    import psutil

    vm = psutil.virtual_memory()
    return (vm.total - vm.available) / 1e9, vm.total / 1e9


def build_dir() -> Path:
    from torch.utils.cpp_extension import _get_build_directory

    return Path(_get_build_directory(NAME, verbose=False))


def built() -> bool:
    """True when gsplat will import without compiling (prebuilt wheel or an earlier build)."""
    try:
        from gsplat import csrc  # noqa: F401  (wheel with compiled kernels)

        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        d = build_dir()
    except Exception:  # noqa: BLE001
        return False
    return (d / f"{NAME}.so").exists() or (d / f"{NAME}.lib").exists()


def jobs_for(used_gb: float, total_gb: float, target_frac: float = 0.80) -> int:
    """Parallel compiler jobs that fit in RAM up to `target_frac` of the machine."""
    return int(max(1, min(MAX_JOBS, (total_gb * target_frac - used_gb) // GB_PER_JOB)))


def _stop(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)        # the build's nvcc/ninja children too
    except Exception:  # noqa: BLE001
        p.kill()
    p.wait()


def _run(cmd: list[str], jobs: int, kill_frac: float, check, poll_s: float) -> dict:
    env = {**os.environ, "MAX_JOBS": str(jobs)}
    t0 = time.time()
    peak = _ram()[0]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    try:
        while p.poll() is None:
            time.sleep(poll_s)      # the main thread stays in Python, so the guard can interrupt
            used, total = _ram()
            peak = max(peak, used)
            if used >= total * kill_frac:
                _stop(p)
                return {"ok": False, "jobs": jobs, "peak_ram_gb": round(peak, 1),
                        "seconds": round(time.time() - t0), "stopped": f"RAM {used:.1f}/{total:.1f} GB"}
            if check is not None:
                check()
    except BaseException:
        _stop(p)
        raise
    out = p.stdout.read() if p.stdout else ""
    return {"ok": p.returncode == 0, "jobs": jobs, "peak_ram_gb": round(peak, 1),
            "seconds": round(time.time() - t0), "tail": out[-400:] if p.returncode else ""}


def build(check=None, kill_frac: float = 0.88, poll_s: float = 1.0, cmd: list[str] | None = None) -> dict:
    """Make sure gsplat's CUDA code is built. Returns what happened (jobs, seconds, peak RAM)."""
    if cmd is None and built():
        return {"ok": True, "already_built": True}
    cmd = cmd or [sys.executable, "-c", PROBE]
    used, total = _ram()
    jobs = jobs_for(used, total)
    tries = []
    while True:
        log.info("building gsplat CUDA code with %d jobs (RAM %.1f/%.1f GB before)", jobs, used, total)
        r = _run(cmd, jobs, kill_frac, check, poll_s)
        tries.append(r)
        if r["ok"] or "stopped" not in r or jobs == 1:
            break
        log.warning("gsplat build stopped at %s; retrying with %d jobs", r["stopped"], max(1, jobs // 2))
        jobs = max(1, jobs // 2)
        time.sleep(3)
        used, total = _ram()
    last = tries[-1]
    return {"ok": last["ok"], "jobs": last["jobs"], "seconds": sum(t["seconds"] for t in tries),
            "peak_ram_gb": max(t["peak_ram_gb"] for t in tries), "ram_total_gb": round(total, 1),
            "tries": len(tries), "error": last.get("tail") or last.get("stopped", "")}
