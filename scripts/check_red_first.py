#!/usr/bin/env python3
"""Prove that a touched test would have failed before the change.

The cheapest bad test an agent can write is one that asserts whatever the code
already does. It is green on the first run, it raises the coverage number, and
it locks the current behavior in place whether or not that behavior is correct.
Reading the diff will not reliably catch it, because a test written to fit the
code looks exactly like a test written to catch a bug.

Running it will catch it. We take the tests from the PR, put them on top of the
base's source, and run them. A test that is motivated by a real gap goes red
there. A test written to fit the code stays green. That is the whole idea:

    tests from HEAD  +  source from the merge base  ->  at least one must fail

The gate needs one red, not all red. A PR that fixes a bug and also adds two
characterization tests around it is doing the right thing, and only the first of
those three proves anything. So we report the full red/green split and pass on
one red. The split is the useful artifact even when it passes: "3 tests touched,
1 red on base" is something you can eyeball in a second.

Isolation matters more than it looks. The base gets its own git worktree and its
own venv, because the editable installs in the main checkout point at absolute
paths in the main checkout. Reusing them would import HEAD's source into the
base's test run, every test would go red for the wrong reason, and the gate
would pass vacuously forever while looking like it worked.

Exit codes: 0 clean, 1 violation, 2 could-not-check. The base environment is
sanity-checked before we believe any red, because a red that comes from a broken
install is a false pass, and false passes are the only failure that matters here.

Refactors are the honest exception. REL-112 through REL-116 change structure and
not behavior, so their tests are supposed to be green on the base. Declare it:

    RED-FIRST: n/a -- <reason>

in the PR body. A refactor PR that cannot say "behavior unchanged" is a PR that
is not a refactor, which is worth finding out.

Usage:
    python3 scripts/check_red_first.py --base origin/main
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

NA_RE = re.compile(r"^\s*RED-FIRST:\s*n/?a\b[\s:-]*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
MIN_REASON = 10
SYNC_TIMEOUT = 600
TEST_TIMEOUT = 900


class CannotCheck(Exception):
    """Raised when we cannot establish the facts. Always exits 2, never 0."""


@dataclass(frozen=True)
class TestId:
    """A pytest node id we intend to run against the base."""

    path: str
    name: str  # "test_foo" or "TestThing::test_foo"

    @property
    def node(self) -> str:
        return f"{self.path}::{self.name}"


def git(*args: str, cwd: str | None = None) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise CannotCheck(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def clean_env() -> dict[str, str]:
    """Env for subprocesses in the base worktree.

    VIRTUAL_ENV has to go. If the caller is inside the main checkout's venv, uv
    honors it over the worktree's own, which is precisely the leak this gate
    exists to avoid.
    """
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    return env


def merge_base(base_ref: str) -> str:
    try:
        return git("merge-base", base_ref, "HEAD").strip()
    except CannotCheck as exc:
        raise CannotCheck(
            f"no merge base between {base_ref} and HEAD. In CI this usually means a shallow "
            f"clone: use actions/checkout with fetch-depth: 0 and fetch the base branch. ({exc})"
        ) from exc


def changed_test_files(base: str) -> list[str]:
    """Files under tests/ that the diff adds or modifies. Deletions are not our problem."""
    raw = git("diff", "--name-status", "--no-renames", f"{base}...HEAD", "--", "tests/")
    out: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() != "D":
            out.append(parts[-1].strip())
    return out


def changed_line_numbers(base: str, path: str) -> set[int]:
    raw = git("diff", "--unified=0", f"{base}...HEAD", "--", path)
    out: set[int] = set()
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
            out.add(lineno)
            lineno += 1
    return out


def touched_tests(base: str, files: list[str]) -> list[TestId]:
    """Test functions whose body the diff actually touched.

    Line-range intersection through the ast, so adding a test at the top of a
    file does not mark every test below it as touched the way a line count would.
    """
    found: list[TestId] = []
    for path in files:
        if not path.endswith(".py") or not Path(path).name.startswith("test_"):
            continue
        changed = changed_line_numbers(base, path)
        if not changed:
            continue
        try:
            tree = ast.parse(Path(path).read_text())
        except SyntaxError as exc:
            raise CannotCheck(f"cannot parse {path}: {exc}") from exc

        def span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> range:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return range(start, (node.end_lineno or node.lineno) + 1)

        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("test_") and changed & set(span(node)):
                    found.append(TestId(path, node.name))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if (
                        isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef)
                        and sub.name.startswith("test_")
                        and changed & set(span(sub))
                    ):
                        found.append(TestId(path, f"{node.name}::{sub.name}"))
    return found


def build_base_tree(base: str, tests_from_head: list[str]) -> str:
    """Check the base out into a temp worktree, then overlay HEAD's tests onto it."""
    tmp = tempfile.mkdtemp(prefix="red-first-")
    tree = str(Path(tmp) / "base")
    git("worktree", "add", "--detach", "--quiet", tree, base)

    for path in tests_from_head:
        src, dst = Path(path), Path(tree) / path
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tree


def sync_and_sanity_check(tree: str) -> None:
    """Install the base's own venv, then prove it works before believing any red.

    Without this, a broken install makes every test error, we read that as red,
    and the gate passes while checking nothing.
    """
    sync = subprocess.run(
        ["uv", "sync", "--dev"],
        cwd=tree,
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=SYNC_TIMEOUT,
    )
    if sync.returncode != 0:
        raise CannotCheck(f"uv sync failed in the base worktree:\n{sync.stderr.strip()[-2000:]}")

    probe = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", "import relic; print('ok')"],
        cwd=tree,
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=120,
    )
    if probe.returncode != 0 or "ok" not in probe.stdout:
        raise CannotCheck(
            "the base worktree cannot import relic, so a red there would mean nothing.\n"
            f"{probe.stderr.strip()[-2000:]}"
        )


