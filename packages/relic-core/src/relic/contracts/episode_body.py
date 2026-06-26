"""The typed, versioned episode body: the shared schema across the ingest/graph seam.

``EpisodeSpec.body`` is a JSON string on the wire (Graphiti requires a str), but
its shape is no longer implicit. Ingest builds one of these models and serializes
it; the graph side (extraction, the detector, the compiler) reads it back against
this same contract. One type, both sides, so the seam cannot drift silently.

Two hard invariants the shape must keep, both load-bearing downstream:

- ``pull_request.url`` / ``issue.url`` are the citation anchors. ``recall._extract_url``
  reads exactly those keys; renaming them degrades every skill citation to a bare
  uuid. Do not move the ``url`` out of those sections.
- ``schema_version`` lives in the body (and the envelope), never in the episode
  ``name``. The name is the checkpoint dedup key; a version embedded in it would
  re-ingest and fork the whole graph.

The keys mirror the flat ``*Node`` attribute names the extractor expects (see
``relic.graph.schema`` and ``docs/data-model.mdx``), so values map onto typed
entities. Fields the connectors do not populate yet (CI checks, Linear cycle and
state history) are declared optional and default empty, so the schema is the full
target while capture catches up wave by wave.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Bump when the body shape changes in a way the detector must branch on. Lives in
# the body, never in the episode name (which is the dedup key).
SCHEMA_VERSION = 2


# --- Shared leaf models -----------------------------------------------------


class RepoRef(BaseModel):
    full_name: str
    url: str
    default_branch: str | None = None


class PersonRef(BaseModel):
    """An author or reviewer. Null on a ghost/deleted account, never identity-less."""

    login: str | None = None
    profile_url: str | None = None


class RequestedReviewer(BaseModel):
    """A requested (not necessarily completed) review, the routing signal.

    ``kind`` separates a person request from a team request: team routing is a
    distinct, first-class procedure (SkillIR ``scope: team``).
    """

    name: str | None = None
    kind: Literal["user", "team"] = "user"
    profile_url: str | None = None


class CoAuthor(BaseModel):
    name: str | None = None
    email: str | None = None


class PrTimestamps(BaseModel):
    """Per-event times derived in-mapper from the reviews list and PR times.

    Stable because PRs are ingested terminally (merged or closed). No extra fetch:
    every value comes from data already on the record.
    """

    created_at: str | None = None
    first_review_at: str | None = None
    approved_at: str | None = None
    merged_at: str | None = None
    time_to_merge_hours: float | None = None


class DiffStats(BaseModel):
    total_additions: int | None = None
    total_deletions: int | None = None
    changed_files: int | None = None


class ReviewEntry(BaseModel):
    reviewer: PersonRef | None = None
    state: str = ""
    submitted_at: str | None = None
    url: str | None = None
    comment: str | None = None


class FileEntry(BaseModel):
    path: str
    additions: int | None = None
    deletions: int | None = None


class CommitEntry(BaseModel):
    sha: str | None = None
    message: str | None = None
    author_login: str | None = None


class CheckEntry(BaseModel):
    """A CI/check-run outcome. Reserved: populated in a later wave."""

    name: str | None = None
    conclusion: str | None = None
    completed_at: str | None = None


class LinkedIssue(BaseModel):
    identifier: str
    relation: Literal["closes", "resolves", "relates"] = "relates"


# --- PR body ----------------------------------------------------------------


class PullRequestSection(BaseModel):
    number: int
    title: str
    description: str | None = None
    url: str  # citation anchor: recall._extract_url reads this. Do not move.
    state: str = ""
    created_at: str | None = None
    merged_at: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    merge_method: str | None = None  # reserved: not reliably exposed by the API yet
    labels: list[str] = Field(default_factory=list)
    author: PersonRef | None = None
    co_authors: list[CoAuthor] = Field(default_factory=list)
    timestamps: PrTimestamps = Field(default_factory=PrTimestamps)
    diff_stats: DiffStats = Field(default_factory=DiffStats)


class PrEpisodeBody(BaseModel):
    schema_version: int = SCHEMA_VERSION
    source_type: Literal["pull_request"] = "pull_request"
    context: str | None = None  # one-line extractor hint, deterministic, no LLM
    repo: RepoRef
    pull_request: PullRequestSection
    reviews: list[ReviewEntry] = Field(default_factory=list)
    requested_reviewers: list[RequestedReviewer] = Field(default_factory=list)
    files: list[FileEntry] = Field(default_factory=list)
    commits: list[CommitEntry] = Field(default_factory=list)
    checks: list[CheckEntry] = Field(default_factory=list)
    linked_issues: list[LinkedIssue] = Field(default_factory=list)


# --- Issue body -------------------------------------------------------------


class IssueStateChange(BaseModel):
    """A point in an issue's workflow history. Reserved: Linear, later wave."""

    state: str | None = None
    at: str | None = None


