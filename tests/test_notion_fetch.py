"""Notion fetch: search -> hydrate page blocks, against a fake client.

Drives `_collect_pages` with an injected fake (no network), asserting editor resolution,
block flattening (incl. nested recursion), the workspace scope, archived-page skipping, the
last-edited window (descending stop), and the `limit` short-circuit. Also unit-tests the pure
parsing helpers. Mirrors test_granola_fetch.py / test_slack_fetch.py.
"""

from datetime import UTC, datetime
from typing import Any, cast

from relic.ingest.notion import (
    NotionClient,
    _block_line,
    _collect_pages,
    _is_archived,
    _older_than_cutoff,
    _page_title,
    _participants,
    _rich_text_to_plain,
    _workspace_scope,
)

CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text}]


def _page_obj(
    page_id: str,
    *,
    title: str = "Doc",
    title_prop: str = "title",
    url: str | None = None,
    last_edited: str = "2026-06-22T15:48:02.000Z",
    created: str = "2026-06-22T15:00:11.000Z",
    created_by: str = "u1",
    last_edited_by: str = "u2",
    archived: bool = False,
) -> dict[str, Any]:
    return {
        "id": page_id,
        "object": "page",
        "url": url or f"https://www.notion.so/{page_id}",
        "created_time": created,
        "last_edited_time": last_edited,
        "created_by": {"object": "user", "id": created_by},
        "last_edited_by": {"object": "user", "id": last_edited_by},
        "archived": archived,
        "properties": {title_prop: {"id": "t", "type": "title", "title": _rich(title)}},
    }


def _search_block(pages: list[dict[str, Any]], *, next_cursor: str | None = None) -> dict[str, Any]:
    return {"results": pages, "has_more": next_cursor is not None, "next_cursor": next_cursor}


def _text_block(
    btype: str, text: str, *, block_id: str = "b", has_children: bool = False
) -> dict[str, Any]:
    return {
        "id": block_id,
        "type": btype,
        btype: {"rich_text": _rich(text)},
        "has_children": has_children,
    }


_ME = {
    "id": "bot-1",
    "object": "user",
    "type": "bot",
    "bot": {"workspace_id": "ws-1", "workspace_name": "Acme"},
}


class _FakeNotion:
    """Serves canned search pages (keyed by cursor), a users map, and block children."""

    def __init__(
        self,
        *,
        pages: dict[str | None, dict[str, Any]],
        children: dict[str, list[dict[str, Any]]] | None = None,
        users: dict[str, str] | None = None,
        me: dict[str, Any] | None = None,
    ) -> None:
        self._pages = pages
        self._children = children or {}
        self._users = users or {"u1": "Zidan Kazi", "u2": "Paris Phan"}
        self._me = me or _ME
        self.search_calls: list[str | None] = []
        self.children_calls: list[str] = []

    async def me(self) -> dict[str, Any]:
        return self._me

    async def users_map(self) -> dict[str, str]:
        return self._users

    async def search_pages(self, *, cursor: str | None = None) -> dict[str, Any]:
        self.search_calls.append(cursor)
        return self._pages[cursor]

    async def block_children(self, block_id: str) -> list[dict[str, Any]]:
        self.children_calls.append(block_id)
        return self._children.get(block_id, [])


# --- search -> hydrate -------------------------------------------------------


async def test_lists_then_hydrates_each_page() -> None:
    client = _FakeNotion(
        pages={None: _search_block([_page_obj("pa"), _page_obj("pb")])},
        children={
            "pa": [_text_block("paragraph", "hello from a")],
            "pb": [_text_block("heading_1", "B title")],
        },
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=None)
    assert [p.id for p in pages] == ["pa", "pb"]
    assert pages[0].url == "https://www.notion.so/pa"  # citation anchor
    assert pages[0].text == "hello from a"  # blocks flattened to plain text
    assert pages[0].workspace_id == "ws-1"  # scope from users/me
    # editor ids resolved to display names (created_by=u1, last_edited_by=u2)
    assert pages[0].participants == ["Zidan Kazi", "Paris Phan"]
    assert client.children_calls == ["pa", "pb"]  # each page hydrated


async def test_older_than_cutoff_stops_the_walk() -> None:
    # search is sorted last_edited desc, so once a page predates the window the walk stops.
    client = _FakeNotion(
        pages={
            None: _search_block(
                [
                    _page_obj("recent", last_edited="2026-06-01T00:00:00.000Z"),
                    _page_obj("old", last_edited="2025-06-01T00:00:00.000Z"),
                    _page_obj("older", last_edited="2024-01-01T00:00:00.000Z"),
                ]
            )
        },
        children={"recent": [_text_block("paragraph", "x")]},
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=None)
    assert [p.id for p in pages] == ["recent"]  # stops at the first pre-cutoff page
    assert client.children_calls == ["recent"]  # never hydrates past the window


