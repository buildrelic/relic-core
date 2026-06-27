# Relic Domain Language

Relic ingests engineering history into a temporal knowledge graph (Graphiti on
FalkorDB) and serves grounded recall. This glossary fixes the terms the engram
ontology uses, so the graph speaks one language across every source. It is a
glossary, not a spec: definitions only, no implementation detail.

## Sources and artifact types

A **source** is a connector. An **artifact type** is the shape a source's records
collapse onto in the graph. Several sources can map to one artifact type.

The engram ontology covers five planned-essential sources mapping to four artifact
types:

| Source | Artifact type |
| -- | -- |
| GitHub (PRs) | Pull request |
| GitHub, Linear | Issue |
| Notion, Granola | Document |
| Slack | Conversation |

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
From Slack.
_Avoid_: chat, thread, message log, meeting, transcript.

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
`default_branch`. Artifacts link to it via `IN_REPO`. (The `group_id` partition
also encodes repo membership; the edge makes it explicit and traversable.)
_Avoid_: repository (in code), project (a Linear Project is a different thing).

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
