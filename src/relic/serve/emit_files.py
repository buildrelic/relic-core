"""Emit verified skills to a target repo's ``./.claude/skills/<id>/SKILL.md``.

Pure filesystem + ``render()``: given a SkillIR, or every verified skill in the
registry, write the rendered SKILL.md. Only verified skills are emitted. No graph
or network dependency.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from relic.compile.render import render
from relic.ontology.skill_ir import SkillIR
from relic.registry.store import list_skills
from relic.serve.catalog import render_catalog


def skill_path(target_repo: str | Path, skill_id: str) -> Path:
    """The SKILL.md path for ``skill_id`` inside ``target_repo``'s skills tree."""
    return Path(target_repo) / ".claude" / "skills" / skill_id / "SKILL.md"


def emit_skill(skill: SkillIR, target_repo: str | Path) -> Path:
    """Render ``skill`` and write it to the target repo's .claude/skills tree."""
    path = skill_path(target_repo, skill.skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(skill), encoding="utf-8")
    return path


def emit_verified(conn: sqlite3.Connection, target_repo: str | Path) -> list[Path]:
    """Write every verified skill in the registry to ``target_repo``."""
    return [emit_skill(skill, target_repo) for skill in list_skills(conn, status="verified")]


def catalog_path(target_repo: str | Path) -> Path:
    """The catalog index path inside ``target_repo``'s skills tree."""
    return Path(target_repo) / ".claude" / "skills" / "README.md"


def emit_catalog(conn: sqlite3.Connection, target_repo: str | Path) -> Path | None:
    """Write a markdown index of verified skills. Returns None if there are none."""
    skills = list_skills(conn, status="verified")
    if not skills:
        return None
    path = catalog_path(target_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_catalog(skills), encoding="utf-8")
    return path
