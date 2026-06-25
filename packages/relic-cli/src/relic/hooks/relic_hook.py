#!/usr/bin/env python3
"""Claude Code hook shim for the Relic daemon — stdlib only, never blocks a turn.

Wired by `relic install-hooks`. Two modes, picked by argv[1]:

  inject   (UserPromptSubmit)  read the prompt, ask the daemon for context, print it
  capture  (SessionEnd)        read the session transcript, hand it to the daemon

The hook payload arrives as JSON on stdin. Any failure is swallowed (exit 0): a
memory layer must never break the user's session, so a dead daemon, a timeout, or a
bad payload all degrade to "no context injected" rather than an error. The daemon
URL/token come from RELIC_DAEMON_URL / RELIC_DAEMON_TOKEN.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

_DEFAULT_URL = "http://127.0.0.1:8788"
# Cap how much of a long session we ship; keep the tail, where the decisions land.
_MAX_TRANSCRIPT_CHARS = 200_000


def _post(path: str, payload: dict, timeout: float) -> dict:
    url = os.environ.get("RELIC_DAEMON_URL", _DEFAULT_URL).rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("content-type", "application/json")
    token = os.environ.get("RELIC_DAEMON_TOKEN")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
        return json.loads(resp.read().decode("utf-8"))


def _inject(hook: dict) -> None:
    prompt = str(hook.get("prompt", "")).strip()
    if not prompt:
        return
    result = _post("/v1/daemon/inject", {"prompt": prompt}, timeout=5.0)
    context = result.get("context", "")
    if context:
        # For UserPromptSubmit, the hook's stdout is injected into the model's context
        # for this turn — exactly the "reflect the engram onto the prompt" step.
        sys.stdout.write(context)


def _capture(hook: dict) -> None:
    session_id = str(hook.get("session_id", "")).strip()
    if not session_id:
        return
    transcript = ""
    path = hook.get("transcript_path")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            transcript = fh.read()[-_MAX_TRANSCRIPT_CHARS:]
    _post(
        "/v1/daemon/capture",
        {"session_id": session_id, "transcript": transcript},
        timeout=10.0,
    )


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        hook = json.load(sys.stdin)
    except Exception:  # a malformed payload must not surface as a hook error
        return 0
    try:
        if mode == "inject":
            _inject(hook)
        elif mode == "capture":
            _capture(hook)
    except Exception:  # daemon down / timeout / anything: degrade silently
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
