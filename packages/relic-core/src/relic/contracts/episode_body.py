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
``relic.graph.engram`` and ``docs/data-model.mdx``), so values map onto typed
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
