from collections.abc import Callable

from mcp.types import TextContent

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, upsert_skill
from relic.serve.mcp_server import build_server, input_schema


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_input_schema_from_skill_inputs(make_skill: Callable[..., SkillIR]) -> None:
    schema = input_schema(make_skill())
    assert schema["type"] == "object"
    assert schema["properties"]["pr_url"]["type"] == "string"
    assert schema["required"] == ["pr_url"]


async def test_build_server_exposes_only_verified(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("draft-skill", status="draft"))
    upsert_skill(conn, make_skill("verified-skill", status="verified"))
    server = build_server(conn)
    names = {t.name for t in await server.list_tools()}
    assert "verified-skill" in names
    assert "draft-skill" not in names


async def test_tool_call_returns_rendered_skill(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("verified-skill", status="verified"))
    server = build_server(conn)
    result = await server.call_tool("verified-skill", {"pr_url": "https://example.com/pr/1"})
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert "# Route auth PRs" in block.text


async def test_search_skills_filters_by_text_and_scope(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("auth-routing", status="verified"))
    upsert_skill(
        conn,
        make_skill(
            "deploy-flow",
            status="verified",
            title="Deploy flow",
            description="Use when deploying a service.",
            scope="team",
        ),
    )
    server = build_server(conn)

    by_text = await server.call_tool("search_skills", {"query": "auth"})
    assert "auth-routing" in _text(by_text)
    assert "deploy-flow" not in _text(by_text)

    by_scope = await server.call_tool("search_skills", {"scope": "team"})
    assert "deploy-flow" in _text(by_scope)
    assert "auth-routing" not in _text(by_scope)


async def test_skills_exposed_as_resources(tmp_path, make_skill: Callable[..., SkillIR]) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("draft-skill", status="draft"))
    upsert_skill(conn, make_skill("verified-skill", status="verified"))
    server = build_server(conn)

    uris = {str(r.uri) for r in await server.list_resources()}
    assert "skill://verified-skill" in uris
    assert "skill://draft-skill" not in uris

    result = await server.read_resource("skill://verified-skill")
    content = result.contents[0].content
    assert isinstance(content, str)
    assert "# Route auth PRs" in content


def test_server_has_instructions(tmp_path) -> None:
    conn = connect(tmp_path / "registry.db")
    server = build_server(conn)
    assert server.instructions
    assert "recall_memory" in server.instructions
