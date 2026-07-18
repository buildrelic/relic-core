#!/usr/bin/env python3
"""Fail a diff that silences a gate instead of satisfying it.

An unattended worker has two ways to turn a red check green: fix the code, or
silence the check. The second is cheaper, always available, and leaves a diff
that reads like progress. Nothing in CI can tell the difference after the fact,
because by then the gate is green either way. So we look at the diff itself and
fail on the edits whose only purpose is to stop an existing gate from firing.

Stdlib only, on purpose. This runs with no `uv sync`, so it reports in seconds
and goes red before the slow jobs have finished installing.

Exit codes are three-way: 0 clean, 1 violation, 2 could-not-check. A check that
could not run must never be mistaken for a check that passed, so every bail-out
path below exits 2 and says why.

What it flags. Each of these is pure suppression. Nothing else motivates them:

- ``# type: ignore``, ``# pyright: ignore``, ``# noqa`` on an added line.
- ``@pytest.mark.skip`` / ``.xfail``, or a bare ``pytest.skip()`` / ``.xfail()``.
  The suite has zero of these today, so this freezes a property we already hold
  rather than asking for a new one.
- A test function that exists on the base and is gone at head. Compared by name
  through the ast, not by counting lines, so moving a test does not trip it.
- pyproject gates loosened: ruff rules dropped from ``select``, per-file-ignores
  grown, pyright downgraded, an import-linter contract deleted or given an
  ``ignore_imports``, a rerun/flaky plugin added.

What it deliberately does not flag, because a check everyone learns to ignore is
worse than no check:

- ``@pytest.mark.skipif``. The live FalkorDB and OpenAI tests are built on it. It
  is the house idiom for "this needs a key we do not have in CI", and flagging it
  would fire on every honest live-test change.
- Widening a type to ``Any``. Real weakening, but it has too many legitimate uses
  (JSON payloads, ``**kwargs``) to separate from the dishonest ones mechanically.

Escape hatch: put ``ALLOW-WEAKENING: <reason>`` in the PR body. That is not a
loophole, it is the whole point. The gate exists so the choice lands in the PR
record where you will see it while skimming, not so the choice can never be made.

Usage:
    python3 scripts/check_weakening.py --base origin/main
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tomllib
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

# Two carve-outs, and only for the added-line scan. Both files have to write the
# patterns out as literals: this one names them in its docstring, and the test file
# has to plant them to prove they get caught. So both trip on themselves, which
# test_pattern_exemption_stays_exactly_two_files pins to exactly this list. It is a
# hole. It is two paths wide, it is matched by exact name so it cannot creep, and
# the deleted-test and config scans below ignore it entirely: deleting a test out
# of the exempt test file is still caught.
PATTERN_LITERAL_FILES = frozenset(
    {
        "scripts/check_weakening.py",
        "tests/test_check_weakening.py",
    }
)

SUPPRESSIONS: list[tuple[str, str]] = [
    (r"#\s*type:\s*ignore", "type: ignore silences pyright"),
    (r"#\s*pyright:\s*ignore", "pyright: ignore silences pyright"),
    (r"#\s*noqa", "noqa silences ruff"),
]

# skip(?!if) keeps @pytest.mark.skipif out of this. See the module docstring.
SKIP_MARKERS: list[tuple[str, str]] = [
    (r"@pytest\.mark\.skip(?!if)", "unconditional skip marker"),
    (r"@pytest\.mark\.xfail", "xfail marker"),
    (r"pytest\.mark\.skip(?!if)\(", "unconditional skip marker"),
    (r"pytest\.mark\.xfail\(", "xfail marker"),
    (r"(?<!import)\bpytest\.skip\(", "bare skip call"),
    (r"\bpytest\.xfail\(", "bare xfail call"),
]

RERUN_PLUGINS = ("pytest-rerunfailures", "flaky", "pytest-retry", "pytest-flakefinder")

# Laxer to stricter. An edit that moves down this list is a downgrade.
PYRIGHT_MODES = ["off", "basic", "standard", "strict"]

ALLOW_RE = re.compile(r"^\s*ALLOW-WEAKENING:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
MIN_REASON = 10


class CannotCheck(Exception):
    """Raised when we cannot establish the facts. Always exits 2, never 0."""


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str
    line: int | None = None

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}\n      {self.kind}: {self.detail}"


def git(*args: str) -> str:
    """Run a git command, or raise CannotCheck. Never returns a partial answer."""
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise CannotCheck(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def merge_base(base_ref: str) -> str:
    try:
        return git("merge-base", base_ref, "HEAD").strip()
    except CannotCheck as exc:
        raise CannotCheck(
            f"no merge base between {base_ref} and HEAD. In CI this usually means a shallow "
            f"clone: use actions/checkout with fetch-depth: 0 and fetch the base branch. ({exc})"
        ) from exc


def changed_files(base: str) -> list[tuple[str, str]]:
    """Return (status, path) for every file the diff touches. Status is git's letter."""
    raw = git("diff", "--name-status", "--no-renames", f"{base}...HEAD")
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0].strip(), parts[-1].strip()))
    return out


