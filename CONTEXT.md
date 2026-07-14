# Relic Domain Language

Relic ingests engineering history into the engram store (Postgres rows plus a
Firestore document mirror, see ADR-0007) and serves grounded recall. This
glossary fixes the terms the engram ontology uses, so the memory speaks one
language across every source. It is a glossary, not a spec: definitions only,
no implementation detail. Entries retired by the store pivot are kept with a
status note, because old ADRs and commit history still use them.

## Sources and artifact types

A **source** is a connector. An **artifact type** is the shape a source's records
collapse onto in the graph. Several sources can map to one artifact type.

The engram ontology covers six planned-essential sources mapping to five artifact
types:

| Source | Artifact type |
| -- | -- |
| GitHub (PRs) | Pull request |
| GitHub, Linear | Issue |
| Notion, Granola | Document |
| Slack | Conversation |
| Claude Code (coding sessions) | AgentSession |

**Pull request**:
A proposed, reviewable code change with its reviews, touched files, and outcome.
_Avoid_: PR (in prose), merge request, change.

**Issue**:
A tracked unit of work: a ticket, bug, or feature. From Linear and GitHub.
_Avoid_: ticket, task, story, card.

**Document**:
A durable, human-authored written artifact: a Notion wiki/reference page or a
Granola meeting summary. A `doc_type` separates the shapes (`wiki`,
`meeting_notes`, `adr`, ...). From Notion and Granola (repo markdown later).
_Avoid_: doc (as the type name), page, article, wiki, meeting note.

**Conversation**:
A bounded back-and-forth thread segment of a few turns, not a whole transcript.
From Slack. A coding agent's work-session is an AgentSession, not a Conversation.
_Avoid_: chat, thread, message log, meeting, transcript, session.

**AgentSession**:
An agent work-session: a distilled, clipped transcript of a coding agent (Claude
Code) doing work in a repo, captured back into the engram as the write half of
context-as-a-service. Unlike a Conversation (human discourse), an AgentSession *did
work*: it links to the pull requests, files, and issues it touched
(`TOUCHED`/`REFERENCES`) and is a prime source of `Decision` and `Action item`
nodes. From Claude Code (other coding agents later).
_Avoid_: conversation, transcript (the raw form), chat, session log.

## Subject

**Subject**:
The central artifact entity an episode is extracted from — the Pull request or Issue every
other entity in that episode's body relates to (its author, reviewers, files, labels,
linked issues, repo). One Subject per episode. It is what edges hang off; distinct from a
Repo (which is the *target* of `IN_REPO`, not the episode's Subject — hence Repo, not the
Subject, keeps the "anchor node" wording). The LLM extractor materializes the Subject node
unreliably, so relic writes it — and the structural edges the extractor drops —
deterministically from the body, behind the Memory seam.
_Avoid_: anchor (reserved for Repo), root, head, principal.

## Entities

**Person**:
A human who acts in the engineering history: author, reviewer, assignee,
participant, attendee. One node per human across every source, joined by explicit
per-source handles (`github_login`, `slack_id`, `linear_id`, `email`) first and
name similarity second.
_Avoid_: user, account, member, contributor, author (as a type).

