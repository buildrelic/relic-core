"""Deterministic **Subject** writes: parse a structured episode body into the central
artifact entity (the Subject -- the PullRequest or Issue) plus the structural edges the
LLM extractor reliably drops, as a pure, Graphiti-free plan the adapter executes.

Why deterministic: measured during REL-94, the LLM materializes the Subject node only ~1
in 3 ingests; when it is missing its edges collapse onto ``Repo`` or vanish. The body
holds this data exactly, so we write it ourselves (ADR-0002). Scope is the *dropped*
edges -- ``IN_REPO``, ``CLOSES`` (+ the linked ``Issue``), ``REQUESTED_REVIEW`` -- which
are additive (the LLM almost never makes them), so they do not duplicate the extractor's
output. The edges the LLM does make (``AUTHORED``/``REVIEWED``/``TOUCHES_PATH``) are left
to it and measured for residual drift before being touched.

This module is pure: it returns a ``SubjectPlan`` of plain values. ``relic.graph.memory``
turns that into Graphiti ``EntityNode`` / ``EntityEdge`` writes. Keeping it Graphiti-free
makes the body->graph mapping unit-testable without FalkorDB.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from relic.contracts.episode_body import IssueEpisodeBody, PrEpisodeBody

if TYPE_CHECKING:
    from relic.contracts import EpisodeSpec

# Stable namespace for deterministic node/edge uuids (uuid5). Fixed forever: changing it
# re-keys every deterministic write and forks the graph on the next ingest.
_NAMESPACE = uuid.UUID("5f2e9b7a-0c14-4e3d-9a6b-7c8d1e2f3a4b")


def det_uuid(group_id: str, *parts: str) -> str:
    """A stable uuid5 for a node/edge, so re-ingest MERGEs in place instead of forking.

    Keyed on the repo-scoped ``group_id`` plus the caller's identity parts (a node's
    label+name, or an edge's relation+endpoint uuids). The unit separator avoids
    collisions between, say, ``("a", "bc")`` and ``("ab", "c")``.
    """
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join((group_id, *parts))))


@dataclass(frozen=True, slots=True)
class PlannedNode:
    """A node to upsert: its ontology label and its identity ``name`` within the group."""

    label: str  # ontology label, e.g. "PullRequest" (Graphiti adds the "Entity" marker)
    name: str  # identity within (group_id, label); also the node's display name
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedEdge:
    """A structural edge to write between the Subject and an endpoint."""

    relation: str  # edge name, e.g. "IN_REPO"
    direction: str  # "out" = Subject->target, "in" = target->Subject
    target: PlannedNode
    fact: str  # human-readable fact string (also what the edge embedding is built from)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubjectPlan:
    """The deterministic write plan for one episode: the Subject node and its edges."""

    subject: PlannedNode
    edges: list[PlannedEdge]


def plan_subject_writes(spec: EpisodeSpec) -> SubjectPlan | None:
    """Parse ``spec.body`` into the Subject node + the dropped structural edges, or None.

    Returns ``None`` when the body has no PR/Issue Subject: another source_type, malformed
    JSON, or a body missing the required sections.
    """
    try:
        body = json.loads(spec.body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    source_type = body.get("source_type")
    if source_type == "pull_request":
        return _plan_pull_request(body)
    if source_type == "issue":
        return _plan_issue(body)
    return None


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop None-valued attributes so absent body fields never land as null properties."""
    return {k: v for k, v in attributes.items() if v is not None}


