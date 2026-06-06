# Configuration

All runtime configuration is environment variables, loaded from `.env` into a
pydantic-settings model ([`config.py`](../src/relic/config.py)). Secrets are
optional, so importing config (and `relic --help`) never needs a populated `.env`.
`get_settings()` reads and caches them.

## Setup

`just setup` does the full setup. For just the configuration pieces:

```bash
just env   # copy .env.example to .env if missing, then fill in keys
just up    # start the graph on localhost:6379, UI on http://localhost:3000
```

Run `just doctor` to confirm what is configured.

## Variables

| Variable | Default | Required for | What it does |
|---|---|---|---|
| `OPENAI_API_KEY` | none | `ingest`, `recall`, `query`, `eval`, `serve` recall | Graphiti extraction, embeddings, reranking |
| `GITHUB_TOKEN` | none | `ingest` (or `gh auth`) | read GitHub history; falls back to `gh auth token` |
| `LINEAR_API_KEY` | none | Linear ingest | enables the Linear source when set |
| `ANTHROPIC_API_KEY` | none | Phase 4 compiler | skill compilation (not used yet) |
| `GEMINI_API_KEY` | none | alternate graph LLM | reserved, not wired |
| `GRAPHITI_LLM_PROVIDER` | `openai` | none | graph LLM provider selector |
| `FALKORDB_HOST` | `localhost` | graph commands | FalkorDB host |
| `FALKORDB_PORT` | `6379` | graph commands | FalkorDB port |
| `FALKORDB_PASSWORD` | none | graph commands | leave unset for a local no-auth instance |
| `FALKORDB_DATABASE` | `relic` | graph commands | default graph for ungrouped data |
| `REGISTRY_DB_PATH` | `./data/registry.db` | skill commands | SQLite registry location |
| `TARGET_REPO` | none | optional | default `owner/name` for `recall`, `query`, `eval` |
| `SEMAPHORE_LIMIT` | `10` | `ingest` | fetch and Graphiti ingestion concurrency |

Unknown keys in `.env` are ignored (`extra="ignore"`), so a leftover variable from
an earlier version does no harm.

## Secret sanitization

The secret fields (`anthropic_api_key`, `gemini_api_key`, `openai_api_key`,
`github_token`, `linear_api_key`) are cleaned after load: a value that is blank,
contains whitespace, or starts with `#` is treated as unset. The reason is that a
`.env` copied from `.env.example` can leave an inline comment as the value
(python-dotenv keeps `KEY=  # note`), and a real API token never contains
whitespace or `#`. So `.env.example` keeps comments on their own lines, and a
half-filled `.env` fails clean rather than sending a garbage credential.

## The stores

Three stores, all under `./data/` by default (gitignored).

- **The graph (memory).** FalkorDB, addressed by `FALKORDB_*`. Not a local file:
  it is a networked service. Per-repo graphs are named by `group_id`; the
  `FALKORDB_DATABASE` graph holds ungrouped data. See
  [memory-and-recall.md](memory-and-recall.md).
- **The registry (skills).** SQLite at `REGISTRY_DB_PATH`. Created on first write.
  See [skills.md](skills.md).
- **The raw store.** Plain JSON under `./data/raw/<source>/<id>.json`, written by
  `ingest`. See [ingestion.md](ingestion.md).

## What keys unlock

`relic doctor` reports each key and what it unlocks
([`doctor.py`](../src/relic/doctor.py)):

| Key | Unlocks |
|---|---|
| openai | graph ingest, recall |
| anthropic | skill compiler (Phase 4) |
| gemini | alternate graph LLM |
| github | github ingest |
| linear | linear ingest |

## Reading doctor

`relic doctor` is a read-only health check. It reports:

- **registry.** Path, whether it exists, and a count of skills by status. If it
  does not exist, it tells you to run `relic register`.
- **graph.** The FalkorDB address (`falkordb://host:port/database`) and whether
  the host and port are reachable (a 0.5s TCP probe). If reachable but
  `OPENAI_API_KEY` is missing, it says recall and ingest still need the key.
- **keys.** Each key, set or missing, and its purpose.

It opens the registry only when the file already exists and never raises, so a
broken setup still produces a readable report.
