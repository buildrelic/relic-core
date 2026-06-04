"""GitHub connector: async pulls of merged PRs (with files + reviews) and issues."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any

from githubkit import GitHub

from relic.ingest.mappers import FileChange, IssueRec, PullRequestRec, RepoBundle, ReviewRec

if TYPE_CHECKING:
    from relic.config import Settings

_NO_TOKEN = "No GitHub token. Set GITHUB_TOKEN in .env or run `gh auth login`."


def resolve_github_token(settings: Settings) -> str:
    """Return a GitHub token from settings, else from `gh auth token`."""
    if settings.github_token:
        return settings.github_token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(_NO_TOKEN) from exc
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(_NO_TOKEN)
    return token


def make_github(token: str) -> GitHub:
    """Build an authenticated githubkit client."""
    return GitHub(token)


def _dump(model: Any) -> dict[str, Any]:
    """Normalize a githubkit model to a JSON-ish dict (Missing fields omitted)."""
    return model.model_dump(mode="json", exclude_unset=True)


async def fetch_repo(gh: GitHub, owner: str, name: str, *, concurrency: int = 10) -> RepoBundle:
    """Fetch merged PRs (with files + reviews) and non-PR issues for `owner/name`."""
    full_name = f"{owner}/{name}"
    bundle = RepoBundle(
        full_name=full_name, url=f"https://github.com/{full_name}", default_branch="main"
    )

    merged: list[dict[str, Any]] = []
    async for pr in gh.rest.paginate(
        gh.rest.pulls.async_list, owner=owner, repo=name, state="closed", per_page=100
    ):
        data = _dump(pr)
        if data.get("merged_at"):
            merged.append(data)

    sem = asyncio.Semaphore(concurrency)

    async def _hydrate(pr_data: dict[str, Any]) -> PullRequestRec:
        async with sem:
            return await _fetch_pr_detail(gh, owner, name, pr_data)

    bundle.pull_requests = list(await asyncio.gather(*[_hydrate(d) for d in merged]))
    bundle.issues = await _fetch_issues(gh, owner, name)
    return bundle


async def _fetch_pr_detail(gh: GitHub, owner: str, name: str, pr: dict[str, Any]) -> PullRequestRec:
    number = int(pr["number"])
    author = pr.get("user") or {}

    files: list[FileChange] = []
    async for raw_file in gh.rest.paginate(
        gh.rest.pulls.async_list_files, owner=owner, repo=name, pull_number=number, per_page=100
    ):
        fd = _dump(raw_file)
        files.append(
            FileChange(
                path=fd.get("filename", ""),
                additions=int(fd.get("additions", 0)),
                deletions=int(fd.get("deletions", 0)),
                status=fd.get("status", ""),
            )
        )

    reviews: list[ReviewRec] = []
    async for raw_review in gh.rest.paginate(
        gh.rest.pulls.async_list_reviews, owner=owner, repo=name, pull_number=number, per_page=100
    ):
        rd = _dump(raw_review)
        reviewer = rd.get("user") or {}
        reviews.append(
            ReviewRec(
                login=reviewer.get("login"),
                profile_url=reviewer.get("html_url"),
                state=rd.get("state", ""),
                submitted_at=rd.get("submitted_at"),
                url=rd.get("html_url"),
            )
        )

    requested = await gh.rest.pulls.async_list_requested_reviewers(
        owner=owner, repo=name, pull_number=number
    )
    req = _dump(requested.parsed_data)
    requested_reviewers = [u.get("login") for u in req.get("users", []) if u.get("login")]

    return PullRequestRec(
        number=number,
        title=pr.get("title", ""),
        url=pr.get("html_url", ""),
        state="merged",
        author_login=author.get("login"),
        author_url=author.get("html_url"),
        created_at=pr.get("created_at", ""),
        merged_at=pr.get("merged_at"),
        body=pr.get("body"),
        reviews=reviews,
        requested_reviewers=[r for r in requested_reviewers if r],
        files=files,
        raw=pr,
    )


async def _fetch_issues(gh: GitHub, owner: str, name: str) -> list[IssueRec]:
    full_name = f"{owner}/{name}"
    issues: list[IssueRec] = []
    async for raw_issue in gh.rest.paginate(
        gh.rest.issues.async_list_for_repo, owner=owner, repo=name, state="all", per_page=100
    ):
        data = _dump(raw_issue)
        if data.get("pull_request"):
            continue  # the issues endpoint also returns PRs
        assignees = [a.get("login") for a in (data.get("assignees") or []) if a.get("login")]
        label_names: list[str] = []
        for label in data.get("labels") or []:
            value = label.get("name") if isinstance(label, dict) else label
            if isinstance(value, str) and value:
                label_names.append(value)
        issues.append(
            IssueRec(
                source="github",
                identifier=f"{full_name}#{data.get('number')}",
                title=data.get("title", ""),
                url=data.get("html_url", ""),
                state=data.get("state", ""),
                assignees=[a for a in assignees if a],
                labels=label_names,
                created_at=data.get("created_at"),
                closed_at=data.get("closed_at"),
                raw=data,
            )
        )
    return issues
