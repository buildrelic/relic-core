"""Deterministic mappers: GitHub/Linear payloads -> structured episodes.

Pure and unit-testable: no network, no LLM. These hold the record dataclasses the
connectors populate, plus transforms into the JSON episodes the loader feeds to
Graphiti. The episode JSON keys mirror the flat ``*Node`` attribute names in
``relic.graph.engram`` so the extractor maps them onto the typed entities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from relic.contracts import EpisodeSpec
from relic.contracts.episode_body import (
    CoAuthor,
    CommitEntry,
    ConversationEpisodeBody,
    DiffStats,
    FileEntry,
    IssueEpisodeBody,
    IssueSection,
    LinkedIssue,
    PersonRef,
    PrEpisodeBody,
    PrTimestamps,
    PullRequestSection,
    RepoRef,
    RequestedReviewer,
    ReviewEntry,
)

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
class RequestedReviewerRec:
    """A requested review. ``kind`` separates a person request from a team request.

    The review-routing signal: who a PR was *sent to*, distinct from who reviewed.
    Team requests are first-class so team-scoped routing is detectable.
    """

    name: str | None
    kind: Literal["user", "team"] = "user"
    profile_url: str | None = None


@dataclass(slots=True)
class CommitRec:
    sha: str | None
    message: str | None
    author_login: str | None


@dataclass(slots=True)
class LinkedIssueRec:
    """An issue this PR closes/resolves/relates to, for the lifecycle subgraph."""

    identifier: str
    relation: Literal["closes", "resolves", "relates"] = "relates"


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
    requested_reviewers: list[RequestedReviewerRec] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    base_ref: str | None = None
    head_ref: str | None = None
    additions: int | None = None  # PR-level total, straight from the API node
    deletions: int | None = None
    changed_files: int | None = None
    commits: list[CommitRec] = field(default_factory=list)
    linked_issues: list[LinkedIssueRec] = field(default_factory=list)
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


@dataclass(slots=True)
class MeetingRec:
    """A normalized Granola meeting: notes + transcript + attendees, ready to map.

    Source-agnostic shape the connector populates. ``url`` is the permalink recall
    cites, ``owner_email`` is the per-user scope key (meetings have no repo), and
    ``summary``/``transcript`` are the conversation prose the extractor mines.
    """

    id: str
    title: str | None
    url: str
    owner_email: str | None
    participants: list[str] = field(default_factory=list)
    summary: str | None = None
    transcript: str | None = None
    occurred_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --- Pure transforms ---------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def repo_group_id(full_name: str) -> str:
    """Slugify owner/name to a Graphiti-legal group_id (matches ^[A-Za-z0-9_-]+$)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", full_name.replace("/", "__"))


def granola_group_id(owner_email: str | None) -> str:
    """Per-owner Granola scope key, e.g. ``granola__zidan_tryrelic_io``.

    Meetings have no repo, so the graph partition is the note owner's identity — a
    real key from the payload, not a faked repo (the seam ``issue_to_episode`` papers
    over for Linear). Reuses ``repo_group_id``'s slugify so the result is
    Graphiti-legal; a missing owner falls back to a single stable bucket.
    """
    return repo_group_id(f"granola/{owner_email or 'unknown'}")


