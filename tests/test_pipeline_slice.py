"""End-to-end demo of the downstream half: draft -> verify -> emit -> serve.

Decoupled from ingestion and the graph DB: a SkillIR (here a fixture, later the
Phase 4 compiler's output) flows through the registry and out to files + MCP.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, mark_verified, upsert_skill
from relic.serve.emit_files import emit_verified, skill_path
from relic.serve.mcp_server import build_server


async def test_draft_to_served_skill_end_to_end(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    repo = tmp_path / "repo"

    # 1. a freshly-compiled skill lands as a draft in the registry
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("pr-review-routing", status="draft"))

    # 2. nothing is emitted or served while it is still a draft
    assert emit_verified(conn, repo) == []
    assert await build_server(conn).list_tools() == []

    # 3. verify promotes it and stamps the verification time
    skill = mark_verified(conn, "pr-review-routing", now=datetime(2026, 6, 4, tzinfo=UTC))
    assert skill.status == "verified"

    # 4. now it emits to .claude/skills/ ...
    paths = emit_verified(conn, repo)
    assert paths == [skill_path(repo, "pr-review-routing")]
    assert "# Route auth PRs" in paths[0].read_text()

    # 5. ... and is served as an MCP tool
    tools = await build_server(conn).list_tools()
    assert [t.name for t in tools] == ["pr-review-routing"]
