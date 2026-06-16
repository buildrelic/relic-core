---
title: "Data model"
description: "The three type layers: the rich ontology, the flat graph nodes, and SkillIR, plus the episode JSON."
---

Relic has three type layers, each with a distinct job. Conflating them is the
mistake the split prevents.

1. **The rich ontology** (`ontology/`) is the typed source of truth for the
   domain: nested models, audit records, canonical URLs.
2. **The flat graph types** (`graph/engram.py`) are the Graphiti-facing view:
   flat scalar fields only, no nested objects, no reserved names.
3. **SkillIR** (`ontology/skill_ir.py`) is the contract for a compiled skill,
   ported verbatim from the system-design doc.

A fourth shape sits between ingestion and the graph: the **episode JSON**, whose
keys mirror the flat graph attributes so the extractor maps cleanly onto them.

## Why three layers

Graphiti's custom entity and edge types must be flat scalar models. They cannot
nest objects, and they cannot use Graphiti's reserved attribute names: `uuid`,
`name`, `group_id`, `labels`, `created_at`, `summary`, `attributes`,
`name_embedding`. The rich ontology violates both rules: it nests (`Person` has a
`ContactInfo`, an `AuditInfo`, a list of `Link`) and it uses names like
`created_at`.

So the two are kept apart. The rich models stay the source of truth for the typed
API. The flat `*Node` models in [`engram.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-graph/src/relic/graph/engram.py) are
the graph-facing mirror. When the typed API needs richer structure than the graph
can hold, the rich layer already has it.

## Layer 1: the rich ontology

Lives in `ontology/`, ported from the Relic system-design doc. Every entity is a
node; relationships are expressed as `Id` reference fields rather than nested
objects, and enums are lowercase `Literal` aliases.

### Primitives

[`primitives.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-core/src/relic/ontology/primitives.py) holds the shared building
blocks: `Id` (a `str` alias), `TimePoint` (a `datetime` alias), `TimeRange`,
`Money`, `Link`, `ContactInfo`, `AuditInfo`, `TextBlob`, and `Taggable`.
`AuditInfo` (created_by, created_at, last_edited_by, last_edited_at) is the
provenance record carried by most entities.

### Engineering subset

[`entities.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-core/src/relic/ontology/entities.py) holds the types the prototype
actually ingests:

- `Person`: a teammate or external collaborator (extends `Taggable`, carries
  `ContactInfo`, `profiles`, `external_org`, `audit`).
- `Repo`, `PullRequest`, `Review`, `Issue`: the GitHub and Linear shapes. Each
  carries a canonical `url` for provenance and an `audit` record.
- `Procedure` plus `ProcedureEvidence`: an inferred standard procedure with
  `confidence` (0 to 1), `support` (a count), and a list of evidence, the target
  output of the Phase 4 detector.

`Person` and the primitives are ported from the doc. `Repo`, `PullRequest`,
`Review`, `Issue`, and `Procedure` were designed here to fit Phase 2 ingestion.

### Broader org model

[`org.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-core/src/relic/ontology/org.py) ports the rest of the system-design
ontology, beyond what the prototype ingests today: `Employee` (extends `Person`),
`Team`, `Customer`, `Product`, `Project`, `Document`, `Meeting`, `Decision`,
`Policy`, and the OKR types (`OKRGoal`, `KeyResult`, `Metric`). These are defined
and exported but not yet populated by any connector. They are the schema the
platform grows into as more sources land.

All ontology types are re-exported from
[`ontology/__init__.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-core/src/relic/ontology/__init__.py).

## Layer 2: the flat graph types

[`engram.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-graph/src/relic/graph/engram.py) declares the types Graphiti extracts
into. Entities:

| Key | Model | Fields |
|---|---|---|
| `Person` | `PersonNode` | full_name, preferred_name, email, slack_handle, external_org, profile_url |
| `Repo` | `RepoNode` | full_name, url, default_branch |
| `PullRequest` | `PullRequestNode` | number, title, state, url, merged_at |
| `Review` | `ReviewNode` | state, url, submitted_at |
| `Issue` | `IssueNode` | identifier, title, state, url |
| `Procedure` | `ProcedureNode` | archetype, confidence, support |

Edges (attributes only; Graphiti owns the endpoints):

