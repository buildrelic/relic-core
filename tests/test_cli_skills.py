"""CLI tests for the registry-facing commands: register, list, show, verify,
emit, deprecate. The graph-backed commands (ingest/resolve/compile/query) are
not exercised here.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from relic.cli import app
from relic.config import Settings
from relic.ontology.skill_ir import SkillIR

runner = CliRunner()


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway registry under tmp_path; return its dir."""
    settings = Settings(registry_db_path=str(tmp_path / "registry.db"))
    monkeypatch.setattr("relic.config.get_settings", lambda: settings)
    return tmp_path


def _write_skill(path: Path, skill: SkillIR) -> Path:
    path.write_text(skill.model_dump_json(), encoding="utf-8")
    return path


def test_register_then_list(cli: Path, make_skill: Callable[..., SkillIR]) -> None:
    skill_json = _write_skill(cli / "skill.json", make_skill("demo-skill"))
    result = runner.invoke(app, ["register", str(skill_json)])
    assert result.exit_code == 0, result.output
    assert "registered" in result.output

    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0
    assert "demo-skill" in listed.output
    assert "draft" in listed.output


def test_register_rejects_invalid_json(cli: Path) -> None:
    bad = cli / "bad.json"
    bad.write_text('{"skill_id": "x"}', encoding="utf-8")  # missing required fields
    result = runner.invoke(app, ["register", str(bad)])
    assert result.exit_code == 1
    assert "invalid SkillIR" in result.output


def test_full_lifecycle_through_cli(cli: Path, make_skill: Callable[..., SkillIR]) -> None:
    skill_json = _write_skill(cli / "skill.json", make_skill("demo-skill"))
    target = cli / "repo"
    assert runner.invoke(app, ["register", str(skill_json)]).exit_code == 0

    # emit refuses a draft
    pre = runner.invoke(app, ["emit", "--repo", str(target)])
    assert "no verified skills" in pre.output

    # verify promotes it
    verified = runner.invoke(app, ["verify", "demo-skill"])
    assert verified.exit_code == 0
    assert "verified" in verified.output

    # emit now writes the rendered SKILL.md
    emitted = runner.invoke(app, ["emit", "--repo", str(target)])
    assert emitted.exit_code == 0
    skill_md = target / ".claude" / "skills" / "demo-skill" / "SKILL.md"
    assert skill_md.exists()
    assert "# Route auth PRs" in skill_md.read_text()


def test_show_renders_markdown_and_json(cli: Path, make_skill: Callable[..., SkillIR]) -> None:
    skill_json = _write_skill(cli / "skill.json", make_skill("demo-skill"))
    runner.invoke(app, ["register", str(skill_json)])

    md = runner.invoke(app, ["show", "demo-skill"])
    assert md.exit_code == 0
    assert "# Route auth PRs" in md.output

    as_json = runner.invoke(app, ["show", "demo-skill", "--json"])
    assert as_json.exit_code == 0
    assert SkillIR.model_validate_json(as_json.output).skill_id == "demo-skill"


def test_show_missing_skill_errors(cli: Path) -> None:
    result = runner.invoke(app, ["show", "ghost"])
    assert result.exit_code == 1
    assert "no such skill" in result.output


def test_catalog_prints_verified_index(cli: Path, make_skill: Callable[..., SkillIR]) -> None:
    skill_json = _write_skill(cli / "skill.json", make_skill("demo-skill"))
    runner.invoke(app, ["register", str(skill_json)])
    runner.invoke(app, ["verify", "demo-skill"])
    result = runner.invoke(app, ["catalog"])
    assert result.exit_code == 0
    assert "# Skills" in result.output
    assert "demo-skill" in result.output


def test_deprecate_removes_from_emit(cli: Path, make_skill: Callable[..., SkillIR]) -> None:
    skill_json = _write_skill(cli / "skill.json", make_skill("demo-skill"))
    runner.invoke(app, ["register", str(skill_json)])
    runner.invoke(app, ["verify", "demo-skill"])

    deprecated = runner.invoke(app, ["deprecate", "demo-skill"])
    assert deprecated.exit_code == 0
    assert "deprecated" in deprecated.output

    target = cli / "repo"
    runner.invoke(app, ["emit", "--repo", str(target)])
    assert not (target / ".claude").exists()
