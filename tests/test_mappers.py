import json
import re
from datetime import UTC, datetime

from relic.ingest.mappers import (
    FileChange,
    IssueRec,
    PullRequestRec,
    RepoBundle,
    ReviewRec,
    issue_to_episode,
    pr_to_episode,
    repo_group_id,
)


def _repo() -> RepoBundle:
    return RepoBundle(
        full_name="paris-phan/course-scheduler",
        url="https://github.com/paris-phan/course-scheduler",
        default_branch="main",
    )


def test_repo_group_id_slugifies_owner_name() -> None:
    assert repo_group_id("paris-phan/course-scheduler") == "paris-phan__course-scheduler"
    # Always Graphiti-legal (^[A-Za-z0-9_-]+$), even with dots and spaces.
    assert re.fullmatch(r"[A-Za-z0-9_-]+", repo_group_id("a/b.c d"))


def test_pr_to_episode_shape_and_group() -> None:
    pr = PullRequestRec(
        number=4,
        title="Gcp setup",
        url="https://github.com/paris-phan/course-scheduler/pull/4",
        state="merged",
        author_login="paris-phan",
        author_url="https://github.com/paris-phan",
        created_at="2025-08-01T10:00:00+00:00",
        merged_at="2025-08-05T12:00:00Z",
        reviews=[
            ReviewRec(
                login="paris-phan",
                profile_url="https://github.com/paris-phan",
                state="COMMENTED",
                submitted_at="2025-08-04T09:00:00Z",
                url="https://github.com/paris-phan/course-scheduler/pull/4#r1",
                body="please add a regression test",
            )
        ],
        requested_reviewers=["abhinavp5"],
        body="Provision GCP and Terraform.",
        files=[FileChange(path="infra/gcp.tf", additions=100, deletions=2, status="added")],
    )
    spec = pr_to_episode(pr, _repo())
    assert spec.name == "PR paris-phan/course-scheduler#4"
    assert spec.source_description == "github pull request"
    assert spec.group_id == "paris-phan__course-scheduler"
    # reference_time prefers merged_at and is tz-aware (Z normalized to UTC).
    assert spec.reference_time == datetime(2025, 8, 5, 12, 0, tzinfo=UTC)
    body = json.loads(spec.body)
    assert body["pull_request"]["number"] == 4
    assert body["pull_request"]["author"]["login"] == "paris-phan"
    assert body["reviews"][0]["state"] == "COMMENTED"
    assert body["reviews"][0]["comment"] == "please add a regression test"
    assert body["requested_reviewers"] == ["abhinavp5"]
    assert body["pull_request"]["description"] == "Provision GCP and Terraform."
    assert body["files"][0]["path"] == "infra/gcp.tf"
    assert "additions" not in body["files"][0]  # per-file stats dropped to keep episodes lean


def test_pr_to_episode_no_reviews_falls_back_to_created_at() -> None:
    pr = PullRequestRec(
        number=1,
        title="seed db",
        url="https://example.com/pr/1",
        state="merged",
        author_login=None,
        author_url=None,
        created_at="2025-01-01T00:00:00Z",
        merged_at=None,
    )
    spec = pr_to_episode(pr, _repo())
    assert spec.reference_time == datetime(2025, 1, 1, tzinfo=UTC)
    body = json.loads(spec.body)
    assert body["reviews"] == []
    assert body["files"] == []
    assert body["pull_request"]["description"] is None


def _review(login: str, *, state: str = "COMMENTED", submitted_at: str | None = None) -> ReviewRec:
    return ReviewRec(
        login=login,
        profile_url=f"https://github.com/{login}",
        state=state,
        submitted_at=submitted_at,
        url=None,
        body=None,
    )


def _pr_with_reviews(reviews: list[ReviewRec]) -> PullRequestRec:
    return PullRequestRec(
        number=1,
        title="t",
        url="https://example.com/pr/1",
        state="merged",
        author_login="a",
        author_url=None,
        created_at="2025-01-01T00:00:00Z",
        merged_at="2025-01-02T00:00:00Z",
        reviews=reviews,
    )


def _episode_review_logins(pr: PullRequestRec) -> list[str | None]:
    return [r["login"] for r in json.loads(pr_to_episode(pr, _repo()).body)["reviews"]]


def test_bot_reviews_are_dropped() -> None:
    pr = _pr_with_reviews(
        [
            _review("alice", submitted_at="2025-01-01T00:00:00Z"),
            _review("dependabot[bot]", submitted_at="2025-01-02T00:00:00Z"),
            _review("github-actions[bot]", submitted_at="2025-01-03T00:00:00Z"),
        ]
    )
    assert _episode_review_logins(pr) == ["alice"]


def test_reviews_capped_to_ten_most_recent_preserving_order() -> None:
    # 12 human reviews with increasing timestamps; the two oldest are dropped, order kept.
    reviews = [_review(f"r{i}", submitted_at=f"2025-01-{i + 1:02d}T00:00:00Z") for i in range(12)]
    logins = _episode_review_logins(_pr_with_reviews(reviews))
    assert logins == [f"r{i}" for i in range(2, 12)]


def test_decided_reviews_preferred_over_comments_when_capping() -> None:
    # r0 is the oldest but APPROVED; capping keeps it over a newer plain comment (r1).
    reviews = [_review("r0", state="APPROVED", submitted_at="2025-01-01T00:00:00Z")]
    reviews += [
        _review(f"r{i}", state="COMMENTED", submitted_at=f"2025-01-{i + 1:02d}T00:00:00Z")
        for i in range(1, 11)
    ]
    logins = _episode_review_logins(_pr_with_reviews(reviews))
    assert len(logins) == 10
    assert "r0" in logins  # decided state survives despite being the oldest
    assert "r1" not in logins  # the oldest plain comment is the one dropped


def test_long_body_is_clipped() -> None:
    pr = PullRequestRec(
        number=1,
        title="t",
        url="u",
        state="merged",
        author_login=None,
        author_url=None,
        created_at="2025-01-01T00:00:00Z",
        merged_at="2025-01-02T00:00:00Z",
        body="x" * 5000,
    )
    desc = json.loads(pr_to_episode(pr, _repo()).body)["pull_request"]["description"]
    assert desc.endswith("...")
    assert len(desc) == 2000 + len("...")


def test_issue_to_episode() -> None:
    issue = IssueRec(
        source="github",
        identifier="paris-phan/course-scheduler#7",
        title="Bug in scheduler",
        url="https://github.com/paris-phan/course-scheduler/issues/7",
        state="open",
        body="scheduler crashes on empty input",
        assignees=["paris-phan"],
        labels=["bug"],
        created_at="2025-02-02T00:00:00Z",
    )
    spec = issue_to_episode(issue, _repo())
    assert spec.name == "Issue paris-phan/course-scheduler#7"
    assert spec.group_id == "paris-phan__course-scheduler"
    assert spec.reference_time == datetime(2025, 2, 2, tzinfo=UTC)
    body = json.loads(spec.body)
    assert body["issue"]["labels"] == ["bug"]
    assert body["issue"]["assignees"] == ["paris-phan"]
    assert body["issue"]["description"] == "scheduler crashes on empty input"
