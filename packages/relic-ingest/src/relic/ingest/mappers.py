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

from relic.contracts import EpisodeSpec

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
    body: str | None = None
    # Set by the fetcher from GraphQL's __typename == "Bot". The login alone is not
    # enough: GraphQL returns bare bot logins (no "[bot]" suffix), unlike REST.
    is_bot: bool = False


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
    body: str | None = None
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
    body: str | None = None
    assignees: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    created_at: str | None = None
    closed_at: str | None = None
    parent: str | None = None  # parent issue identifier, if this is a sub-issue
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class RepoBundle:
    full_name: str
    url: str
    default_branch: str
    pull_requests: list[PullRequestRec] = field(default_factory=list)
    issues: list[IssueRec] = field(default_factory=list)


# --- Pure transforms ---------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def repo_group_id(full_name: str) -> str:
    """Slugify owner/name to a Graphiti-legal group_id (matches ^[A-Za-z0-9_-]+$)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", full_name.replace("/", "__"))


def _parse_aware(value: str) -> datetime:
    """Parse an ISO 8601 timestamp to a tz-aware datetime (assume UTC if naive)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_MAX_DESC_CHARS = 2000
_MAX_REVIEWS_PER_PR = 10
# States that carry a decision; preferred over plain COMMENTED when capping reviews.
_DECIDED_STATES = {"APPROVED", "CHANGES_REQUESTED"}

_FENCE_RE = re.compile(r"`{3,}")
_ZWSP = chr(0x200B)  # zero-width space, woven between backticks to break a fence run
_COAUTHOR_RE = re.compile(
    r"^[ \t]*Co-authored-by:[ \t]*(?P<name>[^<\n]+?)[ \t]*(?:<(?P<email>[^>\n]+)>)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _defuse_fences(text: str) -> str:
    """Break runs of 3+ backticks so a body can't terminate a Markdown code fence.

    Episode bodies are handed to the extractor inside a fenced block, so a literal
    ``` from a quoted commit message or code snippet would close that fence early
    and corrupt extraction. A zero-width space between the backticks keeps the text
    visually identical while removing the three-in-a-row sequence.
    """
    return _FENCE_RE.sub(lambda m: _ZWSP.join(m.group(0)), text)


def _clip(text: str | None) -> str | None:
    """Trim a description to keep episodes (and extraction cost) bounded.

    The PR body is the substance the extractor needs, but an unbounded body would
    blow up the per-episode token cost. Cap it, defuse code fences, and drop empty
    bodies to null.
    """
    if not text or not text.strip():
        return None
    trimmed = text.strip()
    if len(trimmed) > _MAX_DESC_CHARS:
        trimmed = trimmed[:_MAX_DESC_CHARS] + "..."
    return _defuse_fences(trimmed)


def _parse_coauthors(body: str | None) -> list[dict[str, str]]:
    """Pull ``Co-authored-by:`` trailers out of a PR body.

    A squash-merge folds each commit's trailer into the PR body, so one PR can
    credit several people beyond the opener. Surfacing them lets the extractor link
    every contributor to the PR (and feeds cross-source person unification). Parsed
    from the raw body, before clipping, since trailers sit at the very end.

    Partial trailers degrade gracefully: a name-only or email-only trailer yields
    an entry with just that field. One limitation, accepted on purpose: the whole
    body is scanned, so a trailer quoted inside a code block (someone documenting
    the format) is credited as a real co-author.
    """
    if not body:
        return []
    # GitHub returns bodies with CRLF; normalize so the `$`-anchored trailer regex
    # matches (a stray \r before \n otherwise fails the line and drops the author).
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for match in _COAUTHOR_RE.finditer(body):
        name = (match.group("name") or "").strip()
        email = (match.group("email") or "").strip()
        if not name and not email:
            continue
        key = (name.lower(), email.lower())
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, str] = {}
        if name:
            entry["name"] = name
        if email:
            entry["email"] = email
        out.append(entry)
    return out