def added_lines(base: str, path: str) -> list[tuple[int, str]]:
    """Return (line number at head, text) for every line the diff adds to path."""
    raw = git("diff", "--unified=0", f"{base}...HEAD", "--", path)
    out: list[tuple[int, str]] = []
    lineno = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            if m:
                lineno = int(m.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            out.append((lineno, line[1:]))
            lineno += 1
    return out


def show(base: str, path: str) -> str | None:
    """File contents at the base, or None if it did not exist there."""
    proc = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _is_test(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
        "test_"
    )


def test_names(source: str, path: str) -> set[str]:
    """Every test function in source, as a pytest-ish name. Parse errors raise."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CannotCheck(f"cannot parse {path}: {exc}") from exc

    names: set[str] = set()
    for node in tree.body:
        if _is_test(node):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names.update(f"{node.name}::{sub.name}" for sub in node.body if _is_test(sub))
    return names


def scan_added_lines(base: str, files: list[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for status, path in files:
        if status == "D" or not path.endswith(".py") or path in PATTERN_LITERAL_FILES:
            continue
        for lineno, text in added_lines(base, path):
            for pattern, detail in SUPPRESSIONS + SKIP_MARKERS:
                if re.search(pattern, text):
                    findings.append(
                        Finding(
                            kind="suppression added",
                            path=path,
                            line=lineno,
                            detail=f"{detail}: {text.strip()[:80]}",
                        )
                    )
                    break
    return findings


def scan_deleted_tests(base: str, files: list[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for status, path in files:
        if not path.startswith("tests/") or not path.endswith(".py"):
            continue
        before = show(base, path)
        if before is None:
            continue  # New file. It cannot have deleted anything.
        after = "" if status == "D" else Path(path).read_text()
        gone = test_names(before, path) - (test_names(after, path) if after else set())
        findings.extend(
            Finding(kind="test deleted", path=path, detail=name) for name in sorted(gone)
        )
    return findings


def _as_set(value: object) -> set[str]:
    return set(value) if isinstance(value, list) else set()


def _dig(data: dict, *keys: str) -> dict:
    cur: object = data
    for key in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key, {})
    return cur if isinstance(cur, dict) else {}


def scan_gate_config(base: str, files: list[tuple[str, str]]) -> list[Finding]:
    """Compare pyproject's gate config structurally, not as text.

    Text diffing pyproject would fire on every dependency bump. What we care
    about is narrow: did the set of rules we enforce get smaller?
    """
    if not any(path == "pyproject.toml" for _, path in files):
        return []

    before_raw = show(base, "pyproject.toml")
    if before_raw is None:
        raise CannotCheck("pyproject.toml is in the diff but absent at the base")
    try:
        before = tomllib.loads(before_raw)
        after = tomllib.loads(Path("pyproject.toml").read_text())
    except tomllib.TOMLDecodeError as exc:
        raise CannotCheck(f"cannot parse pyproject.toml: {exc}") from exc

    findings: list[Finding] = []

    def add(detail: str) -> None:
        findings.append(Finding(kind="gate loosened", path="pyproject.toml", detail=detail))

    # Ruff: rules dropped from select, or ignores grown.
    b_lint, a_lint = _dig(before, "tool", "ruff", "lint"), _dig(after, "tool", "ruff", "lint")
    dropped = _as_set(b_lint.get("select")) - _as_set(a_lint.get("select"))
    if dropped:
        add(f"ruff select dropped {sorted(dropped)}")
    grew = _as_set(a_lint.get("ignore")) - _as_set(b_lint.get("ignore"))
    if grew:
        add(f"ruff ignore grew by {sorted(grew)}")

    b_pfi = _dig(before, "tool", "ruff", "lint", "per-file-ignores")
    a_pfi = _dig(after, "tool", "ruff", "lint", "per-file-ignores")
    for path, rules in a_pfi.items():
        new_rules = _as_set(rules) - _as_set(b_pfi.get(path))
        if new_rules:
            add(f"per-file-ignores for {path} grew by {sorted(new_rules)}")

    # Pyright: mode downgraded, or a report* rule switched off.
    b_py, a_py = _dig(before, "tool", "pyright"), _dig(after, "tool", "pyright")
    b_mode, a_mode = b_py.get("typeCheckingMode"), a_py.get("typeCheckingMode")
    if (
        isinstance(b_mode, str)
        and isinstance(a_mode, str)
        and b_mode in PYRIGHT_MODES
        and a_mode in PYRIGHT_MODES
        and PYRIGHT_MODES.index(a_mode) < PYRIGHT_MODES.index(b_mode)
    ):
        add(f"pyright typeCheckingMode downgraded {b_mode} -> {a_mode}")
    for key, value in a_py.items():
        if not key.startswith("report"):
            continue
        if value in (False, "none") and b_py.get(key) not in (False, "none"):
            add(f"pyright {key} switched off")

    # Import-linter: a contract deleted, or given an escape list.
    b_contracts = {
        c.get("name") for c in before.get("tool", {}).get("importlinter", {}).get("contracts", [])
    }
    a_all = after.get("tool", {}).get("importlinter", {}).get("contracts", [])
    for name in sorted(b_contracts - {c.get("name") for c in a_all}):
        add(f"import-linter contract deleted: {name!r}")
    for contract in a_all:
        for escape in ("ignore_imports", "unmatched_ignore_imports_alerting"):
            if contract.get(escape):
                add(f"import-linter contract {contract.get('name')!r} gained {escape}")

    # Pytest: a rerun plugin, or addopts that skip collection.
    dev_before = " ".join(before.get("dependency-groups", {}).get("dev", []))
    dev_after = " ".join(after.get("dependency-groups", {}).get("dev", []))
    for plugin in RERUN_PLUGINS:
        if plugin in dev_after and plugin not in dev_before:
            add(f"rerun/flaky plugin added: {plugin}. A retried test is a test that does not fail.")
    b_opts = _dig(before, "tool", "pytest", "ini_options").get("addopts", "")
    a_opts = _dig(after, "tool", "pytest", "ini_options").get("addopts", "")
    if isinstance(a_opts, str) and isinstance(b_opts, str):
        for flag in ("--ignore", "-p no:", "--reruns", "--deselect"):
            if flag in a_opts and flag not in b_opts:
                add(f"pytest addopts gained {flag!r}")

    return findings


def real_reason(raw: str) -> str | None:
    """A declared reason, or None if it is not one a human wrote.

    The length floor stops "ALLOW-WEAKENING: x". The angle-bracket check stops the
    likelier accident: our own failure message spells the line out as
    `<reason, at least 10 characters>`, which is 31 characters and would sail past
    a length check if someone pasted the error into the PR body.
    """
    reason = raw.strip()
    if len(reason) < MIN_REASON or "<" in reason or ">" in reason:
        return None
    return reason


def allowance(body: str) -> str | None:
    """The declared reason from the PR body, if it is a real one."""
    m = ALLOW_RE.search(body or "")
    return real_reason(m.group(1)) if m else None


def warn_if_dirty() -> None:
    """Say so when uncommitted work is invisible to us.

    We diff base...HEAD, which is what CI reviews and what ends up in the PR. Run
    locally mid-change that silently covers nothing, which is the exact shape of
    the bug this script exists to catch, so it gets said out loud instead.
    """
    dirty = git("status", "--porcelain").strip()
    if not dirty:
        return
    print("weakening: NOTE the working tree is dirty. This reads committed work only, so")
    print("weakening: the following are NOT covered by the result below:")
    for line in dirty.splitlines()[:10]:
        print(f"    {line}")


def run(base_ref: str, pr_body: str) -> int:
    base = merge_base(base_ref)
    warn_if_dirty()
    files = changed_files(base)
    if not files:
        print(f"weakening: no committed change against {base_ref} ({base[:8]}). Nothing to check.")
        return 0

    findings = (
        scan_added_lines(base, files)
        + scan_deleted_tests(base, files)
        + scan_gate_config(base, files)
    )

    print(f"weakening: scanned {len(files)} changed file(s) against {base[:8]}.")
    if not findings:
        print("weakening: clean. No gate was silenced.")
        return 0

    print(f"\nweakening: found {len(findings)} edit(s) that silence a gate:\n")
    for finding in findings:
        print(finding.render())

    reason = allowance(pr_body)
    if reason:
        print(f'\nweakening: allowed by the PR body. Reason given: "{reason}"')
        print("weakening: passing, but this PR is on the record as weakening a gate.")
        return 0

    print(
        "\nweakening: FAILED. Fix the code so the gate passes, or own the choice by putting\n"
        f"    ALLOW-WEAKENING: <reason, at least {MIN_REASON} characters>\n"
        "in the PR body. The reason is read by a human, so write it for one."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--base", default="origin/main", help="base ref to diff against")
    parser.add_argument(
        "--pr-body",
        default=os.environ.get("PR_BODY", ""),
        help="PR body text. Defaults to $PR_BODY.",
    )
    args = parser.parse_args()

    try:
        return run(args.base, args.pr_body)
    except CannotCheck as exc:
        print(f"weakening: COULD NOT CHECK. {exc}", file=sys.stderr)
        print("weakening: exiting 2. This is not a pass.", file=sys.stderr)
        return 2
    except Exception as exc:
        # Anything unexpected is a fact we could not establish, so it is a 2. An
        # uncaught traceback would exit 1, which reads as "this PR is bad" when it
        # means "this script is". Guessing wrong in that direction wastes an hour.
        traceback.print_exc()
        print(f"weakening: COULD NOT CHECK. Unexpected {type(exc).__name__}.", file=sys.stderr)
        print("weakening: exiting 2. This is not a pass, and it is not the PR's fault.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
