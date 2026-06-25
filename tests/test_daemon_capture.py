"""The daemon's session distiller: Claude Code transcript JSONL -> clean prose.

Write-back quality hinges on this: extraction sees a conversation, not tool-call noise.
The composition root reads the transcript file the hook points at and distills it.
"""

import json

from relic.cli import _distill_claude_transcript, _resolve_session_transcript


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
    out = _distill_claude_transcript(raw)
    assert "User: how do expired tokens fall back" in out
    assert "Assistant: they fall back to the server identity" in out
    # thinking and tool_result are dropped
    assert "internal noise" not in out
    assert "tool junk" not in out


def test_distill_falls_back_to_raw_for_non_jsonl():
    raw = "just a plain transcript, not jsonl"
    assert _distill_claude_transcript(raw) == raw


def test_distill_clips_to_tail():
    big = "\n".join(_line("user", f"message {i} " + "x" * 500) for i in range(200))
    out = _distill_claude_transcript(big)
    assert len(out) <= 24_000
    # keeps the most recent content
    assert "message 199" in out


def test_resolve_reads_transcript_path(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_line("user", "from the file"), encoding="utf-8")
    out = _resolve_session_transcript({"transcript_path": str(p)})
    assert "User: from the file" in out


def test_resolve_falls_back_to_inline_transcript():
    out = _resolve_session_transcript({"transcript_path": "/no/such/path", "transcript": "inline"})
    assert out == "inline"
