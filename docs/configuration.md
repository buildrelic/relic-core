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
| `GEMINI_API_KEY` | none | `ingest` when provider is `gemini` | Gemini key for extraction; the `gemini` provider is hybrid and **also** needs `OPENAI_API_KEY` for embeddings |
| `GRAPHITI_LLM_PROVIDER` | `openai` | none | graph LLM provider: `openai` (default) or `gemini` (hybrid — Gemini LLM + OpenAI embeddings/rerank) |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | `gemini` provider | model used when `GRAPHITI_LLM_PROVIDER=gemini` |
| `FALKORDB_HOST` | `localhost` | graph commands | FalkorDB host |
| `FALKORDB_PORT` | `6379` | graph commands | FalkorDB port |
| `FALKORDB_PASSWORD` | none | graph commands | leave unset for a local no-auth instance |
| `FALKORDB_DATABASE` | `relic` | graph commands | default graph for ungrouped data |
| `REGISTRY_DB_PATH` | `./data/registry.db` | skill commands | SQLite registry location |
| `TARGET_REPO` | none | optional | default `owner/name` for `recall`, `query`, `eval` |
| `FETCH_CONCURRENCY` | `10` | `ingest` | concurrent GitHub requests (rate-limit safe) |
| `GRAPHITI_MAX_COROUTINES` | `20` | `ingest` | Graphiti internal LLM concurrency during load; lower to ~5 for the `gemini` provider (see note) |
| `BULK_LOAD` | `false` | `ingest` | load via batched `add_episode_bulk` (also `--bulk`) |
| `BULK_BATCH_SIZE` | `10` | `ingest` | episodes per bulk call; lower it to ease OpenAI rate limits |
| `SEMAPHORE_LIMIT` | `10` | `ingest` | deprecated shared knob, superseded by the two above |

Unknown keys in `.env` are ignored (`extra="ignore"`), so a leftover variable from
an earlier version does no harm.

### Tuning concurrency for the `gemini` provider

The defaults (`GRAPHITI_MAX_COROUTINES=20`, `BULK_BATCH_SIZE=10`) are sized for OpenAI,
whose rate-limiting naturally spreads out the graph writes. Gemini is fast enough that
those same writes arrive at once and can overrun FalkorDB's query queue during `--bulk`
(`ResponseError: Max pending queries exceeded`), which forces a slow per-episode
fallback. Lowering both to ~5 keeps bulk under FalkorDB's cap — and is *faster* overall
because nothing falls back:

```bash
GRAPHITI_LLM_PROVIDER=gemini GRAPHITI_MAX_COROUTINES=5 BULK_BATCH_SIZE=5 \
  relic ingest --repo owner/name --bulk --fresh
```

The alternative is to raise FalkorDB's own limit (it accepts a higher
`MAX_QUEUED_QUERIES` at startup) if you want to keep concurrency high.

## Secret sanitization

The secret fields (`anthropic_api_key`, `gemini_api_key`, `openai_api_key`,
`github_token`, `linear_api_key`) are cleaned after load: a value that is blank,
contains whitespace, or starts with `#` is treated as unset. The reason is that a
`.env` copied from `.env.example` can leave an inline comment as the value
(python-dotenv keeps `KEY=  # note`), and a real API token never contains
whitespace or `#`. So `.env.example` keeps comments on their own lines, and a
half-filled `.env` fails clean rather than sending a garbage credential.

## The stores

Four stores, all under `./data/` by default (gitignored).

- **The graph (memory).** FalkorDB, addressed by `FALKORDB_*`. Not a local file:
  it is a networked service. Per-repo graphs are named by `group_id`; the
  `FALKORDB_DATABASE` graph holds ungrouped data. See
  [memory-and-recall.md](memory-and-recall.md).
- **The registry (skills).** SQLite at `REGISTRY_DB_PATH`. Created on first write.
  See [skills.md](skills.md).
- **The raw store.** Plain JSON under `./data/raw/<source>/<id>.json`, written by
  `ingest`. See [ingestion.md](ingestion.md).
- **The ingest checkpoint.** A per-repo ledger at `./data/ingest/<group_id>.log`,
  one landed episode name per line. Lets a re-run skip what already loaded and
  resume a failed run. `ingest --fresh` clears it. See [ingestion.md](ingestion.md).

## Logging

Logs are diagnostics and go to stderr; a command's result goes to stdout. The
default level is `INFO` (phase counts, the run summary). `relic --verbose` (or
`-v`, before the command) drops it to `DEBUG`. Logging attaches to the `relic`
logger only, so third-party `INFO` chatter stays suppressed
([`obs.py`](../src/relic/obs.py)).

`relic ingest` shows a live progress bar over the load phase on an interactive
terminal. It is suppressed automatically when output is piped/redirected or under
`--verbose` (DEBUG logs would churn it), and can be turned off explicitly with
`relic ingest … --no-progress`; in all those cases you get the `loaded x/y`
heartbeat log lines instead. The bar and the log handler share one stderr console
so log lines render above the bar instead of corrupting it.

## What keys unlock

`relic doctor` reports each key and what it unlocks
([`doctor.py`](../src/relic/doctor.py)):

| Key | Unlocks |
|---|---|
| openai | graph ingest, recall (and embeddings/rerank under the `gemini` provider) |
| anthropic | skill compiler (Phase 4) |
| gemini | graph extraction when `GRAPHITI_LLM_PROVIDER=gemini` (hybrid; still needs openai) |
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
