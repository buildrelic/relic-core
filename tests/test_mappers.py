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


def test_issue_to_episode() -> None:
    issue = IssueRec(
        source="github",
        identifier="paris-phan/course-scheduler#7",
        title="Bug in scheduler",
        url="https://github.com/paris-phan/course-scheduler/issues/7",
        state="open",
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
