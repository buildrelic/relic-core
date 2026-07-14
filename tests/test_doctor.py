"""Tests for `relic doctor`: the read-only setup health check.

`diagnose` is exercised directly for its data, and the CLI command for its
wiring. Settings are built with explicit values so the report never depends on
the developer's real .env or shell.
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
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "LINEAR_API_KEY",
    "GRANOLA_API_KEY",
    "NOTION_API_KEY",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_PRIVATE_KEY",
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
        "database_url": "postgresql://relic:relic@localhost:5432/relic",
    }
    base.update(overrides)
    return Settings(**base)


def test_diagnose_empty_setup(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relic.doctor._postgres_reachable", lambda dsn: False)
    report = diagnose(_settings(tmp_path))
    assert report.registry.exists is False
    assert report.registry.total == 0
    assert report.engram.reachable is False
    assert report.engram.firestore is False
    assert {key.name for key in report.keys} == {
        "anthropic",
        "github",
        "linear",
        "granola",
        "notion",
    }
    assert all(not key.configured for key in report.keys)


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
    report = diagnose(_settings(tmp_path, anthropic_api_key="sk-test", github_token="ghp_test"))
    by_name = {key.name: key for key in report.keys}
    assert by_name["anthropic"].configured is True
    assert by_name["github"].configured is True
    assert by_name["linear"].configured is False


def test_engram_dsn_is_redacted(tmp_path: Path, clean_env: None) -> None:
    report = diagnose(
        _settings(tmp_path, database_url="postgresql://relic:s3cret@db.example.com:5432/relic")
    )
    assert "s3cret" not in report.engram.dsn
    assert "db.example.com" in report.engram.dsn


def test_engram_reachable_reads_ready(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relic.doctor._postgres_reachable", lambda dsn: True)
    report = diagnose(_settings(tmp_path))
    assert report.engram.reachable is True
    assert "ingest and recall ready" in format_report(report)


def test_firestore_configured_is_reported(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relic.doctor._postgres_reachable", lambda dsn: True)
    report = diagnose(
        _settings(
            tmp_path,
            firebase_project_id="relic-test",
            firebase_client_email="svc@relic-test.iam.gserviceaccount.com",
            firebase_private_key="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        )
    )
    assert report.engram.firestore is True
    assert "documents mirror to the web workspace" in format_report(report)


def test_format_report_empty_setup_is_readable(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relic.doctor._postgres_reachable", lambda dsn: False)
    text = format_report(diagnose(_settings(tmp_path)))
    assert "relic doctor" in text
    assert "not created yet" in text  # registry
    assert "not reachable" in text  # engram
    assert "documents stay local-only" in text  # firestore unset
    assert "github" in text
    assert "missing" in text


def test_doctor_command_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("relic.config.get_settings", lambda: settings)
    monkeypatch.setattr("relic.doctor._postgres_reachable", lambda dsn: False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "registry:" in result.output
    assert "engram:" in result.output
    assert "keys:" in result.output
