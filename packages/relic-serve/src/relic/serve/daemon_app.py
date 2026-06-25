"""Loopback HTTP surface for the Relic daemon: the closed-loop core.

The daemon is the active half of "context as a service". Claude Code hooks call
this local surface on every turn:

  GET  /v1/daemon/status    liveness + loop counters (for the desktop app)
  POST /v1/daemon/inject    reflect the engram onto a prompt (recall -> context)
  POST /v1/daemon/capture   reflect a finished session back into the engram

Like ``build_http_app``, this is a pure builder over injected callables: it takes a
``RecallFn`` and a ``CaptureFn`` and imports neither graph nor ingest. The
composition root (``relic.cli``) builds the real recall/capture from the engram and
injects them. Bind to loopback only; the optional token is a defence-in-depth guard,
not a real auth boundary (anything on the machine can already reach localhost).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# (query, num_results, cwd) -> formatted cited answer. cwd lets the daemon scope recall
# to the repo the session is in; the composition root resolves cwd -> engram.
DaemonRecallFn = Callable[[str, int, str], Awaitable[str]]

# (capture request dict) -> {"status": ..., "loaded": int, ...}
# The request carries session_id plus the session content (a transcript_path the
# daemon reads, and/or an inline transcript, and an optional summary). The repo/group
# is fixed at daemon boot, so the composition root closes over the target group and
# owns how the session becomes an episode. Capture runs as a background task (LLM
# extraction is slow), so this return value feeds the loop counters, not the HTTP reply.
CaptureFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_MAX_RESULTS = 25
_DEFAULT_RESULTS = 10


def build_daemon_app(
    *,
    recall: DaemonRecallFn,
    capture: CaptureFn,
    token: str | None = None,
) -> Starlette:
    """Build the daemon's loopback app: status, inject (recall), capture (write-back).

    ``recall`` reflects the engram onto a prompt; ``capture`` writes a finished
    session back. When ``token`` is set, ``inject``/``capture`` require
    ``Authorization: Bearer <token>``; ``status`` stays open so a supervisor can poll
    liveness without the secret.
    """

    # An empty or whitespace token means "no auth", not "require the empty string":
    # otherwise a blank RELIC_DAEMON_TOKEN would lock out every caller.
    token = (token or "").strip() or None

    # Loop counters, surfaced on /status so the desktop app can show the loop turning.
    # The *_errors counters make a degraded loop (e.g. the engram is down) visible
    # without breaking a single turn.
    state: dict[str, Any] = {
        "injects": 0,
        "captures": 0,
        "inject_errors": 0,
        "capture_errors": 0,
        "last_inject_at": None,
        "last_capture_at": None,
    }

    def _authorized(request: Request) -> bool:
        return token is None or request.headers.get("authorization") == f"Bearer {token}"

    async def status(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", **state})

    async def inject_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        prompt = str(body.get("prompt", "")).strip()
        # An empty prompt is a no-op, not an error: the hook fires on every submit and
        # must never block the user's turn. Return empty context so nothing is injected.
        if not prompt:
            return JSONResponse({"context": ""})
        num_results = _clamp_results(body.get("num_results"))
        cwd = str(body.get("cwd", ""))  # scope recall to the repo the session is in
        # Recall failing (engram down, FalkorDB unreachable) must not break the user's
        # turn: degrade to "no context injected" and count it, never a 500 on a hot path.
        try:
            context = await recall(prompt, num_results, cwd)
        except Exception:  # noqa: BLE001 - any recall failure degrades, never breaks the turn
            state["inject_errors"] += 1
            return JSONResponse({"context": ""})
        state["injects"] += 1
        state["last_inject_at"] = _now()
        return JSONResponse({"context": context})

    async def recall_route(request: Request) -> JSONResponse:
        # Manual recall: same recall as inject, but a human typing a question in the
        # desktop app is not a loop turn, so it deliberately does not bump the inject
        # counters. The request field is "query" (vs inject's "prompt") to keep the two
        # call sites distinct.
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        query = str(body.get("query", "")).strip()
        if not query:
            return JSONResponse({"context": ""})
        num_results = _clamp_results(body.get("num_results"))
        # Manual search has no session cwd: use the daemon's default scope (cwd="").
        try:
            context = await recall(query, num_results, "")
        except Exception:  # noqa: BLE001 - degrade to no result, never error the ui
            return JSONResponse({"context": "", "error": "recall_unavailable"})
        return JSONResponse({"context": context})

    async def _capture_and_count(payload: dict[str, Any]) -> None:
        # Runs after the 202 is sent. A failed write-back is counted, never fatal: the
        # SessionEnd hook already has its response and the user's shell is unblocked.
        try:
            await capture(payload)
        except Exception:  # noqa: BLE001 - degrade and count, never crash the loop
            state["capture_errors"] += 1
            return
        state["captures"] += 1
        state["last_capture_at"] = _now()

    async def capture_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        session_id = str(body.get("session_id", "")).strip()
        if not session_id:
            return JSONResponse({"error": "session_id required"}, status_code=400)
        transcript = str(body.get("transcript", "")).strip()
        transcript_path = str(body.get("transcript_path", "")).strip()
        raw_summary = body.get("summary")
        summary = raw_summary.strip() if isinstance(raw_summary, str) else None
        cwd = str(body.get("cwd", "")).strip()
        # Nothing to read and nothing said: a clean no-op so an empty session that ends
        # never writes a hollow episode.
        if not transcript and not transcript_path and not summary:
            return JSONResponse({"status": "empty", "session_id": session_id})

        payload = {
            "session_id": session_id,
            "transcript": transcript,
            "transcript_path": transcript_path,
            "summary": summary,
            "cwd": cwd,
        }
        # Write-back runs the LLM extraction pipeline (seconds to a minute). Accept
        # immediately and do the work in the background, so the SessionEnd hook never
        # hangs the user's shell on extraction. Counters update when the task finishes.
        return JSONResponse(
            {"status": "accepted", "session_id": session_id},
            status_code=202,
            background=BackgroundTask(_capture_and_count, payload),
        )

    return Starlette(
        routes=[
            Route("/v1/daemon/status", status, methods=["GET"]),
            Route("/v1/daemon/inject", inject_route, methods=["POST"]),
            Route("/v1/daemon/recall", recall_route, methods=["POST"]),
            Route("/v1/daemon/capture", capture_route, methods=["POST"]),
        ]
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clamp_results(raw: Any) -> int:
    """Parse and bound ``num_results`` so a bad or huge value can't blow up recall."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_RESULTS
    except (ValueError, TypeError):
        value = _DEFAULT_RESULTS
    return max(1, min(value, _MAX_RESULTS))
