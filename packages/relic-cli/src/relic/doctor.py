"""Doctor: a read-only health check of a relic setup.

Reports what relic can see without changing anything: where the registry lives
and how many skills it holds by status, whether the engram store is reachable
and its document mirror configured, and which API keys are configured. It
creates nothing (it opens the registry only when the file already exists) and
never raises: a broken setup should still produce a report you can read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from relic.config import Settings
from relic.engram import firestore_configured as _firestore_configured
from relic.engram import postgres_reachable as _postgres_reachable

# (display name, Settings attribute, what the key unlocks)
_KEYS: list[tuple[str, str, str]] = [
    ("anthropic", "anthropic_api_key", "skill compiler"),
    ("github", "github_token", "github ingest"),
    ("linear", "linear_api_key", "linear ingest"),
    ("granola", "granola_api_key", "granola ingest"),
    ("notion", "notion_api_key", "notion ingest"),
]


@dataclass(slots=True)
class RegistryStatus:
    """Where the skill registry lives and what it holds."""

    path: str
    exists: bool
    readable: bool = True
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(slots=True)
class EngramStatus:
    """Whether the engram store answers, and whether documents mirror to Firestore."""

    dsn: str  # redacted: no password
    reachable: bool
    firestore: bool


@dataclass(slots=True)
class KeyStatus:
    """Whether one API key is configured, and what it unlocks."""

    name: str
    purpose: str
    configured: bool


@dataclass(slots=True)
class DoctorReport:
    """The full read-only picture of a relic setup."""

    registry: RegistryStatus
    engram: EngramStatus
    keys: list[KeyStatus]


def _redact_dsn(dsn: str) -> str:
    """The DSN with any password dropped, safe to print."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    db = (parsed.path or "").lstrip("/") or "relic"
    user = f"{parsed.username}@" if parsed.username else ""
    return f"postgresql://{user}{host}:{port}/{db}"


def diagnose(settings: Settings) -> DoctorReport:
    """Inspect the setup described by ``settings`` and return a report. Never raises."""
    return DoctorReport(
        registry=_registry_status(settings.registry_db_path),
        engram=EngramStatus(
            dsn=_redact_dsn(settings.database_url),
            reachable=_postgres_reachable(settings.database_url),
            firestore=_firestore_configured(settings),
        ),
        keys=[
            KeyStatus(name=name, purpose=purpose, configured=getattr(settings, attr) is not None)
            for name, attr, purpose in _KEYS
        ],
    )


def _registry_status(path: str) -> RegistryStatus:
    if not Path(path).exists():
        return RegistryStatus(path=path, exists=False)
    from relic.registry.store import connect, count_by_status

    try:
        conn = connect(path)
    except sqlite3.Error:
        return RegistryStatus(path=path, exists=True, readable=False)
    try:
        counts = count_by_status(conn)
    except sqlite3.Error:
        return RegistryStatus(path=path, exists=True, readable=False)
    finally:
        conn.close()
    return RegistryStatus(path=path, exists=True, counts=counts)


def format_report(report: DoctorReport) -> str:
    """Render a report as a plain-text block, no color or markup."""
    lines = ["relic doctor", ""]

    lines.append(f"registry: {report.registry.path}")
    if not report.registry.exists:
        lines.append("  not created yet. run `relic register <skill.json>` to create it.")
    elif not report.registry.readable:
        lines.append("  exists but could not be read.")
    elif report.registry.total == 0:
        lines.append("  no skills registered yet.")
    else:
        counts = report.registry.counts
        breakdown = ", ".join(
            f"{counts.get(s, 0)} {s}" for s in ("verified", "draft", "deprecated")
        )
        noun = "skill" if report.registry.total == 1 else "skills"
        lines.append(f"  {report.registry.total} {noun}: {breakdown}")
    lines.append("")

    lines.append(f"engram: {report.engram.dsn}")
    if not report.engram.reachable:
        lines.append("  not reachable. start it with `just up`.")
    else:
        lines.append("  reachable. ingest and recall ready.")
    if report.engram.firestore:
        lines.append("  firestore: configured (documents mirror to the web workspace).")
    else:
        lines.append("  firestore: not configured (documents stay local-only).")
    lines.append("")

    lines.append("keys:")
    width = max(len(key.name) for key in report.keys)
    for key in report.keys:
        state = "set" if key.configured else "missing"
        lines.append(f"  {key.name.ljust(width)}  {state.ljust(7)}  {key.purpose}")

    return "\n".join(lines)