| Key | Model | Attributes |
|---|---|---|
| `AUTHORED` | `Authored` | (none) |
| `REVIEWED` | `Reviewed` | state |
| `TOUCHES_PATH` | `TouchesPath` | path, additions, deletions |
| `REPORTS_TO` | `ReportsTo` | (none) |

`EDGE_TYPE_MAP` constrains which edges can connect which entity pairs:
`(Person, PullRequest)` allows `AUTHORED` and `REVIEWED`; `(Person, Person)`
allows `REPORTS_TO`. These three dicts (`ENTITY_TYPES`, `EDGE_TYPES`,
`EDGE_TYPE_MAP`) are passed to every `add_episode` call so extraction stays
typed and bounded ([`load.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-graph/src/relic/graph/load.py)).

## The episode JSON

The bridge from ingestion to the graph. [`mappers.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-ingest/src/relic/ingest/mappers.py)
turns each fetched record into an `EpisodeSpec` whose `body` is a JSON string. The
keys in that JSON mirror the flat `*Node` attribute names, so Graphiti's extractor
maps the values onto the typed entities.

A PR episode body looks like:

```json
{
  "repo": {"full_name": "buildrelic/relic-core", "url": "https://github.com/buildrelic/relic-core"},
  "pull_request": {
    "number": 12,
    "title": "...",
    "description": "...",
    "url": "https://github.com/buildrelic/relic-core/pull/12",
    "state": "merged",
    "created_at": "...",
    "merged_at": "...",
    "author": {"login": "...", "profile_url": "..."},
    "co_authors": [{"name": "...", "email": "..."}]
  },
  "reviews": [{"reviewer": {"login": "...", "profile_url": "..."}, "state": "approved", "submitted_at": "...", "url": "...", "comment": "..."}],
  "requested_reviewers": ["..."],
  "files": [{"path": "src/relic/cli.py"}]
}
```

An issue episode body is smaller: an `issue` object with identifier, title,
description, state, url, assignees, labels, and parent (the parent issue identifier for a sub-issue, else null). The same shape covers GitHub and
Linear issues, since both map to the `IssueRec` record.

Each `EpisodeSpec` also carries a `name` (for example `PR buildrelic/relic-core#12`),
a `source_description` (`github pull request`, `github issue`, `linear issue`), a
`reference_time` (the merge or creation timestamp, parsed tz-aware), and the
`group_id` (the slugified repo). Bodies are clipped to 2000 characters and reviews are
capped per PR (bots dropped) to bound extraction cost
([`_clip`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-ingest/src/relic/ingest/mappers.py), `_select_reviews`).

> **Raw payload shapes are not uniform.** The episode JSON above is stable, but the
> verbatim source payloads under `data/raw/` are not. PRs are now fetched over GraphQL,
> so their raw payload is the **camelCase GraphQL node** (`createdAt`, `mergedAt`,
> `author { login }`); issues still come over REST, so theirs is the **snake_case REST**
> body (`created_at`, `html_url`), as are PRs from any pre-GraphQL ingest. A future pass
> that re-maps from `data/raw/` must branch on shape (or on `source`) rather than assume
> one casing.

## Layer 3: SkillIR

[`skill_ir.py`](https://github.com/buildrelic/relic-core/blob/main/packages/relic-core/src/relic/ontology/skill_ir.py) is the contract for a compiled
skill. It is ported verbatim from the system-design doc and must not drift,
because the same schema becomes the MCP tool contract and the rendered
`SKILL.md`.

```
SkillIR
├── skill_id, semver                 # identity + versioning
├── title, description               # human-facing (description is the routing rule)
├── scope                            # org | team | repo | project | person
├── inputs:  dict[str, FieldSpec]    # typed contract, also the MCP input schema
├── outputs: dict[str, FieldSpec]
├── preconditions: list[str]         # execution guardrails
├── safety_checks: list[str]
├── citations: list[Citation]        # grounding: label, url, source_type
├── status                           # draft | verified | deprecated
├── owner
├── last_verified_at
└── tags: list[str]
```

`FieldSpec` is a single typed field (type, description, required, default).
`Citation` is a grounded source (label, url, and a `source_type` of pr, issue,
comment, doc, or message). An example lives at
[`examples/skills/pr-review-routing.json`](https://github.com/buildrelic/relic-core/blob/main/examples/skills/pr-review-routing.json).

How `SkillIR` is stored, rendered, served, and emitted is covered in
[skills.md](/skills).
