"""Thin HTTP surface over ingest state, for the connector control plane.

The web app is where a human connects sources and watches them ingest, so it
reads three things over HTTP:

  GET  /v1/status        liveness
  GET  /v1/connectors    per-source sync status (counts, last activity)
  GET  /v1/ingest/runs   ingest run history plus totals
  POST /v1/ingest        kick off an ingest for a repo or non-repo source (backfill / sync)

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
# (repo | None, limit, source | None) -> {"runs": [...], "totals": {...}}
# repo and source are independent filters: a repo scopes to one github repo, source scopes
# to a non-repo connector (granola/notion, whose runs carry no repo). Either, both, or neither.
IngestRunsFn = Callable[[str | None, int, str | None], Awaitable[dict[str, Any]]]
# (source, repo | None, token | None, workspace | None) -> {"status": "running" | ...}
# source is "github" (repo required) or a non-repo source ("granola", "notion") that scopes
# itself server-side. token is the per-user secret for that source (a github OAuth token, a
# granola grn_ key, or a notion integration token). workspace is the engram workspace the
# run writes into (the web app's Clerk scope); absent falls back to the server's own
# RELIC_WORKSPACE.
IngestTriggerFn = Callable[[str, str | None, str | None, str | None], Awaitable[dict[str, Any]]]

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50
_MAX_TOKEN_LEN = 255
_MAX_WORKSPACE_LEN = 128


def _valid_workspace(workspace: str) -> bool:
    """A workspace id is a Clerk scope: short, printable, no whitespace.

    It becomes the RELIC_WORKSPACE env of the spawned ingest, so the same shapes the
    secret sanitizer rejects are rejected here with a clean 400.
    """
    return (
        0 < len(workspace) <= _MAX_WORKSPACE_LEN
        and not workspace.startswith("#")
        and all(c.isprintable() and not c.isspace() for c in workspace)
    )


def _valid_token(token: str) -> bool:
    """A per-user github token must be short and printable with no whitespace.

    The child's Settings._clean_secret nulls a token with whitespace or a leading
    `#`, after which ingest falls back to the server's own identity. Reject the same
    shapes here, with a length bound, so a malformed token is a clean 400 instead of
    a silent run as the wrong identity or a 500 from an unprintable byte.
    """
    return (
        0 < len(token) <= _MAX_TOKEN_LEN
        and not token.startswith("#")
        and all(c.isprintable() and not c.isspace() for c in token)
    )


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
        source = request.query_params.get("source") or None
        limit = _clamp_limit(request.query_params.get("limit"))
        return JSONResponse(await ingest_runs(repo, limit, source))

    async def ingest_route(request: Request) -> JSONResponse:
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        # `source` selects the ingest path. Default is github, for back-compat with the
        # original {repo, token} body. Granola and Notion are non-repo, API-key sources whose
        # graph scope is derived server-side (the note owner / the workspace): they carry a
        # token but no repo, so they must branch before the owner/name guard below.
        source = str(body.get("source", "github")).strip().lower() or "github"
        # Optional per-user secret: a github OAuth token, or a granola grn_ key. Ingest
        # authenticates as the connecting user so it reads their data, not just ours.
        # Absent or blank falls back to the server's own credentials for that source. A
        # malformed token is a 400, not a silent run as the wrong identity. Validated the
        # same way for every source; never logged or echoed back in the response.
        raw_token = body.get("token")
        user_token = raw_token.strip() if isinstance(raw_token, str) else ""
        if user_token and not _valid_token(user_token):
            return JSONResponse({"error": "invalid token"}, status_code=400)
        # Optional engram workspace the run writes into (the web app's Clerk scope).
        # Absent falls back to the server's own RELIC_WORKSPACE.
        raw_workspace = body.get("workspace")
        workspace = raw_workspace.strip() if isinstance(raw_workspace, str) else ""
        if workspace and not _valid_workspace(workspace):
            return JSONResponse({"error": "invalid workspace"}, status_code=400)

        if source in ("granola", "notion"):
            result = await ingest_trigger(source, None, user_token or None, workspace or None)
        elif source == "github":
            repo = str(body.get("repo", "")).strip()
            if "/" not in repo:
                return JSONResponse({"error": "repo must be owner/name"}, status_code=400)
            result = await ingest_trigger("github", repo, user_token or None, workspace or None)
        else:
            return JSONResponse({"error": f"unknown source: {source}"}, status_code=400)
        # A run already in flight for this scope is a conflict, not a new job.
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