async def test_archived_page_is_skipped_not_fatal() -> None:
    client = _FakeNotion(
        pages={None: _search_block([_page_obj("arch", archived=True), _page_obj("live")])},
        children={"live": [_text_block("paragraph", "kept")]},
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=None)
    assert [p.id for p in pages] == ["live"]


async def test_pagination_follows_the_cursor() -> None:
    client = _FakeNotion(
        pages={
            None: _search_block([_page_obj("pa")], next_cursor="c1"),
            "c1": _search_block([_page_obj("pb")]),
        },
        children={"pa": [_text_block("paragraph", "a")], "pb": [_text_block("paragraph", "b")]},
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=None)
    assert [p.id for p in pages] == ["pa", "pb"]
    assert client.search_calls == [None, "c1"]


async def test_limit_caps_pages_and_stops_hydrating() -> None:
    client = _FakeNotion(
        pages={None: _search_block([_page_obj("pa"), _page_obj("pb"), _page_obj("pc")])},
        children={"pa": [_text_block("paragraph", "a")]},
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=2)
    assert [p.id for p in pages] == ["pa", "pb"]
    assert client.children_calls == ["pa", "pb"]  # never hydrates beyond the limit


async def test_nested_blocks_are_walked() -> None:
    # a page whose top block has children: the nested text is folded in.
    client = _FakeNotion(
        pages={None: _search_block([_page_obj("pa")])},
        children={
            "pa": [_text_block("toggle", "top", block_id="t1", has_children=True)],
            "t1": [_text_block("paragraph", "nested")],
        },
    )
    pages = await _collect_pages(cast("NotionClient", client), cutoff=CUTOFF, limit=None)
    assert pages[0].text == "top\nnested"
    assert client.children_calls == ["pa", "t1"]


# --- pure helpers ------------------------------------------------------------


def test_workspace_scope_prefers_workspace_id_then_bot_id() -> None:
    assert _workspace_scope(_ME) == "ws-1"
    # no workspace_id -> fall back to the bot's own id
    assert _workspace_scope({"id": "bot-9", "bot": {}}) == "bot-9"


def test_is_archived_covers_archived_and_trash() -> None:
    assert _is_archived({"archived": True}) is True
    assert _is_archived({"in_trash": True}) is True
    assert _is_archived({"archived": False}) is False


def test_older_than_cutoff_handles_unknown_times() -> None:
    assert _older_than_cutoff("2025-06-01T00:00:00.000Z", CUTOFF) is True
    assert _older_than_cutoff("2026-06-01T00:00:00.000Z", CUTOFF) is False
    # a missing or unparseable time never stops the walk
    assert _older_than_cutoff(None, CUTOFF) is False
    assert _older_than_cutoff("not-a-date", CUTOFF) is False


def test_page_title_found_by_type_not_name() -> None:
    # a database row's title property is the column name, not literally "title".
    page = _page_obj("p", title="Row title", title_prop="Name")
    assert _page_title(page) == "Row title"
    # a page with no title property degrades to None
    assert _page_title({"properties": {"Tags": {"type": "multi_select"}}}) is None


def test_block_line_extracts_text_and_child_titles() -> None:
    assert _block_line(_text_block("paragraph", "para")) == "para"
    assert _block_line(_text_block("code", "print(1)")) == "print(1)"
    # child_page carries a bare title string, not rich_text
    assert _block_line({"type": "child_page", "child_page": {"title": "Sub page"}}) == "Sub page"
    # a non-text block (divider) yields nothing
    assert _block_line({"type": "divider", "divider": {}}) == ""


def test_rich_text_to_plain_concatenates_fragments() -> None:
    rich = [{"plain_text": "hello "}, {"plain_text": "world"}]
    assert _rich_text_to_plain(rich) == "hello world"
    assert _rich_text_to_plain([]) == ""
    assert _rich_text_to_plain(None) == ""


def test_participants_dedupe_and_fall_back_to_id() -> None:
    users = {"u1": "Zidan Kazi"}
    # same editor for create + edit collapses to one; an unresolved id falls back to the id.
    page = _page_obj("p", created_by="u1", last_edited_by="u1")
    assert _participants(page, users) == ["Zidan Kazi"]
    page2 = _page_obj("p", created_by="u1", last_edited_by="u9")
    assert _participants(page2, users) == ["Zidan Kazi", "u9"]
