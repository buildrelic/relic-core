"""Granola connector: two-phase list->hydrate fetch, windowing, and note parsing.

Drives a fake Granola client (dict-in / dict-out, no network) so the fetch logic is
pinned without hitting the API. Mirrors test_github_fetch.py's fake-client style.
"""

from datetime import UTC, datetime
from typing import Any, cast

from relic.ingest.granola import (
    GranolaClient,
    _collect_meetings,
    _flatten_transcript,
    _note_to_rec,
    _participant_names,
)

CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


def _summary(note_id: str, *, updated: str = "2026-06-22T15:48:02Z") -> dict[str, Any]:
    """A lean NoteSummary, as the list endpoint returns it (no url/content)."""
    return {
        "id": note_id,
        "object": "note",
        "title": f"Meeting {note_id}",
        "owner": {"name": "Zidan Kazi", "email": "zidan@tryrelic.io"},
        "created_at": "2026-06-22T15:00:11Z",
        "updated_at": updated,
    }


def _list_block(
    summaries: list[dict[str, Any]], *, next_cursor: str | None = None
) -> dict[str, Any]:
    return {"notes": summaries, "hasMore": next_cursor is not None, "cursor": next_cursor}


def _full_note(
    note_id: str,
    *,
    web_url: str | None = "https://notes.granola.ai/d/uuid",
    owner_email: str = "zidan@tryrelic.io",
    attendees: list[dict[str, Any]] | None = None,
    transcript: list[dict[str, Any]] | None = None,
    summary_markdown: str | None = "## Decisions\n- ship it",
    summary_text: str | None = "ship it",
    scheduled_start: str | None = "2026-06-22T15:00:00Z",
) -> dict[str, Any]:
    note: dict[str, Any] = {
        "id": note_id,
        "object": "note",
        "title": f"Meeting {note_id}",
        "owner": {"name": "Zidan Kazi", "email": owner_email},
        "created_at": "2026-06-22T15:00:11Z",
        "updated_at": "2026-06-22T15:48:02Z",
        "web_url": web_url,
        "attendees": attendees
        if attendees is not None
        else [{"name": "Zidan Kazi", "email": "zidan@tryrelic.io"}],
        "summary_text": summary_text,
        "summary_markdown": summary_markdown,
        "transcript": transcript or [],
    }
    if scheduled_start is not None:
        note["calendar_event"] = {
            "event_title": f"Meeting {note_id}",
            "scheduled_start_time": scheduled_start,
        }
    return note


class _FakeGranola:
    """Canned list pages (keyed by cursor) plus a note store for hydration.

    ``notes`` maps note_id -> full note (or omits an id to exercise the 404/skip path).
    Records the calls so the windowing/pagination/limit assertions can inspect them.
    """

    def __init__(
        self, pages: dict[str | None, dict[str, Any]], notes: dict[str, dict[str, Any]]
    ) -> None:
        self.pages = pages
        self.notes = notes
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    async def list_notes(
        self, *, cursor: str | None = None, updated_after: str | None = None, page_size: int = 30
    ) -> dict[str, Any]:
        self.list_calls.append(
            {"cursor": cursor, "updated_after": updated_after, "page_size": page_size}
        )
        return self.pages[cursor]

    async def get_note(
        self, note_id: str, *, include_transcript: bool = True
    ) -> dict[str, Any] | None:
        self.get_calls.append(note_id)
        return self.notes.get(note_id)  # None -> the 404/skip path


# --- list -> hydrate ---------------------------------------------------------


async def test_lists_then_hydrates_each_note() -> None:
    client = _FakeGranola(
        {None: _list_block([_summary("not_a"), _summary("not_b")])},
        {
            "not_a": _full_note("not_a", web_url="https://notes.granola.ai/d/a"),
            "not_b": _full_note("not_b"),
        },
    )
    meetings = await _collect_meetings(cast("GranolaClient", client), cutoff=CUTOFF, limit=None)
    assert [m.id for m in meetings] == ["not_a", "not_b"]
    assert client.get_calls == ["not_a", "not_b"]  # the lean index forces a hydrate per note
    assert meetings[0].url == "https://notes.granola.ai/d/a"  # permalink only comes from hydrate


