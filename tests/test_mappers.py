import json
import re
from datetime import UTC, datetime

from relic.ingest.mappers import (
    _MAX_REVIEWS_PER_PR,
    FileChange,
    IssueRec,
    PullRequestRec,
    RepoBundle,
    RequestedReviewerRec,
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
        requested_reviewers=[RequestedReviewerRec(name="abhinavp5", kind="user")],
        body="Provision GCP and Terraform.",
        files=[FileChange(path="infra/gcp.tf", additions=100, deletions=2, status="added")],
    )
    spec = pr_to_episode(pr, _repo())
    assert spec.name == "PR paris-phan/course-scheduler#4"
    assert spec.source_description == "github pull request"
    assert spec.group_id == "paris-phan__course-scheduler"
    assert spec.schema_version == 2  # versioned body, surfaced on the envelope
    # reference_time prefers merged_at and is tz-aware (Z normalized to UTC).
    assert spec.reference_time == datetime(2025, 8, 5, 12, 0, tzinfo=UTC)
    body = json.loads(spec.body)
    assert body["schema_version"] == 2  # and inside the body, for the detector
    assert body["pull_request"]["number"] == 4
    assert body["pull_request"]["author"]["login"] == "paris-phan"
    assert body["reviews"][0]["reviewer"]["login"] == "paris-phan"
    assert body["reviews"][0]["state"] == "COMMENTED"
    assert body["reviews"][0]["comment"] == "please add a regression test"
    assert body["requested_reviewers"][0]["name"] == "abhinavp5"
    assert body["requested_reviewers"][0]["kind"] == "user"
    assert body["pull_request"]["description"] == "Provision GCP and Terraform."
    assert body["files"][0]["path"] == "infra/gcp.tf"
    # Per-file stats are now carried (recovered dead payload), feeding TOUCHES_PATH.
    assert body["files"][0]["additions"] == 100
    assert body["files"][0]["deletions"] == 2


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


