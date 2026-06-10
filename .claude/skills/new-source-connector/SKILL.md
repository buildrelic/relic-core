---
name: new-source-connector
description: Add a new ingest source (Notion, Slack, Granola, Jira, or any new system) to relic-core, or significantly extend an existing connector. Use whenever the task is fetching a new kind of data into the memory graph, "add X as a source", "ingest from X", or writing a new connector module under src/relic/ingest/. The repo has a strict connector pattern; follow it instead of inventing a new shape.
---

# Adding an ingest source

Every source follows the same pipeline: **connector** (network I/O) → **records** (typed dataclasses) → **mapper** (pure transform) → **episodes** (JSON for Graphiti). The split exists so everything after the connector is deterministic and testable offline. Study `src/relic/ingest/linear.py` (small) and `github.py` (full-featured) before writing anything.

## The pattern, step by step

1. **Connector module**: `src/relic/ingest/<source>.py`. Owns all network calls. Conventions from the existing two:
   - Gate on config: add the API key to `src/relic/config.py` settings and `.env.example`, expose a `<source>_enabled()` check (see `linear_enabled()`). The source must be a no-op when unconfigured, never an error.
   - Page everything with cursors; cap page sizes like the existing connectors (GitHub uses 25 PRs/page, Linear uses first=50).
   - Window by time where the API supports it (GitHub uses `--months` cutoff and stops paging past it). Unbounded backfills are an LLM-cost problem, not just a speed problem: every episode goes through extraction.
2. **Records**: frozen dataclasses in `mappers.py` (`PullRequestRec`, `IssueRec` are the models to copy). Parse all timestamps through `_parse_aware()` so everything is UTC-aware. No optional network types may leak in; records are plain data.
3. **Raw provenance**: dump the verbatim API payload via `raw_store.dump_raw()` to `data/raw/<source>/`. Citations must always be able to point at the original.
4. **Mapper**: a pure `<thing>_to_episode()` returning `EpisodeSpec`. Episode **names must be deterministic** from the source (e.g. `pr-<number>`), because checkpoints key on names: that is what makes re-runs resumable. Clip long bodies (`_MAX_DESC_CHARS = 2000`), filter bots (REST logins end in `[bot]`; GraphQL marks `__typename == "Bot"`), include the source URL in the episode JSON so recall can cite it.
5. **Wiring**: extend the `ingest` command in `src/relic/cli.py` (lazy imports inside the command body) and thread the checkpoint skip-set through, same as GitHub/Linear.
6. **Tests**: one `tests/test_<source>_fetch.py` with fake clients (no network; copy the fake-client style of `test_github_fetch.py`) and mapper cases in `test_mappers.py`. The fetch tests assert paging, windowing, and parse edge cases.
7. **Docs**: update `docs/ingestion.md` and `src/relic/ingest/CLAUDE.md` in the same commit.

## Things that bite

- group_id comes from `repo_group_id()`-style slugging and must match `^[A-Za-z0-9_-]+$`. Pick the partition key deliberately (per repo? per workspace?) since it decides graph isolation.
- Bot filtering and review/body capping are not optional polish: they control extraction cost and graph noise.
- Keep the mapper free of network and LLM calls. If you need a lookup, do it in the connector and put the result in the record.
