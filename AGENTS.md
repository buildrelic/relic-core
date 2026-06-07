# Relic

Relic is an early, in-progress memory layer for AI-native teams, built on
**Graphiti**. It is not finished and its shape is not settled. We are figuring
out what it should be by using it ourselves and with a few friends who are
building companies.

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

## What it roughly is (a starting shape, not a contract)

The loop we are exploring:

1. **Capture.** Feed work into Graphiti as episodes (PRs, tickets, threads,
   meeting notes from Notion, Slack, Linear, Granola, GitHub). Graphiti extracts
   entities and relationships and tracks how they change over time. No manual
   tagging or schema design.
2. **Memory.** A temporal knowledge graph of what the team knows and decided.
   Each edge traces back to the source episode it came from, so answers can cite
   where they came from.
3. **Recall.** Query the graph (semantic + keyword + graph traversal) and get an
   answer with its sources. Likely two surfaces over one store: a typed API /
   MCP for agents, and a wiki-style view for humans.

The MCP server is probably the keystone: connect over MCP, sit below the editor,
so the same memory is reachable from whatever tool a team codes in. A strong
hypothesis, not a settled fact.

**Smallest useful slice (dogfood target):** one source in, Graphiti builds the
graph, `recall` returns an answer with its sources, exposed as an MCP server we
use ourselves every day. Get that loop good before auth, multi-tenancy, or a
many-source ingestion pipeline.

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

## Git, commits, PRs

We track work in Linear. Branches, commits, and PRs all tie back to the Linear
issue they belong to.

### Branches

Work on an issue from the branch Linear generates for it. Copy the branch name
off the issue (Linear's "Copy git branch name") and use it as is, for example
`paris/rel-10-ingest-run-observability`. The name carries the owner, the issue
ID, and a slug, so the branch maps back to the issue on sight.

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

### Pull requests

The title follows the same rules as a commit title. The body is a walkthrough,
not a one-liner. Cover three things:
- **What changed.** Walk through every change, grouped so a reviewer can follow
  it.
- **Consequences.** What each change affects: behavior, other components,
  migrations, anything downstream.
- **How to test it.** The exact steps to verify it, commands and expected
  results included.

## Stack

- **Memory engine.** Graphiti (getzep/graphiti). Read its docs before touching
  ingestion or retrieval: it moves fast, heed version notes.
- **Graph DB.** FalkorDB. For now, while in dev, self-hosted via Docker
  (`just up`, localhost:6379): a managed/hosted instance is the likely move once
  we are past dogfooding. Chosen over embedded Kuzu for multi-tenant group_id
  partitioning (per-repo graphs) and working full-text search. Connection is
  configured by the `FALKORDB_*` env vars.
- **Graphiti models.** OpenAI `gpt-4o-mini` for extraction and reranking, and
  `text-embedding-3-small` for search (`src/relic/graph/engram.py`).
- **Relational DB.** Supabase (Postgres) is available for app/auth/relational
  data, not the memory graph itself.
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