def _plan_pull_request(body: dict[str, Any]) -> SubjectPlan | None:
    try:
        pr_body = PrEpisodeBody.model_validate(body)
    except Exception:  # noqa: BLE001 - a malformed body yields no deterministic write
        return None
    repo = pr_body.repo
    pr = pr_body.pull_request
    ts = pr.timestamps
    ds = pr.diff_stats
    # The PR's stable identity within its repo; matches the "PR owner/repo#n" episode name.
    pr_name = f"{repo.full_name}#{pr.number}"

    # Attribute keys mirror schema.py PullRequestNode so deterministic and extracted nodes
    # describe the same shape (opened_at is the body's created_at; the rest flatten the
    # nested timestamps/diff_stats the extractor otherwise has to flatten itself).
    subject = PlannedNode(
        label="PullRequest",
        name=pr_name,
        attributes=_clean(
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state or None,
                "url": pr.url,
                "opened_at": pr.created_at or ts.created_at,
                "merged_at": pr.merged_at or ts.merged_at,
                "base_ref": pr.base_ref,
                "head_ref": pr.head_ref,
                "first_review_at": ts.first_review_at,
                "approved_at": ts.approved_at,
                "time_to_merge_hours": ts.time_to_merge_hours,
                "total_additions": ds.total_additions,
                "total_deletions": ds.total_deletions,
                "changed_files": ds.changed_files,
            }
        ),
    )

    repo_node = PlannedNode(
        label="Repo",
        name=repo.full_name,
        attributes=_clean(
            {"full_name": repo.full_name, "url": repo.url, "default_branch": repo.default_branch}
        ),
    )
    edges: list[PlannedEdge] = [
        PlannedEdge("IN_REPO", "out", repo_node, f"{pr_name} is in repo {repo.full_name}")
    ]
    # REL-94 escape hatches: exact body data the LLM measurably fumbles (see the
    # REL-94 PR for the measured coverage that justifies each field). These are edges
    # the extractor often *does* make, so the adapter skips a planned edge when an
    # equivalent one already exists rather than doubling the fact.
    if pr.author and pr.author.login:
        author = PlannedNode(
            label="Person",
            name=pr.author.login,
            attributes=_clean(
                {"github_login": pr.author.login, "profile_url": pr.author.profile_url}
            ),
        )
        edges.append(PlannedEdge("AUTHORED", "in", author, f"{pr.author.login} authored {pr_name}"))
    for f in pr_body.files:
        file_node = PlannedNode(label="File", name=f.path)
        edges.append(PlannedEdge("TOUCHES_PATH", "out", file_node, f"{pr_name} touches {f.path}"))
    for li in pr_body.linked_issues:
        issue_node = PlannedNode(
            label="Issue", name=li.identifier, attributes={"identifier": li.identifier}
        )
        edges.append(
            PlannedEdge(
                "CLOSES",
                "out",
                issue_node,
                f"{pr_name} {li.relation} {li.identifier}",
                attributes={"relation": li.relation},
            )
        )
    for rr in pr_body.requested_reviewers:
        # Person requests only; team review requests are a distinct, first-class procedure.
        if rr.kind != "user" or not rr.name:
            continue
        person = PlannedNode(
            label="Person",
            name=rr.name,
            attributes=_clean({"github_login": rr.name, "profile_url": rr.profile_url}),
        )
        edges.append(
            PlannedEdge(
                "REQUESTED_REVIEW", "in", person, f"{rr.name} was requested to review {pr_name}"
            )
        )
    return SubjectPlan(subject=subject, edges=edges)


def _plan_issue(body: dict[str, Any]) -> SubjectPlan | None:
    try:
        issue_body = IssueEpisodeBody.model_validate(body)
    except Exception:  # noqa: BLE001 - a malformed body yields no deterministic write
        return None
    issue = issue_body.issue
    subject = PlannedNode(
        label="Issue",
        name=issue.identifier,
        attributes=_clean(
            {
                "identifier": issue.identifier,
                "title": issue.title,
                "state": issue.state or None,
                "url": issue.url,
                "opened_at": issue.created_at,
                "closed_at": issue.closed_at,
                "priority": issue.priority,
                "cycle": issue.cycle,
            }
        ),
    )
    edges: list[PlannedEdge] = []
    if issue.parent:
        # PARENT_OF runs parent -> child; the Subject is the child, so the edge points in.
        parent = PlannedNode(
            label="Issue", name=issue.parent, attributes={"identifier": issue.parent}
        )
        edges.append(
            PlannedEdge(
                "PARENT_OF", "in", parent, f"{issue.parent} is parent of {issue.identifier}"
            )
        )
    return SubjectPlan(subject=subject, edges=edges)
