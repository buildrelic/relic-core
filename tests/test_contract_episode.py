"""The episode-body contract: the seam between ingest and graph.

This file is the executable contract for the typed episode body. It pins the
invariants the downstream side (extraction, recall, the detector, the compiler)
relies on, so a change to ingestion that would break them fails here rather than
silently in production:

- the wire body validates against ``PrEpisodeBody`` / ``IssueEpisodeBody`` (the
  detector can deserialize it),
- ``recall._extract_url`` still resolves a citation URL from the body (I3),
- the episode ``name`` carries no version or mutable data (I2),
- the body stays under the per-episode token budget (I4).

The ``canonical_*`` builders are the shared reference instance Abhinav tunes the
entity/edge types against.
"""

import json

from pydantic import BaseModel

from relic.contracts.episode_body import (
    AgentSessionEpisodeBody,
    CiRunEpisodeBody,
    CommitEpisodeBody,
    ConversationEpisodeBody,
    DiscussionEpisodeBody,
    DocEpisodeBody,
    IncidentEpisodeBody,
    IssueEpisodeBody,
    PrEpisodeBody,
    ReleaseEpisodeBody,
    RepoRef,
)
from relic.graph.recall import _extract_url
from relic.ingest.mappers import (
    _MAX_BODY_CHARS,
    CommitRec,
    FileChange,
    IssueRec,
    LinkedIssueRec,
    PullRequestRec,
    RepoBundle,
    RequestedReviewerRec,
    ReviewRec,
    issue_to_episode,
    pr_to_episode,
)


def _repo() -> RepoBundle:
    return RepoBundle(
        full_name="buildrelic/relic-core",
        url="https://github.com/buildrelic/relic-core",
        default_branch="main",
    )


def canonical_pr_record() -> PullRequestRec:
    """A representative merged PR exercising every review-routing field."""
    return PullRequestRec(
        number=12,
        title="Route auth PRs to the right reviewer",
        url="https://github.com/buildrelic/relic-core/pull/12",
        state="merged",
        author_login="paris-phan",
        author_url="https://github.com/paris-phan",
        created_at="2025-08-01T10:00:00Z",
        merged_at="2025-08-02T12:00:00Z",
        body="Adds reviewer routing for src/auth/**.",
        reviews=[
            ReviewRec(
                login="abhinavp5",
                profile_url="https://github.com/abhinavp5",
                state="COMMENTED",
                submitted_at="2025-08-01T14:00:00Z",
                url="https://github.com/buildrelic/relic-core/pull/12#r1",
                body="please add a test",
            ),
            ReviewRec(
                login="abhinavp5",
                profile_url="https://github.com/abhinavp5",
                state="APPROVED",
                submitted_at="2025-08-02T09:00:00Z",
                url="https://github.com/buildrelic/relic-core/pull/12#r2",
                body="lgtm",
            ),
        ],
        requested_reviewers=[
            RequestedReviewerRec(
                name="abhinavp5", kind="user", profile_url="https://github.com/abhinavp5"
            ),
            RequestedReviewerRec(name="auth-team", kind="team"),
        ],
        files=[FileChange(path="src/auth/login.py", additions=80, deletions=10, status="")],
        labels=["area:auth", "type:fix"],
        base_ref="main",
        head_ref="paris/auth-routing",
        additions=80,
        deletions=10,
        changed_files=1,
        commits=[CommitRec(sha="abc123", message="route auth PRs", author_login="paris-phan")],
        linked_issues=[LinkedIssueRec(identifier="buildrelic/relic-core#100", relation="closes")],
    )


def canonical_issue_record() -> IssueRec:
    return IssueRec(
        source="github",
        identifier="buildrelic/relic-core#100",
        title="Auth PRs go to the wrong reviewer",
        url="https://github.com/buildrelic/relic-core/issues/100",
        state="closed",
        body="Reviews for src/auth land on the wrong person.",
        assignees=["paris-phan"],
        labels=["area:auth", "bug"],
        created_at="2025-07-30T00:00:00Z",
        closed_at="2025-08-02T12:00:00Z",
    )


# --- the body is a valid instance of the shared contract --------------------


def test_pr_body_validates_against_contract() -> None:
    spec = pr_to_episode(canonical_pr_record(), _repo())
    # The wire body round-trips through the typed model: the detector can deserialize it.
    body = PrEpisodeBody.model_validate_json(spec.body)
    assert body.schema_version == 2
    assert body.source_type == "pull_request"
    assert body.pull_request.number == 12
    assert body.pull_request.labels == ["area:auth", "type:fix"]
    assert body.pull_request.diff_stats.changed_files == 1
    assert {rr.kind for rr in body.requested_reviewers} == {"user", "team"}
    assert body.files[0].additions == 80
    assert body.linked_issues[0].relation == "closes"
    assert body.context  # a non-empty extractor hint


def test_issue_body_validates_against_contract() -> None:
    spec = issue_to_episode(canonical_issue_record(), _repo())
    body = IssueEpisodeBody.model_validate_json(spec.body)
    assert body.schema_version == 2
    assert body.source_type == "issue"
    assert body.issue.identifier == "buildrelic/relic-core#100"
    assert body.issue.labels == ["area:auth", "bug"]
    assert body.issue.assignees == ["paris-phan"]


