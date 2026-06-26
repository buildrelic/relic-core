"""Granola connector: REST pulls of meeting notes (guarded behind GRANOLA_API_KEY).

Granola exposes a per-user, read-only public API (``Authorization: Bearer grn_...``)
documented at docs.granola.ai. The note index is deliberately lean -- no permalink,
no content -- so ingest is two-phase: list note ids (cheap, windowed server-side by
``updated_after``) then hydrate each note (``GET /v1/notes/{id}?include=transcript``)
for its ``web_url`` (the recall citation), summary, attendees, and speaker-attributed
transcript. Hydration is sequential to stay under the API's ~5 req/s budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from relic.ingest.mappers import MeetingRec

if TYPE_CHECKING:
    from relic.config import Settings

_BASE_URL = "https://public-api.granola.ai/v1"
_DEFAULT_MONTHS = 12
_DAYS_PER_MONTH = 30.44
# Granola caps page_size at 30 (docs); request the max to minimize list round trips.
_PAGE_SIZE = 30


def granola_enabled(settings: Settings) -> bool:
    """True when a Granola API key is configured."""
    return bool(settings.granola_api_key)


class GranolaClient:
    """Thin async wrapper over Granola's two read endpoints.

    Kept minimal and injectable so the fetch logic can be tested against a fake. httpx
    is imported lazily (mirroring linear.py's lazy ``gql`` import), so importing this
    module stays cheap and has no hard client dependency until a fetch actually runs.
    """

    def __init__(self, api_key: str) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def __aenter__(self) -> GranolaClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def list_notes(
        self,
        *,
        cursor: str | None = None,
        updated_after: str | None = None,
        page_size: int = _PAGE_SIZE,
    ) -> dict[str, Any]:
        """One page of the note index: ``{notes: [...], hasMore, cursor}``."""
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        if updated_after:
            params["updated_after"] = updated_after
        resp = await self._client.get("/notes", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_note(
        self, note_id: str, *, include_transcript: bool = True
    ) -> dict[str, Any] | None:
        """Hydrate one note (with transcript). ``None`` if it has no AI summary (404)."""
        params = {"include": "transcript"} if include_transcript else {}
        resp = await self._client.get(f"/notes/{note_id}", params=params)
        # The index only lists summarized notes, but a note can lose its summary between
        # list and hydrate; skip it rather than abort the whole run.
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def fetch_meetings(
    api_key: str, *, months: int = _DEFAULT_MONTHS, limit: int | None = None
) -> list[MeetingRec]:
    """Fetch recent Granola meetings, mapped into ``MeetingRec``.

    Windowed server-side by ``updated_after`` to the last ``months`` (mirrors the
    GitHub cutoff). Lists note ids cheaply, then hydrates each for its permalink and
    content. ``limit`` caps the number of meetings, most-recently-updated first.
    """
    cutoff = datetime.now(UTC) - timedelta(days=round(months * _DAYS_PER_MONTH))
    async with GranolaClient(api_key) as client:
        return await _collect_meetings(client, cutoff=cutoff, limit=limit)


async def _collect_meetings(
    client: GranolaClient, *, cutoff: datetime, limit: int | None
) -> list[MeetingRec]:
    """List ids windowed by ``cutoff``, hydrate each, return normalized records.

    Two-phase because the list endpoint omits the permalink and content. The index is
    walked by cursor; ``updated_after`` does the windowing server-side, so no
    client-side date filter is needed. ``limit`` short-circuits the walk so no more
    than ``limit`` notes are ever hydrated.
    """
    out: list[MeetingRec] = []
    cursor: str | None = None
    updated_after = cutoff.isoformat()
    while True:
        block = await client.list_notes(cursor=cursor, updated_after=updated_after)
        for summary in block.get("notes", []):
            note_id = summary.get("id")
            if not note_id:
                continue
            note = await client.get_note(note_id, include_transcript=True)
            if note is None:
                continue
            out.append(_note_to_rec(note))
            if limit is not None and len(out) >= limit:
                return out
        if not block.get("hasMore"):
            break
        cursor = block.get("cursor")
        if not cursor:
            break
    return out


def _participant_names(note: dict[str, Any]) -> list[str]:
    """Attendee handles for the conversation body: name, falling back to email.

    Granola gives a name + email per attendee, but Relic's ``PersonRef`` has no name
    field, so the natural handle is the display name (or email when a name is missing).
    Order preserved; empties and duplicates dropped.
    """
    seen: set[str] = set()
    out: list[str] = []
    for person in note.get("attendees") or []:
        handle = (person.get("name") or person.get("email") or "").strip()
        if not handle or handle in seen:
            continue
        seen.add(handle)
        out.append(handle)
    return out


def _flatten_transcript(segments: list[dict[str, Any]] | None) -> str | None:
    """Render speaker-attributed transcript segments to plain prose lines.

    Granola returns the transcript as time-stamped segments, each tagged with a speaker
    ``source`` (microphone vs speaker) or, on iOS, a ``diarization_label``. Collapse to
    ``"<speaker>: <text>"`` lines so the extractor reads clean conversation prose. The
    summary and ``participants`` carry the named identities; this is the evidence they
    cite. Clipping to the body budget happens in the mapper.
    """
    if not segments:
        return None
    lines: list[str] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or {}
        label = (speaker.get("diarization_label") or speaker.get("source") or "speaker").strip()
        lines.append(f"{label}: {text}")
    return "\n".join(lines) or None


def _note_to_rec(note: dict[str, Any]) -> MeetingRec:
    """Normalize a hydrated Granola note into a ``MeetingRec``. Pure, defensive."""
    owner = note.get("owner") or {}
    calendar = note.get("calendar_event") or {}
    return MeetingRec(
        id=note.get("id", ""),
        title=note.get("title") or calendar.get("event_title"),
        # web_url is the permalink recall cites; absent only if the note is unshareable.
        url=note.get("web_url", ""),
        owner_email=owner.get("email"),
        participants=_participant_names(note),
        # Prefer the markdown summary (richer), fall back to the plain-text one.
        summary=note.get("summary_markdown") or note.get("summary_text"),
        transcript=_flatten_transcript(note.get("transcript")),
        # The meeting time is the scheduled start when present, else when the note began.
        occurred_at=calendar.get("scheduled_start_time") or note.get("created_at"),
        created_at=note.get("created_at"),
        updated_at=note.get("updated_at"),
        raw=note,
    )
