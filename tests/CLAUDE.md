# tests: fast, mostly offline pytest suite

One flat directory covering fetch to serve. Run with `just test` or `uv run pytest`. Almost everything runs offline with fake clients; only `test_ingest_integration.py` touches live services, and it skips itself when `OPENAI_API_KEY` is missing or FalkorDB is unreachable.

## Layout

Files map to the module they cover: `test_mappers.py`, `test_github_fetch.py`, `test_load.py`, `test_recall.py`, `test_store.py`, `test_serve.py`, and so on. End-to-end paths live in `test_pipeline_slice.py` (draft to verify to emit to serve) and `test_mcp_e2e.py`. `conftest.py` provides `make_skill()`, a SkillIR factory with sensible defaults and per-test overrides.

## Conventions

- `asyncio_mode = "auto"` in pyproject: async tests need no decorator.
- `test_render.py` uses syrupy snapshots. A deliberate template change means regenerating them (`--snapshot-update`).
- Live-service tests guard with `pytest.mark.skipif`, never with network try/except. Follow that pattern for anything needing FalkorDB or OpenAI.
- New module in `src/relic/` means a matching `test_<module>.py` here.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
