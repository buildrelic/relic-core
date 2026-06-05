"""Doctor: a read-only health check of a relic setup.

Reports what relic can see without changing anything: where the registry lives
and how many skills it holds by status, where the engram graph lives and whether
it is built, and which API keys are configured. It creates nothing (it opens the
registry only when the file already exists) and never raises: a broken setup
should still produce a report you can read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from relic.config import Settings

# (display name, Settings attribute, what the key unlocks)
_KEYS: list[tuple[str, str, str]] = [
    ("openai", "openai_api_key", "graph ingest, recall"),
    ("anthropic", "anthropic_api_key", "skill compiler"),
    ("gemini", "gemini_api_key", "alternate graph llm"),
    ("github", "github_token", "github ingest"),
    ("linear", "linear_api_key", "linear ingest"),
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
class GraphStatus:
    """Where the engram graph lives and whether it is built on disk."""

    path: str
    exists: bool


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
    graph: GraphStatus
    keys: list[KeyStatus]

    @property
    def openai_configured(self) -> bool:
        return any(key.name == "openai" and key.configured for key in self.keys)


def _falkordb_reachable(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def diagnose(settings: Settings) -> DoctorReport:
    """Inspect the setup described by ``settings`` and return a report. Never raises."""
    host = settings.falkordb_host
    port = settings.falkordb_port
    db = settings.falkordb_database
    path = f"falkordb://{host}:{port}/{db}"
    exists = _falkordb_reachable(host, port)
    return DoctorReport(
        registry=_registry_status(settings.registry_db_path),
        graph=GraphStatus(
            path=path,
            exists=exists,
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

    lines.append(f"graph: {report.graph.path}")
    if not report.graph.exists:
        lines.append("  not built yet. run `relic ingest --repo owner/name` to build it.")
    elif report.openai_configured:
        lines.append("  present. recall and ingest ready.")
    else:
        lines.append("  present, but OPENAI_API_KEY is missing: recall and ingest need it.")
    lines.append("")

    lines.append("keys:")
    width = max(len(key.name) for key in report.keys)
    for key in report.keys:
        state = "set" if key.configured else "missing"
        lines.append(f"  {key.name.ljust(width)}  {state.ljust(7)}  {key.purpose}")

    return "\n".join(lines)