**File**:
A repository-relative path touched by a pull request. A node, so ownership ("who
changes path X") is traversable (`TOUCHES_PATH`).
_Avoid_: path, blob, source file.

**Review**:
A completed code review of a pull request, modeled as the `REVIEWED` edge
(Person → PullRequest) carrying `state`/`submitted_at`/`comment`, not a node.
Distinct from a review *request* (`REQUESTED_REVIEW`).
_Avoid_: Review node, approval (which is one review `state`).

**Repo**:
A source-code repository: an anchor node carrying `full_name`/`url`/
`default_branch`. Artifacts link to it via `IN_REPO`. A Repo is identity and
provenance only — it is *not* the access boundary (that is a Zone) and an
artifact's Repo is orthogonal to the Zone it lives in.
_Avoid_: repository (in code), project (a Linear Project is a different thing),
zone (a Repo is not an access unit).

**Project**:
A Linear project: a base node grouping issues toward a goal. Issues link via
`IN_PROJECT`. Base now (`name`/`url`/`id`), enriched later.
_Avoid_: repo, initiative, epic, milestone.

**Decision**:
A durable choice the team made, extracted from a Document or Conversation ("use
FalkorDB for group_id partitioning"). A node, so a pull request can later
implement it and a newer decision can supersede it. Provisional: prose-extracted,
quality unproven.
_Avoid_: choice, conclusion, fact, resolution.

**Action item**:
A follow-up task with an owner, extracted from a Document or Conversation. A node
assigned to a Person. Distinct from an Issue (the *tracked* unit of work).
Provisional: prose-extracted.
_Avoid_: todo, task, ticket.

## Tenancy and access

The unit of physical isolation. Repos, teams, and topical neighborhoods all live
inside one tenant graph and may link to each other; access is governed *within*
that graph, not by physical separation.

**Tenant**:
*Retired with the graph (ADR-0007): tenancy is now the `workspace_id` column,
one workspace per Clerk scope or `RELIC_WORKSPACE`.*
An organization. One FalkorDB database per tenant — the hard isolation boundary
that nothing crosses. A Tenant owns many Zones.
_Avoid_: org, account, customer, workspace, group.

**Zone**:
*Retired with the graph (ADR-0007): the access unit is the workspace, and the
old `group_id` slug lives on as the per-source `scope` column.*
A governed, deliberately-bounded region of a tenant graph that access is granted
against. Every artifact lives in exactly one Zone. A Repo's data, a sprint team's
work, the company-wide knowledge base, and a restricted "classified" region are
each a Zone (or kind of Zone). Distinct from a Community: a Zone is *deliberate
and stable* (a human owns its boundary), a Community is *emergent and recomputed*.
_Avoid_: scope, group, community, workspace, enclave, compartment.

**Community**:
*Retired with the graph (ADR-0007): there is no community detection over the
relational store.*
An emergent topical cluster of related nodes, detected automatically (Graphiti's
`build_communities`, label propagation) and recomputed as the graph grows. About
*what a neighborhood is about*, never about who may see it. Access never rides on
a Community.
_Avoid_: zone, cluster, neighborhood, group, topic.

**Zoned vs global**:
The ontology splits in two for access. **Zoned**: artifacts (Pull request, Issue,
Document, Conversation, AgentSession, Decision, Action item) and every fact (edge)
carry exactly one Zone — access is enforced on these. **Global**: the connective
identity spine (Person, Repo, Label, File) is tenant-wide and Zone-exempt — any
member can see these nodes, but traversing *from* them into a Zoned fact still
dead-ends at Zone boundaries. (Consequence: a person's or repo's *existence* is
tenant-public; only their *activity* is gated.)

## Graph soundness

*This section is retired with the graph (ADR-0007). In the engram store the
guarantees are structural: every read binds to a workspace, and supersession is
the upsert on `(workspace_id, name)`. Kept because ADR-0005 and ADR-0006 use
these terms.*

The guarantees a reader of the graph can rely on. They make the access boundary a
property of the *data and the seam*, not of a filter the caller must remember to
pass — so the graph is safe to read past recall (see
[ADR-0006](/adr/0006-sound-graph-for-consumers)).

**Zone integrity**:
The invariant that every Zoned node and edge carries exactly one Zone, and no
global node carries any. A graph satisfying it is **well-tagged**. Integrity is the
precondition for the access boundary to mean anything: an untagged Zoned fact is
either lost (filtered everywhere) or leaked (filtered nowhere).
_Avoid_: tagged (alone — say *well-tagged*), valid (overloaded with fact validity).

**Scoped read** / **Unscoped read**:
A **scoped read** is a read made on behalf of a principal: it returns only the
connected subgraph that principal's Zone-set unlocks, and out-of-Zone nodes read as
nonexistent (no error, no redaction marker). An **unscoped read** sees the whole
tenant graph and is reserved for trusted, non-principal callers (ingest, eval,
admin); it is never reachable from a serving surface.
_Avoid_: filtered read, authorized read, public read.

**Current fact** / **Superseded fact**:
Facts are bi-temporal. A **current fact** holds as of now; a **superseded fact** was
true but has been invalidated by a later one (e.g. an owner changed). Recall returns
current facts by default — a superseded fact surfaced as if current is a grounding
error, not a recall.
_Avoid_: stale (imprecise), old, expired (a specific Graphiti field), invalid (Zone
integrity term).
