# graph: Graphiti on FalkorDB

Builds and queries the temporal knowledge graph. Owns the Graphiti client, the typed graph schema, episode loading with backoff, and the recall and query paths.

## Files

- `engram.py`: flat Pydantic entity and edge types for Graphiti (`PersonNode`, `PullRequestNode`, `Authored`, `Reviewed`, `TouchesPath`, ...) plus `ENTITY_TYPES`, `EDGE_TYPES`, `EDGE_TYPE_MAP`. `make_engram()` builds the client on FalkorDB with the chosen LLM provider. Also `falkordb_reachable()` and `ensure_indexes()`, and two patches: `_patch_falkordb_empty_query()` (RediSearch empty-paren guard) and `_patch_prompt_json_datetime()` (datetime-tolerant prompt JSON).
- `load.py`: `load_episodes()` (sequential, per-episode `on_loaded` checkpoint callback) and `load_episodes_bulk()` (batched, falls back to per-episode on batch failure). `_call_with_backoff()` retries `RateLimitError` with exponential backoff (max 60s, 6 attempts). One bad episode never aborts a run.
- `recall.py`: `recall()` returns a `RecallAnswer` of facts with resolved `Source` citations. `format_answer()` renders the cited block. Never raises.
- `queries.py`: `reviewers_of()` hybrid search with a deterministic Cypher fallback over REVIEWED and AUTHORED edges. Never raises.

## Invoked by

`relic ingest`, `relic recall`, `relic query`, `relic doctor` in `cli.py`. `scorecard.py` uses `RecallAnswer` for eval. Imports `EpisodeSpec` from `ingest/mappers.py`.

## Gotchas

- Env: `OPENAI_API_KEY` (default provider), `GRAPHITI_LLM_PROVIDER` (`openai` or `gemini`), `GEMINI_API_KEY` + `GEMINI_MODEL` (hybrid), `FALKORDB_HOST/PORT/PASSWORD/DATABASE`.
- The gemini provider uses Gemini for extraction only. Embeddings stay OpenAI on purpose: switching the embedder changes vector dims and breaks search against the existing graph, forcing a fresh backfill.
- Graphiti does not retry 429s itself; `_call_with_backoff()` in `load.py` is what saves long ingest runs.
- Graphs partition per repo by `group_id`; the Cypher fallback filters on it.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