def _is_bot(login: str | None) -> bool:
    """True for a REST-style bot login, whose name ends in ``[bot]``."""
    return bool(login) and login.endswith("[bot]")  # type: ignore[union-attr]


def _review_is_bot(review: ReviewRec) -> bool:
    """True if a review came from a bot, by either signal.

    GraphQL sets ``is_bot`` from ``__typename`` (bare logins, the production path);
    ``_is_bot`` catches the REST-style ``[bot]`` suffix for any legacy/REST-shaped data.
    """
    return review.is_bot or _is_bot(review.login)


def _select_reviews(reviews: list[ReviewRec]) -> list[ReviewRec]:
    """Drop bot reviews and cap to ``_MAX_REVIEWS_PER_PR``, preserving order.

    Bot reviews (CI, dependabot) add tokens without informing reviewer-routing, so
    they are dropped. When a PR has more human reviews than the cap, the most
    informative are kept — decided states (approve / changes-requested) over plain
    comments, then most recent — but the survivors are emitted in their original
    order so the episode body stays stable and chronological.
    """
    human = [(i, r) for i, r in enumerate(reviews) if not _review_is_bot(r)]
    if len(human) <= _MAX_REVIEWS_PER_PR:
        return [r for _, r in human]
    ranked = sorted(
        human,
        key=lambda ir: (ir[1].state in _DECIDED_STATES, ir[1].submitted_at or ""),
        reverse=True,
    )
    keep = {i for i, _ in ranked[:_MAX_REVIEWS_PER_PR]}
    return [r for i, r in human if i in keep]


def pr_to_episode(pr: PullRequestRec, repo: RepoBundle) -> EpisodeSpec:
    """Map a PR (any state) to a JSON episode whose keys mirror the entity attributes."""
    # A ghost/bot opener has no login: omit the author rather than emit an empty
    # Person the extractor would have to invent a name for.
    author = (
        {"login": pr.author_login, "profile_url": pr.author_url}
        if pr.author_login or pr.author_url
        else None
    )
    body = {
        "repo": {"full_name": repo.full_name, "url": repo.url},
        "pull_request": {
            "number": pr.number,
            "title": _defuse_fences(pr.title),
            "description": _clip(pr.body),
            "url": pr.url,
            "state": pr.state,
            "created_at": pr.created_at,
            "merged_at": pr.merged_at,
            "author": author,
            "co_authors": _parse_coauthors(pr.body),
        },
        "reviews": [
            {
                # Same treatment as the PR author: a ghost/deleted reviewer gets
                # reviewer: null, not an identity-less Person. The review itself
                # (state, comment) still carries signal, so it is kept.
                "reviewer": (
                    {"login": r.login, "profile_url": r.profile_url}
                    if r.login or r.profile_url
                    else None
                ),
                "state": r.state,
                "submitted_at": r.submitted_at,
                "url": r.url,
                "comment": _clip(r.body),
            }
            for r in _select_reviews(pr.reviews)
        ],
        "requested_reviewers": pr.requested_reviewers,
        "files": [{"path": f.path} for f in pr.files],
    }
    # A non-merged PR (draft, open, closed-without-merge) has no merged_at; anchor it
    # to when it was opened, and fall back to the epoch if even that is missing.
    ref = pr.merged_at or pr.created_at
    return EpisodeSpec(
        name=f"PR {repo.full_name}#{pr.number}",
        body=json.dumps(body, default=str),
        source_description="github pull request",
        reference_time=_parse_aware(ref) if ref else _EPOCH,
        group_id=repo_group_id(repo.full_name),
    )


def issue_to_episode(issue: IssueRec, repo: RepoBundle) -> EpisodeSpec:
    """Map an issue (GitHub or Linear) to a JSON episode."""
    body = {
        "issue": {
            "identifier": issue.identifier,
            "title": _defuse_fences(issue.title),
            "description": _clip(issue.body),
            "state": issue.state,
            "url": issue.url,
            "assignees": issue.assignees,
            "labels": issue.labels,
            "parent": issue.parent,
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
