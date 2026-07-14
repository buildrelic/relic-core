# Relic

Relic is an early, in-progress memory layer for AI-native teams, built on the
**engram store** (Postgres plus a Firestore document mirror, ADR-0007). It is
not finished and its shape is not settled. We are figuring out what it should
be by using it ourselves and with a few friends who are building companies.

This repo is the product. The marketing site lives elsewhere; its copy decisions
do not bind anything here.

## How we work (read this first)

- **We build for ourselves and a handful of friends' companies, then iterate on
  what they and we actually hit.** The roadmap is the real problems we run into,
  not a spec written in advance.
- **We do not know our wedge yet.** Do not optimize for a positioning we have not
  earned. Optimize for the core loop being genuinely useful to us first.
- **Bias to a rough thing we can use over a polished thing we cannot.** Ship the
  smallest version we can put into our own daily workflow, then learn from it.

## Who owns what

The codebase is a uv workspace of owned packages under `packages/`. Each engineer
owns one subsystem and works inside it.

- **Paris.** `relic-ingest`: the ingestion pipeline. GitHub and Linear connectors,
  raw store, mappers.
- **Abhinav.** `relic-engram`: the memory and retrieval store. The Postgres
  store, derive, recall, the Firestore document mirror, and the procedure
  detector and compiler.
- **Zidan.** `relic-serve` and `relic-cli`: the MCP server, the skill registry,
  render, and the composition-root CLI. The web app under `apps/` is his too, once
  it lands (the directory does not exist yet).
- **Shared.** `relic-core`: the interface contracts (`relic.contracts`), the
  ontology, settings, and logging. All three own it together.

Boundaries hold by construction. A package lists its dependencies in its own
`pyproject.toml`, so it can import another package only if it depends on it, and
only that package's public API (its `__init__.py`). `relic.cli` is the one place
that wires the three subsystems together.

## How we build: interface-driven

Subsystems talk through contracts, never through each other's internals.

1. **Define the contract first.** A cross-subsystem interaction starts as a type
   in `relic.contracts`: a Protocol, a dataclass, or a Callable. Agree on it with
   the affected owner before building behind it.
2. **The consumer depends on the contract, the producer fills it.** Neither
   imports the other.
3. **The composition root injects the concrete piece.** `relic.cli` (later the
   daemon and web app) builds the real implementation and hands it in.

Recall is the worked example. `relic-serve` takes a `RecallFn` and never imports
`relic-engram`; `relic.cli` builds the recall function from the store and injects
it. `EpisodeSpec` lives in `relic.contracts` for the same reason: ingest produces
it, engram consumes it, neither imports the other. Add new seams the same way.

`just check` runs ruff, pyright, pytest, and `lint-imports`. The last one fails
the build if a subsystem reaches across a boundary.

## What it roughly is (a starting shape, not a contract)

The loop we are exploring:

1. **Capture.** Feed work into the engram as typed episodes (PRs, tickets,
   meeting notes, pages, coding sessions from GitHub, Linear, Granola, Notion,
   Claude Code). Each episode lands deterministically as one memory row plus
   the people and paths it names. No manual tagging, no LLM extraction.
2. **Memory.** Rows a human can see and correct: the web app's Drive-style
   Memory workspace renders each memory's document and lets you edit it. Every
   row keeps its source URL, so answers can cite where they came from.
3. **Recall.** Search the store (Postgres full-text search) and get an answer
   with its sources. Two surfaces over one store: a typed API / MCP for agents,
   and the web workspace for humans.

The MCP server is probably the keystone: connect over MCP, sit below the editor,
so the same memory is reachable from whatever tool a team codes in. A strong
hypothesis, not a settled fact.

**Smallest useful slice (dogfood target):** one source in, the engram store
holds the rows, `recall` returns an answer with its sources, exposed as an MCP
server we use ourselves every day. Get that loop good before auth,
multi-tenancy, or a many-source ingestion pipeline.

## Positioning — current hypothesis, expect it to move

We have not locked a wedge. What the landing page currently leans on, treat as a
hypothesis to test, not gospel:

- a source of truth for **humans and agents**, not agents alone
- **provenance**: every answer cites its source
- a phrase we like: **"stored isn't remembered"** (a thread is saved somewhere
  but not retrievable knowledge)

If using the product points somewhere else, follow the product. Update this
section when the wedge gets sharper.

## Voice (any copy: docs, UI, errors, marketing)

Write like a sharp teammate dumping context, not a brand writing copy.

- Short declarative sentences. Subject-verb-object. No wind-up.
- **No em dashes.** Period to stop, colon to land a result or list, commas for
  asides.
- Specifics over adjectives: real IDs, dates, counts, tool names.
- No flourishes: no rule-of-three ("fast, simple, powerful"), no "it's not just
  X, it's Y," no rhetorical questions in body, no "imagine."
