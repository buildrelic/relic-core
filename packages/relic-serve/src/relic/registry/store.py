"""SQLite registry for compiled skills (Neon later, same schema).

The full SkillIR is stored as JSON in ``document`` and is the source of truth on
read; the scalar columns (status, scope, semver, ...) are denormalized only for
cheap listing and filtering. Any mutation that changes status also rewrites
``document`` so a reloaded SkillIR always matches its row.

No graph or network dependency: this is the durable home for skills once the
compiler (Phase 4) produces them, and what ``verify``, ``emit``, and the MCP
server read from. The interface to the rest of the pipeline is SkillIR alone.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from relic.contracts import SkillIR

SkillStatus = Literal["draft", "verified", "deprecated"]

# ADR-0005: where a Person↔Zone grant came from. Config-driven mappings are the
# deterministic default; "api" is reserved for grants made through an admin surface.
ZoneGrantSource = Literal["config", "api"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id          TEXT PRIMARY KEY,
    semver            TEXT NOT NULL,
    title             TEXT NOT NULL,
    scope             TEXT NOT NULL,
    status            TEXT NOT NULL,
    owner             TEXT NOT NULL,
    last_verified_at  TEXT,
    document          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- ADR-0005 control plane: which Zones a Person may see. Deliberately OUT of the graph
-- so an access check is never itself a graph traversal. The graph holds each node's Zone
-- tag; this table holds the authorization, and the two are joined at the API boundary.
CREATE TABLE IF NOT EXISTS zone_grants (
    person_id   TEXT NOT NULL,
    zone_id     TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (person_id, zone_id)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and initialize) the registry at ``db_path``, creating parent dirs."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def upsert_skill(conn: sqlite3.Connection, skill: SkillIR, *, now: datetime | None = None) -> None:
    """Insert or update a skill, preserving ``created_at`` across updates."""
    stamp = _iso(now or _now())
    conn.execute(
        """
        INSERT INTO skills (skill_id, semver, title, scope, status, owner,
                            last_verified_at, document, created_at, updated_at)
        VALUES (:skill_id, :semver, :title, :scope, :status, :owner,
                :last_verified_at, :document, :stamp, :stamp)
        ON CONFLICT(skill_id) DO UPDATE SET
            semver=excluded.semver,
            title=excluded.title,
            scope=excluded.scope,
            status=excluded.status,
            owner=excluded.owner,
            last_verified_at=excluded.last_verified_at,
            document=excluded.document,
            updated_at=excluded.updated_at
        """,
        {
            "skill_id": skill.skill_id,
            "semver": skill.semver,
            "title": skill.title,
            "scope": skill.scope,
            "status": skill.status,
            "owner": skill.owner,
            "last_verified_at": _iso(skill.last_verified_at),
            "document": skill.model_dump_json(),
            "stamp": stamp,
        },
    )
    conn.commit()


def get_skill(conn: sqlite3.Connection, skill_id: str) -> SkillIR | None:
    """Return the skill by id, or ``None`` if it is not registered."""
    row = conn.execute("SELECT document FROM skills WHERE skill_id = ?", (skill_id,)).fetchone()
    if row is None:
        return None
    return SkillIR.model_validate_json(row["document"])


def list_skills(conn: sqlite3.Connection, *, status: str | None = None) -> list[SkillIR]:
    """List registered skills, optionally filtered by ``status``, ordered by id."""
    if status is None:
        rows = conn.execute("SELECT document FROM skills ORDER BY skill_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT document FROM skills WHERE status = ? ORDER BY skill_id", (status,)
        ).fetchall()
    return [SkillIR.model_validate_json(row["document"]) for row in rows]


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a count of skills per status, omitting statuses with no skills.

    Reads the scalar ``status`` column directly, so it never deserializes a
    document: cheap enough for a status check over a large registry.
    """
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM skills GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


def set_status(
    conn: sqlite3.Connection,
    skill_id: str,
    status: SkillStatus,
    *,
    last_verified_at: datetime | None = None,
    now: datetime | None = None,
) -> SkillIR:
    """Set a skill's status (and optionally ``last_verified_at``), rewriting its document."""
    skill = get_skill(conn, skill_id)
    if skill is None:
        raise KeyError(f"skill not found: {skill_id}")
    skill.status = status
    if last_verified_at is not None:
        skill.last_verified_at = last_verified_at
    upsert_skill(conn, skill, now=now)
    return skill


def mark_verified(
    conn: sqlite3.Connection, skill_id: str, *, now: datetime | None = None
) -> SkillIR:
    """Promote a draft skill to verified and stamp ``last_verified_at``."""
    skill = get_skill(conn, skill_id)
    if skill is None:
        raise KeyError(f"skill not found: {skill_id}")
    if skill.status == "deprecated":
        raise ValueError(f"cannot verify a deprecated skill: {skill_id}")
    stamp = now or _now()
    return set_status(conn, skill_id, "verified", last_verified_at=stamp, now=stamp)


def grant_zone(
    conn: sqlite3.Connection,
    person_id: str,
    zone_id: str,
    *,
    source: ZoneGrantSource = "config",
    now: datetime | None = None,
) -> None:
    """Grant a Person access to a Zone. Idempotent on ``(person_id, zone_id)``."""
    conn.execute(
        """
        INSERT INTO zone_grants (person_id, zone_id, source, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(person_id, zone_id) DO NOTHING
        """,
        (person_id, zone_id, source, _iso(now or _now())),
    )
    conn.commit()


def revoke_zone(conn: sqlite3.Connection, person_id: str, zone_id: str) -> None:
    """Remove one Person↔Zone grant. A no-op if the grant does not exist."""
    conn.execute(
        "DELETE FROM zone_grants WHERE person_id = ? AND zone_id = ?", (person_id, zone_id)
    )
    conn.commit()


def zones_for_person(conn: sqlite3.Connection, person_id: str) -> set[str]:
    """Return the Zones a Person may access. Empty set when none -- fail closed (ADR-0005).

    An unknown principal resolves to no Zones, so a caller that injects this set as the
    recall scope sees nothing rather than everything.
    """
    rows = conn.execute(
        "SELECT zone_id FROM zone_grants WHERE person_id = ?", (person_id,)
    ).fetchall()
    return {row["zone_id"] for row in rows}
