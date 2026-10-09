"""Load the resource budget (configs/colab_budget.toml) and detect where we are running."""

from __future__ import annotations

import importlib.util
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BUDGET = REPO_ROOT / "configs" / "colab_budget.toml"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def platform() -> str:
    """'colab', 'kaggle' or 'local'."""
    if "COLAB_RELEASE_TAG" in os.environ or _has_module("google.colab"):
        return "colab"
    if "KAGGLE_KERNEL_RUN_TYPE" in os.environ:
        return "kaggle"
    return "local"


@dataclass(frozen=True)
class Budget:
    ram_soft_frac: float = 0.80
    ram_hard_frac: float = 0.92
    disk_soft_free_gb: float = 12.0
    disk_hard_free_gb: float = 5.0
    gpu_soft_frac: float = 0.90
    session_hours: float = 11.0
    safety_minutes: float = 20.0
    monitor_interval_s: float = 15.0
    max_file_mb: float = 5.0
    max_run_mb: float = 50.0
    work_dir: str = "/content/work"
    repo_dir: str = "/content/UncertainScene"


def load_budget(path: str | Path | None = None) -> Budget:
    data = tomllib.loads(Path(path or DEFAULT_BUDGET).read_text())
    kw = {
        "ram_soft_frac": data["ram"]["soft_frac"],
        "ram_hard_frac": data["ram"]["hard_frac"],
        "disk_soft_free_gb": data["disk"]["soft_free_gb"],
        "disk_hard_free_gb": data["disk"]["hard_free_gb"],
        "gpu_soft_frac": data["gpu"]["soft_frac"],
        "session_hours": data["time"]["session_hours"],
        "safety_minutes": data["time"]["safety_minutes"],
        "monitor_interval_s": data["monitor"]["interval_s"],
        "max_file_mb": data["git"]["max_file_mb"],
        "max_run_mb": data["git"]["max_run_mb"],
        "work_dir": data["paths"]["work_dir"],
        "repo_dir": data["paths"]["repo_dir"],
    }
    if platform() == "kaggle":
        kw["work_dir"] = "/kaggle/temp/work"
        kw["repo_dir"] = "/kaggle/working/UncertainScene"
    elif platform() == "local":
        kw["work_dir"] = str(REPO_ROOT / "outputs" / "work")
        kw["repo_dir"] = str(REPO_ROOT)
    return Budget(**kw)