def run_against_base(tree: str, tests: list[TestId]) -> dict[str, str]:
    """Run the node ids in the base worktree. Returns node id -> passed|red.

    Uses junit xml rather than the exit code because we have to tell "this test
    failed" apart from "pytest fell over". A collection error counts as red: a
    test importing a symbol the base does not have yet is the textbook red-first
    case, not a broken harness. The sanity check above is what earns us that.
    """
    report = str(Path(tree).parent / "report.xml")
    subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            "-o",
            "addopts=",
            *[t.node for t in tests],
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        env=clean_env(),
        timeout=TEST_TIMEOUT,
    )

    if not Path(report).exists():
        raise CannotCheck("pytest wrote no junit report in the base worktree")
    return parse_outcomes(report, tests)


def parse_outcomes(report: str, tests: list[TestId]) -> dict[str, str]:
    """Map each requested node id to red|passed|skipped by reading pytest's junit xml.

    Split out from the subprocess call so the matching can be tested without a
    real install. A collection error shows up as an <error> on a testcase whose
    name is the module, so we fall back to marking every test in that file red.
    """
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError as exc:
        raise CannotCheck(f"cannot parse the junit report: {exc}") from exc

    outcomes: dict[str, str] = {}
    collection_errors: set[str] = set()

    for case in root.iter("testcase"):
        file_attr = (case.get("file") or "").strip()
        classname = (case.get("classname") or "").strip()
        name = (case.get("name") or "").strip()
        red = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None

        matched = False
        for test in tests:
            cls = test.name.split("::")[0] if "::" in test.name else None
            func = test.name.split("::")[-1]
            dotted = test.path.replace("/", ".").removesuffix(".py")
            same_file = file_attr == test.path or dotted in classname
            if func == name and same_file and (cls is None or cls in classname):
                outcomes[test.node] = "skipped" if skipped else ("red" if red else "passed")
                matched = True
        # A collection error names the module, not a test, so nothing above matched.
        if not matched and red:
            for test in tests:
                dotted = test.path.replace("/", ".").removesuffix(".py")
                if file_attr == test.path or dotted in (classname, name):
                    collection_errors.add(test.path)

    for test in tests:
        if test.node not in outcomes and test.path in collection_errors:
            outcomes[test.node] = "red"
    return outcomes


def declared_na(body: str) -> str | None:
    m = NA_RE.search(body or "")
    if not m:
        return None
    reason = m.group(1).strip()
    return reason if len(reason) >= MIN_REASON else None


def warn_if_dirty() -> None:
    """Say so when uncommitted work is invisible to us. See check_weakening."""
    dirty = git("status", "--porcelain").strip()
    if not dirty:
        return
    print("red-first: NOTE the working tree is dirty. Which tests count as touched comes")
    print("red-first: from the committed diff, so uncommitted edits are not covered:")
    for line in dirty.splitlines()[:10]:
        print(f"    {line}")


def run(base_ref: str, pr_body: str) -> int:
    base = merge_base(base_ref)
    warn_if_dirty()
    files = changed_test_files(base)
    if not files:
        print("red-first: no test files touched. Nothing to prove here.")
        print("red-first: note this gate does not ask whether there SHOULD be a test.")
        return 0

    tests = touched_tests(base, files)
    if not tests:
        print(f"red-first: {len(files)} test file(s) touched, but no test function bodies changed.")
        return 0

    reason = declared_na(pr_body)
    if reason:
        print(f"red-first: {len(tests)} touched test(s), declared n/a by the PR body.")
        print(f'red-first: reason given: "{reason}"')
        print("red-first: skipping. This PR is on the record as claiming behavior is unchanged.")
        return 0

    print(f"red-first: {len(tests)} touched test(s). Running them against {base[:8]}.")
    tree = build_base_tree(base, files)
    try:
        sync_and_sanity_check(tree)
        outcomes = run_against_base(tree, tests)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", tree],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(Path(tree).parent, ignore_errors=True)

    missing = [t.node for t in tests if t.node not in outcomes]
    if missing:
        raise CannotCheck(
            "these touched tests never reported an outcome on the base, so we cannot say "
            "whether they were red:\n  " + "\n  ".join(missing)
        )

    red = [node for node, outcome in outcomes.items() if outcome == "red"]
    print("\nred-first: how the touched tests behaved on the base:\n")
    for node, outcome in sorted(outcomes.items()):
        mark = {"red": "RED  ", "passed": "green", "skipped": "skip "}[outcome]
        print(f"  {mark}  {node}")

    if red:
        print(
            f"\nred-first: passing. {len(red)} of {len(tests)} touched test(s) failed on the base."
        )
        print("red-first: the change is motivated by something a test can see.")
        return 0

    print(
        f"\nred-first: FAILED. All {len(tests)} touched test(s) already pass on the base,\n"
        "so they do not demonstrate the change fixes anything. Either the test asserts what\n"
        "the code already did (write one that fails first), or this is a refactor, in which\n"
        f"case say so with\n    RED-FIRST: n/a -- <reason, at least {MIN_REASON} characters>\n"
        "in the PR body."
    )
    if all(outcome == "skipped" for outcome in outcomes.values()):
        print(
            "\nred-first: note every touched test SKIPPED on the base. Live tests skip without\n"
            "OPENAI_API_KEY or FalkorDB, so this gate cannot see them. That is the n/a case."
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
        print(f"red-first: COULD NOT CHECK. {exc}", file=sys.stderr)
        print("red-first: exiting 2. This is not a pass.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"red-first: COULD NOT CHECK. Timed out: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
