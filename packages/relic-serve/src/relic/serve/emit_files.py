"""Emit verified skills to a target repo's ``./.claude/skills/<id>/SKILL.md``.

Pure filesystem + ``render()``: given a SkillIR, or every verified skill in the
registry, write the rendered SKILL.md. Only verified skills are emitted. No graph
or network dependency.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from relic.contracts import SkillIR
from relic.registry import list_skills
from relic.serve.catalog import render_catalog
from relic.serve.render import render


def skill_dir(target_repo: str | Path, skill_id: str) -> Path:
    """The directory for ``skill_id`` inside ``target_repo``'s skills tree."""
    return Path(target_repo) / ".claude" / "skills" / skill_id


def skill_path(target_repo: str | Path, skill_id: str) -> Path:
    """The SKILL.md path for ``skill_id`` inside ``target_repo``'s skills tree."""
    return skill_dir(target_repo, skill_id) / "SKILL.md"


def emit_skill(skill: SkillIR, target_repo: str | Path) -> Path:
    """Render ``skill`` and write it to the target repo's .claude/skills tree."""
    path = skill_path(target_repo, skill.skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(skill), encoding="utf-8")
    return path


def emit_verified(conn: sqlite3.Connection, target_repo: str | Path) -> list[Path]:
    """Write every verified skill in the registry to ``target_repo``."""
    return [emit_skill(skill, target_repo) for skill in list_skills(conn, status="verified")]


def prune_unverified(conn: sqlite3.Connection, target_repo: str | Path) -> list[Path]:
    """Remove emitted directories for registry skills that are no longer verified.

    A skill that was verified, emitted, then deprecated leaves a stale SKILL.md
    behind: ``emit_verified`` only writes, it never deletes. This reconciles the
    tree by removing the directory of any registry skill whose status is not
    verified. Only directories whose id matches a skill in the registry are
    touched, so hand-authored skills under .claude/skills/ are left alone.
    Returns the directories removed.
    """
    removed: list[Path] = []
    for skill in list_skills(conn):
        if skill.status == "verified":
            continue
        directory = skill_dir(target_repo, skill.skill_id)
        if directory.is_dir():
            shutil.rmtree(directory)
            removed.append(directory)
    return removed


def catalog_path(target_repo: str | Path) -> Path:
    """The catalog index path inside ``target_repo``'s skills tree."""
    return Path(target_repo) / ".claude" / "skills" / "README.md"


def emit_catalog(conn: sqlite3.Connection, target_repo: str | Path) -> Path | None:
    """Write a markdown index of verified skills.

    Returns the index path, or None if there are no verified skills. In that case
    a previously written index is removed rather than left to list skills that are
    no longer emitted.
    """
    skills = list_skills(conn, status="verified")
    path = catalog_path(target_repo)
    if not skills:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_catalog(skills), encoding="utf-8")
    return path
