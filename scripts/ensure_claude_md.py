#!/usr/bin/env python3
"""Ensure every code directory carries a CLAUDE.md.

A code directory is any directory that directly contains at least one
git-tracked or untracked-but-not-ignored file. The repo root is exempt
(AGENTS.md covers it), and so are hidden directories like .github and
.claude.

Creates a stub CLAUDE.md wherever one is missing. Exit codes:
  0  nothing to do
  1  stubs created (pre-commit mode: stage them and commit again)
  2  stubs created (--hook mode: tells Claude Code to fill them in)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STUB = """\
# {name}

TODO: fill this in. One paragraph on what this directory is for, then the
files in it, how the code is invoked, and any gotchas. Copy the shape of
src/relic/graph/CLAUDE.md. House voice rules are in AGENTS.md.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state
only: history lives in the git log.
"""


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def code_dirs(root: Path) -> set[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )
    dirs: set[Path] = set()
    for line in out.stdout.splitlines():
        parent = Path(line).parent
        if parent == Path("."):
            continue
        if any(part.startswith(".") for part in parent.parts):
            continue
        dirs.add(parent)
    return dirs


def main() -> int:
    hook_mode = "--hook" in sys.argv[1:]
    root = repo_root()
    created: list[Path] = []
    for d in sorted(code_dirs(root)):
        target = root / d / "CLAUDE.md"
        if target.exists():
            continue
        target.write_text(STUB.format(name=d.as_posix()))
        created.append(target.relative_to(root))
    if not created:
        return 0
    stream = sys.stderr if hook_mode else sys.stdout
    print("created CLAUDE.md stubs, fill them in:", file=stream)
    for p in created:
        print(f"  {p}", file=stream)
    return 2 if hook_mode else 1


if __name__ == "__main__":
    sys.exit(main())