def _review(
    login: str,
    *,
    state: str = "COMMENTED",
    submitted_at: str | None = None,
    is_bot: bool = False,
) -> ReviewRec:
    return ReviewRec(
        login=login,
        profile_url=f"https://github.com/{login}",
        state=state,
        submitted_at=submitted_at,
        url=None,
        body=None,
        is_bot=is_bot,
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
    # Reviews nest the (nullable) reviewer; a ghost/deleted reviewer surfaces as None.
    reviews = json.loads(pr_to_episode(pr, _repo()).body)["reviews"]
    return [r["reviewer"]["login"] if r["reviewer"] else None for r in reviews]


def test_bot_reviews_are_dropped() -> None:
    # REST-style shape: bot-ness carried in the "[bot]" login suffix.
    pr = _pr_with_reviews(
        [
            _review("alice", submitted_at="2025-01-01T00:00:00Z"),
            _review("dependabot[bot]", submitted_at="2025-01-02T00:00:00Z"),
            _review("github-actions[bot]", submitted_at="2025-01-03T00:00:00Z"),
        ]
    )
    assert _episode_review_logins(pr) == ["alice"]


def test_bot_reviews_dropped_by_typename_flag() -> None:
    # Production (GraphQL) shape: bot logins are bare (no "[bot]" suffix); bot-ness comes
    # from the is_bot flag the fetcher sets from __typename. Without it these would slip
    # through, which is exactly the bug that hid behind the REST-style test above.
    pr = _pr_with_reviews(
        [
            _review("alice", submitted_at="2025-01-01T00:00:00Z"),
            _review("dependabot", submitted_at="2025-01-02T00:00:00Z", is_bot=True),
            _review("github-actions", submitted_at="2025-01-03T00:00:00Z", is_bot=True),
        ]
    )
    assert _episode_review_logins(pr) == ["alice"]


def test_reviews_capped_to_max_most_recent_preserving_order() -> None:
    # Two more than the cap, increasing timestamps: the two oldest are dropped, order kept.
    n = _MAX_REVIEWS_PER_PR + 2
    reviews = [_review(f"r{i}", submitted_at=f"2025-01-01T00:{i:02d}:00Z") for i in range(n)]
    logins = _episode_review_logins(_pr_with_reviews(reviews))
    assert logins == [f"r{i}" for i in range(2, n)]


def test_decided_reviews_preferred_over_comments_when_capping() -> None:
    # r0 is the oldest but APPROVED; capping keeps it over a newer plain comment (r1).
    # One more review than the cap, so exactly one is dropped.
    reviews = [_review("r0", state="APPROVED", submitted_at="2025-01-01T00:00:00Z")]
    reviews += [
        _review(f"r{i}", state="COMMENTED", submitted_at=f"2025-01-01T00:{i:02d}:00Z")
        for i in range(1, _MAX_REVIEWS_PER_PR + 1)
    ]
    logins = _episode_review_logins(_pr_with_reviews(reviews))
    assert len(logins) == _MAX_REVIEWS_PER_PR
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


def test_ghost_reviewer_is_omitted_not_emitted_empty() -> None:
    # Same class as the ghost author: a review by a deleted account keeps its
    # state/comment signal but emits reviewer: null, not an identity-less Person.
    pr = _pr(
        reviews=[
            ReviewRec(
                login=None,
                profile_url=None,
                state="APPROVED",
                submitted_at="2025-01-01T12:00:00Z",
                url="https://github.com/paris-phan/course-scheduler/pull/1#r1",
                body="lgtm",
            )
        ]
    )
    review = json.loads(pr_to_episode(pr, _repo()).body)["reviews"][0]
    assert review["reviewer"] is None
    assert review["state"] == "APPROVED"
    assert review["comment"] == "lgtm"


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


def test_parse_coauthors_handles_crlf_line_endings() -> None:
    # GitHub returns PR bodies with CRLF; the trailers must still parse (regression).
    body = (
        "Implements it.\r\n\r\n"
        "Co-authored-by: Bob <bob@x.com>\r\n"
        "Co-authored-by: Carol <carol@y.com>\r\n"
    )
    out = _parse_coauthors(body)
    assert {"name": "Bob", "email": "bob@x.com"} in out
    assert {"name": "Carol", "email": "carol@y.com"} in out
    assert len(out) == 2


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


def test_parse_coauthors_email_only_trailer_yields_email_only_entry() -> None:
    # Git-generated trailers always carry a name, but a hand-typed email-only one
    # still identifies a person: keep it as an email-only entry rather than drop it.
    out = _parse_coauthors("Co-authored-by: <bob@x.com>\nCo-authored-by: Carol <c@y.com>\n")
    assert out == [{"email": "bob@x.com"}, {"name": "Carol", "email": "c@y.com"}]


def test_code_fences_in_body_are_defused() -> None:
    # A ``` in a quoted commit message / code block must not survive as a run of
    # three, or it would close the fenced block the extractor wraps the body in.
    pr = _pr(body="see the snippet:\n```python\nprint('hi')\n```\nthanks")
    desc = json.loads(pr_to_episode(pr, _repo()).body)["pull_request"]["description"]
    assert "```" not in desc  # no surviving fence delimiter
    assert "print('hi')" in desc  # surrounding content is otherwise intact
    assert "python" in desc


def test_code_fences_in_titles_are_defused() -> None:
    # Titles bypass _clip (never nulled or truncated) but still need defusing: a
    # ``` in a PR or issue title would close the extractor's fence just like a body.
    pr = _pr(title="fix ``` handling in parser")
    pr_title = json.loads(pr_to_episode(pr, _repo()).body)["pull_request"]["title"]
    assert "```" not in pr_title
    assert "fix" in pr_title and "handling in parser" in pr_title

    issue = IssueRec(
        source="github",
        identifier="paris-phan/course-scheduler#9",
        title="docs show ``` blocks unrendered",
        url="https://github.com/paris-phan/course-scheduler/issues/9",
        state="open",
        created_at="2025-02-02T00:00:00Z",
    )
    issue_title = json.loads(issue_to_episode(issue, _repo()).body)["issue"]["title"]
    assert "```" not in issue_title


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
