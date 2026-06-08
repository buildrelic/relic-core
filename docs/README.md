# Relic platform docs

Relic is an early memory layer for AI-native teams, built on
[Graphiti](https://github.com/getzep/graphiti). It ingests engineering history,
structures it in a temporal knowledge graph, and serves grounded answers and
skills to coding agents over MCP. Every answer cites its source.

This directory documents the platform as it stands today: what the pieces are,
how they fit, and how data moves through them. For the product framing and house
voice, read [`AGENTS.md`](../AGENTS.md) at the repo root. For setup and command
examples, read [`README.md`](../README.md).

## Start here

- New to the codebase? Read [architecture.md](architecture.md), then
  [processes.md](processes.md).
- Want to run something? Read [cli-reference.md](cli-reference.md) and
  [configuration.md](configuration.md).
- Changing ingestion or retrieval? Read [ingestion.md](ingestion.md) and
  [memory-and-recall.md](memory-and-recall.md).
- Working on skills, emit, or the MCP server? Read [skills.md](skills.md).

## The map

| Doc | What it covers |
|---|---|
| [architecture.md](architecture.md) | The two halves, the one contract, the two stores, the runtime model, and a system diagram. |
| [processes.md](processes.md) | Every process the platform runs: what triggers it, what it reads and writes, what it depends on, and its lifecycle. |
| [data-model.md](data-model.md) | The three type layers (rich ontology, flat graph nodes, SkillIR), the episode JSON, and how they relate. |
| [ingestion.md](ingestion.md) | The capture pipeline: GitHub and Linear to deterministic records to episodes to the graph. |
| [memory-and-recall.md](memory-and-recall.md) | The graph, recall, query, eval, per-repo partitioning, provenance, and the FalkorDB workaround. |
| [skills.md](skills.md) | SkillIR lifecycle, the SQLite registry, emit, catalog, and the MCP server. |
| [cli-reference.md](cli-reference.md) | Every `relic` command, its flags, and what it does. |
| [configuration.md](configuration.md) | Settings, environment variables, the two stores, and API keys. |
| [development.md](development.md) | Local setup, the test suite, CI, and code conventions. |
| [roadmap.md](roadmap.md) | Milestones M0 to M4, phases 0 to 8, and what is built versus pending. |

## The core loop in one paragraph

`relic ingest --repo owner/name` pulls merged PRs (title, description, files,
reviewers) and issues from GitHub, maps them to deterministic records, turns each
into a JSON episode, and feeds them to Graphiti, which extracts typed entities
and relationships into a FalkorDB graph partitioned per repo. `relic recall "..."`
queries that graph and returns facts, each carrying the PR or issue it came from.
Skills are a separate track: typed `SkillIR` records in a SQLite registry, moved
through draft to verified to deprecated, then written to a repo's
`.claude/skills/` with `relic emit` or served to agents with `relic serve`. Two
stores, kept apart: the graph holds memory, the registry holds skills.

## Status

Relic is a prototype, built phase by phase. Capture, memory, recall, the
registry, emit, catalog, the scorecard, the MCP server, and doctor are wired up.
Entity resolution (`resolve`, Phase 3) and the procedure detector plus skill
compiler (`compile`, Phase 4) are stubs that exit with a "not implemented yet"
message. See [roadmap.md](roadmap.md) for the full breakdown.
