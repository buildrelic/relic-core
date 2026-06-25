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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from relic.contracts import RecallFn

# (session_id, transcript, summary | None) -> {"status": ..., "loaded": int, ...}
# The repo/group the session is written to is fixed at daemon boot, so the hook only
# sends the session content; the composition root closes over the target group.
CaptureFn = Callable[[str, str, str | None], Awaitable[dict[str, Any]]]

_MAX_RESULTS = 25
_DEFAULT_RESULTS = 10


def build_daemon_app(
    *,
    recall: RecallFn,
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
        # Recall failing (engram down, FalkorDB unreachable) must not break the user's
        # turn: degrade to "no context injected" and count it, never a 500 on a hot path.
        try:
            context = await recall(prompt, num_results)
        except Exception:  # noqa: BLE001 - any recall failure degrades, never breaks the turn
            state["inject_errors"] += 1
            return JSONResponse({"context": ""})
        state["injects"] += 1
        state["last_inject_at"] = _now()
        return JSONResponse({"context": context})

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
        raw_summary = body.get("summary")
        summary = raw_summary.strip() if isinstance(raw_summary, str) else None
        # Nothing said, nothing to remember: a clean no-op keeps an empty session from
        # writing a hollow episode every time it ends.
        if not transcript and not summary:
            return JSONResponse({"status": "empty", "session_id": session_id})
        # Write-back failing must not surface as a hook error either: record it and tell
        # the caller, but stay a 200 so the SessionEnd hook always exits clean.
        try:
            result = await capture(session_id, transcript, summary or None)
        except Exception:  # noqa: BLE001 - a failed write-back is counted, never fatal
            state["capture_errors"] += 1
            return JSONResponse({"status": "error", "session_id": session_id})
        state["captures"] += 1
        state["last_capture_at"] = _now()
        return JSONResponse(result)

    return Starlette(
        routes=[
            Route("/v1/daemon/status", status, methods=["GET"]),
            Route("/v1/daemon/inject", inject_route, methods=["POST"]),
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
