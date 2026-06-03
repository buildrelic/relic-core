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

Commands: `ingest`, `resolve`, `compile`, `verify`, `emit`, `query`. Only the
skeleton is wired up so far; each command lands in its phase.

## Development

```bash
uv run ruff check
uv run pyright
uv run pytest
```