# --- I3: recall._extract_url must still resolve the citation anchor ----------


def test_extract_url_resolves_pr_citation() -> None:
    spec = pr_to_episode(canonical_pr_record(), _repo())
    assert _extract_url(spec.body) == "https://github.com/buildrelic/relic-core/pull/12"


def test_extract_url_resolves_issue_citation() -> None:
    spec = issue_to_episode(canonical_issue_record(), _repo())
    assert _extract_url(spec.body) == "https://github.com/buildrelic/relic-core/issues/100"


# --- I2: the episode name is the dedup key, version-free and stable ----------


def test_schema_version_absent_from_name() -> None:
    pr_spec = pr_to_episode(canonical_pr_record(), _repo())
    issue_spec = issue_to_episode(canonical_issue_record(), _repo())
    # The checkpoint dedups on name; a version or mutable field in it would re-ingest
    # and fork the graph. Names stay exactly the historical, deterministic shape.
    assert pr_spec.name == "PR buildrelic/relic-core#12"
    assert issue_spec.name == "Issue buildrelic/relic-core#100"
    assert "schema_version" not in pr_spec.name
    # The version lives on the envelope and in the body, never woven into the name.
    assert pr_spec.schema_version == 2
    assert json.loads(pr_spec.body)["schema_version"] == 2


# --- I4: the body stays under the per-episode token budget -------------------


def test_oversized_body_is_trimmed_to_budget() -> None:
    pr = canonical_pr_record()
    # 40 commits with ~1000-char messages would blow well past the budget.
    pr.commits = [
        CommitRec(sha=f"sha{i}", message="x" * 1000, author_login="paris-phan") for i in range(40)
    ]
    spec = pr_to_episode(pr, _repo())
    assert len(spec.body) <= _MAX_BODY_CHARS
    body = PrEpisodeBody.model_validate_json(spec.body)
    # Commits are shed first (they aid commit-convention skills but not routing);
    # the routing-critical reviews and requested reviewers survive.
    assert body.commits == []
    assert len(body.reviews) == 2
    assert {rr.kind for rr in body.requested_reviewers} == {"user", "team"}


# --- development source bodies: structure only (no connectors yet) ----------

_REPO = RepoRef(full_name="buildrelic/relic-core", url="https://github.com/buildrelic/relic-core")

# A minimal valid instance of every development source body. Structure only: these
# pin the contract shape and the cross-source invariants below; no connector exists.
_SOURCE_BODIES: list[BaseModel] = [
    CommitEpisodeBody(url="https://github.com/o/r/commit/abc", repo=_REPO, sha="abc"),
    ReleaseEpisodeBody(url="https://github.com/o/r/releases/tag/v1.0.0", repo=_REPO, tag="v1.0.0"),
    CiRunEpisodeBody(url="https://github.com/o/r/actions/runs/1", repo=_REPO, name="ci"),
    IncidentEpisodeBody(url="https://example.com/incidents/42", title="auth outage"),
    DocEpisodeBody(url="https://github.com/o/r/blob/main/docs/adr/0001.md", title="Use FalkorDB"),
    DiscussionEpisodeBody(url="https://github.com/o/r/discussions/7", title="RFC: routing"),
    ConversationEpisodeBody(url="https://slack.com/archives/C/p1"),
    AgentSessionEpisodeBody(url="session://abc123", title="Coding session abc123"),
]

_ALL_BODY_MODELS = [
    PrEpisodeBody,
    IssueEpisodeBody,
    CommitEpisodeBody,
    ReleaseEpisodeBody,
    CiRunEpisodeBody,
    IncidentEpisodeBody,
    DocEpisodeBody,
    DiscussionEpisodeBody,
    ConversationEpisodeBody,
    AgentSessionEpisodeBody,
]


def test_every_source_type_is_distinct() -> None:
    # The source_type discriminator must uniquely identify the body model, so a
    # consumer can pick the right model to deserialize.
    source_types = [m.model_fields["source_type"].default for m in _ALL_BODY_MODELS]
    assert None not in source_types
    assert len(source_types) == len(set(source_types))


def test_dev_source_bodies_carry_a_resolvable_citation_anchor() -> None:
    # I3 for every new source, for free: each body exposes a top-level `url`, which
    # recall._extract_url resolves via its top-level fallback. No recall change is
    # needed per source.
    for body in _SOURCE_BODIES:
        assert _extract_url(body.model_dump_json()) == body.url  # type: ignore[attr-defined]


# --- derived timestamps (no extra fetch) ------------------------------------


def test_pr_timestamps_are_derived_from_reviews_and_times() -> None:
    body = PrEpisodeBody.model_validate_json(pr_to_episode(canonical_pr_record(), _repo()).body)
    ts = body.pull_request.timestamps
    assert ts.first_review_at == "2025-08-01T14:00:00Z"  # earliest review
    assert ts.approved_at == "2025-08-02T09:00:00Z"  # the APPROVED review
    assert ts.time_to_merge_hours == 26.0  # 2025-08-01T10:00 -> 2025-08-02T12:00
