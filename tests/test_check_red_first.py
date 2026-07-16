"""Tests for the red-first gate.

The expensive half of this script (worktree, uv sync, pytest) is exercised by
running it on a real branch, not from here. What these tests pin down is the
logic that decides WHICH tests to run and WHAT their outcome was, because that
is where a wrong answer is silent. Misreading a junit report as "red" would let
every PR through while the job stayed green, which is the only failure mode of
this gate that actually costs anything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import check_red_first as rf

BASE_TEST = """\
def test_alpha():
    assert 1 == 1


def test_beta():
    assert 2 == 2
"""

TESTS = "tests/test_thing.py"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "gate")
    (root / TESTS).write_text(BASE_TEST)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "work")
    monkeypatch.chdir(root)
    return root


def change(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "change")


def junit(tmp_path: Path, body: str) -> str:
    path = tmp_path / "report.xml"
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'
    )
    return str(path)


def case(name: str, inner: str = "", classname: str = "tests.test_thing") -> str:
    """One <testcase>, shaped the way pytest's junit writer shapes them."""
    return f'<testcase classname="{classname}" file="{TESTS}" name="{name}">{inner}</testcase>'


# --- which tests are touched -------------------------------------------------


def test_touched_tests_finds_only_the_changed_one(repo: Path) -> None:
    """Adding a test must not mark its neighbours as touched."""
    change(repo, {TESTS: BASE_TEST + "\n\ndef test_gamma():\n    assert 3 == 3\n"})
    base = rf.merge_base("main")
    touched = rf.touched_tests(base, rf.changed_test_files(base))
    assert [t.name for t in touched] == ["test_gamma"]


def test_touched_tests_catches_an_edited_body(repo: Path) -> None:
    edited = BASE_TEST.replace("assert 2 == 2", "assert 2 == 3")
    change(repo, {TESTS: edited})
    base = rf.merge_base("main")
    touched = rf.touched_tests(base, rf.changed_test_files(base))
    assert [t.name for t in touched] == ["test_beta"]


def test_touched_tests_includes_decorators(repo: Path) -> None:
    """A test whose only change is a new decorator still counts as touched."""
    decorated = BASE_TEST.replace(
        "def test_beta():", "@pytest.mark.parametrize('x', [1])\ndef test_beta(x):"
    )
    change(repo, {TESTS: decorated})
    base = rf.merge_base("main")
    touched = rf.touched_tests(base, rf.changed_test_files(base))
    assert [t.name for t in touched] == ["test_beta"]


def test_touched_tests_handles_classes(repo: Path) -> None:
    change(
        repo,
        {TESTS: BASE_TEST + "\n\nclass TestGroup:\n    def test_inner(self):\n        assert 1\n"},
    )
    base = rf.merge_base("main")
    touched = rf.touched_tests(base, rf.changed_test_files(base))
    assert [t.node for t in touched] == [f"{TESTS}::TestGroup::test_inner"]


def test_new_test_file_is_all_touched(repo: Path) -> None:
    change(repo, {"tests/test_new.py": "def test_fresh():\n    assert 1\n"})
    base = rf.merge_base("main")
    touched = rf.touched_tests(base, rf.changed_test_files(base))
    assert [t.name for t in touched] == ["test_fresh"]


def test_non_test_files_are_ignored(repo: Path) -> None:
    change(repo, {"tests/conftest.py": "import pytest\n"})
    base = rf.merge_base("main")
    assert rf.touched_tests(base, rf.changed_test_files(base)) == []


def test_source_only_change_touches_nothing(repo: Path) -> None:
    change(repo, {"src/thing.py": "def add(a, b):\n    return a + b\n"})
    base = rf.merge_base("main")
    assert rf.changed_test_files(base) == []


# --- reading the junit report ------------------------------------------------


def test_parse_outcomes_reads_a_failure(tmp_path: Path) -> None:
    report = junit(tmp_path, case("test_alpha", "<failure>boom</failure>"))
    tests = [rf.TestId(TESTS, "test_alpha")]
    assert rf.parse_outcomes(report, tests) == {f"{TESTS}::test_alpha": "red"}