class IssueRelation(BaseModel):
    """A cross-issue link. Reserved: Linear, later wave."""

    identifier: str
    relation: str | None = None


class IssueSection(BaseModel):
    identifier: str
    title: str
    description: str | None = None
    state: str = ""
    url: str  # citation anchor: recall._extract_url reads this. Do not move.
    assignees: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    parent: str | None = None
    priority: str | None = None  # reserved: Linear
    estimate: float | None = None  # reserved: Linear
    cycle: str | None = None  # reserved: Linear
    created_at: str | None = None
    closed_at: str | None = None
    state_history: list[IssueStateChange] = Field(default_factory=list)  # reserved: Linear
    relations: list[IssueRelation] = Field(default_factory=list)  # reserved: Linear


class IssueEpisodeBody(BaseModel):
    schema_version: int = SCHEMA_VERSION
    source_type: Literal["issue"] = "issue"
    context: str | None = None
    issue: IssueSection


# --- Development source bodies (defined; capture lands wave by wave) ----------
#
# One model per development *artifact type*, not per connector: a GitHub PR and a
# GitLab MR are both `pull_request`; a postmortem doc and a PagerDuty incident are
# both `incident`. Sources collapse onto the artifact type, with source-specific
# fields optional and default-empty (the superset-with-optionals pattern).
#
# Each body carries a top-level `url` as its citation anchor, so
# `recall._extract_url` resolves a source URL for every fact with no per-source
# change (it already falls back to a top-level `url`). `schema_version` lives in the
# body, never in the episode name (the dedup key).
#
# Field choices are grounded in established methods, cited per model: Conventional
# Commits, Semantic Versioning + Keep a Changelog, DORA's four keys, Google SRE
# postmortems, MADR architecture decision records, and the Diataxis doc taxonomy.
# These are structure only: no connector or mapper is wired here.


# --- Shared leaf models for the development bodies --------------------------


class RelatedRef(BaseModel):
    """A cross-artifact link (a PR that fixed an incident, an issue an RFC resolves)."""

    identifier: str  # owner/repo#12, REL-42, a sha, or a URL
    relation: str | None = None  # e.g. fixed_by, caused_by, supersedes, relates


class ActionItem(BaseModel):
    """A follow-up with an owner, the unit a postmortem or retro produces."""

    description: str
    owner: PersonRef | None = None
    status: str | None = None
    url: str | None = None


class TimelineEvent(BaseModel):
    """A timestamped event in an incident timeline."""

    at: str | None = None
    text: str


class MessageEntry(BaseModel):
    """One message in a chat thread."""

    author: PersonRef | None = None
    text: str
    at: str | None = None


class CommentEntry(BaseModel):
    """One reply on a discussion/forum thread."""

    author: PersonRef | None = None
    body: str
    at: str | None = None


class ConventionalCommit(BaseModel):
    """Parsed Conventional Commits header, when the message follows the spec.

    Grounds commit-convention and release-automation skills. See
    https://www.conventionalcommits.org/en/v1.0.0/.
    """

    type: str | None = None  # feat, fix, chore, docs, refactor, perf, test, ci, build
    scope: str | None = None
    breaking: bool = False


