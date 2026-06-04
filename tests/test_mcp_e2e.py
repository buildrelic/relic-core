"""End-to-end MCP tests: drive the server through a real fastmcp.Client.

Unlike the unit tests that call build_server() and poke handlers directly, these
exercise the actual MCP protocol (initialize handshake, JSON-RPC, transports):
once in-process over the in-memory transport, and once against the real
``relic serve`` subprocess over stdio.
"""

import os
import sys
from collections.abc import Callable

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from relic.ontology.skill_ir import SkillIR
from relic.registry.store import connect, upsert_skill
from relic.serve.mcp_server import build_server


def _text(block) -> str:
    return getattr(block, "text", "") or ""


async def test_inmemory_client_full_protocol(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    conn = connect(tmp_path / "registry.db")
    upsert_skill(conn, make_skill("verified-skill", status="verified"))
    upsert_skill(conn, make_skill("draft-skill", status="draft"))

    async def recall_fn(query: str, num_results: int = 10) -> str:
        return f"recalled: {query}"

    server = build_server(conn, recall_fn=recall_fn)
    async with Client(server) as client:
        await client.ping()

        tools = {t.name for t in await client.list_tools()}
        assert {"verified-skill", "search_skills", "recall_memory"} <= tools
        assert "draft-skill" not in tools

        resources = {str(r.uri) for r in await client.list_resources()}
        assert "skill://verified-skill" in resources

        called = await client.call_tool("verified-skill", {"pr_url": "https://x/pr/1"})
        assert "Route auth PRs" in _text(called.content[0])

        read = await client.read_resource("skill://verified-skill")
        assert "Route auth PRs" in _text(read[0])

        recalled = await client.call_tool("recall_memory", {"query": "auth"})
        assert "recalled: auth" in _text(recalled.content[0])


async def test_real_serve_subprocess_speaks_mcp(
    tmp_path, make_skill: Callable[..., SkillIR]
) -> None:
    db = tmp_path / "registry.db"
    conn = connect(db)
    upsert_skill(conn, make_skill("verified-skill", status="verified"))
    conn.close()

    # No OPENAI_API_KEY: recall is disabled, which exercises serve's stderr diagnostic.
    # If that diagnostic went to stdout it would corrupt the MCP stream and this would fail.
    env = {**os.environ, "REGISTRY_DB_PATH": str(db)}
    env.pop("OPENAI_API_KEY", None)

    transport = StdioTransport(command=sys.executable, args=["-m", "relic", "serve"], env=env)
    async with Client(transport, init_timeout=30) as client:
        await client.ping()
        tools = {t.name for t in await client.list_tools()}
        assert "verified-skill" in tools
        assert "recall_memory" not in tools
