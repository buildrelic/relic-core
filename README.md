# relic-core

Relic is an early memory layer for AI-native teams, built on Graphiti. See
[`AGENTS.md`](AGENTS.md) for what it is and how we work.

This repo holds the prototype pipeline: ingest engineering history, structure it
in a graph, detect recurring procedures, and compile them into grounded skills a
coding agent can follow.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- `gh` CLI (reads your GitHub history)
- API keys for Anthropic, Gemini or OpenAI, GitHub, and Linear

## Setup

```bash
uv python install 3.12
uv sync --dev
cp .env.example .env   # then fill in keys
```

## Usage

```bash
uv run relic --help
```

Commands: `ingest`, `resolve`, `compile`, `register`, `list`, `show`, `verify`,
`emit`, `serve`, `recall`, `deprecate`, `query`. `resolve` and `compile` land in
later phases. Everything else is wired up.

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

# emit verified skills to a repo's .claude/skills/, and/or serve them over MCP
uv run relic emit --repo /path/to/target-repo
uv run relic serve

# retire a skill so it stops being emitted or served
uv run relic deprecate pr-review-routing
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

## Development

```bash
uv run ruff check
uv run pyright
uv run pytest
```