def test_parse_outcomes_reads_a_pass(tmp_path: Path) -> None:
    report = junit(tmp_path, case("test_alpha"))
    tests = [rf.TestId(TESTS, "test_alpha")]
    assert rf.parse_outcomes(report, tests) == {f"{TESTS}::test_alpha": "passed"}


def test_parse_outcomes_reads_a_skip(tmp_path: Path) -> None:
    """A skip is not a red. Live tests skip in CI and prove nothing there."""
    report = junit(tmp_path, case("test_alpha", "<skipped/>"))
    tests = [rf.TestId(TESTS, "test_alpha")]
    assert rf.parse_outcomes(report, tests) == {f"{TESTS}::test_alpha": "skipped"}


def test_collection_error_counts_as_red(tmp_path: Path) -> None:
    """A test importing a symbol the base lacks is the textbook red-first case.

    pytest reports it against the module, not the test, so the fallback has to
    attribute it to every test we asked for in that file.
    """
    report = junit(
        tmp_path,
        case(TESTS, "<error>ImportError: cannot import name 'new_thing'</error>"),
    )
    tests = [rf.TestId(TESTS, "test_alpha"), rf.TestId(TESTS, "test_beta")]
    assert rf.parse_outcomes(report, tests) == {
        f"{TESTS}::test_alpha": "red",
        f"{TESTS}::test_beta": "red",
    }


def test_parse_outcomes_matches_class_tests(tmp_path: Path) -> None:
    report = junit(
        tmp_path,
        case("test_inner", "<failure>boom</failure>", classname="tests.test_thing.TestGroup"),
    )
    tests = [rf.TestId(TESTS, "TestGroup::test_inner")]
    assert rf.parse_outcomes(report, tests) == {f"{TESTS}::TestGroup::test_inner": "red"}


def test_parse_outcomes_omits_a_test_that_never_ran(tmp_path: Path) -> None:
    """An unreported test must not silently read as red. run() turns this into exit 2."""
    report = junit(tmp_path, case("test_alpha"))
    tests = [rf.TestId(TESTS, "test_alpha"), rf.TestId(TESTS, "test_ghost")]
    assert f"{TESTS}::test_ghost" not in rf.parse_outcomes(report, tests)


def test_unparseable_report_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.xml"
    path.write_text("<not-xml")
    with pytest.raises(rf.CannotCheck):
        rf.parse_outcomes(str(path), [rf.TestId(TESTS, "test_alpha")])


# --- the n/a declaration -----------------------------------------------------


def test_declared_na_accepts_a_real_reason() -> None:
    assert rf.declared_na("RED-FIRST: n/a -- pure refactor, behavior unchanged")


def test_declared_na_rejects_a_token_reason() -> None:
    assert rf.declared_na("RED-FIRST: n/a -- x") is None


def test_declared_na_rejects_our_own_placeholder() -> None:
    """Pasting the failure message back into the PR body must not bypass the gate."""
    assert rf.declared_na("RED-FIRST: n/a -- <reason, at least 10 characters>") is None


def test_declared_na_ignores_an_unrelated_body() -> None:
    assert rf.declared_na("Fixes a bug.\n\nSome prose about the change.") is None


# --- the top-level decisions -------------------------------------------------


def test_no_test_files_touched_passes(repo: Path) -> None:
    change(repo, {"README.md": "hello\n"})
    assert rf.run("main", "") == 0


def test_declared_na_short_circuits_before_any_install(repo: Path) -> None:
    """The n/a path must not pay for a worktree and a sync."""
    change(repo, {TESTS: BASE_TEST + "\n\ndef test_gamma():\n    assert 3 == 3\n"})
    assert rf.run("main", "RED-FIRST: n/a -- pure refactor, behavior unchanged") == 0


def test_missing_base_ref_exits_2_not_0(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(sys, "argv", ["check_red_first.py", "--base", "origin/nope"])
    assert rf.main() == 2


def test_clean_env_drops_the_callers_venv() -> None:
    """The leak this gate exists to avoid: VIRTUAL_ENV pointing at the main checkout."""
    env = rf.clean_env()
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
