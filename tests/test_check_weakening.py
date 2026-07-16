"""Tests for the weakening gate.

Every check gets a test that proves it fires and, where the distinction is one we
made on purpose, a test that proves it does not fire on the honest version of the
same edit. A gate nobody has watched fail is a gate that does not work, and this
project has shipped that failure before: the landing repo's boundary check
printed "boundary intact" and exited 0 while every check silently no-oped.

This file is the one path exempt from the added-line scan, because the fixtures
below have to write `# type: ignore` and friends out as real literals to prove
they get caught. test_scans_itself_clean covers the script itself, which is not
exempt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import check_weakening as cw

BASE_PYPROJECT = """\
[dependency-groups]
dev = ["ruff>=0.6", "pyright>=1.1.380", "pytest>=8.3"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pyright]
typeCheckingMode = "basic"
reportMissingTypeStubs = false

[tool.pytest.ini_options]
addopts = "-q"

[tool.importlinter]
root_package = "relic"

[[tool.importlinter.contracts]]
name = "ingest, graph, serve are independent siblings"
type = "independence"
modules = ["relic.ingest", "relic.graph", "relic.serve"]
"""

BASE_TEST = """\
def test_a():
    assert 1 == 1


def test_b():
    assert 2 == 2
"""

THING = "src/thing.py"
TESTS = "tests/test_thing.py"
PYPROJECT = "pyproject.toml"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo with a `main` baseline and a `work` branch checked out."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "gate")

    (root / PYPROJECT).write_text(BASE_PYPROJECT)
    (root / TESTS).write_text(BASE_TEST)
    (root / THING).write_text("def add(a, b):\n    return a + b\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "work")

    monkeypatch.chdir(root)
    return root


def change(root: Path, files: dict[str, str]) -> None:
    """Write each path with its content and commit them on `work`."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "change")


def test_clean_diff_passes(repo: Path) -> None:
    change(
        repo, {THING: "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"}
    )
    assert cw.run("main", "") == 0


def test_no_changes_passes(repo: Path) -> None:
    assert cw.run("main", "") == 0


def test_flags_type_ignore(repo: Path) -> None:
    change(repo, {THING: "def add(a, b):\n    return a + b  # type: ignore\n"})
    assert cw.run("main", "") == 1


def test_flags_pyright_ignore(repo: Path) -> None:
    change(repo, {THING: "def add(a, b):\n    return a + b  # pyright: ignore[reportGeneral]\n"})
    assert cw.run("main", "") == 1


def test_flags_noqa(repo: Path) -> None:
    change(repo, {THING: "import os  # noqa: F401\n\n\ndef add(a, b):\n    return a + b\n"})
    assert cw.run("main", "") == 1


def test_flags_skip_marker(repo: Path) -> None:
    added = "\n\n@pytest.mark.skip(reason='later')\ndef test_c():\n    assert 0\n"
    change(repo, {TESTS: BASE_TEST + added})
    assert cw.run("main", "") == 1


def test_flags_xfail_marker(repo: Path) -> None:
    change(repo, {TESTS: BASE_TEST + "\n\n@pytest.mark.xfail\ndef test_c():\n    assert 0\n"})
    assert cw.run("main", "") == 1


def test_flags_bare_pytest_skip_call(repo: Path) -> None:
    change(repo, {TESTS: BASE_TEST + "\n\ndef test_c():\n    pytest.skip('nope')\n"})
    assert cw.run("main", "") == 1


def test_does_not_flag_skipif(repo: Path) -> None:
    """skipif is the house idiom for live tests. Flagging it would kill the gate."""
    added = (
        "\n\n@pytest.mark.skipif(not os.environ.get('OPENAI_API_KEY'), reason='needs a key')\n"
        "def test_c():\n    assert 1\n"
    )
    change(repo, {TESTS: BASE_TEST + added})
    assert cw.run("main", "") == 0


def test_flags_deleted_test(repo: Path) -> None:
    change(repo, {TESTS: "def test_a():\n    assert 1 == 1\n"})
    assert cw.run("main", "") == 1


def test_flags_test_file_deleted_wholesale(repo: Path) -> None:
    (repo / TESTS).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "drop tests")
    assert cw.run("main", "") == 1


def test_does_not_flag_reordered_tests(repo: Path) -> None:
    """Moving a test around is not deleting it. Names come from the ast, not line counts."""
    change(
        repo, {TESTS: "def test_b():\n    assert 2 == 2\n\n\ndef test_a():\n    assert 1 == 1\n"}
    )
    assert cw.run("main", "") == 0


def test_does_not_flag_added_test(repo: Path) -> None:
    change(repo, {TESTS: BASE_TEST + "\n\ndef test_c():\n    assert 3 == 3\n"})
    assert cw.run("main", "") == 0


