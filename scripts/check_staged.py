"""Reject staged (or listed) files over the size limit or under data/raw/.

Usage: check_staged.py              # staged files (pre-commit hook)
       check_staged.py --all        # every tracked file (CI)
"""

import subprocess
import sys
from pathlib import Path

MAX_MB = 5


def files(all_tracked: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_tracked else ["git", "diff", "--cached", "--name-only", "--diff-filter=AM"]
    return [f for f in subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines() if f]


def main() -> int:
    bad = []
    for f in files("--all" in sys.argv):
        p = Path(f)
        if f.startswith("data/raw/"):
            bad.append(f"{f}: raw data never goes in git (use the artifact store)")
        elif p.is_file() and p.stat().st_size > MAX_MB * 1024**2:
            bad.append(f"{f}: {p.stat().st_size / 1024**2:.1f} MB > {MAX_MB} MB (use the artifact store)")
    for b in bad:
        print(b, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
