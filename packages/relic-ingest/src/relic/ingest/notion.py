"""Notion connector: REST pulls of shared pages (guarded behind NOTION_API_KEY).

Notion exposes a per-workspace REST API (``Authorization: Bearer <token>`` plus the
required ``Notion-Version`` header). An internal integration only sees pages that have
been shared with it, so ingest is two-phase per page: list pages (``POST /v1/search``,
filtered to objects of type ``page``, sorted most-recently-edited first and windowed
client-side to the last N months) then hydrate each page's block children
(``GET /v1/blocks/{id}/children``) into flattened plain text. Each page becomes one
Conversation episode; its Notion ``url`` is the recall citation, and the workspace
(``GET /v1/users/me`` ``bot.workspace_id``) is the graph scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from relic.ingest.mappers import PageRec

if TYPE_CHECKING:
    from relic.config import Settings

_BASE_URL = "https://api.notion.com/v1"
# Required on every request; the API 400s without it. Pinned to the version the shapes
# below were verified against, so a server-side default bump can't silently reshape us.
_NOTION_VERSION = "2022-06-28"
_DEFAULT_MONTHS = 12
_DAYS_PER_MONTH = 30.44
# Notion caps page_size at 100 for the list/search/children methods; request the max to
# minimize round trips.
_PAGE_SIZE = 100
# Bound the per-page block walk: stop descending past this depth and stop fetching once
# the flattened text passes this many chars. The mapper clips the summary further, but
# this keeps the fetch itself bounded (API calls and memory) on a deeply nested page.
_MAX_BLOCK_DEPTH = 3
_MAX_PAGE_TEXT_CHARS = 8000


def notion_enabled(settings: Settings) -> bool:
    """True when a Notion API token is configured."""
    return bool(settings.notion_api_key)


class NotionError(RuntimeError):
    """A Notion REST call returned a non-2xx status."""


class NotionClient:
    """Thin async wrapper over the Notion REST methods the connector needs.

    Kept minimal and injectable so the fetch logic can be tested against a fake. httpx is
    imported lazily (mirroring granola.py/slack.py), so importing this module stays cheap.
    Every request carries the required ``Notion-Version`` header; a non-2xx response is
    surfaced as ``NotionError`` with the API's message.
    """

    def __init__(self, token: str) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": _NOTION_VERSION,
            },
            timeout=30.0,
        )

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> dict[str, Any]:
        resp = await self._client.request(method, path, params=params, json=json)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text
            except Exception:  # noqa: BLE001 - a non-JSON error body still needs surfacing
                detail = resp.text
            raise NotionError(f"{method} {path}: {resp.status_code} {detail}")
        return resp.json()

    async def _paginate_get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect every page of a cursor-paginated GET list into one list."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            extra = {"start_cursor": cursor} if cursor else {}
            page = await self._request("GET", path, params={**params, **extra})
            out.extend(page.get("results", []))
            if not page.get("has_more"):
                return out
            cursor = page.get("next_cursor")
            if not cursor:
                return out

    async def me(self) -> dict[str, Any]:
        """The bot user from ``/users/me`` — carries the workspace id (the graph scope)."""
        return await self._request("GET", "/users/me")

    async def users_map(self) -> dict[str, str]:
        """user id -> display name, to resolve page editors (pages carry only ids)."""
        out: dict[str, str] = {}
        for user in await self._paginate_get("/users", {"page_size": _PAGE_SIZE}):
            uid = user.get("id")
            name = user.get("name")
            if uid and name:
                out[uid] = name
        return out

    async def search_pages(self, *, cursor: str | None = None) -> dict[str, Any]:
        """One page of the page index, most-recently-edited first.

        Filters to objects of type ``page`` (databases are excluded) and sorts descending
        by ``last_edited_time`` so the caller can window client-side and stop early.
        """
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": _PAGE_SIZE,
        }
        if cursor:
            body["start_cursor"] = cursor
        return await self._request("POST", "/search", json=body)

    async def block_children(self, block_id: str) -> list[dict[str, Any]]:
        """Every direct child block of a page or block, in order (cursor-paginated)."""
        return await self._paginate_get(f"/blocks/{block_id}/children", {"page_size": _PAGE_SIZE})


async def fetch_pages(
    token: str, *, months: int = _DEFAULT_MONTHS, limit: int | None = None
) -> list[PageRec]:
    """Fetch recent Notion pages shared with the integration, mapped into ``PageRec``.

    Windowed to the last ``months`` (mirrors the GitHub/Granola/Slack cutoff). Lists pages
    most-recently-edited first, hydrates each page's blocks into flattened text, and
    resolves editor ids to display names. ``limit`` caps the number of pages.
    """
    cutoff = datetime.now(UTC) - timedelta(days=round(months * _DAYS_PER_MONTH))
    async with NotionClient(token) as client:
        return await _collect_pages(client, cutoff=cutoff, limit=limit)


async def _collect_pages(
    client: NotionClient, *, cutoff: datetime, limit: int | None
) -> list[PageRec]:
    """List pages windowed by ``cutoff``, hydrate each, return normalized records.

    Two-phase because search omits the page content: search is sorted most-recently-edited
    first, so the first page older than ``cutoff`` means every remaining page is older too
    and the walk stops. Archived/trashed pages are skipped. ``limit`` short-circuits the
    walk so no more than ``limit`` pages are ever hydrated.
    """
    scope = _workspace_scope(await client.me())
    users = await client.users_map()
    out: list[PageRec] = []
    cursor: str | None = None
    while True:
        block = await client.search_pages(cursor=cursor)
        for page in block.get("results", []):
            page_id = page.get("id")
            if not page_id or _is_archived(page):
                continue
            if _older_than_cutoff(page.get("last_edited_time"), cutoff):
                return out
            text = await _page_text(client, page_id)
            out.append(_page_to_rec(page, workspace_id=scope, text=text, users=users))
            if limit is not None and len(out) >= limit:
                return out
        if not block.get("has_more"):
            return out
        cursor = block.get("next_cursor")
        if not cursor:
            return out


def _workspace_scope(me: dict[str, Any]) -> str | None:
    """The workspace id (the per-workspace graph scope), falling back to the bot id.

    A page belongs to a workspace, so the partition is ``bot.workspace_id`` when present;
    the bot's own id is a stable fallback for a token that omits it.
    """
    bot = me.get("bot") or {}
    return bot.get("workspace_id") or me.get("id")


def _is_archived(page: dict[str, Any]) -> bool:
    """True for an archived or trashed page (excluded from ingest)."""
    return bool(page.get("archived") or page.get("in_trash") or page.get("is_archived"))


def _older_than_cutoff(last_edited: str | None, cutoff: datetime) -> bool:
    """True when a page's last-edited time is before the window; False if unknown/unparseable.

    Unparseable or missing timestamps never stop the walk (they are kept), so a malformed
    time can't silently truncate the backfill.
    """
    if not last_edited:
        return False
    try:
        parsed = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < cutoff


async def _page_text(client: NotionClient, page_id: str) -> str | None:
    """Flatten a page's block children (recursively, bounded) into plain prose lines."""
    lines: list[str] = []
    await _walk_blocks(client, page_id, lines, depth=0)
    text = "\n".join(lines).strip()
    return text or None