async def test_updated_after_window_is_passed_server_side() -> None:
    client = _FakeGranola({None: _list_block([_summary("not_a")])}, {"not_a": _full_note("not_a")})
    await _collect_meetings(cast("GranolaClient", client), cutoff=CUTOFF, limit=None)
    # The live API rejects isoformat()'s "+00:00" offset + microseconds as an "Invalid date";
    # it wants RFC3339 with a Z suffix at second precision. Pin the exact wire format.
    assert client.list_calls[0]["updated_after"] == "2026-01-01T00:00:00Z"


async def test_pagination_follows_the_cursor() -> None:
    client = _FakeGranola(
        {
            None: _list_block([_summary("not_a")], next_cursor="c1"),
            "c1": _list_block([_summary("not_b")]),
        },
        {"not_a": _full_note("not_a"), "not_b": _full_note("not_b")},
    )
    meetings = await _collect_meetings(cast("GranolaClient", client), cutoff=CUTOFF, limit=None)
    assert [m.id for m in meetings] == ["not_a", "not_b"]
    assert [c["cursor"] for c in client.list_calls] == [None, "c1"]


async def test_limit_caps_meetings_and_stops_hydrating() -> None:
    client = _FakeGranola(
        {None: _list_block([_summary("not_a"), _summary("not_b"), _summary("not_c")])},
        {"not_a": _full_note("not_a"), "not_b": _full_note("not_b"), "not_c": _full_note("not_c")},
    )
    meetings = await _collect_meetings(cast("GranolaClient", client), cutoff=CUTOFF, limit=2)
    assert [m.id for m in meetings] == ["not_a", "not_b"]
    assert client.get_calls == ["not_a", "not_b"]  # never hydrates beyond the limit


async def test_note_without_summary_is_skipped_not_fatal() -> None:
    # not_b is in the index but 404s on hydrate (lost its summary) -> skip, keep going.
    client = _FakeGranola(
        {None: _list_block([_summary("not_a"), _summary("not_b"), _summary("not_c")])},
        {"not_a": _full_note("not_a"), "not_c": _full_note("not_c")},
    )
    meetings = await _collect_meetings(cast("GranolaClient", client), cutoff=CUTOFF, limit=None)
    assert [m.id for m in meetings] == ["not_a", "not_c"]


# --- note normalization ------------------------------------------------------


async def test_note_to_rec_normalizes_fields() -> None:
    note = _full_note(
        "not_x",
        web_url="https://notes.granola.ai/d/x",
        attendees=[
            {"name": "Zidan Kazi", "email": "zidan@tryrelic.io"},
            {"name": None, "email": "paris@tryrelic.io"},  # name missing -> email handle
        ],
        transcript=[{"speaker": {"source": "microphone"}, "text": "ship it"}],
    )
    rec = _note_to_rec(note)
    assert rec.id == "not_x"
    assert rec.url == "https://notes.granola.ai/d/x"  # the citation anchor
    assert rec.owner_email == "zidan@tryrelic.io"  # the per-owner scope key
    assert rec.participants == ["Zidan Kazi", "paris@tryrelic.io"]
    assert rec.summary == "## Decisions\n- ship it"  # markdown preferred over plain text
    assert rec.transcript == "microphone: ship it"
    assert rec.occurred_at == "2026-06-22T15:00:00Z"  # scheduled start preferred over created_at


def test_occurred_at_falls_back_to_created_at_without_calendar() -> None:
    rec = _note_to_rec(_full_note("not_y", scheduled_start=None))
    assert rec.occurred_at == "2026-06-22T15:00:11Z"  # no calendar_event -> created_at


def test_flatten_transcript_labels_speakers() -> None:
    segments = [
        {"speaker": {"source": "microphone", "diarization_label": "Zidan"}, "text": "hi"},
        {"speaker": {"source": "speaker"}, "text": "  hello  "},
        {"speaker": {"source": "speaker"}, "text": ""},  # empty -> dropped
    ]
    assert (
        _flatten_transcript(segments) == "Zidan: hi\nspeaker: hello"
    )  # label preferred over source
    assert _flatten_transcript([]) is None
    assert _flatten_transcript(None) is None


def test_participant_names_dedupes_and_drops_empties() -> None:
    note = {
        "attendees": [
            {"name": "Zidan Kazi", "email": "zidan@tryrelic.io"},
            {"name": "Zidan Kazi", "email": "zidan@tryrelic.io"},  # dup
            {"name": None, "email": None},  # empty -> dropped
        ]
    }
    assert _participant_names(note) == ["Zidan Kazi"]
