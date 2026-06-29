"""The session distiller: Claude Code transcript JSONL -> clean prose.

Write-back quality hinges on this: extraction sees a conversation, not tool-call noise.
Lives in ``relic.ingest`` (session capture), run by the daemon's composition root.
"""

import json

from relic.ingest import (
    distill_claude_transcript,
    resolve_session_transcript,
    session_to_episode,
)


def _line(type_, content):
    return json.dumps({"type": type_, "message": {"role": type_, "content": content}})


def test_distill_keeps_user_prompts_and_assistant_text():
    raw = "\n".join(
        [
            _line("user", "how do expired tokens fall back"),
            _line(
                "assistant",
                [
                    {"type": "thinking", "thinking": "internal noise"},
                    {"type": "text", "text": "they fall back to the server identity"},
                ],
            ),
            _line("user", [{"type": "tool_result", "content": "tool junk"}]),
        ]
    )
    out = distill_claude_transcript(raw)
    assert "User: how do expired tokens fall back" in out
    assert "Assistant: they fall back to the server identity" in out
    # thinking and tool_result are dropped
    assert "internal noise" not in out
    assert "tool junk" not in out


def test_distill_falls_back_to_raw_for_non_jsonl():
    raw = "just a plain transcript, not jsonl"
    assert distill_claude_transcript(raw) == raw


def test_distill_clips_to_tail():
    big = "\n".join(_line("user", f"message {i} " + "x" * 500) for i in range(200))
    out = distill_claude_transcript(big)
    assert len(out) <= 24_000
    # keeps the most recent content
    assert "message 199" in out


def test_resolve_reads_transcript_path(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("user", "from the file"), encoding="utf-8")
    out = resolve_session_transcript({"transcript_path": str(p)})
    assert "User: from the file" in out


def test_resolve_falls_back_to_inline_transcript():
    out = resolve_session_transcript({"transcript_path": "/no/such/path", "transcript": "inline"})
    assert out == "inline"


def test_session_to_episode_maps_payload_to_spec():
    from datetime import UTC, datetime

    stamp = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
    payload = {
        "session_id": "abc12345",
        "cwd": "/repo",
        "transcript": _line("user", "what changed"),
        "summary": "fixed the thing",
    }
    spec = session_to_episode(payload, "team-a", now=stamp)
    assert spec.name == "AgentSession abc12345"
    assert spec.group_id == "team-a"
    assert spec.reference_time == stamp
    body = json.loads(spec.body)
    assert body["url"] == "session://abc12345"
    assert body["agent"] == "claude-code"
    assert body["summary"] == "fixed the thing"
    assert "User: what changed" in body["transcript"]


def test_session_to_episode_name_is_stable_for_idempotent_capture():
    # The name is the dedup key on the checkpoint ledger, so two captures of the same
    # session collide (skip) rather than fork -- regardless of capture time.
    from datetime import UTC, datetime

    payload = {"session_id": "s1", "transcript": "t"}
    a = session_to_episode(payload, "g", now=datetime(2026, 1, 1, tzinfo=UTC))
    b = session_to_episode(payload, "g", now=datetime(2026, 2, 2, tzinfo=UTC))
    assert a.name == b.name == "AgentSession s1"
