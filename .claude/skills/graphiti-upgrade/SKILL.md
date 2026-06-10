---
name: graphiti-upgrade
description: Safely upgrade or troubleshoot the graphiti-core dependency in relic-core. Use whenever bumping graphiti-core (a new Graphiti release, a dependabot PR, "upgrade graphiti", uv.lock changes touching graphiti), and whenever extraction, episode loading, or search breaks right after a dependency change. Relic monkeypatches graphiti internals, so an unchecked bump can break ingest silently.
---

# Upgrading graphiti-core

Relic is built on Graphiti, and Graphiti moves fast. Treat every version bump as potentially breaking, not because the API churns (it does), but because relic reaches into graphiti private internals in two places. An upstream refactor can make a patch silently no-op, which is worse than a crash: ingest keeps running and quietly produces a broken graph.

## The two patches to re-verify

Both live in `src/relic/graph/engram.py` and are written against graphiti-core 0.29.1 (the version is noted in the comments there):

1. `_patch_falkordb_empty_query()` wraps `search_ops._build_falkor_fulltext_query` and `FalkorDriver.build_fulltext_query` so RediSearch never receives an empty parenthesized query (happens when entity names sanitize to nothing: version tags, symbols, stopwords).
2. `_patch_prompt_json_datetime()` rebinds `to_prompt_json` with `default=str` in a hardcoded list of prompt modules (`_PROMPT_JSON_MODULES` in the same file), because those modules import it by name and hold their own reference. New upstream modules that import it by name will NOT be covered automatically (known gap: `search/search_helpers.py` already imports it outside the patched list).

## Procedure

1. Find the current version: `uv tree 2>/dev/null | grep graphiti` or grep `uv.lock`. Find the target and read every release note in between: https://github.com/getzep/graphiti/releases.
2. Before bumping, open the upstream source for the new version (`.venv/lib/python3.12/site-packages/graphiti_core/` after sync, or the GitHub tag) and verify:
   - `search.search_utils` (or wherever `_build_falkor_fulltext_query` lives now) still has that symbol with the same signature.
   - `FalkorDriver.build_fulltext_query` still exists.
   - The module list in `_PROMPT_JSON_MODULES` still matches the modules that import `to_prompt_json` by name: `grep -rl "to_prompt_json" .venv/lib/python3.12/site-packages/graphiti_core/`.
3. Check for embedder or vector-dimension changes in the release notes. Switching embedding behavior breaks search against the existing graph and forces a fresh backfill (`just reset-graph` + re-ingest). Flag this to the user before proceeding; it is their call.
4. Bump the constraint in `pyproject.toml`, then `uv lock && uv sync`.
5. Static gate: `just check` (lint, types, tests). The offline tests cover the patch call paths but not live behavior.
6. Live gate (needs FalkorDB up and `OPENAI_API_KEY`): `just up`, then `uv run pytest tests/test_ingest_integration.py -v`. This is the only test that exercises real extraction.
7. Update the version note in `engram.py` comments, and `src/relic/graph/CLAUDE.md` if patch details changed, in the same commit as the bump.

## If something broke after a bump

Work backwards through the same list: confirm both patches still find their targets (add a temporary assert or log in `make_engram()`), then diff the graphiti release notes for the loaded version against what `engram.py` assumes.