def _parse_aware(value: str) -> datetime:
    """Parse an ISO 8601 timestamp to a tz-aware datetime (assume UTC if naive)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_MAX_DESC_CHARS = 2000
_MAX_REVIEW_COMMENT_CHARS = 1500
_MAX_COMMIT_MSG_CHARS = 1000
# Reviews are the routing signal, so keep more than the old cap of 10. The hard
# ceiling is the per-episode body budget below, not this count: Graphiti extraction
# degrades on very large bodies (entity conflation, edge hallucination), so the body
# is trimmed to a token-proxy budget after assembly rather than left unbounded.
_MAX_REVIEWS_PER_PR = 25
_MAX_COMMITS_PER_PR = 20
# ~4k tokens at ~4 chars/token: the size past which JSON-episode extraction quality
# falls off. _fit_budget trims the assembled body to stay under it.
_MAX_BODY_CHARS = 16000
# States that carry a decision; preferred over plain COMMENTED when capping reviews.
_DECIDED_STATES = {"APPROVED", "CHANGES_REQUESTED"}
# Meeting bodies: the AI summary is the mined signal (give it a generous cap); the
# transcript is the bulky evidence and is trimmed to whatever budget remains after
# assembly. Both stay under the shared _MAX_BODY_CHARS ceiling via _fit_conversation_budget.
_MAX_SUMMARY_CHARS = 4000
_MAX_TRANSCRIPT_CHARS = 14000

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


def _clip(text: str | None, limit: int = _MAX_DESC_CHARS) -> str | None:
    """Trim free text to keep episodes (and extraction cost) bounded.

    The substance the extractor needs (a PR body, a review comment, a commit
    message), capped so an unbounded blob does not blow up per-episode token cost.
    Defuse code fences and drop empty text to null. ``limit`` lets each field carry
    its own cap (a description is worth more characters than a review comment).
    """
    if not text or not text.strip():
        return None
    trimmed = text.strip()
    if len(trimmed) > limit:
        trimmed = trimmed[:limit] + "..."
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


def _select_commits(commits: list[CommitRec]) -> list[CommitRec]:
    """Keep the most recent commits, capped, preserving the API's order.

    Commits feed two signals: who is an active author (an input to the
    active-maintainer safety check) and the team's commit-message convention. The
    full list is unbounded on a long-lived PR, so cap it; the connector fetches the
    last N, so keeping the head of the list keeps the most recent.
    """
    return commits[:_MAX_COMMITS_PER_PR]


def _pr_timestamps(pr: PullRequestRec) -> PrTimestamps:
    """Derive per-event times from the reviews and PR times. No extra fetch.

    Stable because a PR is ingested only at a terminal state (merged or closed), so
    these never change after ingest. ISO 8601 strings from GitHub are UTC ("Z"), so
    a lexical min is a chronological min.
    """
    submitted = [r.submitted_at for r in pr.reviews if r.submitted_at]
    approved = [
        r.submitted_at for r in pr.reviews if r.submitted_at and r.state.upper() == "APPROVED"
    ]
    ttm: float | None = None
    if pr.merged_at and pr.created_at:
        try:
            delta = _parse_aware(pr.merged_at) - _parse_aware(pr.created_at)
            ttm = round(delta.total_seconds() / 3600.0, 2)
        except ValueError:
            ttm = None
    return PrTimestamps(
        created_at=pr.created_at or None,
        first_review_at=min(submitted) if submitted else None,
        approved_at=min(approved) if approved else None,
        merged_at=pr.merged_at,
        time_to_merge_hours=ttm,
    )


def _pr_context(pr: PullRequestRec, n_reviews: int, n_files: int) -> str:
    """A one-line, deterministic summary the extractor reads first. No LLM."""
    bits = [f"{pr.state or 'unknown'} PR"]
    if n_reviews:
        bits.append(f"{n_reviews} review{'s' if n_reviews != 1 else ''}")
    if n_files:
        bits.append(f"{n_files} file{'s' if n_files != 1 else ''}")
    if pr.linked_issues:
        n = len(pr.linked_issues)
        bits.append(f"{n} linked issue{'s' if n != 1 else ''}")
    return ", ".join(bits)


def _fit_budget(body: PrEpisodeBody) -> PrEpisodeBody:
    """Trim an assembled PR body to the token-proxy ceiling, deterministically.

    Large JSON episodes degrade extraction (entity conflation, edge hallucination),
    so the body is bounded after assembly. Trim in priority order: drop commits
    first (they aid commit-convention skills but not routing), then shed the oldest
    reviews. Reviews stay in chronological order, so slicing keeps the head.
    """
    if len(body.model_dump_json()) <= _MAX_BODY_CHARS:
        return body
    body.commits = []
    while len(body.model_dump_json()) > _MAX_BODY_CHARS and len(body.reviews) > 5:
        body.reviews = body.reviews[: max(5, len(body.reviews) // 2)]
    return body


def _person_ref(login: str | None, profile_url: str | None) -> PersonRef | None:
    """A PersonRef, or None for a ghost/deleted account (never identity-less)."""
    return PersonRef(login=login, profile_url=profile_url) if (login or profile_url) else None


def pr_to_episode(pr: PullRequestRec, repo: RepoBundle) -> EpisodeSpec:
    """Map a PR (any state) to a typed, versioned episode body for extraction.

    The body keys mirror the flat ``*Node`` attribute names the extractor expects, so
    values land on typed entities. Co-authors are parsed from the unclipped body (the
    trailers sit at the end), and every free-text field is fence-defused and clipped.
    The assembled body is then trimmed to the per-episode token budget.
    """
    reviews = _select_reviews(pr.reviews)
    files = [FileEntry(path=f.path, additions=f.additions, deletions=f.deletions) for f in pr.files]
    body = PrEpisodeBody(
        context=_pr_context(pr, len(reviews), len(files)),
        repo=RepoRef(full_name=repo.full_name, url=repo.url, default_branch=repo.default_branch),
        pull_request=PullRequestSection(
            number=pr.number,
            title=_defuse_fences(pr.title),
            description=_clip(pr.body),
            url=pr.url,
            state=pr.state,
            created_at=pr.created_at or None,
            merged_at=pr.merged_at,
            base_ref=pr.base_ref,
            head_ref=pr.head_ref,
            labels=pr.labels,
            # A ghost/bot opener has no login: author: null, not an empty Person.
            author=_person_ref(pr.author_login, pr.author_url),
            co_authors=[CoAuthor(**ca) for ca in _parse_coauthors(pr.body)],
            timestamps=_pr_timestamps(pr),
            diff_stats=DiffStats(
                total_additions=pr.additions,
                total_deletions=pr.deletions,
                changed_files=pr.changed_files,
            ),
        ),
        # A ghost/deleted reviewer surfaces reviewer: null; the review's state and
        # comment still carry routing signal, so the review is kept.
        reviews=[
            ReviewEntry(
                reviewer=_person_ref(r.login, r.profile_url),
                state=r.state,
                submitted_at=r.submitted_at,
                url=r.url,
                comment=_clip(r.body, _MAX_REVIEW_COMMENT_CHARS),
            )
            for r in reviews
        ],
        requested_reviewers=[
            RequestedReviewer(name=rr.name, kind=rr.kind, profile_url=rr.profile_url)
            for rr in pr.requested_reviewers
        ],
        files=files,
        commits=[
            CommitEntry(
                sha=c.sha,
                message=_clip(c.message, _MAX_COMMIT_MSG_CHARS),
                author_login=c.author_login,
            )
            for c in _select_commits(pr.commits)
        ],
        linked_issues=[
            LinkedIssue(identifier=li.identifier, relation=li.relation) for li in pr.linked_issues
        ],
    )
    # A non-merged PR (draft, open, closed-without-merge) has no merged_at; anchor it
    # to when it was opened, and fall back to the epoch if even that is missing.
    ref = pr.merged_at or pr.created_at
    return EpisodeSpec(
        name=f"PR {repo.full_name}#{pr.number}",
        body=_fit_budget(body).model_dump_json(),
        source_description="github pull request",
        reference_time=_parse_aware(ref) if ref else _EPOCH,
        group_id=repo_group_id(repo.full_name),
    )


def issue_to_episode(issue: IssueRec, repo: RepoBundle) -> EpisodeSpec:
    """Map an issue (GitHub or Linear) to a typed, versioned episode body."""
    n_labels = len(issue.labels)
    context = f"{issue.state or 'unknown'} issue"
    if n_labels:
        context += f", {n_labels} label{'s' if n_labels != 1 else ''}"
    body = IssueEpisodeBody(
        context=context,
        issue=IssueSection(
            identifier=issue.identifier,
            title=_defuse_fences(issue.title),
            description=_clip(issue.body),
            state=issue.state,
            url=issue.url,
            assignees=issue.assignees,
            labels=issue.labels,
            parent=issue.parent,
            created_at=issue.created_at,
            closed_at=issue.closed_at,
        ),
    )
    ref = issue.created_at or issue.closed_at
    return EpisodeSpec(
        name=f"Issue {issue.identifier}",
        body=body.model_dump_json(),
        source_description=f"{issue.source} issue",
        reference_time=_parse_aware(ref) if ref else _EPOCH,
        group_id=repo_group_id(repo.full_name),
    )


def _fit_conversation_budget(body: ConversationEpisodeBody) -> ConversationEpisodeBody:
    """Trim an assembled conversation body to the token-proxy ceiling, deterministically.

    The transcript is by far the largest field and the lowest signal-per-character (the
    summary, decisions, and action items are what the extractor mines), so it is shed
    first: halve it until the serialized body fits, then drop it entirely once it is too
    small to matter. The summary is already capped, so a transcript-less body is bounded.
    """
    while len(body.model_dump_json()) > _MAX_BODY_CHARS and body.transcript:
        if len(body.transcript) <= 500:
            body.transcript = None
            break
        body.transcript = body.transcript[: len(body.transcript) // 2] + "..."
    return body


def meeting_to_episode(meeting: MeetingRec) -> EpisodeSpec:
    """Map a Granola meeting to a Conversation episode (``medium="meeting"``).

    Rides the existing conversation path rather than inventing a Meeting type: the
    permalink lands in ``url`` (the anchor recall cites), attendees become
    ``participants``, and the AI summary + transcript are the prose the extractor
    mines. Every free-text field is fence-defused and clipped, and the assembled body
    is trimmed to the per-episode token budget. The scope (``group_id``) is per
    note-owner — a real identity, so it takes no faked ``RepoBundle``.
    """
    n_people = len(meeting.participants)
    context = "meeting"
    if n_people:
        context += f", {n_people} participant{'s' if n_people != 1 else ''}"
    body = ConversationEpisodeBody(
        context=context,
        url=meeting.url,
        medium="meeting",
        title=_defuse_fences(meeting.title) if meeting.title else None,
        occurred_at=meeting.occurred_at,
        participants=[PersonRef(login=name) for name in meeting.participants],
        transcript=_clip(meeting.transcript, _MAX_TRANSCRIPT_CHARS),
        summary=_clip(meeting.summary, _MAX_SUMMARY_CHARS),
    )
    # Anchor to when the meeting happened, falling back to when the note was created,
    # then the epoch if even that is missing.
    ref = meeting.occurred_at or meeting.created_at
    return EpisodeSpec(
        # The stable note id is the dedup key, so a re-presented meeting is skipped, not
        # re-extracted. Deliberately NO updated_at folded in: a version in the name forks
        # the graph (Graphiti mints a fresh Episodic uuid per add) instead of updating in
        # place. Re-ingesting an edited note (regenerated summary, late edits) needs
        # episode supersession at the loader/checkpoint layer, not a mapper rename.
        name=f"Meeting {meeting.id}",
        body=_fit_conversation_budget(body).model_dump_json(),
        source_description="granola meeting",
        reference_time=_parse_aware(ref) if ref else _EPOCH,
        group_id=granola_group_id(meeting.owner_email),
    )
