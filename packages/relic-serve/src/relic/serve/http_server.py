"""Thin HTTP surface over team memory, for the web app.

``GET /v1/recall`` returns the facts matching a query, each with its sources, as
JSON. The web app calls it server-side. This mirrors the MCP server: the recall
function is injected, so this module never imports graph or ingest. The injected
callable returns a JSON-ready dict, so no graph types reach here either. The
composition root (``relic.cli``) builds the real recall and hands it in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# (query, repo | None, num_results) -> {"query": str, "facts": [...]}.
# repo is an "owner/name" string. The composition root maps it to a group_id.
HttpRecallFn = Callable[[str, str | None, int], Awaitable[dict[str, Any]]]

_MAX_RESULTS = 50
_DEFAULT_RESULTS = 10


def build_http_app(
    recall: HttpRecallFn,
    *,
    token: str | None = None,
    on_shutdown: list[Callable[[], Awaitable[None]]] | None = None,
) -> Starlette:
    """Build the HTTP app: ``GET /v1/status`` and ``GET /v1/recall``.

    When ``token`` is set, /v1/recall requires ``Authorization: Bearer <token>``.
    Leave it unset for local dev. ``on_shutdown`` runs on server stop (close the
    engram). Calls are server-to-server (the web app calls this from its own
    backend), so there is no CORS layer.
    """

    async def status(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def recall_route(request: Request) -> JSONResponse:
        if token is not None and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        query = (request.query_params.get("q") or "").strip()
        if not query:
            return JSONResponse({"error": "q is required"}, status_code=400)
        repo = request.query_params.get("repo") or None
        num_results = _clamp_results(request.query_params.get("num_results"))
        answer = await recall(query, repo, num_results)
        return JSONResponse(answer)

    hooks = list(on_shutdown or [])

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Starlette dropped on_shutdown for lifespan: run the hooks (close the
        # engram) when the server stops.
        try:
            yield
        finally:
            for hook in hooks:
                await hook()

    return Starlette(
        routes=[
            Route("/v1/status", status, methods=["GET"]),
            Route("/v1/recall", recall_route, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def _clamp_results(raw: str | None) -> int:
    """Parse and bound ``num_results`` so a bad or huge value can't reach search."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_RESULTS
    except ValueError:
        value = _DEFAULT_RESULTS
    return max(1, min(value, _MAX_RESULTS))
