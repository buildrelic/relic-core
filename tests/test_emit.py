from collections.abc import Callable

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, upsert_skill
from relic.serve.emit_files import catalog_path, emit_catalog, emit_skill, emit_verified, skill_path


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
