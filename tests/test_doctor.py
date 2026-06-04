"""Tests for `relic doctor`: the read-only setup health check.

`diagnose` is exercised directly for its data, and the CLI command for its
wiring. Settings are built with `_env_file=None` and the key env vars cleared so
the report never depends on the developer's real .env or shell.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from relic.cli import app
from relic.config import Settings
from relic.doctor import diagnose, format_report
from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, upsert_skill

runner = CliRunner()

_KEY_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "LINEAR_API_KEY",
)


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate Settings from the developer's env: clear key vars, run from an empty cwd.

    `chdir` into the (empty) tmp_path means the relative `.env` resolves to nothing,
    so the report never reflects a local .env or shell key.
    """
    for var in _KEY_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "registry_db_path": str(tmp_path / "registry.db"),
        "engram_db_path": str(tmp_path / "engram.kuzu"),
    }
    base.update(overrides)
    return Settings(**base)


def test_diagnose_empty_setup(tmp_path: Path, clean_env: None) -> None:
    report = diagnose(_settings(tmp_path))
    assert report.registry.exists is False
    assert report.registry.total == 0
    assert report.graph.exists is False
    assert {key.name for key in report.keys} == {
        "openai",
        "anthropic",
        "gemini",
        "github",
        "linear",
    }
    assert all(not key.configured for key in report.keys)
    assert report.openai_configured is False


def test_diagnose_counts_skills_by_status(
    tmp_path: Path, clean_env: None, make_skill: Callable[..., SkillIR]
) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings.registry_db_path)
    upsert_skill(conn, make_skill("a", status="verified"))
    upsert_skill(conn, make_skill("b", status="draft"))
    upsert_skill(conn, make_skill("c", status="draft"))
    upsert_skill(conn, make_skill("d", status="deprecated"))
    conn.close()

    report = diagnose(settings)
    assert report.registry.exists is True
    assert report.registry.readable is True
    assert report.registry.total == 4
    assert report.registry.counts == {"verified": 1, "draft": 2, "deprecated": 1}

    text = format_report(report)
    assert "4 skills: 1 verified, 2 draft, 1 deprecated" in text


def test_diagnose_detects_configured_keys(tmp_path: Path, clean_env: None) -> None:
    report = diagnose(_settings(tmp_path, openai_api_key="sk-test", github_token="ghp_test"))
    by_name = {key.name: key for key in report.keys}
    assert by_name["openai"].configured is True
    assert by_name["github"].configured is True
    assert by_name["anthropic"].configured is False
    assert report.openai_configured is True


def test_graph_present_with_openai_reads_ready(tmp_path: Path, clean_env: None) -> None:
    (tmp_path / "engram.kuzu").write_text("", encoding="utf-8")  # stand in for a built store
    report = diagnose(_settings(tmp_path, openai_api_key="sk-test"))
    assert report.graph.exists is True
    assert "recall and ingest ready" in format_report(report)


def test_graph_present_without_openai_flags_missing_key(tmp_path: Path, clean_env: None) -> None:
    (tmp_path / "engram.kuzu").write_text("", encoding="utf-8")
    report = diagnose(_settings(tmp_path))
    assert report.graph.exists is True
    assert "OPENAI_API_KEY is missing" in format_report(report)


def test_format_report_empty_setup_is_readable(tmp_path: Path, clean_env: None) -> None:
    text = format_report(diagnose(_settings(tmp_path)))
    assert "relic doctor" in text
    assert "not created yet" in text  # registry
    assert "not built yet" in text  # graph
    assert "openai" in text
    assert "missing" in text


def test_doctor_command_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("relic.config.get_settings", lambda: settings)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "registry:" in result.output
    assert "graph:" in result.output
    assert "keys:" in result.output
