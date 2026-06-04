from collections.abc import Callable

from mcp.types import TextContent

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, upsert_skill
from relic.serve.mcp_server import build_server, input_schema


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
    tools = await server.list_tools()
    assert {t.name for t in tools} == {"verified-skill"}


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
