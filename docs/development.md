# Development

Relic is a `uv` project on Python 3.12. The toolchain is ruff (lint and format),
pyright (types), and pytest (tests). CI runs all three.

## Requirements

- Python 3.12 (`.python-version` pins it)
- [uv](https://docs.astral.sh/uv/)
- [just](https://github.com/casey/just) (task runner)
- Docker (for FalkorDB)
- `gh` CLI logged in, or a `GITHUB_TOKEN`
- An OpenAI API key for any live graph work

## Setup

From a fresh clone:

```bash
just setup
```

`just setup` installs Python 3.12 with uv, syncs the dev dependencies, creates
`.env` from `.env.example` if you do not have one, starts FalkorDB and waits for
it to report healthy, installs the pre-commit hook, then runs `relic doctor`. It
is safe to re-run.

Missing keys show as missing in the doctor report. Fill them into `.env`, then run
`just doctor` again. Run `just` with no arguments to list every recipe.

## Daily commands

```bash
just                          # list every recipe
just doctor                   # is the environment wired up?
just check                    # lint, types, tests (what CI runs)
just fmt                      # format with ruff
just up                       # start FalkorDB, wait for healthy
just down                     # stop FalkorDB, keep the volume
just serve                    # MCP server over stdio, for manual testing
just ingest astral-sh/uv 20   # ingest a repo, capped at 20
just recall "who reviews auth changes?"
```

`just check` runs ruff, pyright, and pytest, the same three CI runs. `just lint`,
`just types`, and `just test` run them one at a time. `just reset-graph` and `just
clean` are destructive: the first wipes the FalkorDB volume, the second removes
local state under `data/`.

## Conventions

Tooling config is in [`pyproject.toml`](../pyproject.toml).

- **ruff.** Line length 100, target py312, lint rules `E`, `F`, `I`, `UP`, `B`,
  `SIM`. One per-file ignore: `skill_ir.py` keeps `Optional[...]` (`UP` off)
  because it is ported verbatim and must not drift from the system-design doc.
- **pyright.** Basic mode over `src` and `tests`, against the `.venv`.
- **pytest.** `asyncio_mode = "auto"` (async tests need no decorator), quiet
  output, tests under `tests/`.

The house voice for all prose (docs, UI, errors, commit messages) is in
[`AGENTS.md`](../AGENTS.md). The short version: short declarative sentences, no em
dashes, specifics over adjectives, real names only, lowercase product terms
(memory, capture, recall).

## Commits and PRs

From [`AGENTS.md`](../AGENTS.md):

- Titles are short and skimmable, read like a person dashed them off, no
  `feat:`/`fix:` prefixes, no emoji.
- Descriptions carry the detail: what changed and why, context, tradeoffs. Write
  the reasoning the diff cannot show.

## Tests

The suite is fast and mostly offline. Live graph work (real Graphiti, real
FalkorDB, real OpenAI) is exercised by hand, not in unit tests.

| Test | Covers |
|---|---|
| [`test_ontology.py`](../tests/test_ontology.py) | ontology models round-trip, SkillIR schema |
| [`test_mappers.py`](../tests/test_mappers.py) | deterministic payload to episode transforms |
| [`test_raw_store.py`](../tests/test_raw_store.py) | raw artifact paths and writes |
| [`test_ingest_integration.py`](../tests/test_ingest_integration.py) | the fetch and map path with stubbed sources |
| [`test_render.py`](../tests/test_render.py) | SkillIR to SKILL.md, snapshot-style |
| [`test_store.py`](../tests/test_store.py) | registry upsert, status, lifecycle |
| [`test_catalog.py`](../tests/test_catalog.py) | the markdown catalog index |
| [`test_emit.py`](../tests/test_emit.py) | emit, prune, catalog reconciliation |
| [`test_serve.py`](../tests/test_serve.py) | MCP server: tools, resources, search |
| [`test_mcp_e2e.py`](../tests/test_mcp_e2e.py) | the MCP server end to end |
| [`test_recall.py`](../tests/test_recall.py) | recall facts, provenance, formatting |
| [`test_falkordb_query.py`](../tests/test_falkordb_query.py) | the query path and Cypher fallback |
| [`test_scorecard.py`](../tests/test_scorecard.py) | eval scoring and summary |
| [`test_doctor.py`](../tests/test_doctor.py) | the health report |
| [`test_cli.py`](../tests/test_cli.py), [`test_cli_skills.py`](../tests/test_cli_skills.py) | CLI wiring |
| [`test_pipeline_slice.py`](../tests/test_pipeline_slice.py) | draft to verify to emit to serve, end to end |

Shared fixtures are in [`conftest.py`](../tests/conftest.py); `make_skill` builds
`SkillIR` instances with overridable defaults.

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on push to `main`
and on every PR. It spins up a FalkorDB service container, syncs with `uv`, then
runs ruff, pyright, and pytest in sequence. A red check on any of the three fails
the build. `just check` runs the same three locally before you push.
