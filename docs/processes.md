# Processes

Relic is a set of processes over shared stores, not one running service. This doc
catalogs every process: what triggers it, what it reads and writes, what it
depends on, and how long it lives.

Most processes are one-shot CLI commands. One process is a long-running server.
Two are external services the platform talks to. A few are stubs that land in
later phases.

## At a glance

| Process | Kind | Lifetime | Reads | Writes | Hard deps |
|---|---|---|---|---|---|
| FalkorDB | service | persistent | graph | graph | Docker |
| `relic serve` | server | per connection | registry, graph | nothing | registry; graph optional |
| `relic ingest` | CLI | one-shot | GitHub, Linear | graph, raw store | GitHub token, OpenAI, FalkorDB |
| `relic recall` | CLI | one-shot | graph | nothing | OpenAI, FalkorDB |
| `relic query` | CLI | one-shot | graph | nothing | OpenAI, FalkorDB |
| `relic eval` | CLI | one-shot | graph, gold set | nothing | OpenAI, FalkorDB |
| `relic register` | CLI | one-shot | a JSON file | registry | none |
| `relic list` / `show` | CLI | one-shot | registry | nothing | none |
| `relic verify` / `deprecate` | CLI | one-shot | registry | registry | none |
| `relic emit` | CLI | one-shot | registry | target `.claude/skills/` | none |
| `relic catalog` | CLI | one-shot | registry | stdout | none |
| `relic doctor` | CLI | one-shot | registry, settings | nothing | none |
| `relic resolve` | CLI | stub | none | none | Phase 3 |
| `relic compile` | CLI | stub | none | none | Phase 4 |
| `pipeline/run.py` | script | stub | none | none | Phases 4 to 8 |

Every CLI process is import-light: only typer and rich load at startup, and each
command imports its heavy dependencies inside the command body, so `relic --help`
is fast and key-free ([`cli.py`](../src/relic/cli.py)).

## Service processes

### FalkorDB

The graph database. Relic runs it locally as a Docker container
([`docker-compose.yml`](../docker-compose.yml)).

- **Start.** `just up` runs `docker compose up -d --wait falkordb`, blocking
  until the healthcheck passes. `just down` stops it, `just logs` tails it.
- **Ports.** `6379` for the graph protocol (`FALKORDB_PORT`), `3000` for the
  FalkorDB Browser UI at `http://localhost:3000`.
- **Persistence.** A Docker named volume, `falkordb-data`, mounted at `/data`.
- **Restart policy.** `unless-stopped`.
- **Required by.** `ingest`, `recall`, `query`, `eval`, and `serve` when recall
  is enabled. Start it before any of them.

In production this becomes a managed or hosted FalkorDB instance. See
[roadmap.md](roadmap.md).

### relic serve

The MCP server. The one long-running process Relic itself runs. It exposes a
team's verified skills and its memory to any MCP client over stdio.

- **Invocation.** `relic serve`. Stays up for the life of the client connection
  (`server.run_stdio_async()`).
- **Exposes.** Each verified skill as a tool (call it for the steps) and as a
  resource at `skill://<id>`; a `search_skills` tool; and, when the graph is
  reachable, a `recall_memory` tool.
- **Reads.** The SQLite registry for skills. The graph for `recall_memory`.
- **Writes.** Nothing.
- **Recall is optional.** The server builds an engram client for recall. If that
  fails (no OpenAI key, FalkorDB down), it logs to stderr and serves skills
  anyway. Diagnostics go to stderr because stdout is the MCP transport and any
  stray bytes corrupt the stream ([`cli.py`](../src/relic/cli.py),
  `_serve`).
- **Client config.** Point any MCP client at it over stdio. For Claude Code, add
  it to `.mcp.json`. See [skills.md](skills.md).

## Capture and memory processes

### relic ingest

The capture pipeline. Pulls engineering history into the graph.

- **Invocation.** `relic ingest --repo owner/name [--limit N] [--months M] [--bulk] [--fresh]`.
- **Reads.** GitHub GraphQL (merged PRs with files and reviews, batched per page) and
  GitHub REST (non-PR issues); both windowed to the last `--months`. Linear GraphQL when
  `LINEAR_API_KEY` is set; the per-repo checkpoint ledger.
