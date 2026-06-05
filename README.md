# relic-core

Relic is an early memory layer for AI-native teams, built on Graphiti. See
[`AGENTS.md`](AGENTS.md) for what it is and how we work.

This repo holds the prototype pipeline: ingest engineering history, structure it
in a graph, detect recurring procedures, and compile them into grounded skills a
coding agent can follow.

## How it works

Relic is two halves over one contract. `SkillIR`
([`src/relic/ontology/skill_ir.py`](src/relic/ontology/skill_ir.py)) is the seam:
it is the compiler's output, the registry record, and the MCP tool schema, all
one type.

**Capture, memory, recall (the graph side).**

- `ingest` pulls merged PRs (title, description, files, reviewers) and issues
  from GitHub and feeds them to Graphiti, which extracts typed entities and
  relationships into a temporal knowledge graph. The store is a FalkorDB graph
  running on localhost, so you must start it with Docker first. `--limit`
  bounds how many PRs and issues it pulls, most recent first, to keep ingestion
  cheap.
- `recall` queries that graph (semantic plus keyword, via `graphiti.search`) and
  returns facts, each carrying the PR or issue it came from. Provenance is the
  point: every answer cites its source.
- `eval` scores recall against a gold set, so a change to ingestion or retrieval
  is measured, not guessed.

**Skills, serve (the registry side).**

- Skills are typed `SkillIR` records in a SQLite registry at
  `./data/registry.db`, separate from the graph. Lifecycle: draft, verified,
  deprecated. Today they are hand-authored and loaded with `register`. The
  Phase 4 compiler (procedure to grounded `SkillIR`) is the pending piece that
  will produce them from the graph.
- `emit` renders verified skills into a repo's `.claude/skills/`, `catalog`
  builds a human index, and `serve` exposes skills and recall over MCP.

Two stores, kept apart on purpose: the FalkorDB graph holds memory, the SQLite
registry holds skills. Graphiti uses OpenAI for extraction, embeddings, and
reranking (`gpt-4o-mini`, `text-embedding-3-small`).

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker (runs the FalkorDB graph backend)
- `gh` CLI logged in, or a `GITHUB_TOKEN` (to read history)
- An OpenAI API key (Graphiti uses it for extraction, embeddings, and recall)
- Optional: API keys for Anthropic (the Phase 4 compiler), Gemini, and Linear (a second source)

Run `relic doctor` to see what is configured.

## Setup

```bash
uv python install 3.12
uv sync --dev
cp .env.example .env   # then fill in keys
docker compose up -d falkordb   # graph backend on localhost:6379, UI on :3000
```

The graph lives in FalkorDB, not a local file. Start it before `relic ingest` or
`relic query`. Connection settings are the `FALKORDB_*` vars in `.env`.

## Usage

```bash
uv run relic --help
```

Commands: `ingest`, `resolve`, `compile`, `register`, `list`, `show`, `verify`,
`emit`, `serve`, `recall`, `eval`, `deprecate`, `doctor`, `query`. `resolve` and
`compile` land in later phases. Everything else is wired up.

### Scorecard

`relic eval` measures recall against a gold set: each case is a question plus the
PR number(s) whose content actually answers it. It runs recall and checks the
cited sources for an expected PR url, then reports the hit rate. Deterministic,
no LLM judge, so the ruler stays cheap. Use it to tell whether an ingestion or
retrieval change actually moved recall, instead of eyeballing a few answers.

```bash
uv run relic eval                          # scores eval/github_recall.json
uv run relic eval --path eval/my-set.json  # or your own gold set
```

### Doctor

`relic doctor` reports your setup at a glance: where the registry lives and how
many skills it holds by status, where the engram graph lives and whether it is
built, and which API keys are configured. It reads only and changes nothing.

```bash
uv run relic doctor
```

### Skill lifecycle

Skills live in a SQLite registry, independent of ingestion. A skill enters as a
draft, either from the Phase 4 compiler or hand-authored and loaded with
`register`, and is promoted once trusted:

```bash
# load a hand-authored skill as a draft
uv run relic register examples/skills/pr-review-routing.json
uv run relic list

# inspect, then promote
uv run relic show pr-review-routing
uv run relic verify pr-review-routing

# emit verified skills (with a catalog index) to a repo's .claude/skills/, or serve over MCP
uv run relic emit --repo /path/to/target-repo
uv run relic catalog   # print the index of verified skills
uv run relic serve

# retire a skill so it stops being emitted or served
uv run relic deprecate pr-review-routing
uv run relic emit --repo /path/to/target-repo   # re-emit removes the deprecated skill's files
```

`emit` reconciles the target: it writes verified skills and removes the files of
any skill it has since deprecated. It only touches skill directories it knows
from the registry, so hand-authored skills under `.claude/skills/` are left alone.

### Ingest

Pull engineering history into the graph. `--limit` caps the pull, most recent
first, which keeps a first run on a large repo cheap.

```bash
uv run relic ingest --repo astral-sh/uv             # pull merged PRs and issues
uv run relic ingest --repo astral-sh/uv --limit 20  # bound the pull
```

### Recall

Query the memory graph and get facts back with their sources, linking to the PR
or issue where available, from the CLI or over MCP (`relic serve` exposes a
`recall_memory` tool):

```bash
uv run relic recall "who usually reviews auth changes?"
```

### MCP server

`relic serve` exposes one MCP endpoint over stdio:

- each verified skill as a tool (call it for the steps) and a resource at `skill://<id>`
- `search_skills` to find a skill by text or scope
- `recall_memory` to query the graph and get facts with their sources

Point any MCP client at it over stdio. For Claude Code, add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "relic": { "command": "uv", "args": ["run", "relic", "serve"] }
  }
}
```

## Development

```bash
uv run ruff check
uv run pyright
uv run pytest
```
