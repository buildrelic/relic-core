"""Derive the engram row fields from a typed episode body. Pure, no I/O.

One function per artifact type turns the body into what the store needs: the
``search_text`` that feeds Postgres full-text search, the ``markdown`` the web
workspace renders and lets a human edit, the people (with roles) for the
``memory_people`` join, and the file paths for ``memory_paths``.

This lives in the engram package, not ingest: the independence contract forbids
engram from importing ingest, and both sides already share the body models through
``relic.contracts.episode_body``. Dispatch keys on the body's ``source_type``
discriminator; an unknown or malformed body degrades to a raw-text row rather than
failing the episode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from relic.contracts.episode_body import (
    AgentSessionEpisodeBody,
    ConversationEpisodeBody,
    DocEpisodeBody,
    IssueEpisodeBody,
    PersonRef,
    PrEpisodeBody,
)

if TYPE_CHECKING:
    from relic.contracts import EpisodeSpec

# A person ref for the join table: the keys _upsert_person reads.
PersonDict = dict[str, str | None]


@dataclass(slots=True)
class Derived:
    """Everything the store writes that is not carried verbatim on the spec."""

    source: str
    artifact_type: str
    title: str
    url: str | None
    search_text: str
    markdown: str
    people: list[tuple[PersonDict, str]] = field(default_factory=list)  # (person, role)
    paths: list[str] = field(default_factory=list)


def derive(spec: EpisodeSpec) -> Derived:
    """Derive row fields from ``spec.body``. Never raises: bad JSON degrades to raw text."""
    try:
        raw = json.loads(spec.body)
    except json.JSONDecodeError:
        raw = None
    if not isinstance(raw, dict):
        return Derived(
            source=_source_of(spec.source_description),
            artifact_type="doc",
            title=spec.name,
            url=None,
            search_text=spec.body[:8000],
            markdown=f"# {spec.name}\n\n```\n{spec.body[:8000]}\n```\n",
        )
    kind = raw.get("source_type")
    builder = _BUILDERS.get(kind if isinstance(kind, str) else "")
    if builder is None:
        return Derived(
            source=_source_of(spec.source_description),
            artifact_type=kind if isinstance(kind, str) else "doc",
            title=str(raw.get("title") or spec.name),
            url=raw.get("url") if isinstance(raw.get("url"), str) else None,
            search_text=_flatten(raw),
            markdown=f"# {raw.get('title') or spec.name}\n",
        )
    try:
        return builder(raw, spec)
    except Exception:  # noqa: BLE001 - a malformed body degrades, never fails the episode
        return Derived(
            source=_source_of(spec.source_description),
            artifact_type=kind if isinstance(kind, str) else "doc",
            title=str(raw.get("title") or spec.name),
            url=None,
            search_text=_flatten(raw),
            markdown=f"# {raw.get('title') or spec.name}\n",
        )


# --- Per-type builders --------------------------------------------------------


def _derive_pr(raw: dict, spec: EpisodeSpec) -> Derived:
    body = PrEpisodeBody.model_validate(raw)
    pr = body.pull_request

    people: list[tuple[PersonDict, str]] = []
    if pr.author:
        people.append((_person(pr.author), "author"))
    for review in body.reviews:
        if review.reviewer:
            people.append((_person(review.reviewer), "reviewer"))
    for req in body.requested_reviewers:
        if req.name and req.kind == "user":
            people.append(({"github_login": req.name, "display_name": None, "email": None},
                           "requested_reviewer"))

    paths = [f.path for f in body.files if f.path]

    text_parts: list[str] = [
        pr.title,
        pr.description or "",
        " ".join(pr.labels),
        f"{pr.base_ref or ''} {pr.head_ref or ''}",
        pr.author.login or "" if pr.author else "",
    ]
    for review in body.reviews:
        login = review.reviewer.login if review.reviewer else None
        text_parts.append(" ".join(p for p in (login, review.state, review.comment) if p))
    text_parts.extend(r.name or "" for r in body.requested_reviewers)
    text_parts.extend(
        " ".join(p for p in (c.message, c.author_login) if p) for c in body.commits
    )
    # Paths and their segments, so a query like "auth" matches src/auth/token.py.
    for path in paths:
        text_parts.append(path)
        text_parts.append(path.replace("/", " "))
    text_parts.extend(li.identifier for li in body.linked_issues)

    md: list[str] = [
        f"# {pr.title}",
        "",
        f"[{spec.name}]({pr.url}) in {body.repo.full_name} · {pr.state or 'unknown'}",
        "",
    ]
    if pr.author and pr.author.login:
        md.append(f"**Author:** {pr.author.login}")
    if pr.labels:
        md.append(f"**Labels:** {', '.join(pr.labels)}")
    if pr.merged_at:
        md.append(f"**Merged:** {pr.merged_at}")
    if pr.description:
        md.extend(["", "## Description", "", pr.description])
    if body.reviews:
        md.extend(["", "## Reviews", ""])
        for review in body.reviews:
            login = (review.reviewer.login if review.reviewer else None) or "unknown"
            line = f"- **{login}** — {review.state or 'commented'}"
            if review.comment:
                line += f": {review.comment}"
            md.append(line)
    if paths:
        md.extend(["", "## Files", ""])
        md.extend(f"- `{p}`" for p in paths)
    if body.commits:
        md.extend(["", "## Commits", ""])
        for c in body.commits:
            message = (c.message or "").splitlines()[0] if c.message else ""
            md.append(f"- {message}" + (f" ({c.author_login})" if c.author_login else ""))

    return Derived(
        source="github",
        artifact_type="pull_request",
        title=pr.title,
        url=pr.url,
        search_text=_join(text_parts),
        markdown="\n".join(md) + "\n",
        people=people,
        paths=paths,
    )


def _derive_issue(raw: dict, spec: EpisodeSpec) -> Derived:
    body = IssueEpisodeBody.model_validate(raw)
    issue = body.issue

    people: list[tuple[PersonDict, str]] = [
        ({"github_login": a, "display_name": None, "email": None}, "assignee")
        for a in issue.assignees
        if a
    ]

    text_parts = [
        issue.identifier,
        issue.title,
        issue.description or "",
        issue.state,
        " ".join(issue.labels),
        " ".join(issue.assignees),
    ]

    # "REL-42" is Linear; "owner/name#7" is GitHub. Both collapse onto the issue type.
    is_linear = not issue.identifier.startswith("#") and "#" not in issue.identifier
    source = "linear" if is_linear else "github"
    md: list[str] = [
        f"# {issue.title}",
        "",
        f"[{issue.identifier}]({issue.url}) · {issue.state or 'unknown'}",
        "",
    ]
    if issue.assignees:
        md.append(f"**Assignees:** {', '.join(issue.assignees)}")
    if issue.labels:
        md.append(f"**Labels:** {', '.join(issue.labels)}")
    if issue.description:
        md.extend(["", "## Description", "", issue.description])

    return Derived(
        source=source,
        artifact_type="issue",
        title=issue.title,
        url=issue.url,
        search_text=_join(text_parts),
        markdown="\n".join(md) + "\n",
        people=people,
    )


def _derive_doc(raw: dict, spec: EpisodeSpec) -> Derived:
    body = DocEpisodeBody.model_validate(raw)

    people = [(_person(a), "participant") for a in body.authors if a.login or a.profile_url]
    text_parts = [body.title, body.body or "", " ".join(a.login or "" for a in body.authors)]

    md: list[str] = [f"# {body.title}", "", f"[source]({body.url})", ""]
    if body.authors:
        names = ", ".join(a.login or "unknown" for a in body.authors)
        md.append(f"**Authors:** {names}")
    if body.last_edited_at:
        md.append(f"**Last edited:** {body.last_edited_at}")
    if body.body:
        md.extend(["", body.body])

    return Derived(
        source="notion",
        artifact_type="doc",
        title=body.title,
        url=body.url,
        search_text=_join(text_parts),
        markdown="\n".join(md) + "\n",
        people=people,
    )


def _derive_conversation(raw: dict, spec: EpisodeSpec) -> Derived:
    body = ConversationEpisodeBody.model_validate(raw)
    title = body.title or spec.name

    people = [(_person(p), "participant") for p in body.participants if p.login]
    text_parts = [
        title,
        body.summary or "",
        body.transcript or "",
        " ".join(p.login or "" for p in body.participants),
        " ".join(body.decisions),
        " ".join(a.description for a in body.action_items),
    ]

    md: list[str] = [f"# {title}", "", f"[source]({body.url}) · {body.medium}", ""]
    if body.participants:
        names = ", ".join(p.login or "unknown" for p in body.participants)
        md.append(f"**Participants:** {names}")
    if body.occurred_at:
        md.append(f"**When:** {body.occurred_at}")
    if body.summary:
        md.extend(["", "## Summary", "", body.summary])
    if body.decisions:
        md.extend(["", "## Decisions", ""])
        md.extend(f"- {d}" for d in body.decisions)
    if body.transcript:
        md.extend(["", "## Transcript", "", body.transcript])

    return Derived(
        source="granola" if body.medium == "meeting" else body.medium,
        artifact_type="conversation",
        title=title,
        url=body.url,
        search_text=_join(text_parts),
        markdown="\n".join(md) + "\n",
        people=people,
    )


def _derive_agent_session(raw: dict, spec: EpisodeSpec) -> Derived:
    body = AgentSessionEpisodeBody.model_validate(raw)
    title = body.title or spec.name

    people: list[tuple[PersonDict, str]] = []
    if body.actor and body.actor.login:
        people.append((_person(body.actor), "author"))
    paths = [f.path for f in body.files_touched if f.path]

    cwd_leaf = (body.cwd or "").rstrip("/").rsplit("/", 1)[-1] if body.cwd else ""
    text_parts = [
        title,
        body.summary or "",
        body.transcript or "",
        cwd_leaf,
        " ".join(body.decisions),
        " ".join(paths),
    ]

    md: list[str] = [f"# {title}", "", f"`{body.url}` · agent: {body.agent or 'unknown'}", ""]
    if body.cwd:
        md.append(f"**Working directory:** `{body.cwd}`")
    if body.ended_at:
        md.append(f"**Ended:** {body.ended_at}")
    if body.summary:
        md.extend(["", "## Summary", "", body.summary])
    if paths:
        md.extend(["", "## Files touched", ""])
        md.extend(f"- `{p}`" for p in paths)
    if body.transcript:
        md.extend(["", "## Transcript", "", body.transcript])

    return Derived(
        source="claude-code" if (body.agent or "") == "claude-code" else (body.agent or "agent"),
        artifact_type="agent_session",
        title=title,
        url=body.url,
        search_text=_join(text_parts),
        markdown="\n".join(md) + "\n",
        people=people,
        paths=paths,
    )


_BUILDERS = {
    "pull_request": _derive_pr,
    "issue": _derive_issue,
    "doc": _derive_doc,
    "conversation": _derive_conversation,
    "agent_session": _derive_agent_session,
}


# --- Helpers ------------------------------------------------------------------


def _person(ref: PersonRef) -> PersonDict:
    return {"github_login": ref.login, "display_name": None, "email": None}


def _join(parts: list[str]) -> str:
    """Newline-join the non-empty parts, collapsing internal runs of whitespace."""
    return "\n".join(" ".join(p.split()) for p in parts if p and p.strip())


def _flatten(value: object, depth: int = 0) -> str:
    """Best-effort text of an unknown JSON body, for the degraded row."""
    if depth > 3:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten(v, depth + 1) for v in value)
    return ""


def _source_of(source_description: str) -> str:
    """Best-effort source slug from the spec's free-text source description."""
    lowered = source_description.lower()
    for known in ("github", "linear", "granola", "notion", "claude-code"):
        if known in lowered:
            return known
    return "unknown"