- **Real names only.** Real first names or handles in examples. Never
  `FirstName LastName` placeholders, never stock companies (Acme, Northwind).
- Lowercase product terms: memory, capture, recall, source of truth.
- Avoid the AI bullet `**Bold term** — explanation`. Use `**Bold term.** Short
  sentence.` instead.

## Documentation

`docs/` is the canonical documentation for the codebase and the platform, a
Mintlify site. The pages there are the source of truth, not code comments or
memory. Read the page for a subsystem before you change it.

Keep it in sync in the same change. A PR or commit that changes behavior, a
contract in `relic.contracts`, a CLI command or flag, config, the data model, or
a process updates the affected page under `docs/` in that same PR or commit. Docs
and code ship together, not in a follow-up.

- A new or changed command or flag: `docs/cli-reference.mdx`.
- A new env var or store: `docs/configuration.mdx`.
- A schema, graph node, or episode change: `docs/data-model.mdx`.
- A stub that became wired, or a phase that landed: `docs/roadmap.mdx`.
- The HTTP API the web app consumes (`relic serve-http`), or the ingest trigger:
  `docs/web-contract.mdx`. Read it before changing the seam.
- A new page: add `title` and `description` frontmatter and list it in the right
  group in `docs/docs.json`.

The Voice rules above apply to every page.

## Git, commits, issues

One shared repo, light process. The package boundaries do the heavy lifting, so
we do not gate every change behind a long review.

### Where you commit

Commit inside the package you own once `just check` passes. A change to a package
you do not own, or any change to `relic-core` (the shared contracts), gets a quick
review from the affected owner first: it crosses a boundary the others depend on.
`CODEOWNERS` records who owns each path.

### Review

PRs are de-emphasized. Lean on `/code-review` for a lightweight check instead of a
heavy PR walkthrough. Save real review for contract changes in `relic-core` and
edits that cross a package boundary.

### Issues

We track work in Linear, but only concrete work. Clear out stale and vague issues;
open a new one only when the task is real and identified, not as a placeholder.

### Branches

Optional for solo work inside your own package. When you do branch, copy the name
Linear generates for the issue and use it as is, for example
`paris/rel-10-ingest-run-observability`: it carries the owner, the issue ID, and a
slug, so the branch maps back to the issue on sight.

### Commits

The skimmable rules govern the **title** (commit subject). The **description** is
where the detail goes.

Titles:
- Short and skimmable.
- Read like a person dashed it off, not like an AI wrote it.
- No `feat:` / `fix:` / `chore:` prefixes unless this repo adopts them. No emoji.
- When the commit relates to a Linear issue, append `[part of <issue-ID>]`, for
  example `tighten ingest retry backoff [part of REL-10]`. This links the commit
  to the issue in Linear.

Descriptions:
- This is where detail lives: what changed and why, context, tradeoffs, anything
  the next person needs. Length is fine, structure (paragraphs, bullets) is fine.
- Write the reasoning the diff cannot show. The title says what at a glance; the
  body says why and how.

## Stack

- **Layout.** A uv workspace: `relic-core`, `relic-ingest`, `relic-engram`,
  `relic-serve`, `relic-cli` under `packages/`, each an installable package with
  its own dependencies. Import paths stay `relic.*` (a namespace package).
  `just setup` syncs the whole workspace; `just check` runs the gate.
- **Memory store.** Postgres 16 over asyncpg (ADR-0007). For now, while in dev,
  self-hosted via Docker (`just up`, localhost:5432, `DATABASE_URL`); a hosted
  instance (e.g. Neon) is the likely move once we are past dogfooding. The
  schema is owned by relic-core and applied by `ensure_schema()`; the landing
  web app reads and edits the same tables, so DDL changes are a cross-repo
  contract change.
- **Document mirror.** Firestore over firebase-admin, optional (`FIREBASE_*`,
  the same service account as the landing app). Best-effort: it degrades the
  web UI, never an ingest.
- **Recall.** Postgres full-text search. No model API calls at ingest or
  recall; `ANTHROPIC_API_KEY` is reserved for the Phase 4 compiler.
- **Primary interface.** MCP server.
- **Other libraries.** Everything else is open. Read the relevant docs before writing code.

## Maintaining this file

Keep this file current, but keep it lean. It is the first thing the next agent
reads; stale or bloated context is worse than none.

- Update a section when a real decision changes it: a stack choice (graph DB,
  models), a change to the core loop, the wedge getting sharper. Edit in place.
- Prune as you go. Delete lines that stop being true. This file is not a
  changelog; history lives in the git log and commit descriptions.
- Positioning is protected. Do not silently harden the wedge. If use points to a
  new wedge, write it as a dated hypothesis and flag it for a human, do not
  overwrite the belief as settled fact.
- When a commit changes how the project works, update the relevant section in the
  same commit.
