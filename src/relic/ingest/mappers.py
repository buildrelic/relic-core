"""Deterministic mappers: GitHub/Linear payloads -> structured episodes.

Pure and unit-testable: no network, no LLM. These hold the record dataclasses the
connectors populate, plus transforms into the JSON episodes the loader feeds to
Graphiti. The episode JSON keys mirror the flat ``*Node`` attribute names in
``relic.graph.engram`` so the extractor maps them onto the typed entities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# --- Fetched records (populated by the connectors, consumed here) -----------


@dataclass(slots=True)
class FileChange:
    path: str
    additions: int
    deletions: int
    status: str


@dataclass(slots=True)
class ReviewRec:
    login: str | None
    profile_url: str | None
    state: str
    submitted_at: str | None
    url: str | None


@dataclass(slots=True)
class PullRequestRec:
    number: int
    title: str
    url: str
    state: str
    author_login: str | None
    author_url: str | None
    created_at: str
    merged_at: str | None
    reviews: list[ReviewRec] = field(default_factory=list)
    requested_reviewers: list[str] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class IssueRec:
    source: str  # "github" | "linear"
    identifier: str
    title: str
    url: str
    state: str
    assignees: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    created_at: str | None = None
    closed_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class RepoBundle:
    full_name: str
    url: str
    default_branch: str
    pull_requests: list[PullRequestRec] = field(default_factory=list)
    issues: list[IssueRec] = field(default_factory=list)


# --- Episode spec (consumed by graph.load) ----------------------------------


@dataclass(slots=True)
class EpisodeSpec:
    name: str
    body: str
    source_description: str
    reference_time: datetime
    group_id: str


# --- Pure transforms ---------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def repo_group_id(full_name: str) -> str:
    """Slugify owner/name to a Graphiti-legal group_id (matches ^[A-Za-z0-9_-]+$)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", full_name.replace("/", "__"))


def _parse_aware(value: str) -> datetime:
    """Parse an ISO 8601 timestamp to a tz-aware datetime (assume UTC if naive)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def pr_to_episode(pr: PullRequestRec, repo: RepoBundle) -> EpisodeSpec:
    """Map a merged PR to a JSON episode whose keys mirror the entity attributes."""
    body = {
        "repo": {"full_name": repo.full_name, "url": repo.url},
        "pull_request": {
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "state": pr.state,
            "created_at": pr.created_at,
            "merged_at": pr.merged_at,
            "author": {"login": pr.author_login, "profile_url": pr.author_url},
        },
        "reviews": [
            {
                "login": r.login,
                "profile_url": r.profile_url,
                "state": r.state,
                "submitted_at": r.submitted_at,
                "url": r.url,
            }
            for r in pr.reviews
        ],
        "requested_reviewers": pr.requested_reviewers,
        "files": [
            {"path": f.path, "additions": f.additions, "deletions": f.deletions, "status": f.status}
            for f in pr.files
        ],
    }
    return EpisodeSpec(
        name=f"PR {repo.full_name}#{pr.number}",
        body=json.dumps(body, default=str),
        source_description="github pull request",
        reference_time=_parse_aware(pr.merged_at or pr.created_at),
        group_id=repo_group_id(repo.full_name),
    )


def issue_to_episode(issue: IssueRec, repo: RepoBundle) -> EpisodeSpec:
    """Map an issue (GitHub or Linear) to a JSON episode."""
    body = {
        "issue": {
            "identifier": issue.identifier,
            "title": issue.title,
            "state": issue.state,
            "url": issue.url,
            "assignees": issue.assignees,
            "labels": issue.labels,
        }
    }
    ref = issue.created_at or issue.closed_at
    return EpisodeSpec(
        name=f"Issue {issue.identifier}",
        body=json.dumps(body, default=str),
        source_description=f"{issue.source} issue",
        reference_time=_parse_aware(ref) if ref else _EPOCH,
        group_id=repo_group_id(repo.full_name),
    )
