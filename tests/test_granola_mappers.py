"""Granola mapper: MeetingRec -> Conversation EpisodeSpec, scope key, body budget.

Builds MeetingRec records directly (no fetch) and asserts the EpisodeSpec envelope and
the parsed body, mirroring test_mappers.py. Also round-trips the shipped raw fixtures
through the full normalize->map path so a realistic payload is pinned end to end.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from relic.ingest.granola import _note_to_rec
from relic.ingest.mappers import (
    _MAX_BODY_CHARS,
    MeetingRec,
    granola_group_id,
    meeting_to_episode,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "granola"


def _meeting(**overrides: object) -> MeetingRec:
    base: dict[str, object] = {
        "id": "not_teamsync01a2b3",
        "title": "Relic weekly sync",
        "url": "https://notes.granola.ai/d/abc",
        "owner_email": "zidan@tryrelic.io",
        "participants": ["Zidan Kazi", "Paris Phan"],
        "summary": "## Decisions\n- ship the connector",
        "transcript": "Zidan: ship it\nParis: lgtm",
        "occurred_at": "2026-06-22T15:00:00Z",
        "created_at": "2026-06-22T15:00:11Z",
        "updated_at": "2026-06-22T15:48:02Z",
    }
    base.update(overrides)
    return MeetingRec(**base)  # type: ignore[arg-type]


def test_granola_group_id_is_per_owner() -> None:
    assert granola_group_id("zidan@tryrelic.io") == "granola__zidan_tryrelic_io"
    # Always Graphiti-legal (^[A-Za-z0-9_-]+$), even with dots and the @.
    assert re.fullmatch(r"[A-Za-z0-9_-]+", granola_group_id("paris@tryrelic.io"))
    # A missing owner falls back to one stable bucket rather than a crash or empty key.
    assert granola_group_id(None) == "granola__unknown"


def test_meeting_to_episode_shape_and_scope() -> None:
    spec = meeting_to_episode(_meeting())
    assert spec.name == "Meeting not_teamsync01a2b3"  # stable dedup key, no version
    assert spec.source_description == "granola meeting"
    assert spec.group_id == "granola__zidan_tryrelic_io"  # per-owner partition
    assert spec.schema_version == 2
    assert spec.reference_time == datetime(2026, 6, 22, 15, 0, tzinfo=UTC)

    body = json.loads(spec.body)
    assert body["schema_version"] == 2
    assert body["source_type"] == "conversation"
    assert body["medium"] == "meeting"
    assert body["url"] == "https://notes.granola.ai/d/abc"  # recall citation anchor
    assert body["title"] == "Relic weekly sync"
    assert [p["login"] for p in body["participants"]] == ["Zidan Kazi", "Paris Phan"]
    assert body["summary"] == "## Decisions\n- ship the connector"
    assert body["transcript"] == "Zidan: ship it\nParis: lgtm"
    assert body["last_edited_at"] == "2026-06-22T15:48:02Z"  # the freshness token (REL-118)


def test_reference_time_falls_back_to_created_at() -> None:
    spec = meeting_to_episode(_meeting(occurred_at=None))
    assert spec.reference_time == datetime(2026, 6, 22, 15, 0, 11, tzinfo=UTC)


def test_long_transcript_is_trimmed_to_budget() -> None:
    spec = meeting_to_episode(_meeting(transcript="word " * 20000))
    # The whole serialized body stays under the per-episode token-proxy ceiling.
    assert len(spec.body) <= _MAX_BODY_CHARS
    body = json.loads(spec.body)
    # The summary (the mined signal) survives even when the transcript is shed.
    assert body["summary"] == "## Decisions\n- ship the connector"


def test_edited_note_moves_the_freshness_token() -> None:
    # REL-118 edit-reingest: the loader supersedes on a changed body fingerprint
    # (relic.engram.load._content_token). An edit bumps the note's updated_at, which
    # rides in the body, so the token moves even when the clipped summary/transcript
    # are unchanged; a no-op re-capture maps to identical bytes and is skipped.
    from relic.engram.load import _content_token

    v1 = meeting_to_episode(_meeting())
    edited = meeting_to_episode(_meeting(updated_at="2026-06-23T09:00:00Z"))
    recaptured = meeting_to_episode(_meeting())
    assert edited.name == v1.name  # same dedup key: supersede, never fork
    assert _content_token(edited) != _content_token(v1)
    assert _content_token(recaptured) == _content_token(v1)


def test_missing_url_still_validates() -> None:
    # web_url can be absent on an unshareable note; the body is required to carry a url,
    # so it degrades to "" rather than failing to construct.
    spec = meeting_to_episode(_meeting(url=""))
    assert json.loads(spec.body)["url"] == ""


def test_fixtures_round_trip_with_citation_and_owner_scope() -> None:
    cases = {
        "not_teamsync01a2b3.json": "granola__zidan_tryrelic_io",
        "not_designrev0001x.json": "granola__paris_tryrelic_io",
    }
    for filename, expected_group in cases.items():
        note = json.loads((_FIXTURES / filename).read_text())
        spec = meeting_to_episode(_note_to_rec(note))
        assert spec.group_id == expected_group  # per-owner scope, derived from the payload
        body = json.loads(spec.body)
        assert body["medium"] == "meeting"
        assert body["url"] == note["web_url"]  # citation survives the full path
        assert body["url"].startswith("https://notes.granola.ai/d/")
        assert body["participants"]  # attendees normalized onto the body
        assert body["transcript"]  # speaker-attributed segments flattened to prose
        assert body["last_edited_at"] == note["updated_at"]  # freshness survives the full path
