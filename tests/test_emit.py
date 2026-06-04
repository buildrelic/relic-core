from collections.abc import Callable

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, set_status, upsert_skill
from relic.serve.emit_files import (
    catalog_path,
    emit_catalog,
    emit_skill,
    emit_verified,
    prune_unverified,
    skill_dir,
    skill_path,
)


def test_emit_skill_writes_rendered_markdown(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    skill = make_skill()
    path = emit_skill(skill, tmp_path)
    assert path == skill_path(tmp_path, skill.skill_id)
    assert path.exists()
    text = path.read_text()
    assert "# Route auth PRs" in text
    assert "[PR #12](https://example.com/pr/12)" in text


def test_emit_verified_only_emits_verified(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("draft-one", status="draft"))
    upsert_skill(conn, make_skill("verified-one", status="verified"))
    repo = tmp_path / "target"
    paths = emit_verified(conn, repo)
    assert paths == [skill_path(repo, "verified-one")]
    assert skill_path(repo, "verified-one").exists()
    assert not skill_path(repo, "draft-one").exists()


def test_emit_catalog_writes_index_of_verified(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("verified-one", status="verified"))
    upsert_skill(conn, make_skill("draft-one", status="draft"))
    repo = tmp_path / "target"
    path = emit_catalog(conn, repo)
    assert path == catalog_path(repo)
    assert path is not None
    text = path.read_text()
    assert "verified-one" in text
    assert "draft-one" not in text


def test_emit_catalog_none_without_verified(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("draft-one", status="draft"))
    assert emit_catalog(conn, tmp_path / "target") is None


def test_prune_unverified_removes_deprecated_dir(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("keep", status="verified"))
    upsert_skill(conn, make_skill("gone", status="verified"))
    repo = tmp_path / "target"
    emit_verified(conn, repo)  # writes both, the verify-then-emit baseline
    assert skill_path(repo, "gone").exists()

    set_status(conn, "gone", "deprecated")  # retire one after it was emitted
    removed = prune_unverified(conn, repo)

    assert removed == [skill_dir(repo, "gone")]
    assert not skill_dir(repo, "gone").exists()
    assert skill_path(repo, "keep").exists()  # verified skill is untouched


def test_prune_unverified_leaves_unmanaged_dirs_alone(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("known", status="verified"))
    repo = tmp_path / "target"
    # a hand-authored skill relic does not know about
    unmanaged = skill_dir(repo, "hand-authored")
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("mine", encoding="utf-8")

    removed = prune_unverified(conn, repo)

    assert removed == []
    assert (unmanaged / "SKILL.md").read_text() == "mine"


def test_emit_catalog_removes_stale_index_when_empty(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("solo", status="verified"))
    repo = tmp_path / "target"
    assert emit_catalog(conn, repo) == catalog_path(repo)
    assert catalog_path(repo).exists()

    set_status(conn, "solo", "deprecated")  # nothing verified remains
    assert emit_catalog(conn, repo) is None
    assert not catalog_path(repo).exists()  # stale index is removed, not left behind
