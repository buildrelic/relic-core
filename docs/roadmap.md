---
title: "Roadmap and status"
description: "Milestones M0 to M4, the phase plan, and what is built versus pending."
---

Relic is a prototype built phase by phase against an external plan. The goal is to
prove one loop end to end: ingest real engineering history, structure it in a
graph, detect a recurring procedure, compile it into a grounded `SKILL.md`, and
have a coding agent visibly follow it.

The thesis to prove (M3): in a real repo, Claude Code opens a PR touching
`src/auth/**` and tags the correct reviewer because a Relic-compiled skill told it
to, with no hand-written `CLAUDE.md` and no prompt engineering.

## Milestones

- **M0, skeleton.** Repo, `uv` env, CI, `relic --help`. Done.
- **M1, graph populated.** A repo's merged PRs, reviews, and issues are queryable
  in the graph. Done. (The plan also scoped one person resolved across GitHub and
  Linear into M1; that part waits on Phase 3.)
- **M2, first skill.** A grounded `SKILL.md` for PR review routing, generated from
  real history, passing schema validation. Pending: needs the Phase 4 compiler.
- **M3, activation moment.** Claude Code follows a compiled skill on a live PR.
  Pending.
- **M4, on GCP.** The pipeline runs as a Cloud Run Job against a hosted FalkorDB,
  and the MCP server is reachable. Pending.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo and toolchain | done |
| 1 | Ontology and SkillIR as code | done |
| 2 | Deterministic ingestion (GitHub + Linear) | done |
| 3 | Entity resolution | stub (`resolve`) |
| 4 | Procedure detection + skill compilation | stub (`compile`, `detect`, `compiler`); `render` (in `relic-serve`) done |
| 5 | Registry + human verification | done |
| 6 | Serve into the coding agent | done (files + MCP) |
| 7 | Activation-moment demo | pending |
| 8 | Lift to GCP | pending (`pipeline/run.py` stub) |

### Done

- **Phase 0 to 2.** The capture pipeline runs: `ingest` pulls GitHub PRs (with
  descriptions, files, reviews, review comments) and issues (with bodies), keeps
  raw copies, maps them deterministically, and lands them in the graph per repo.
  See [ingestion.md](/ingestion).
- **Phase 5.** The registry and lifecycle are wired: `register`, `list`, `show`,
  `verify`, `deprecate`. See [skills.md](/skills).
- **Phase 6.** Both serve paths work: `emit` plus `catalog` write skills into a
  repo's `.claude/skills/`, and `serve` exposes them over MCP.

### Built beyond the original plan

- **Recall.** A general `recall` that returns facts with sources, over the CLI and
  as an MCP `recall_memory` tool, alongside the narrower `query`.
- **The scorecard.** `eval` measures recall against a gold set, so retrieval
  changes are measured rather than guessed.
- **doctor.** A read-only setup health check.
- **`--limit`.** Bounds ingestion cost on large repos.

### Pending

- **Phase 3, entity resolution.** Unify a person across GitHub and Linear:
  deterministic key match (email, normalized handle) first, LLM only for the
  ambiguous tail. `resolve` is a stub.
- **Phase 4, the core.** The detector finds a recurring procedure with a
  deterministic graph query plus support and confidence thresholds, with no LLM,
  which is what makes it trustworthy. The compiler feeds that grounded evidence to
  Anthropic with `SkillIR` as a structured-output schema, so the model can only
  return a valid skill, every field cites a source, anything ungrounded is
  dropped, and `status` is always `draft`. `detect` and `compiler` are stubs;
  `render` (in the serve package) is done.
- **Phase 7, the demo.** Install a compiled skill in a repo, prompt Claude Code,
  and record it tagging the right reviewer because of the skill. Then compile a
  second skill to show the loop generalizes.
- **Phase 8, GCP.** `pipeline/run.py` becomes a Cloud Run Job (ingest, resolve,
  detect, compile); the registry moves to Postgres; the raw store moves to object
  storage; the MCP server runs as a reachable service. It is a stub today.

## A note on the graph backend

The plan scoped the prototype on embedded Kuzu (zero-infra) and put FalkorDB at
Phase 8 (production). That changed. The graph was migrated to FalkorDB early, run
self-hosted via Docker, because Kuzu blocked two things the loop needs: per-repo
`group_id` partitioning crashed `add_episode`, and full-text search did not work.
FalkorDB gives both. Recall and query code did not change across the migration,
since both ride the portable `graphiti.search` API. See
[memory-and-recall.md](/memory-and-recall). The production move is now from
self-hosted FalkorDB to a managed or hosted FalkorDB instance, not a backend swap.

## Explicitly not building yet

From the plan, deferred until the core loop works: the Relic daemon, Local Engram,
and Sync Client; the radioactive-graph partition; multi-tenancy, tenant and org
management, and scope enforcement; the tiered cost model; Slack, Notion, Drive,
and Granola connectors; the generalized confidence-scored detector and learning
loop; and the full org ontology beyond the engineering subset (the `org.py` types
exist but no connector populates them). Each is straightforward to add after the
loop works, and none changes whether it works.
