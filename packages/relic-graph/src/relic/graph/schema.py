"""The flat, Graphiti-facing ontology types: the REL-93 schema the extractor fills.

Graphiti's custom entity/edge types must be flat scalar models (no nested objects) and
must avoid the reserved attribute names: uuid, name, group_id, labels, created_at,
summary, attributes, name_embedding. So these ``*Node`` / edge models are the
graph-facing view of the ontology (docs/adr/0001-engram-graph-ontology.mdx); the rich
models in ``relic.ontology`` stay the typed-API source of truth.

``ENTITY_TYPES`` / ``EDGE_TYPES`` / ``EDGE_TYPE_MAP`` are passed per ``add_episode()``
call by the Memory adapter (``relic.graph.memory``), not to the Graphiti constructor.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# --- Flat entity types (Graphiti-facing) ------------------------------------
#
# These implement the REL-93 ontology (docs/adr/0001-engram-graph-ontology.mdx)
# for the two captured artifact bodies, `pull_request` and `issue`. Only types a
# captured body actually feeds are registered: `Project`, `IN_PROJECT`, and the
# `Issue -> Repo` edge are deferred until the Linear wave puts `project`/`repo` in
# the issue body, so the extractor is never told to manufacture entities it has no
# data for (which would pollute the graph_stats baseline REL-94 measures against).
#
# Attribute names mirror the episode-body keys (relic.contracts.episode_body) so
# the LLM extractor can map body fields onto these scalars. Where the body nests or
# renames a field -- the PR's `opened_at` is the body's `pull_request.created_at`
# (renamed off Graphiti's reserved `created_at`); the derived timestamps and diff
# totals nest under `pull_request.timestamps` / `pull_request.diff_stats` -- the
# extractor must flatten and rename. That is a known fumble surface, measured
# before any deterministic write is added (REL-94 step 3).


class PersonNode(BaseModel):
    """A person: teammate or external collaborator.

    One node per human across every source, joined by the handle attributes first
    (`github_login`, `slack_id`, `linear_id`, `email`) and name similarity second.
    The PR/issue bodies carry only a `login`, so `github_login` is the live join key
    today; the other handles wait on the Notion/Slack/Linear capture waves. With no
    full name in the body, `name` resolves to the login for a GitHub-only ingest.
    """

    full_name: str | None = Field(None, description="Person's full name")
    preferred_name: str | None = Field(None, description="Preferred or short name")
    email: str | None = Field(None, description="Primary email address")
    github_login: str | None = Field(
        None, description="GitHub login of the PR author or reviewer, e.g. octocat"
    )
    slack_id: str | None = Field(None, description="Slack user id or handle")
    linear_id: str | None = Field(None, description="Linear user id or handle")
    profile_url: str | None = Field(None, description="Canonical profile URL")
    external_org: str | None = Field(None, description="External org if not internal")


class RepoNode(BaseModel):
    """A source code repository."""

    full_name: str | None = Field(None, description="owner/name slug, e.g. buildrelic/relic")
    url: str | None = Field(None, description="Canonical repository URL")
    default_branch: str | None = Field(None, description="Default branch name")


class PullRequestNode(BaseModel):
    """A pull request: a reviewable code change with its outcome.

    `opened_at` is the body's `pull_request.created_at`; the derived timestamps and
    diff totals are nested under `pull_request.timestamps` / `pull_request.diff_stats`
    in the body, so extraction must flatten and rename to fill them here.
    """

    number: int | None = Field(None, description="PR number within its repo")
    title: str | None = Field(None, description="PR title")
    state: str | None = Field(None, description="open, closed, or merged")
    url: str | None = Field(None, description="Canonical PR URL")
    opened_at: datetime | None = Field(
        None, description="When the PR was opened (the body's created_at)"
    )
    merged_at: datetime | None = Field(None, description="Merge timestamp if merged")
    base_ref: str | None = Field(None, description="Base branch the PR targets")
    head_ref: str | None = Field(None, description="Head branch the PR merges from")
    first_review_at: datetime | None = Field(
        None, description="When the first review was submitted"
    )
    approved_at: datetime | None = Field(None, description="When the PR was approved")
    time_to_merge_hours: float | None = Field(None, description="Hours from open to merge")
    total_additions: int | None = Field(None, description="Total lines added across all files")
    total_deletions: int | None = Field(None, description="Total lines deleted across all files")
    changed_files: int | None = Field(None, description="Count of files changed")


class IssueNode(BaseModel):
    """A tracked issue or ticket (GitHub or Linear)."""

    identifier: str | None = Field(None, description="External id, e.g. REL-42 or repo#123")
    title: str | None = Field(None, description="Issue title")
    state: str | None = Field(None, description="Workflow state")
    url: str | None = Field(None, description="Canonical issue URL")
    source: str | None = Field(None, description="github or linear")
    opened_at: datetime | None = Field(
        None, description="When the issue was opened (the body's created_at)"
    )
    closed_at: datetime | None = Field(None, description="When the issue was closed")
    priority: str | None = Field(None, description="Priority label (Linear)")
    cycle: str | None = Field(None, description="Cycle/sprint name (Linear)")


class LabelNode(BaseModel):
    """A label/tag on a PR or issue. The label text is the node `name`, so there are
    no custom attributes."""


class FileNode(BaseModel):
    """A repository-relative file path touched by a PR. The path is the node `name`,
    so there are no custom attributes."""


class AgentSessionNode(BaseModel):
    """A coding agent's work-session captured into the engram.

    Distinct from a Conversation: an AgentSession *did work*, so it links to the files it
    touched (`TOUCHED`) and the PR/issue it opened (`REFERENCES`), and is a prime prose
    source for `Decision`/`ActionItem`. The session title is the node `name`. See the
    AgentSession amendment in docs/adr/0001-engram-graph-ontology.mdx.
    """

    title: str | None = Field(None, description="AgentSession title (also the node name)")
    agent: str | None = Field(None, description="The coding agent, e.g. claude-code")
    repo: str | None = Field(None, description="owner/name slug of the repo worked in")
    cwd: str | None = Field(None, description="Working directory the session ran in")
    url: str | None = Field(None, description="AgentSession URL, e.g. session://<id>")
    started_at: datetime | None = Field(None, description="When the session started")
    ended_at: datetime | None = Field(None, description="When the session ended")


# --- Flat edge types (attributes only; Graphiti owns the endpoints) ---------


class Authored(BaseModel):
    """Person authored a pull request."""

    co_author: bool | None = Field(
        None, description="True if a co-author rather than the primary author"
    )


class Reviewed(BaseModel):
    """Person reviewed a pull request."""

    state: str | None = Field(None, description="approved, changes_requested, or commented")
    submitted_at: datetime | None = Field(None, description="When the review was submitted")
    comment: str | None = Field(None, description="Review summary comment, if any")


class RequestedReview(BaseModel):
    """Person was requested to review a pull request (the routing signal)."""


class TouchesPath(BaseModel):
    """A pull request touched a file path. The path is the target File node's name."""

    additions: int | None = Field(None, description="Lines added in this file")
    deletions: int | None = Field(None, description="Lines deleted in this file")


