"""Artifact store for files too big for git: GitHub release assets.

Raw videos, point clouds, splats, meshes and checkpoints go here. Any Colab account with the
GitHub token can read and write them, which Google Drive cannot offer across accounts.
Limits: 2 GB per file; keep the total sensible (GitHub has no hard cap on release storage,
but downloads are slow beyond a few GB per session).

Releases used as buckets (tags):
- `data`          raw recordings, one asset per video (`pair01_before_a.mp4`)
- `run-<run_id>`  large outputs of one run

What gets committed instead is a small manifest entry: name, tag, size, sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import requests

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
MAX_ASSET_BYTES = 2 * 1024**3


def repo_slug(repo_dir: str | Path) -> str:
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_dir,
                         capture_output=True, text=True, check=True).stdout.strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        raise ValueError(f"origin is not a GitHub remote: {url}")
    return m.group(1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    def __init__(self, slug: str, token: str | None = None):
        self.slug = slug
        self.s = requests.Session()
        token = token or os.environ.get("GITHUB_TOKEN")
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.headers["Accept"] = "application/vnd.github+json"

    def _release(self, tag: str, create: bool) -> dict:
        r = self.s.get(f"{API}/repos/{self.slug}/releases/tags/{tag}")
        if r.status_code == 404 and create:
            r = self.s.post(f"{API}/repos/{self.slug}/releases",
                            json={"tag_name": tag, "name": tag, "prerelease": True,
                                  "body": "Artifact bucket managed by uncertainscene.infra.store"})
        r.raise_for_status()
        return r.json()

    def put(self, path: str | Path, tag: str, name: str | None = None, overwrite: bool = False) -> dict:
        """Upload a file; returns the manifest entry to commit."""
        path = Path(path)
        size = path.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise ValueError(f"{path.name} is {size / 1024**3:.1f} GB; split it (limit 2 GB)")
        name = name or path.name
        rel = self._release(tag, create=True)
        for a in rel.get("assets", []):
            if a["name"] == name:
                if not overwrite:
                    raise FileExistsError(f"{name} already in release {tag}")
                self.s.delete(f"{API}/repos/{self.slug}/releases/assets/{a['id']}").raise_for_status()
        with open(path, "rb") as f:
            r = self.s.post(f"{UPLOADS}/repos/{self.slug}/releases/{rel['id']}/assets",
                            params={"name": name}, data=f,
                            headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()
        return {"name": name, "tag": tag, "bytes": size, "sha256": sha256(path)}

    def get(self, name: str, tag: str, dest: str | Path, expect_sha256: str | None = None) -> Path:
        """Download an asset to `dest` (a folder). Skips the download if a matching file exists."""
        dest = Path(dest) / name
        if dest.exists() and (expect_sha256 is None or sha256(dest) == expect_sha256):
            return dest
        rel = self._release(tag, create=False)
        asset = next((a for a in rel["assets"] if a["name"] == name), None)
        if asset is None:
            raise FileNotFoundError(f"{name} not in release {tag}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.s.get(asset["url"], headers={"Accept": "application/octet-stream"}, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if expect_sha256 and sha256(dest) != expect_sha256:
            raise IOError(f"checksum mismatch for {name}")
        return dest

    def list(self, tag: str) -> list[dict]:
        rel = self._release(tag, create=False)
        return [{"name": a["name"], "bytes": a["size"]} for a in rel["assets"]]


def write_manifest(entries: list[dict], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(entries, indent=2) + "\n")
