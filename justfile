# Relic dev tasks. Run `just` with no arguments to list them.
#
# uv runs Python on the host, Docker runs FalkorDB only. `just setup` takes a
# fresh clone to a working, verifiable environment. Daily work runs through the
# short recipes below.

set dotenv-load

# List the recipes.
default:
    @just --list

# Full local setup: deps, .env, FalkorDB, pre-commit hook, then doctor. Safe to re-run.
setup: env && doctor
    uv python install 3.12
    uv sync --dev
    docker compose up -d --wait falkordb
    -uv run pre-commit install

# Create .env from .env.example if it is missing. Never clobbers an existing .env.
env:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .env ]; then
      echo ".env exists, leaving it untouched"
    else
      cp .env.example .env
      echo "created .env from .env.example. fill in keys, then run just doctor"
    fi

# Start FalkorDB and wait until it reports healthy.
up:
    docker compose up -d --wait falkordb

# Stop FalkorDB. The graph volume is kept.
down:
    docker compose down

# Tail FalkorDB logs. Ctrl-C to stop.
logs:
    docker compose logs -f falkordb

# Report registry, graph, and key status. Reads only, changes nothing.
doctor:
    uv run relic doctor

# Lint, types, tests, and import contracts. Exactly what CI runs.
check: lint types test imports

# Format the code with ruff.
fmt:
    uv run ruff format

# Lint with ruff.
lint:
    uv run ruff check

# Type-check with pyright. (python -m form is robust to entry-point shebangs.)
types:
    uv run python -m pyright

# Run the test suite. The e2e loop smoke is excluded (slow, spends OpenAI tokens);
# run it deliberately with `just loop-smoke`.
test:
    uv run python -m pytest -m "not e2e"

# End-to-end closed-loop smoke against a real FalkorDB: ingest a fixture repo,
# daemon inject (cited recall) and capture (write-back + readback), tear down.
# Needs OPENAI_API_KEY; uses a throwaway per-run graph, never touches shared ones.
loop-smoke: up
    uv run python -m pytest -m e2e -rs tests/test_loop_smoke.py

# Enforce the subsystem import boundaries (import-linter contracts in pyproject).
imports:
    uv run lint-imports

# Gate: did this branch silence a check instead of satisfying it? Reads the diff
# only, stdlib only, so it is instant. PR-only in CI. Exit 2 means "could not
# check", which is not a pass.
weakening BASE="origin/main":
    uv run python scripts/check_weakening.py --base {{BASE}}

# Gate: would this branch's new tests have failed before the change? Runs them
# against a worktree of the merge base, so it pays for a second uv sync and takes
# a couple of minutes. Refactors declare `RED-FIRST: n/a -- <reason>` in the PR body.
red-first BASE="origin/main":
    uv run python scripts/check_red_first.py --base {{BASE}}

# Both diff gates, the way CI runs them on a PR. Not part of `just check`: these
# need a base branch to diff against, and `check` has to work on a bare checkout.
gates BASE="origin/main": (weakening BASE) (red-first BASE)

# Run the MCP server over stdio. Mainly manual testing; the real consumer is an MCP client via .mcp.json.
serve:
    uv run relic serve

# Ingest a repo's PRs and issues. REPO is owner/name; LIMIT caps the pull (most recent first), empty pulls all.
ingest REPO LIMIT="":
    uv run relic ingest --repo {{REPO}} {{ if LIMIT != "" { "--limit " + LIMIT } else { "" } }}

# Recall facts for QUERY with their sources. Quote a multi-word query: just recall "who reviews auth?"
recall QUERY:
    uv run relic recall "{{QUERY}}"

# repo_group_id slugs owner/name to owner__name, the per-repo graph key.
# Destructive: wipe the FalkorDB volume (every repo's graph, not one). One repo: redis-cli GRAPH.DELETE astral-sh__uv.
reset-graph:
    docker compose down -v
    docker compose up -d --wait falkordb

# Destructive: remove local state under data/ (registry, raw artifacts) and tool caches. Keeps the FalkorDB volume.
clean:
    rm -rf data .ruff_cache .pytest_cache
