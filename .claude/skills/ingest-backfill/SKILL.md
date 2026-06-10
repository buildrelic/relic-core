---
name: ingest-backfill
description: Run, resume, or re-run a repo ingest/backfill into the relic memory graph. Use whenever the task is "ingest <repo>", "backfill", "re-ingest", "wipe and reload the graph", an ingest run that failed partway, or questions about checkpoints, --fresh, --bulk, or ingest rate limits. Ingest runs cost real LLM money and are resumable; doing this wrong wastes both.
---

# Running an ingest backfill

Ingest fetches a repo's history (GitHub PRs and issues, plus Linear if configured), maps it to episodes, and loads each episode through Graphiti extraction. The extraction step calls the LLM per episode: a backfill is a real money-and-minutes operation, so prefer resuming over restarting.

## Prerequisites (check before launching)

1. FalkorDB up: `just up`, then `uv run relic doctor` for a one-shot health report (graph reachability, which API keys are set, registry state).
2. Required env in `.env`: `OPENAI_API_KEY` (extraction + embeddings), `GITHUB_TOKEN` (falls back to `gh auth token`), optional `LINEAR_API_KEY`.
3. Provider choice: default is OpenAI. For large backfills on a low OpenAI tier, `GRAPHITI_LLM_PROVIDER=gemini` (with `GEMINI_API_KEY`) uses Gemini Flash for extraction at much higher rate limits; embeddings stay on OpenAI regardless, on purpose (switching embedders changes vector dims and breaks search against the existing graph).

## The command

```
uv run relic ingest --repo owner/name [--months N] [--limit M] [--fresh] [--bulk]
```

- `--months N` windows the fetch; always set it for a first run against a big repo. `--limit M` caps the episode count for a cheap smoke run (try `--limit 10` first against an unfamiliar repo).
- **Resume is the default.** Checkpoints (per group_id, keyed on deterministic episode names) record every episode that landed; a re-run skips them. A failed run is resumed by running the same command again.
- `--fresh` clears ONLY the checkpoint ledger (see `_ingest` in `src/relic/cli.py`: it just calls `clear(ledger)`). It does not touch graph data, so re-loading into an un-wiped graph duplicates episodes. A true wipe-and-reload is two steps: `docker compose exec falkordb redis-cli GRAPH.DELETE <group_id>` (the repo's graph key, e.g. `buildrelic__relic-core`), then ingest with `--fresh`. Confirm with the user before wiping a graph that took hours to build.
- `--bulk` batches episodes through `add_episode_bulk` for a 5-10x speedup, with per-batch fallback to per-episode on failure. Good default for backfills; sequential mode gives better per-episode failure visibility.

## While it runs

- A live progress bar shows on stderr; `--verbose` swaps it for DEBUG logs.
- OpenAI 429s are retried with exponential backoff (max 60s, 6 attempts) by `_call_with_backoff` in `src/relic/graph/load.py`. Long pauses on a low tier are backoff, not a hang.
- One bad episode never aborts the run; failures are counted and listed in the final `LoadStats`.
- Raw payloads land in `data/raw/<source>/` and per-repo logs in `data/ingest/<group_id>.log`.

## Verifying afterwards

1. The run summary prints attempted/loaded/skipped/failed. Investigate failures before trusting recall.
2. Spot-check the graph (the graph-inspect skill covers this in depth): episode counts for the group_id should be near the loaded count.
3. Run a recall: `uv run relic recall "<something you know is in the repo history>"` and check the answer cites real PRs.
