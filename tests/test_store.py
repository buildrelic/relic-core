from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import (
    connect,
    get_skill,
    list_skills,
    mark_verified,
    upsert_skill,
)


def test_upsert_and_get_roundtrip(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    skill = make_skill()
    upsert_skill(conn, skill)
    loaded = get_skill(conn, skill.skill_id)
    assert loaded is not None
    assert loaded.skill_id == skill.skill_id
    assert loaded.status == "draft"
    assert loaded.inputs["pr_url"].description == "The PR being opened"


def test_get_missing_returns_none(tmp_path) -> None:
    conn = connect(tmp_path / "registry.db")
    assert get_skill(conn, "nope") is None


def test_upsert_preserves_created_at(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 2, 1, tzinfo=UTC)
    upsert_skill(conn, make_skill(), now=t0)
    upsert_skill(conn, make_skill(title="Renamed"), now=t1)
    row = conn.execute(
        "SELECT created_at, updated_at FROM skills WHERE skill_id = ?", ("pr-review-routing",)
    ).fetchone()
    assert row["created_at"] == t0.isoformat()
    assert row["updated_at"] == t1.isoformat()


def test_list_filters_by_status(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("a", status="draft"))
    upsert_skill(conn, make_skill("b", status="verified"))
    assert {s.skill_id for s in list_skills(conn)} == {"a", "b"}
    assert [s.skill_id for s in list_skills(conn, status="verified")] == ["b"]


def test_mark_verified_promotes_and_stamps(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("a", status="draft"))
    now = datetime(2026, 6, 4, tzinfo=UTC)
    skill = mark_verified(conn, "a", now=now)
    assert skill.status == "verified"
    assert skill.last_verified_at == now
    reloaded = get_skill(conn, "a")
    assert reloaded is not None
    assert reloaded.status == "verified"
    assert reloaded.last_verified_at == now


def test_mark_verified_missing_raises(tmp_path) -> None:
    conn = connect(tmp_path / "registry.db")
    with pytest.raises(KeyError):
        mark_verified(conn, "ghost")


def test_cannot_verify_deprecated(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("a", status="deprecated"))
    with pytest.raises(ValueError):
        mark_verified(conn, "a")
