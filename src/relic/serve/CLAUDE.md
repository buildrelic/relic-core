# serve: MCP server, emit, catalog

Serves verified skills and memory over MCP, writes skills into target repos, and renders the browsable index. Only `verified` skills are served or emitted.

## Files

- `mcp_server.py`: FastMCP server builder. `build_server()` takes a registry connection and an optional recall function, then registers each verified skill as both a tool (returns rendered SKILL.md) and a `skill://<id>` TextResource, plus a `search_skills` tool. With a recall function it also adds `recall_memory`.
- `emit_files.py`: pure filesystem writer. Renders verified skills to `.claude/skills/<id>/SKILL.md` in a target repo. `prune_unverified()` removes directories for skills that lost verified status.
- `catalog.py`: deterministic markdown index of verified skills with links to each SKILL.md.

## Invoked by

`relic serve` (stdio MCP), `relic emit --repo <path>`, and `relic catalog` in `cli.py`.

## Gotchas

- The recall function is injected into `build_server()`, never imported, so this package stays free of graph and network dependencies.
- `emit` only writes. Stale skills are removed by `prune_unverified()`, not by emit itself.
- The catalog index file is deleted when no verified skills remain, not left empty.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