async def _walk_blocks(
    client: NotionClient, block_id: str, lines: list[str], *, depth: int
) -> None:
    """Depth-first walk of a block's children, appending each block's text to ``lines``.

    Bounded by ``_MAX_BLOCK_DEPTH`` and ``_MAX_PAGE_TEXT_CHARS`` so a deeply nested or huge
    page can't blow up the fetch; the mapper clips the assembled text further.
    """
    if _lines_len(lines) >= _MAX_PAGE_TEXT_CHARS:
        return
    for block in await client.block_children(block_id):
        line = _block_line(block)
        if line:
            lines.append(line)
            if _lines_len(lines) >= _MAX_PAGE_TEXT_CHARS:
                return
        child_id = block.get("id")
        if block.get("has_children") and child_id and depth < _MAX_BLOCK_DEPTH:
            await _walk_blocks(client, child_id, lines, depth=depth + 1)
            if _lines_len(lines) >= _MAX_PAGE_TEXT_CHARS:
                return


def _lines_len(lines: list[str]) -> int:
    return sum(len(line) for line in lines)


def _rich_text_to_plain(rich: list[dict[str, Any]] | None) -> str:
    """Concatenate a rich_text array's ``plain_text`` fragments into one line."""
    if not rich:
        return ""
    return "".join(fragment.get("plain_text", "") for fragment in rich).strip()


def _block_line(block: dict[str, Any]) -> str:
    """One block's plain text: its ``rich_text`` (text-bearing blocks) or a child title.

    Every text-bearing block type (paragraph, headings, list items, to_do, toggle, quote,
    callout, code) nests its text under ``block[type].rich_text``, so the extraction is
    generic; non-text blocks (divider, image, table, ...) yield an empty line and are
    dropped. ``child_page``/``child_database`` carry a bare ``title`` string instead.
    """
    btype = block.get("type")
    if not btype:
        return ""
    payload = block.get(btype)
    if not isinstance(payload, dict):
        return ""
    rich = payload.get("rich_text")
    if isinstance(rich, list):
        return _rich_text_to_plain(rich)
    title = payload.get("title")
    return title.strip() if isinstance(title, str) else ""


def _page_title(page: dict[str, Any]) -> str | None:
    """The page title: the value of its ``type == "title"`` property (any property name).

    A page's title lives in ``properties`` under a property whose ``type`` is ``title`` —
    named "title" on a workspace/child page, but the database's own column name on a
    database row — so it is found by type, not name.
    """
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title")) or None
    return None


def _person_name(partial: dict[str, Any] | None, users: dict[str, str]) -> str | None:
    """Resolve a partial user object (``{object, id}``) to a display name, else its id."""
    uid = (partial or {}).get("id")
    if not uid:
        return None
    return users.get(uid) or uid


def _participants(page: dict[str, Any], users: dict[str, str]) -> list[str]:
    """The page's editors (creator + last editor) as display names, deduped in order."""
    seen: set[str] = set()
    out: list[str] = []
    for key in ("created_by", "last_edited_by"):
        name = _person_name(page.get(key), users)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _page_to_rec(
    page: dict[str, Any], *, workspace_id: str | None, text: str | None, users: dict[str, str]
) -> PageRec:
    """Normalize a Notion page (+ its flattened text) into a ``PageRec``. Pure, defensive."""
    return PageRec(
        id=page.get("id", ""),
        title=_page_title(page),
        # url is the permalink recall cites; always present on a real page object.
        url=page.get("url", ""),
        workspace_id=workspace_id,
        participants=_participants(page, users),
        text=text,
        created_at=page.get("created_time"),
        last_edited_at=page.get("last_edited_time"),
        raw=page,
    )