- **Writes.** The FalkorDB graph (one episode per PR and per issue, landed under
  the repo's `group_id`); the raw store at `./data/raw/<source>/<id>.json`; the
  checkpoint at `./data/ingest/<group_id>.log`.
- **Depends on.** A GitHub token (from `GITHUB_TOKEN` or `gh auth token`),
  `OPENAI_API_KEY` (Graphiti extraction and embeddings), and FalkorDB.
- **Flow.** Probe the graph, fetch the window (PRs in one batched GraphQL query per page),
  dump raw, map to deterministic episodes, then add episodes to Graphiti — sequentially by
  default so later episodes resolve entities from earlier ones, or batched via `--bulk`.
- **Resumable.** The checkpoint records each episode that lands, so a re-run skips
  what already loaded and a failed run resumes where it stopped. `--fresh` clears
  it and reloads everything.
- **Observable.** Logs the fetch counts and timing, per-episode failures, and a
  final loaded/skipped/failed summary, all to stderr. `relic --verbose ingest`
  adds debug detail.
- **`--limit`.** Caps PRs and issues pulled, most recent first, to bound API and
  per-episode LLM cost on a large repo.

Full detail in [ingestion.md](ingestion.md).

### relic recall

General memory recall. Returns facts with their sources.

- **Invocation.** `relic recall "question" [--repo owner/name] [--num-results N]`.
- **Reads.** The graph, scoped to a repo's `group_id` (defaults to `TARGET_REPO`).
- **Writes.** Nothing. Prints a cited, human-readable block.
- **Depends on.** `OPENAI_API_KEY` (search and rerank) and FalkorDB.
- **Never raises.** If search fails, it returns an empty answer rather than an
  error ([`recall.py`](../src/relic/graph/recall.py)).

### relic query

A narrower, people-focused query. Finds people connected to work matching the
text, with a deterministic Cypher fallback if hybrid search comes back empty.

- **Invocation.** `relic query "text" [--repo owner/name]`.
- **Reads.** The graph, scoped to a repo's `group_id`.
- **Writes.** Nothing. Prints each person, their relation, the fact, profile URL,
  and source episodes, de-duplicated by name.
- **Depends on.** `OPENAI_API_KEY` and FalkorDB.

### relic eval

The recall scorecard. Measures whether recall cites the PR or issue that actually
holds each answer.

- **Invocation.** `relic eval [--path eval/github_recall.json] [--num-results N]`.
- **Reads.** A gold set JSON (questions plus the PR or issue numbers that answer
  them); the graph.
- **Writes.** Nothing. Prints three metrics (hit rate, MRR, coverage) plus a
  per-case pass or miss line.
- **Depends on.** `OPENAI_API_KEY` and FalkorDB.
- **No LLM judge.** Scoring is a deterministic URL match, so the ruler itself
  costs nothing beyond the recall calls it measures
  ([`scorecard.py`](../src/relic/scorecard.py)).

## Skill and registry processes

These read and write the SQLite registry. None touches the graph or the network.

### relic register

Load a hand-authored `SkillIR` JSON file into the registry. The manual authoring
path until the Phase 4 compiler produces skills.

- **Invocation.** `relic register path/to/skill.json`.
- **Reads.** The JSON file (validated against `SkillIR`; invalid input exits 1).
- **Writes.** The registry. Upsert by `skill_id`, preserving `created_at`.

### relic list, relic show

Inspect the registry.

- **`relic list [--status draft|verified|deprecated]`.** Prints a table of skills.
- **`relic show <skill_id> [--json]`.** Prints the rendered `SKILL.md`, or the raw
  `SkillIR` JSON with `--json`.

### relic verify, relic deprecate

Move a skill through its lifecycle. Each mutation rewrites the stored document so
a reloaded `SkillIR` always matches its row.

- **`relic verify <skill_id>`.** Promotes draft to verified and stamps
  `last_verified_at`. Refuses to verify a deprecated skill.
- **`relic deprecate <skill_id>`.** Marks a skill deprecated so it stops being
  emitted or served.

### relic emit

Write verified skills to a target repo's `.claude/skills/`, and reconcile.

- **Invocation.** `relic emit --repo /path/to/target-repo`.
- **Reads.** The registry.
- **Writes.** One `.claude/skills/<id>/SKILL.md` per verified skill, plus a
  `README.md` catalog index. Removes the directories of registry skills that are
  no longer verified.
- **Reconciles, does not clobber.** It only touches skill directories whose id
  matches a skill in the registry, so hand-authored skills under `.claude/skills/`
  are left alone ([`emit_files.py`](../src/relic/serve/emit_files.py)).

### relic catalog

Print the browsable markdown index of verified skills to stdout (the same index
`emit` writes to the target repo).

## Diagnostics

### relic doctor

A read-only health check. Reports where the registry lives and how many skills it
holds by status, whether FalkorDB is reachable, and which API keys are
configured. It creates nothing and never raises: a broken setup still produces a
readable report ([`doctor.py`](../src/relic/doctor.py)).

- **Invocation.** `relic doctor`.
- **Reads.** The registry (only if the file exists), settings, and a TCP probe of
  the FalkorDB host and port.
- **Writes.** Nothing.

## External processes the platform calls

These run outside Relic, invoked per command as HTTP calls.

- **OpenAI.** Graphiti's LLM (`gpt-4o-mini` for extraction and reranking) and
  embeddings (`text-embedding-3-small`). Used by `ingest`, `recall`, `query`,
  `eval`, and `serve` (recall).
- **GitHub REST.** Source for `ingest`, via `githubkit`.
- **Linear GraphQL.** Optional source for `ingest`, via `gql`, guarded behind
  `LINEAR_API_KEY`.
- **Anthropic.** The Phase 4 skill compiler. Not called yet.

## Pending processes

These are stubs today. See [roadmap.md](roadmap.md).

- **`relic resolve` (Phase 3).** Unify a person across GitHub and Linear into one
  `Person` node. Prints "not implemented yet" and exits 1.
- **`relic compile` (Phase 4).** Detect a recurring procedure and compile it into
  a grounded `SkillIR`. Prints "not implemented yet" and exits 1. The detector
  ([`detect.py`](../src/relic/compile/detect.py)) and compiler
  ([`compiler.py`](../src/relic/compile/compiler.py)) are docstring-only stubs;
  the renderer ([`render.py`](../src/relic/compile/render.py)) is already wired.
- **`pipeline/run.py` (Phases 4 to 8).** The full-run orchestrator (ingest,
  resolve, detect, compile), the eventual Cloud Run Job entry point. Raises
  `NotImplementedError`.
