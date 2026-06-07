# Relic system brief

## What it is

Relic is a memory layer for AI-native teams, built on
[Graphiti](https://github.com/getzep/graphiti). It ingests a team's engineering
history, structures it as a temporal knowledge graph, and serves two things to
coding agents over MCP: grounded answers about past work, each citing its source,
and compiled skills, the repeatable procedures an agent can follow. This repo,
`relic-core`, is the prototype: about 2,800 lines of Python in one package, driven
by a Typer CLI.

## Core loop

```
GitHub / Linear ─► ingest ─► Graphiti extraction ─► FalkorDB graph ─► recall ─► facts + sources
                                                                          │
                                                                          ▼
                                              (Phase 4) compile ─► SkillIR ─► SQLite registry
                                                                          │
                                                          register / verify / emit / serve
                                                                          ▼
                                              .claude/skills/  +  MCP tools  ─►  coding agent
```

1. **Capture.** `ingest --repo owner/name` pulls merged PRs (title, description,
   files, reviews, reviewers) and issues from GitHub, optionally Linear issues. It
   writes the raw payloads to disk for citation and maps each record to a JSON
   episode.
2. **Memory.** Episodes are added to Graphiti one at a time. An LLM extracts typed
   entities and edges into a FalkorDB graph. Each repo is its own tenant, keyed by
   `group_id`.
3. **Recall.** `recall "..."` runs hybrid retrieval (semantic, keyword, rerank)
   and returns ranked facts, each carrying its PR or issue url.
4. **Skills.** Procedures become `SkillIR` records in a separate SQLite registry,
   promoted through a lifecycle, then emitted to a repo's `.claude/skills/` or
   served as MCP tools.

## Architecture

**Two halves, one contract.** The graph side holds memory. The registry side
holds skills. The seam is one Pydantic type, `SkillIR`. It is the compiler's
output, the registry record, the SKILL.md render input, and the MCP tool schema.
One type in four roles, so the served tool and the rendered document cannot drift.

**Two stores.**

| Store | Backend | Holds | Why separate |
|---|---|---|---|
| Memory graph | FalkorDB (Redis protocol, Docker) | Entities, edges, episodes, partitioned per repo by `group_id` | Multi-tenant partitioning and full-text search |
| Skill registry | SQLite (`./data/registry.db`) | `SkillIR` JSON documents plus denormalized columns | Skills are a typed catalog, not graph data |

**Subsystems** (`src/relic/`):

| Module | Role | Status |
|---|---|---|
| `ingest/` | GitHub (githubkit) and Linear (gql) connectors, raw-payload store, record-to-episode mappers | Wired |
| `graph/` | Graphiti + FalkorDB + OpenAI factory, episode loader, recall, reviewer query | Wired |
| `ontology/` | Rich typed models and `SkillIR`; flat graph-facing entity/edge models live in `graph/engram.py` | Wired |
| `registry/` | SQLite skill store and lifecycle (draft, verified, deprecated) | Wired |
| `serve/` | FastMCP stdio server, emit to `.claude/skills/`, catalog index, Jinja SKILL.md renderer | Wired |
| `resolve/` | Cross-source person unification | Stub (Phase 3) |
| `compile/` | Procedure detector and LLM compiler that turns graph evidence into a `SkillIR` | Stub (Phase 4); the SKILL.md renderer is live |

**MCP server.** `relic serve` exposes one stdio endpoint: each verified skill as a
tool and a `skill://<id>` resource, a `search_skills` tool, and a `recall_memory`
tool. Recall is injected, not imported, so `serve` has no graph dependency. If
FalkorDB is unreachable, `serve` logs to stderr and still serves skills. stdout
carries the MCP transport.

## Runtime and setup

- `uv` runs Python on the host. Docker runs only FalkorDB. `just setup` takes a
  fresh clone to a verifiable state: installs Python 3.12, syncs deps, creates
  `.env`, starts FalkorDB and waits for healthy, installs the pre-commit hook,
  runs `relic doctor`.
- Every command is a one-shot CLI except `serve`, which is long-running. The
  `pipeline/run.py` orchestrator (ingest, resolve, detect, compile) is a stub that
  raises `NotImplementedError`.
- Config is a `pydantic-settings` model over `.env`. All API keys are optional, so
  `relic --help` and imports never need a populated env. A validator drops blank,
  whitespace-bearing, or `#`-commented secrets.
- 15 CLI commands: 13 wired (`ingest`, `recall`, `query`, `eval`, `register`,
  `list`, `show`, `verify`, `deprecate`, `emit`, `catalog`, `serve`, `doctor`) and
  2 stubs (`resolve`, `compile`).

## Stack

- Memory engine: Graphiti (`graphiti-core[falkordb]` 0.29.x).
- Graph DB: FalkorDB, self-hosted via Docker. Chosen over embedded Kuzu for
  `group_id` multi-tenancy and full-text search.
- Models: OpenAI `gpt-4o-mini` for extraction and reranking,
  `text-embedding-3-small` for embeddings.
- Server: FastMCP (stdio). CLI: Typer and Rich. Schema: Pydantic v2. Rendering:
  Jinja2.
- Tooling: ruff (lint and format), pyright (basic), pytest. CI runs all three
  against a FalkorDB service container.

## Status

Built: capture, memory, recall, the SQLite registry and skill lifecycle,
emit/catalog reconciliation, the MCP server, the recall scorecard, and doctor. The
test suite is 18 files. 17 run offline against throwaway SQLite and in-memory
fakes. One live integration test is skip-gated on `OPENAI_API_KEY` and a reachable
FalkorDB.

Pending: `resolve` (Phase 3, person identity across GitHub and Linear) and
`compile` (Phase 4, procedure detection and grounded `SkillIR` generation). Until
`compile` lands, skills are hand-authored and loaded with `register`.

## Design decisions and risks

- **Provenance is in the schema.** Every entity carries a canonical url and an
  audit record. `SkillIR` carries typed citations. Recall cites the PR or issue
  url, never the repo url.
- **Graphiti compatibility shim.** `engram.py` monkeypatches a `graphiti-core`
  ≤0.29.1 FalkorDB bug where empty full-text queries and hyphenated `group_id`s
  produce invalid RediSearch syntax that aborts ingestion. Revisit on upgrade.
- **Flat and rich models are duplicated.** Graphiti needs flat scalar models with
  no reserved attribute names, so the graph-facing models in `engram.py` mirror
  the rich `ontology/` models. Keep them in sync by hand.
- **Cost controls.** `--limit` caps PRs and GitHub issues (most recent first), and
  episode bodies are clipped to 4000 chars. `--limit` does not apply to Linear,
  which always pulls all issues.
- **Served recall is not repo-scoped.** CLI `recall` scopes to a repo's
  `group_id`. The MCP `recall_memory` tool queries the default graph with no
  `group_id`. Align this before multi-repo serving.
- **Infra is transitional.** Comments point to a managed FalkorDB, the raw store
  moving to R2, and the SQLite registry moving to Neon after dogfooding.
