---
name: mcp-smoke-test
description: Verify the relic MCP server works end to end - tools, skill resources, search, recall. Use whenever the task is testing "relic serve", debugging an MCP client that can't see relic's tools, verifying skills are exposed after register/verify/emit changes, or any "does the MCP server work" check. MCP is relic's primary interface, so changes to serve/, registry/, or compile/ should end with this smoke test.
---

# Smoke-testing the MCP server

The MCP server (`src/relic/serve/mcp_server.py`) exposes every **verified** skill as both a callable tool and a `skill://<id>` resource, plus `search_skills` and (when a graph is reachable) `recall_memory`. The most common "bug" is a skill that simply isn't verified: draft and deprecated skills are invisible by design.

## Fast path: the existing e2e tests

`uv run pytest tests/test_mcp_e2e.py tests/test_serve.py -v` already covers the real protocol: an in-memory fastmcp `Client` (handshake, list_tools, call_tool, read_resource) and a real `relic serve` subprocess over stdio. Run these first; only go manual when you need to poke a specific live registry.

## Manual smoke test, end to end

1. Seed a registry with a verified skill. Either use the real one (`./data/registry.db`) or build a throwaway:
   ```
   uv run relic register examples/skills/pr-review-routing.json
   uv run relic verify pr-review-routing
   uv run relic list
   ```
2. Exercise the server in-process with a fastmcp `Client` (this is the pattern from `tests/test_mcp_e2e.py`, and it avoids stdio plumbing):
   ```python
   from fastmcp import Client
   from relic.registry.store import connect
   from relic.serve.mcp_server import build_server

   conn = connect("data/registry.db")
   server = build_server(conn)  # recall_fn optional; pass one to get recall_memory
   async with Client(server) as client:
       print([t.name for t in await client.list_tools()])
       print([str(r.uri) for r in await client.list_resources()])
       print(await client.call_tool("search_skills", {"query": "review"}))
   ```
3. For the real transport, run `uv run relic serve` (stdio) and connect with fastmcp's `StdioTransport`, or register it in a real client (Claude Code: `claude mcp add relic -- uv run relic serve` from the repo root).

## What to assert

- Verified skills appear in `list_tools` AND as `skill://<id>` resources; calling the tool returns the rendered SKILL.md.
- Draft/deprecated skills appear in neither (if one does, registry status is wrong: `uv run relic show <id>`).
- `search_skills` finds skills by words from their title/description.
- `recall_memory` only exists when serve was built with a recall function: it needs FalkorDB reachable at startup (`just up`), otherwise the server starts without it. Empty recall answers usually mean the graph for `TARGET_REPO` is empty, not a server bug.

## Where to look when it fails

- Tool missing → registry status (`relic list`), then `build_server()` registration loop.
- Tool present but content wrong → renderer (`compile/render.py`, template `templates/skill.md.j2`), not the server.
- Server won't start over stdio → remember logging goes to stderr and stdout must stay clean JSON-RPC; a stray `print()` in startup code corrupts the protocol.