class ChangelogEntry(BaseModel):
    """One Keep a Changelog line, bucketed by category. See https://keepachangelog.com."""

    category: Literal["added", "changed", "deprecated", "removed", "fixed", "security", "other"] = (
        "other"
    )
    text: str


class AdrFields(BaseModel):
    """MADR architecture-decision fields; populated when ``doc_type == 'adr'``.

    The decision record is the most direct SOP artifact a team produces. Fields
    follow MADR (https://adr.github.io/madr/): a status, the decision, its
    consequences, the drivers, and the options that were weighed.
    """

    status: str | None = None  # proposed | accepted | deprecated | superseded
    decision: str | None = None
    consequences: list[str] = Field(default_factory=list)
    decision_drivers: list[str] = Field(default_factory=list)
    considered_options: list[str] = Field(default_factory=list)
    superseded_by: str | None = None


# --- Commit ----------------------------------------------------------------


class CommitEpisodeBody(BaseModel):
    """A single commit, for file ownership and commit-convention signal.

    Distinct from the commits embedded in a PR body: this is the unit for
    direct-to-default-branch pushes and the authoritative "who changes path X"
    view. ``conventional`` parses the Conventional Commits header where present.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["commit"] = "commit"
    context: str | None = None
    url: str
    repo: RepoRef
    sha: str
    message: str | None = None
    author: PersonRef | None = None
    co_authors: list[CoAuthor] = Field(default_factory=list)
    committed_at: str | None = None
    conventional: ConventionalCommit | None = None
    files: list[FileEntry] = Field(default_factory=list)
    parents: list[str] = Field(default_factory=list)  # >1 parent marks a merge commit


# --- Release ---------------------------------------------------------------


class ReleaseEpisodeBody(BaseModel):
    """A tagged release, for release-process and changelog-convention skills.

    Grounded in Semantic Versioning (``semver``) and Keep a Changelog
    (``changelog``); ``previous_tag`` gives the diff range a lead-time/DORA
    deployment-frequency measure needs.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["release"] = "release"
    context: str | None = None
    url: str
    repo: RepoRef
    tag: str
    name: str | None = None
    semver: str | None = None
    prerelease: bool = False
    author: PersonRef | None = None
    published_at: str | None = None
    target_sha: str | None = None
    previous_tag: str | None = None
    notes: str | None = None
    changelog: list[ChangelogEntry] = Field(default_factory=list)


# --- CI / CD run -----------------------------------------------------------


class CiRunEpisodeBody(BaseModel):
    """A CI/CD pipeline or deployment run, for ship-process and gating skills.

    The signal behind DORA's four keys: deployment frequency and lead time
    (``is_deployment``, ``environment``, ``duration_seconds``) and change-failure
    rate (``conclusion``). ``checks`` reuses the per-check outcome shape.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["ci_run"] = "ci_run"
    context: str | None = None
    url: str
    repo: RepoRef
    name: str  # workflow / pipeline name
    event: str | None = None  # push | pull_request | schedule | deployment
    status: str = ""  # queued | in_progress | completed
    conclusion: str | None = None  # success | failure | cancelled | timed_out
    branch: str | None = None
    commit_sha: str | None = None
    is_deployment: bool = False
    environment: str | None = None  # production | staging, for a deployment run
    triggered_by: PersonRef | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    linked_pr: str | None = None
    checks: list[CheckEntry] = Field(default_factory=list)


# --- Incident --------------------------------------------------------------


class IncidentEpisodeBody(BaseModel):
    """An incident and its postmortem, the densest source of operational SOPs.

    Fields follow the Google SRE blameless-postmortem structure (summary, impact,
    root cause, timeline, action items) plus the DORA stability measures
    (``severity``, ``time_to_resolve_hours`` = MTTR). ``related`` links the fix
    PRs/commits so the incident-to-fix lifecycle is one subgraph.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["incident"] = "incident"
    context: str | None = None
    url: str
    title: str
    summary: str | None = None
    severity: str | None = None  # SEV1 | SEV2 | SEV3 (team scale)
    status: str | None = None  # active | mitigated | resolved
    impact: str | None = None
    root_cause: str | None = None
    detected_at: str | None = None
    resolved_at: str | None = None
    time_to_resolve_hours: float | None = None
    commander: PersonRef | None = None
    responders: list[PersonRef] = Field(default_factory=list)
    services_affected: list[str] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    related: list[RelatedRef] = Field(default_factory=list)


