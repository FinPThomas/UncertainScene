"""Prepare labelled recordings: frames, previews, manifest, upload.

    python -m uncertainscene.prep --raw <folder with original videos> [--upload gh|api|none]

Reads data/recordings.json (original file name -> label), and for every recording not marked
`ignore`:
  1. checksums and probes the original;
  2. extracts sharp frames (default 3 fps, long side 1024 px) to data/frames/<label>/
     (committed: ~150 KB per frame, ~25 MB per minute of video, so any Colab clone has them);
  3. writes a contact sheet to data/previews/<label>.jpg (committed);
  4. with --upload gh|api, also uploads `<label>.<ext>` (the original, renamed) to the GitHub
     release `data` (needs a token that may create releases, e.g. from Colab);
  5. records everything in data/manifest.json.
Already-processed recordings (same sha256 in the manifest, assets present) are skipped.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from ..infra.budget import REPO_ROOT
from ..infra.store import Store, repo_slug, sha256
from .frames import contact_sheet, extract, probe

DATA = REPO_ROOT / "data"
TAG = "data"


def upload(path: Path, name: str, how: str) -> None:
    if how == "none":
        return
    if how == "gh":
        slug = repo_slug(REPO_ROOT)
        if subprocess.run(["gh", "release", "view", TAG, "-R", slug], capture_output=True).returncode:
            subprocess.run(["gh", "release", "create", TAG, "-R", slug, "--prerelease", "--title", TAG,
                            "--notes", "Raw recordings and frame sets (see data/manifest.json)"],
                           check=True, capture_output=True)
        staged = path.with_name(name)
        if staged != path:
            staged.unlink(missing_ok=True)
            staged.hardlink_to(path)
        subprocess.run(["gh", "release", "upload", TAG, str(staged), "-R", slug, "--clobber"], check=True)
        if staged != path:
            staged.unlink()
    else:
        Store(repo_slug(REPO_ROOT)).put(path, TAG, name=name, overwrite=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", required=True, help="folder holding the original videos")
    ap.add_argument("--recordings", default=str(DATA / "recordings.json"))
    ap.add_argument("--out", default=str(DATA / "frames"))
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--long-side", type=int, default=1024)
    ap.add_argument("--upload", choices=["gh", "api", "none"], default="none")
    ap.add_argument("--only", nargs="*", help="labels to process (default: all)")
    ap.add_argument("--delete-raw", action="store_true", help="delete each original after upload (saves disk)")
    args = ap.parse_args()

    recs = json.loads(Path(args.recordings).read_text())["recordings"]
    manifest_path = DATA / "manifest.json"
    manifest = {m["label"]: m for m in json.loads(manifest_path.read_text() or "[]")}
    out_root = Path(args.out)

    for r in recs:
        if r.get("ignore") or r.get("missing") or (args.only and r["label"] not in args.only):
            continue
        src = Path(args.raw) / r["original"]
        label = r["label"]
        if not src.exists():
            print(f"[skip] {label}: {src} not found")
            continue
        digest = sha256(src)
        done = manifest.get(label, {})
        if done.get("sha256") == digest and done.get("uploaded") == (args.upload != "none"):
            print(f"[done] {label}")
            continue
        info = probe(src)
        print(f"[{label}] {src.name}: {info.duration_s:.0f} s, {info.width}x{info.height} @ {info.fps:.0f} fps")
        fdir = out_root / label
        shutil.rmtree(fdir, ignore_errors=True)
        frames = extract(src, fdir, fps=args.fps, long_side=args.long_side)
        contact_sheet(fdir, DATA / "previews" / f"{label}.jpg")
        ext = src.suffix.lower()
        upload(src, f"{label}{ext}", args.upload)
        fbytes = sum(f.stat().st_size for f in fdir.iterdir())
        sharp = [f["sharpness"] for f in frames]
        manifest[label] = {
            **{k: r[k] for k in ("label", "original", "scene", "lighting", "state", "take") if k in r},
            "file": f"{label}{ext}", "release": TAG if args.upload != "none" else None, "bytes": src.stat().st_size, "sha256": digest,
            "video": info.as_dict(),
            "frames": {"dir": f"data/frames/{label}", "count": len(frames), "fps": args.fps,
                       "long_side": args.long_side, "bytes": fbytes,
                       "median_sharpness": sorted(sharp)[len(sharp) // 2],
                       "mean_brightness": round(sum(f["brightness"] for f in frames) / len(frames), 1)},
            "preview": f"data/previews/{label}.jpg",
            "uploaded": args.upload != "none",
        }
        manifest_path.write_text(json.dumps(sorted(manifest.values(), key=lambda m: m["label"]), indent=2) + "\n")
        print(f"[{label}] {len(frames)} frames, {fbytes / 1e6:.0f} MB")
        if args.delete_raw and args.upload != "none":
            src.unlink()


if __name__ == "__main__":
    main()
