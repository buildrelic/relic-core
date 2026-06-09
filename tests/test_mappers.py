import json
import re
from datetime import UTC, datetime

from relic.ingest.mappers import (
    FileChange,
    IssueRec,
    PullRequestRec,
    RepoBundle,
    ReviewRec,
    _parse_coauthors,
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
    assert body["issue"]["parent"] is None  # no parent for a top-level issue


# --- REL-9 edge cases -------------------------------------------------------


def _pr(**overrides: object) -> PullRequestRec:
    """A minimal merged PR; override fields per edge case."""
    base: dict[str, object] = {
        "number": 1,
        "title": "do a thing",
        "url": "https://github.com/paris-phan/course-scheduler/pull/1",
        "state": "merged",
        "author_login": "paris-phan",
        "author_url": "https://github.com/paris-phan",
        "created_at": "2025-01-01T00:00:00Z",
        "merged_at": "2025-01-02T00:00:00Z",
    }
    base.update(overrides)
    return PullRequestRec(**base)  # type: ignore[arg-type]


def test_closed_without_merge_keeps_state_and_falls_back_to_created_at() -> None:
    # A PR closed without merging has no merged_at: it should still map, anchored
    # to when it was opened, and keep its real state rather than "merged".
    pr = _pr(number=2, state="closed", merged_at=None, created_at="2025-03-01T00:00:00Z")
    spec = pr_to_episode(pr, _repo())
    assert spec.reference_time == datetime(2025, 3, 1, tzinfo=UTC)
    body = json.loads(spec.body)
    assert body["pull_request"]["state"] == "closed"
    assert body["pull_request"]["merged_at"] is None


def test_draft_pr_state_is_preserved() -> None:
    pr = _pr(number=3, state="draft", merged_at=None)
    body = json.loads(pr_to_episode(pr, _repo()).body)
    assert body["pull_request"]["state"] == "draft"


def test_ghost_author_is_omitted_not_emitted_empty() -> None:
    # A deleted/bot opener has no login; emit author: null rather than a phantom
    # Person with no identity for the extractor to choke on.
    pr = _pr(author_login=None, author_url=None)
    body = json.loads(pr_to_episode(pr, _repo()).body)
    assert body["pull_request"]["author"] is None


def test_coauthored_by_trailers_become_co_authors() -> None:
    pr = _pr(
        body=(
            "Implements the feature.\n\n"
            "Co-authored-by: Bob Jones <bob@example.com>\n"
            "Co-authored-by: Carol <carol@example.com>\n"
        )
    )
    body = json.loads(pr_to_episode(pr, _repo()).body)
    co = body["pull_request"]["co_authors"]
    assert {"name": "Bob Jones", "email": "bob@example.com"} in co
    assert {"name": "Carol", "email": "carol@example.com"} in co
    assert len(co) == 2


def test_parse_coauthors_dedupes_and_allows_missing_email() -> None:
    body = (
        "Co-authored-by: Bob <bob@x.com>\n"
        "co-authored-by: Bob <bob@x.com>\n"  # duplicate, different keyword case
        "Co-authored-by: Dana\n"  # name only, no email
    )
    out = _parse_coauthors(body)
    assert {"name": "Bob", "email": "bob@x.com"} in out
    assert {"name": "Dana"} in out
    assert len(out) == 2


def test_code_fences_in_body_are_defused() -> None:
    # A ``` in a quoted commit message / code block must not survive as a run of
    # three, or it would close the fenced block the extractor wraps the body in.
    pr = _pr(body="see the snippet:\n```python\nprint('hi')\n```\nthanks")
    desc = json.loads(pr_to_episode(pr, _repo()).body)["pull_request"]["description"]
    assert "```" not in desc  # no surviving fence delimiter
    assert "print('hi')" in desc  # surrounding content is otherwise intact
    assert "python" in desc


def test_subissue_parent_is_carried_into_episode() -> None:
    issue = IssueRec(
        source="linear",
        identifier="REL-9",
        title="mapper edge cases",
        url="https://linear.app/buildrelic/issue/REL-9",
        state="Todo",
        created_at="2025-02-02T00:00:00Z",
        parent="REL-1",
    )
    body = json.loads(issue_to_episode(issue, _repo()).body)
    assert body["issue"]["parent"] == "REL-1"
