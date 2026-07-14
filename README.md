# relic-core

Relic is an early memory layer for AI-native teams. It ingests engineering
history into the engram store (Postgres, plus a Firestore document mirror the
web app renders and lets humans edit) and serves grounded answers and skills to
coding agents over MCP. Every answer cites its source. No LLM runs at ingest or
recall.

This repo is the prototype pipeline: ingest engineering history, land it in the
engram store, detect recurring procedures, and compile them into grounded skills
a coding agent can follow. See [`AGENTS.md`](AGENTS.md) for what Relic is and how we
work, and [`docs/`](docs/), the canonical documentation for the codebase and platform.

## How it works

`relic ingest` pulls history from a source (GitHub PRs and issues, Linear
issues, Granola meetings, Notion pages), maps each item to a typed episode, and
lands it as one row in Postgres: title, url, search text, the people it names,
the paths it touches, and a rendered markdown document mirrored to Firestore
for the web workspace. The whole path is deterministic. `relic recall` searches
those rows with Postgres full-text search and returns facts with their source
URLs. The daemon closes the loop for Claude Code: recall injected on every
prompt, each finished session captured back. See
[`system-brief`](docs/system-brief.mdx) for the one-page version.

## Requirements

- Python 3.12 (`.python-version` pins it)
- [uv](https://docs.astral.sh/uv/), which installs Python and dependencies
- [just](https://github.com/casey/just), the task runner you use day to day
- Docker, which runs Postgres, the engram store
- `gh` CLI logged in, or a `GITHUB_TOKEN`, to read history
- Optional: API keys for Linear, Granola, and Notion (more sources), Anthropic
  (the Phase 4 compiler), and the `FIREBASE_*` service account for the Firestore
  document mirror

No model API key is needed for the core loop. Run `just doctor` to see what is
configured.

## Setup

From a fresh clone:

```bash
just setup
```

`just setup` installs Python 3.12 with uv, syncs dependencies, creates `.env`,
starts Postgres and waits for it to report healthy, installs the pre-commit
hook, then runs `relic doctor`. It is safe to re-run. Fill your keys into `.env`
and run `just doctor` to confirm.

Memory lives in Postgres, not a local file. Start it before ingest or recall:
`just up` and `just down` control it. The connection is the `DATABASE_URL` var
in `.env` (the default matches `docker-compose.yml`), and the schema is applied
automatically on first use.

## Daily workflow

`just` with no arguments lists every recipe. The common ones:

```bash
just doctor                    # report registry, engram, and key status
just up                        # start Postgres, wait for healthy
just down                      # stop Postgres, keep the volume
just ingest astral-sh/uv 20    # pull a repo's PRs and issues, capped at 20
just recall "who reviews auth changes?"
just serve                     # run the MCP server over stdio
just check                     # lint, types, and tests, what CI runs
just fmt                       # format with ruff
```

## Commands

Every command is a subcommand of `relic`. The recipes above wrap the common ones;
commands without a recipe run through the CLI directly. See
[`docs/cli-reference.mdx`](docs/cli-reference.mdx) for every flag and exit code.

- **Capture and memory** (need Postgres running): `ingest`, `load`, `recall`,
  `query`, `eval`.
- **Skills** (read and write the SQLite registry): `register`, `list`, `show`,
  `verify`, `deprecate`, `emit`, `catalog`.
- **Long-running surfaces**: `serve` (MCP), `daemon` plus `install-hooks` (the
  closed loop), `serve-http` (the web app's control plane).
- **Diagnostics**: `doctor`.
- **Pending**: `resolve` (Phase 3) and `compile` (Phase 4) print "not implemented
  yet" and exit.

Skill lifecycle, end to end:

```bash
relic register examples/skills/pr-review-routing.json  # load a hand-authored skill as a draft
relic list
relic show pr-review-routing                           # inspect the rendered SKILL.md
relic verify pr-review-routing                         # promote a draft to verified
relic emit --repo /path/to/target-repo                 # write verified skills to .claude/skills/
relic catalog                                          # print the index of verified skills
relic deprecate pr-review-routing                      # retire it; re-emit removes its files
```

`emit` reconciles the target: it writes verified skills and removes the files of
any skill it has since deprecated. It only touches skill directories it knows from
the registry, so hand-authored skills under `.claude/skills/` are left alone.

`relic eval` scores recall against a gold set. Each case is a question plus the PR
number(s) whose content answers it. It runs recall and checks the cited sources
for an expected PR url, then reports the hit rate. It is deterministic, with no
LLM judge, so the ruler stays cheap:

```bash
relic eval                          # scores eval/github_recall.json
relic eval --path eval/my-set.json  # or your own gold set
```

## MCP server

`relic serve` exposes one MCP endpoint over stdio:

- each verified skill as a tool (call it for the steps) and a resource at `skill://<id>`
- `search_skills` to find a skill by text or scope
- `recall_memory` to query the graph and get facts with their sources

Recall is best effort: if Postgres is unreachable, `serve` logs a warning to
stderr and still serves skills. Point any MCP client at it over stdio.
For Claude Code, add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "relic": { "command": "relic", "args": ["serve"] }
  }
}
```

`relic` must be on the client's PATH; the project's `.venv` exposes it after
`just setup`. See [`docs/skills.mdx`](docs/skills.mdx) for the served surface in
detail.

## Further development

The dev loop is two recipes:

```bash
just check   # ruff, pyright, pytest, the exact three CI runs
just fmt     # format with ruff
```

`just check` is what CI gates on, so run it before you push. `just lint`, `just
types`, and `just test` run the three one at a time. The suite is fast and mostly
offline: the store integration tests self-skip without a reachable Postgres (CI
always runs them against a service container), and the end-to-end loop smoke
(`just loop-smoke`) runs against a real Postgres in about two seconds with no
API keys.

Where to look:

- New to the codebase? Read [`docs/architecture.mdx`](docs/architecture.mdx), then
  [`docs/processes.mdx`](docs/processes.mdx).
- Changing capture or retrieval? Read [`docs/ingestion.mdx`](docs/ingestion.mdx) and
  [`docs/memory-and-recall.mdx`](docs/memory-and-recall.mdx).
- Working on skills, emit, or the MCP server? Read [`docs/skills.mdx`](docs/skills.mdx).
- [`docs/roadmap.mdx`](docs/roadmap.mdx) tracks what is built versus pending.

What is built: capture (GitHub, Linear, Granola, Notion, agent sessions), the
engram store, recall, the closed-loop daemon and hooks, the registry, emit,
catalog, the scorecard, the MCP server, the web HTTP surface, and doctor are
wired up. The next two pieces are entity resolution (`resolve`, Phase 3) and the
procedure detector plus skill compiler (`compile`, Phase 4), which will produce
`SkillIR` records from the engram instead of by hand.

Three rules worth knowing before you start:

- `docs/` is the canonical documentation. Read the relevant page before you change
  a subsystem, and update it in the same PR or commit. Docs and code ship together.
- The engram and the registry never mix. Memory lives in Postgres, skills live in
  SQLite, and `SkillIR` is the only type that crosses the skill side.
- The house voice for all prose (docs, UI, errors, commits) is in
  [`AGENTS.md`](AGENTS.md). The short version: short declarative sentences, no em
  dashes, specifics over adjectives, lowercase product terms.

Tooling config (ruff, pyright, pytest) is in [`pyproject.toml`](pyproject.toml).
For the full setup, the test inventory, and CI, see
[`docs/development.mdx`](docs/development.mdx).
