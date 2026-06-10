# src/relic: the Python package

Everything `relic` ships lives here. The CLI is the front door; subpackages do the work.

## Files

- `cli.py`: Typer app, console script `relic`. Commands: ingest, resolve, compile, verify, emit, catalog, serve, register, list, show, deprecate, recall, eval, doctor, query. Heavy imports happen inside each command body so `relic --help` stays fast.
- `config.py`: pydantic settings read from env and `.env`. API keys (OpenAI, Anthropic, Gemini, GitHub, Linear), `FALKORDB_*` connection, registry path, concurrency (`FETCH_CONCURRENCY`, `GRAPHITI_MAX_COROUTINES`). `SEMAPHORE_LIMIT` is deprecated.
- `obs.py`: logging setup. The `relic` logger writes to stderr only, so stdout stays a clean channel for command output.
- `doctor.py`: read-only health report. Registry path and counts, graph reachability, which API keys are set.
- `scorecard.py`: recall eval harness. Scores recall answers against the gold set in `eval/github_recall.json`.

## Subpackages, in pipeline order

`ingest/` fetches sources and maps them to episodes. `graph/` loads episodes into Graphiti on FalkorDB and answers recall and query. `resolve/` is the Phase 3 identity-resolution stub. `ontology/` holds the typed models and the SkillIR contract. `compile/` turns procedures into SkillIR and renders SKILL.md. `registry/` is the SQLite skill store. `serve/` is the MCP server plus emit and catalog. Each has its own CLAUDE.md.

## Conventions

- New CLI commands: lazy-import inside the command body, log to stderr, print results to stdout.
- `TARGET_REPO` defaults recall, query, and eval to one repo when no flag is passed.
- FalkorDB must be up (`just up`) for ingest, recall, and query.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
