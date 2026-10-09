"""Check a commit message against docs/COMMITS.md. Usage: check_commit_msg.py <file|-m text>"""

import re
import sys

TYPES = "feat|fix|exp|results|docs|data|infra|refactor|test|chore"
PATTERN = re.compile(rf"^({TYPES})(\([a-z0-9][a-z0-9-]*\))?: [a-z0-9].{{0,70}}[^.]$")
ALLOWED_PREFIXES = ("Merge ", "Revert ", "fixup! ", "squash! ")


def check(message: str) -> str | None:
    lines = [ln for ln in message.splitlines() if not ln.startswith("#")]
    summary = lines[0].strip() if lines else ""
    if summary.startswith(ALLOWED_PREFIXES):
        return None
    if len(summary) > 72:
        return f"summary is {len(summary)} characters (max 72)"
    if not PATTERN.match(summary):
        return ("summary must look like '<type>(<scope>): <imperative summary>' with type one of "
                f"{TYPES.replace('|', ', ')}, lower case start, no full stop")
    if len(lines) > 1 and lines[1].strip():
        return "leave a blank line after the summary"
    return None


def main(argv: list[str]) -> int:
    text = argv[2] if argv[1:2] == ["-m"] else open(argv[1], encoding="utf-8").read()
    err = check(text)
    if err:
        print(f"commit message rejected: {err}\nsee docs/COMMITS.md", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
