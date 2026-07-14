# Relic dev tasks. Run `just` with no arguments to list them.
#
# uv runs Python on the host, Docker runs Postgres only. `just setup` takes a
# fresh clone to a working, verifiable environment. Daily work runs through the
# short recipes below.

set dotenv-load

# List the recipes.
default:
    @just --list

# Full local setup: deps, .env, Postgres, pre-commit hook, then doctor. Safe to re-run.
setup: env && doctor
    uv python install 3.12
    uv sync --dev
    docker compose up -d --wait postgres
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

# Start Postgres and wait until it reports healthy.
up:
    docker compose up -d --wait postgres

# Stop Postgres. The data volume is kept.
down:
    docker compose down

# Tail Postgres logs. Ctrl-C to stop.
logs:
    docker compose logs -f postgres

# Report registry, engram, and key status. Reads only, changes nothing.
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

# Run the test suite. The e2e loop smoke is excluded (needs a live Postgres);
# run it deliberately with `just loop-smoke`.
test:
    uv run python -m pytest -m "not e2e"

# End-to-end closed-loop smoke against a real Postgres: ingest a fixture repo,
# daemon inject (cited recall) and capture (write-back + readback), tear down.
# No API keys needed; uses a throwaway per-run workspace, never touches shared ones.
loop-smoke: up
    uv run python -m pytest -m e2e -rs tests/test_loop_smoke.py

# Enforce the subsystem import boundaries (import-linter contracts in pyproject).
imports:
    uv run lint-imports

# Run the MCP server over stdio. Mainly manual testing; the real consumer is an MCP client via .mcp.json.
serve:
    uv run relic serve

# Ingest a repo's PRs and issues. REPO is owner/name; LIMIT caps the pull (most recent first), empty pulls all.
ingest REPO LIMIT="":
    uv run relic ingest --repo {{REPO}} {{ if LIMIT != "" { "--limit " + LIMIT } else { "" } }}

# Recall facts for QUERY with their sources. Quote a multi-word query: just recall "who reviews auth?"
recall QUERY:
    uv run relic recall "{{QUERY}}"

# Destructive: wipe the Postgres volume (every workspace's memories, not one).
reset-db:
    docker compose down -v
    docker compose up -d --wait postgres

# Destructive: remove local state under data/ (registry, raw artifacts) and tool caches. Keeps the Postgres volume.
clean:
    rm -rf data .ruff_cache .pytest_cache