def test_flags_ruff_rule_dropped(repo: Path) -> None:
    dropped = BASE_PYPROJECT.replace(
        'select = ["E", "F", "I", "UP", "B", "SIM"]', 'select = ["E", "F"]'
    )
    change(repo, {PYPROJECT: dropped})
    assert cw.run("main", "") == 1


def test_flags_per_file_ignore_added(repo: Path) -> None:
    grown = BASE_PYPROJECT + '\n[tool.ruff.lint.per-file-ignores]\n"src/thing.py" = ["E501"]\n'
    change(repo, {PYPROJECT: grown})
    assert cw.run("main", "") == 1


def test_flags_pyright_downgrade(repo: Path) -> None:
    off = BASE_PYPROJECT.replace('typeCheckingMode = "basic"', 'typeCheckingMode = "off"')
    change(repo, {PYPROJECT: off})
    assert cw.run("main", "") == 1


def test_flags_pyright_report_switched_off(repo: Path) -> None:
    silenced = BASE_PYPROJECT.replace(
        "[tool.pytest.ini_options]",
        "reportAttributeAccessIssue = false\n\n[tool.pytest.ini_options]",
    )
    change(repo, {PYPROJECT: silenced})
    assert cw.run("main", "") == 1


def test_flags_import_linter_contract_deleted(repo: Path) -> None:
    trimmed = BASE_PYPROJECT.split("[[tool.importlinter.contracts]]")[0]
    change(repo, {PYPROJECT: trimmed})
    assert cw.run("main", "") == 1


def test_flags_import_linter_ignore_imports(repo: Path) -> None:
    escaped = BASE_PYPROJECT + '\nignore_imports = ["relic.graph -> relic.ingest"]\n'
    change(repo, {PYPROJECT: escaped})
    assert cw.run("main", "") == 1


def test_flags_rerun_plugin_added(repo: Path) -> None:
    flaky = BASE_PYPROJECT.replace('"pytest>=8.3"', '"pytest>=8.3", "pytest-rerunfailures>=14"')
    change(repo, {PYPROJECT: flaky})
    assert cw.run("main", "") == 1


def test_flags_addopts_ignore(repo: Path) -> None:
    skipped = BASE_PYPROJECT.replace(
        'addopts = "-q"', 'addopts = "-q --ignore=tests/test_thing.py"'
    )
    change(repo, {PYPROJECT: skipped})
    assert cw.run("main", "") == 1


def test_does_not_flag_dependency_bump(repo: Path) -> None:
    """pyproject changes constantly. Only gate config should trip this."""
    change(repo, {PYPROJECT: BASE_PYPROJECT.replace('"ruff>=0.6"', '"ruff>=0.7"')})
    assert cw.run("main", "") == 0


def test_does_not_flag_stricter_ruff(repo: Path) -> None:
    change(repo, {PYPROJECT: BASE_PYPROJECT.replace('"SIM"]', '"SIM", "ARG"]')})
    assert cw.run("main", "") == 0


def test_allow_weakening_passes(repo: Path) -> None:
    change(repo, {THING: "def add(a, b):\n    return a + b  # type: ignore\n"})
    body = "Fixes the thing.\n\nALLOW-WEAKENING: upstream stubs are wrong, filed as REL-999\n"
    assert cw.run("main", body) == 0


def test_allow_weakening_needs_a_real_reason(repo: Path) -> None:
    change(repo, {THING: "def add(a, b):\n    return a + b  # type: ignore\n"})
    assert cw.run("main", "ALLOW-WEAKENING: x\n") == 1


def test_missing_base_ref_exits_2_not_0(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """The whole point of a three-way exit: could-not-check is not a pass."""
    monkeypatch.setattr(sys, "argv", ["check_weakening.py", "--base", "origin/nope"])
    assert cw.main() == 2


def test_pattern_exemption_stays_exactly_two_files() -> None:
    """The exemption is a hole, so it gets pinned rather than trusted.

    Both files name the patterns as literals and would trip on themselves. That
    is the only reason either is exempt. Widening this list should mean editing
    this test, which is a thing you notice in a diff.
    """
    assert {
        "scripts/check_weakening.py",
        "tests/test_check_weakening.py",
    } == cw.PATTERN_LITERAL_FILES


def test_exempt_file_is_still_checked_for_deleted_tests(repo: Path) -> None:
    """The carve-out covers the line scan only. Deletions are caught everywhere."""
    exempt = "tests/test_check_weakening.py"
    change(repo, {exempt: BASE_TEST})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "work")
    _git(repo, "checkout", "-qb", "work2")
    change(repo, {exempt: "def test_a():\n    assert 1 == 1\n"})
    assert cw.run("main", "") == 1


def test_exempt_file_may_carry_the_literals(repo: Path) -> None:
    """The other half of the carve-out: planting a pattern there must not trip."""
    change(repo, {"tests/test_check_weakening.py": BASE_TEST + "\n\nX = '# type: ignore'\n"})
    assert cw.run("main", "") == 0
