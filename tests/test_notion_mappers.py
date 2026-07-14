"""Notion mapper: PageRec -> Doc EpisodeSpec (doc_type="other"), scope, body budget.

Builds PageRec records directly (no fetch) and asserts the EpisodeSpec envelope and the
parsed body, mirroring test_granola_mappers.py / test_slack_mappers.py.
"""

import json
import re
from datetime import UTC, datetime

from relic.ingest.mappers import (
    _MAX_BODY_CHARS,
    PageRec,
    notion_group_id,
    page_to_episode,
)


def _page(**overrides: object) -> PageRec:
    base: dict[str, object] = {
        "id": "1a2b3c4d-5e6f-7080-90a0-b0c0d0e0f000",
        "title": "Relic architecture notes",
        "url": "https://www.notion.so/Relic-architecture-notes-1a2b3c",
        "workspace_id": "11112222-3333-4444-5555-666677778888",
        "participants": ["Zidan Kazi", "Paris Phan"],
        "text": "## Decisions\n- ship the notion connector",
        "created_at": "2026-06-22T15:00:11.000Z",
        "last_edited_at": "2026-06-22T15:48:02.000Z",
    }
    base.update(overrides)
    return PageRec(**base)  # type: ignore[arg-type]


def test_notion_group_id_is_per_workspace() -> None:
    assert notion_group_id("11112222-3333-4444-5555-666677778888") == (
        "notion__11112222-3333-4444-5555-666677778888"
    )
    # Always Graphiti-legal (^[A-Za-z0-9_-]+$); a workspace uuid's dashes are already legal.
    assert re.fullmatch(r"[A-Za-z0-9_-]+", notion_group_id("ws-abc"))
    # A missing workspace falls back to one stable bucket rather than a crash or empty key.
    assert notion_group_id(None) == "notion__unknown"


def test_page_to_episode_shape_and_scope() -> None:
    spec = page_to_episode(_page())
    assert spec.name == "Notion 1a2b3c4d-5e6f-7080-90a0-b0c0d0e0f000"  # stable page dedup key
    assert spec.source_description == "notion page"
    assert spec.group_id == "notion__11112222-3333-4444-5555-666677778888"  # per-workspace
    assert spec.schema_version == 2
    # anchored to the last-edited time, not created time
    assert spec.reference_time == datetime(2026, 6, 22, 15, 48, 2, tzinfo=UTC)

    body = json.loads(spec.body)
    assert body["schema_version"] == 2
    assert body["source_type"] == "doc"
    assert body["doc_type"] == "other"
    # citation anchor stays the top-level url (recall._extract_url falls back to it)
    assert body["url"] == "https://www.notion.so/Relic-architecture-notes-1a2b3c"
    assert body["title"] == "Relic architecture notes"
    assert [p["login"] for p in body["authors"]] == ["Zidan Kazi", "Paris Phan"]
    assert body["body"] == "## Decisions\n- ship the notion connector"
    assert body["last_edited_at"] == "2026-06-22T15:48:02.000Z"
    # no immutable snapshot key from Notion; supersession is the checkpoint layer's job
    assert body["version"] is None


def test_reference_time_falls_back_to_created_at() -> None:
    spec = page_to_episode(_page(last_edited_at=None))
    assert spec.reference_time == datetime(2026, 6, 22, 15, 0, 11, tzinfo=UTC)
    # last_edited_at in the body follows the same fallback
    assert json.loads(spec.body)["last_edited_at"] == "2026-06-22T15:00:11.000Z"


def test_untitled_page_still_validates() -> None:
    # the doc body requires a title, so an untitled page degrades to "" rather than
    # failing to construct (or inventing a title).
    body = json.loads(page_to_episode(_page(title=None)).body)
    assert body["title"] == ""


def test_long_text_is_trimmed_to_budget() -> None:
    spec = page_to_episode(_page(text="word " * 20000))
    # The whole serialized body stays under the per-episode token-proxy ceiling (the
    # flattened text lands in body, clipped before assembly then halved to fit).
    assert len(spec.body) <= _MAX_BODY_CHARS
    assert json.loads(spec.body)["body"]  # some text survives the clip, never dropped


def test_missing_url_still_validates() -> None:
    # a page url should always be present, but the body requires a url, so an empty one
    # degrades to "" rather than failing to construct.
    spec = page_to_episode(_page(url=""))
    assert json.loads(spec.body)["url"] == ""


def test_no_participants_context_is_bare() -> None:
    body = json.loads(page_to_episode(_page(participants=[])).body)
    assert body["authors"] == []
    assert body["context"] == "notion page"  # no ", N editors" suffix


def test_edited_page_moves_the_freshness_token() -> None:
    # REL-118 edit-reingest falls out naturally for notion: the page's last_edited_time
    # already rides in the body, so the loader's content fingerprint
    # (relic.engram.load._content_token, sha256 of the body bytes) moves on any edit and
    # the stale episode is superseded; an unchanged re-capture maps to identical bytes
    # and is skipped. Deliberately asserts on body bytes, not a named field, so the pin
    # holds across body-shape changes (conversation today, doc-shaped later).
    v1 = page_to_episode(_page())
    edited = page_to_episode(_page(last_edited_at="2026-06-23T09:00:00Z"))
    recaptured = page_to_episode(_page())
    assert edited.name == v1.name  # same dedup key: supersede, never fork
    assert edited.body != v1.body
    assert recaptured.body == v1.body
