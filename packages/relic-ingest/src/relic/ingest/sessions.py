"""Session capture: a Claude Code coding session -> one AgentSession episode.

The write half of "context as a service" (docs/adr/0004) and the AgentSession artifact
type (docs/adr/0001). This is the ingest side of session capture, peer to the
``pr_to_episode`` / ``issue_to_episode`` mappers: it owns turning a raw capture payload
into a typed ``EpisodeSpec``, so the composition root only wires it to the loader.

Two concerns live here:
- **Distillation** (``distill_claude_transcript`` / ``resolve_session_transcript``):
  a JSONL transcript becomes clean User/Assistant prose, so extraction sees a
  conversation rather than tool-call noise.
- **Mapping** (``session_to_episode``): the distilled session becomes an ``EpisodeSpec``,
  the same shape the GitHub/Linear/Granola mappers produce.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from relic.contracts import AgentSessionEpisodeBody, EpisodeSpec

# A coding session is captured as one AgentSession episode (docs/adr/0001, the AgentSession
# amendment). Clip to the recent chars, where the decisions land: 200k would be a single
# huge add_episode call.
MAX_SESSION_CHARS = 24_000


def distill_claude_transcript(raw: str, *, max_chars: int = MAX_SESSION_CHARS) -> str:
    """Turn a Claude Code transcript (JSONL) into clean User/Assistant prose.

    Keeps real user prompts and assistant text; drops thinking, tool calls, tool
    results, and the bookkeeping line types, so extraction sees a conversation rather
    than tool-call noise (the engram ontology has no Decision/ActionItem types yet, so
    clean prose is what gives it a chance). Falls back to the raw text when the input
    is not the expected JSONL (an inline transcript). Clipped to the recent tail.
    """
    turns: list[str] = []
    parsed_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_any = True
        if obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "\n".join(
                b["text"]
                for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ).strip()
        else:
            text = ""
        if text:
            who = "User" if msg.get("role") == "user" else "Assistant"
            turns.append(f"{who}: {text}")
    distilled = "\n\n".join(turns) if parsed_any else raw
    return distilled[-max_chars:]


def resolve_session_transcript(payload: dict[str, Any]) -> str:
    """Read and distill the session text from a capture payload.

    Prefer the transcript file the hook points at, so the size/curation policy lives
    server-side (tunable without reinstalling hooks) and the HTTP payload stays small;
    fall back to an inline transcript.
    """
    raw = ""
    path = str(payload.get("transcript_path", "")).strip()
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            raw = ""
    if not raw:
        raw = str(payload.get("transcript", ""))
    return distill_claude_transcript(raw)


def session_to_episode(
    payload: dict[str, Any], group_id: str, *, now: datetime | None = None
) -> EpisodeSpec:
    """Map a finished-session capture payload to one AgentSession ``EpisodeSpec``.

    Peer of ``pr_to_episode``: pure given ``now`` (the reference/ended time, defaulting to
    the current UTC instant). The episode name is the session id, so a repeated SessionEnd
    (Claude Code can fire it more than once) collides on the checkpoint ledger and is
    skipped rather than forked. Deterministic ``files_touched`` / ``references`` links stay
    empty until the capture hook enriches the payload with git metadata; a transcript-only
    capture still lands.
    """
    stamp = now or datetime.now(UTC)
    session_id = str(payload.get("session_id", "")).strip()
    body = AgentSessionEpisodeBody(
        url=f"session://{session_id}",
        title=f"Coding session {session_id[:8]}",
        agent="claude-code",
        cwd=str(payload.get("cwd", "")).strip() or None,
        ended_at=stamp.isoformat(),
        transcript=resolve_session_transcript(payload) or None,
        summary=payload.get("summary") or None,
    )
    return EpisodeSpec(
        name=f"AgentSession {session_id}",
        body=body.model_dump_json(),
        source_description="Claude Code session (relic daemon)",
        reference_time=stamp,
        group_id=group_id,
    )
