"""Thin HTTP surface over ingest state, for the connector control plane.

The web app is where a human connects sources and watches them ingest, so it
reads three things over HTTP:

  GET  /v1/status        liveness
  GET  /v1/connectors    per-source sync status (counts, last activity)
  GET  /v1/ingest/runs   ingest run history plus totals
  POST /v1/ingest        kick off an ingest for a repo (backfill / webhook sync)

Mirrors the MCP server's injection: the data providers and the ingest trigger
are handed in, so this module never imports ingest or graph. Recall is not here:
that is an agent feature served by the MCP server (`relic serve`), not the web.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# () -> {"connectors": [...]}
ConnectorsFn = Callable[[], Awaitable[dict[str, Any]]]
# (repo | None, limit) -> {"runs": [...], "totals": {...}}
IngestRunsFn = Callable[[str | None, int], Awaitable[dict[str, Any]]]
# (repo) -> {"status": "running" | "already_running", "repo": ...}
IngestTriggerFn = Callable[[str], Awaitable[dict[str, Any]]]

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def build_http_app(
    *,
    connectors: ConnectorsFn,
    ingest_runs: IngestRunsFn,
    ingest_trigger: IngestTriggerFn,
    token: str | None = None,
) -> Starlette:
    """Build the HTTP app: status, connector status, ingest run history, trigger.

    When ``token`` is set, the data and trigger routes require ``Authorization:
    Bearer <token>``. Leave it unset for local dev. Calls are server-to-server
    (the web app calls this from its own backend), so there is no CORS layer.
    """

    def _authorized(request: Request) -> bool:
        return token is None or request.headers.get("authorization") == f"Bearer {token}"

    async def status(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def connectors_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(await connectors())

    async def ingest_runs_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        repo = request.query_params.get("repo") or None
        limit = _clamp_limit(request.query_params.get("limit"))
        return JSONResponse(await ingest_runs(repo, limit))

    async def ingest_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        repo = str(body.get("repo", "")).strip()
        if "/" not in repo:
            return JSONResponse({"error": "repo must be owner/name"}, status_code=400)
        result = await ingest_trigger(repo)
        # A run already in flight for this repo is a conflict, not a new job.
        code = 409 if result.get("status") == "already_running" else 202
        return JSONResponse(result, status_code=code)

    return Starlette(
        routes=[
            Route("/v1/status", status, methods=["GET"]),
            Route("/v1/connectors", connectors_route, methods=["GET"]),
            Route("/v1/ingest/runs", ingest_runs_route, methods=["GET"]),
            Route("/v1/ingest", ingest_route, methods=["POST"]),
        ]
    )


def _clamp_limit(raw: str | None) -> int:
    """Parse and bound ``limit`` so a bad or huge value can't blow up the read."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_LIMIT
    except ValueError:
        value = _DEFAULT_LIMIT
    return max(1, min(value, _MAX_LIMIT))