# --- Doc (ADR, runbook, design doc, README, CONTRIBUTING) ------------------


class DocEpisodeBody(BaseModel):
    """A document: the authoritative, human-authored SOP source.

    Covers repo-resident markdown (README, CONTRIBUTING, ADRs, runbooks) and
    external docs (Notion, Confluence). ``doc_type`` follows the Diataxis taxonomy
    plus the SOP-bearing kinds (adr, runbook, contributing); ``adr`` carries the
    MADR fields when ``doc_type == 'adr'``.

    Docs are mutable, but episodes are immutable, so a doc is captured as a
    versioned snapshot: ``version`` holds the immutable snapshot key (the git commit
    for a repo doc, the version id for Notion). It is the episode-name component, so
    each version is a distinct, non-forking episode and never freezes in place.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["doc"] = "doc"
    context: str | None = None
    url: str
    title: str
    doc_type: Literal[
        "adr",
        "runbook",
        "contributing",
        "readme",
        "design",
        "reference",
        "how_to",
        "explanation",
        "other",
    ] = "other"
    repo: RepoRef | None = None  # for a repo-resident doc
    path: str | None = None  # repo-relative path
    version: str | None = None  # immutable snapshot key (commit sha or doc version id)
    authors: list[PersonRef] = Field(default_factory=list)
    last_edited_at: str | None = None
    body: str | None = None  # content, clipped
    status: str | None = None  # lifecycle, esp. for an ADR
    adr: AdrFields | None = None
    links: list[RelatedRef] = Field(default_factory=list)


# --- Discussion (GitHub Discussions, RFC threads, Q&A) ---------------------


class DiscussionEpisodeBody(BaseModel):
    """A forum-style discussion or RFC: design rationale and Q&A resolutions.

    Modeled on GitHub Discussions (a ``category``, an ``answered`` flag, an accepted
    ``answer``). The accepted answer and resolved RFCs are durable decisions worth
    compiling into skills.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["discussion"] = "discussion"
    context: str | None = None
    url: str
    repo: RepoRef | None = None
    title: str
    category: str | None = None  # Q&A | Ideas | RFC | Announcements
    body: str | None = None
    author: PersonRef | None = None
    created_at: str | None = None
    answered: bool = False
    answer: str | None = None
    answer_author: PersonRef | None = None
    participants: list[PersonRef] = Field(default_factory=list)
    comments: list[CommentEntry] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    links: list[RelatedRef] = Field(default_factory=list)


# --- Conversation (Slack thread, meeting notes, Discord) -------------------


class ConversationEpisodeBody(BaseModel):
    """A threaded conversation: chat or a meeting, where decisions get made.

    One artifact type across mediums (``medium``): a Slack thread, a meeting
    transcript (Granola), a Discord thread. The signal worth mining is not the chat
    itself but the ``decisions`` and ``action_items`` extracted from it; ``messages``
    (chat) and ``transcript`` (meeting) are the evidence they cite.
    """

    schema_version: int = SCHEMA_VERSION
    source_type: Literal["conversation"] = "conversation"
    context: str | None = None
    url: str
    medium: Literal["slack", "meeting", "discord", "other"] = "other"
    title: str | None = None  # channel name or meeting title
    channel: str | None = None
    occurred_at: str | None = None
    participants: list[PersonRef] = Field(default_factory=list)
    messages: list[MessageEntry] = Field(default_factory=list)  # chat mediums
    transcript: str | None = None  # meeting mediums, clipped
    summary: str | None = None
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    links: list[RelatedRef] = Field(default_factory=list)