class HasLabel(BaseModel):
    """A pull request or issue carries a label."""


class InRepo(BaseModel):
    """A pull request belongs to a repository."""


class AssignedTo(BaseModel):
    """An issue is assigned to a person."""


class ParentOf(BaseModel):
    """An issue is the parent of another issue."""


class Closes(BaseModel):
    """A pull request closes, resolves, or relates to an issue."""

    relation: str | None = Field(None, description="closes, resolves, or relates")


class Touched(BaseModel):
    """A session edited a file path. The path is the target File node's name."""


class References(BaseModel):
    """A session references the pull request or issue it opened or named.

    Deterministic (Tier 1) from an AgentSession -- the link is known from git metadata --
    unlike the deferred Tier 3 `REFERENCES` from a Document/Conversation (prose-inferred).
    """


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": PersonNode,
    "Repo": RepoNode,
    "PullRequest": PullRequestNode,
    "Issue": IssueNode,
    "Label": LabelNode,
    "File": FileNode,
    "AgentSession": AgentSessionNode,
}

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "AUTHORED": Authored,
    "REVIEWED": Reviewed,
    "REQUESTED_REVIEW": RequestedReview,
    "TOUCHES_PATH": TouchesPath,
    "TOUCHED": Touched,
    "REFERENCES": References,
    "HAS_LABEL": HasLabel,
    "IN_REPO": InRepo,
    "ASSIGNED_TO": AssignedTo,
    "PARENT_OF": ParentOf,
    "CLOSES": Closes,
}

# Keys are (source_label, target_label) using ENTITY_TYPES keys. A coarse edge is
# discriminated by its signature (HAS_LABEL fans to PR and Issue; ASSIGNED_TO is
# Issue -> Person here, the same move REVIEWED makes on PR).
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "PullRequest"): ["AUTHORED", "REVIEWED", "REQUESTED_REVIEW"],
    ("PullRequest", "File"): ["TOUCHES_PATH"],
    ("PullRequest", "Label"): ["HAS_LABEL"],
    ("Issue", "Label"): ["HAS_LABEL"],
    ("PullRequest", "Repo"): ["IN_REPO"],
    ("Issue", "Person"): ["ASSIGNED_TO"],
    ("Issue", "Issue"): ["PARENT_OF"],
    ("PullRequest", "Issue"): ["CLOSES"],
    # AgentSession edges: AUTHORED is reused (Person -> AgentSession), the same coarse-edge move
    # as REVIEWED on a PR. TOUCHED/REFERENCES are deterministic from session metadata.
    ("Person", "AgentSession"): ["AUTHORED"],
    ("AgentSession", "File"): ["TOUCHED"],
    ("AgentSession", "PullRequest"): ["REFERENCES"],
    ("AgentSession", "Issue"): ["REFERENCES"],
}

# ADR-0005: the ontology splits in two for access control. The tenant-global identity
# spine (Person/Repo/Label/File) is Zone-exempt -- any tenant member may see these
# connective nodes, and entity resolution requires one node per human, which is
# impossible if identity were Zoned. Every other node, and every edge (fact), is Zoned:
# it carries exactly one Zone and access is enforced on it. The partition must stay
# exhaustive over ENTITY_TYPES (test_schema guards it); a new entity type lands in one
# tier on purpose, not by omission.
GLOBAL_ENTITY_TYPES: frozenset[str] = frozenset({"Person", "Repo", "Label", "File"})
ZONED_ENTITY_TYPES: frozenset[str] = frozenset(ENTITY_TYPES) - GLOBAL_ENTITY_TYPES


def is_global_type(label: str) -> bool:
    """True if ``label`` is part of the tenant-global identity spine (Zone-exempt)."""
    return label in GLOBAL_ENTITY_TYPES
