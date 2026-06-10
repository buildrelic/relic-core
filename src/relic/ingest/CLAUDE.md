# ingest: fetch sources, map to episodes

Pulls engineering history from GitHub and Linear and maps it into deterministic episode specs for the graph. No LLM calls here: fetching is I/O, mapping is pure transforms.

## Files

- `github.py`: GraphQL-first GitHub connector on githubkit. `fetch_repo()` pages merged PRs (25 per page, up to 100 files and 50 reviews each) and issues (REST `since` filter), windowed to the last N months. Token comes from `GITHUB_TOKEN`, falling back to `gh auth token`.
- `linear.py`: Linear GraphQL connector. Gated on `LINEAR_API_KEY` via `linear_enabled()`.
- `mappers.py`: pure, deterministic transforms. Record dataclasses (`PullRequestRec`, `IssueRec`, `RepoBundle`, `EpisodeSpec`), `repo_group_id()` slugging, `pr_to_episode()` and `issue_to_episode()`. Filters bots, caps reviews at 10 (decided states ranked first), clips bodies at 2000 chars.
- `checkpoint.py`: per-group_id ledger of landed episode names. `load_done()`, `record_done()`, `clear()`. Makes re-runs resumable.
- `raw_store.py`: `dump_raw()` writes verbatim payloads to `data/raw/<source>/` for provenance.

## Invoked by

`relic ingest --repo owner/name [--months N] [--limit M] [--fresh] [--bulk]` in `cli.py`. `graph/load.py` consumes `EpisodeSpec`.

## Gotchas

- Checkpoints key on episode names (deterministic from source), not UUIDs. `--fresh` clears the ledger only; wiping the graph itself needs `GRAPH.DELETE` on the repo's graph key.
- Bot detection differs by API: REST logins end in `[bot]`, GraphQL marks `__typename == "Bot"` with a bare login.
- All datetimes parse to UTC-aware via `_parse_aware()`.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
