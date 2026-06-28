"""Relic command-line interface.

Only typer/rich are imported at module load so `relic --help` stays fast and
import-clean. Heavier work is imported inside each command as it lands in its
phase.
"""

from contextlib import suppress
from datetime import UTC
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from relic.config import Settings
    from relic.contracts import EpisodeSpec
    from relic.graph import GraphitiMemory, LoadStats

app = typer.Typer(
    name="relic",
    help="Relic: ingest engineering history, compile it into grounded skills.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _todo(phase: str) -> None:
    console.print(f"[yellow]not implemented yet[/] (lands in {phase})")
    raise typer.Exit(code=1)


@app.callback()
def _main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="debug-level logging to stderr")
    ] = False,
) -> None:
    """Set up logging before any command runs. Logs go to stderr, results to stdout."""
    from relic.obs import configure_logging

    configure_logging(verbose=verbose)


@app.command()
def ingest(
    repo: Annotated[str, typer.Option(help="owner/name to ingest")],
    limit: Annotated[
        int | None, typer.Option(help="cap PRs and issues pulled, most recent first")
    ] = None,
    months: Annotated[int, typer.Option(help="how many months of history to backfill")] = 12,
    bulk: Annotated[
        bool,
        typer.Option("--bulk", help="load via batched add_episode_bulk (faster, experimental)"),
    ] = False,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="ignore the checkpoint and reload every episode")
    ] = False,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="disable the live progress bar")
    ] = False,
    no_load: Annotated[
        bool,
        typer.Option(
            "--no-load",
            help="capture and spool episodes but skip LLM extraction; run `relic load` after",
        ),
    ] = False,
) -> None:
    """Pull merged and closed PRs, reviews, and issues into the graph (Phase 2).

    Two halves: a fast, deterministic capture (fetch, map, raw store, spool) with no
    LLM, then the LLM-heavy extraction into the graph. ``--no-load`` runs only the
    first half and stops, leaving the episodes spooled for a later ``relic load``.
    """
    import asyncio

    from relic.obs import get_logger

    try:
        asyncio.run(
            _ingest(
                repo,
                limit,
                months=months,
                bulk=bulk,
                fresh=fresh,
                no_progress=no_progress,
                no_load=no_load,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - report infra failures concisely, not as a traceback
        # A missing token, an unreachable graph, a bad key: log one line, keep the
        # traceback for `--verbose`. Episodes already loaded stay checkpointed.
        log = get_logger("ingest")
        log.error("ingest failed: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        log.debug("ingest traceback", exc_info=exc)
        raise typer.Exit(code=1) from exc


@app.command()
def load(
    repo: Annotated[str, typer.Option(help="owner/name whose spooled episodes to extract")],
    limit: Annotated[
        int | None,
        typer.Option(help="extract at most N not-yet-loaded episodes, for case-by-case loading"),
    ] = None,
    bulk: Annotated[
        bool,
        typer.Option("--bulk", help="load via batched add_episode_bulk (faster, experimental)"),
    ] = False,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="ignore the checkpoint and reload every episode")
    ] = False,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="disable the live progress bar")
    ] = False,
) -> None:
    """Extract spooled episodes into the graph: the LLM-heavy half of ingest, on demand.

    Reads what `relic ingest --no-load` spooled for the repo and runs Graphiti
    extraction. Run it whenever: right after capture, on a schedule, or one batch at a
    time with ``--limit``. Resumable and idempotent: the checkpoint skips episodes that
    already landed, so a re-run only extracts what is new.
    """
    import asyncio

    from relic.obs import get_logger

    try:
        asyncio.run(_load(repo, limit, bulk=bulk, fresh=fresh, no_progress=no_progress))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - report infra failures concisely, not as a traceback
        log = get_logger("load")
        log.error("load failed: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        log.debug("load traceback", exc_info=exc)
        raise typer.Exit(code=1) from exc


def _safe_ident(identifier: str) -> str:
    return identifier.replace("/", "_").replace("#", "-")


def _quiet_background_errors(log: "logging.Logger") -> None:
    """Route the FalkorDB driver's detached index-build errors to debug.

    The driver schedules an index build in its constructor as an orphaned task. If the
    graph is unhealthy that task fails too, and asyncio dumps a full traceback to
    stderr. The run's own error reporting already covers the foreground failure, so
    these orphaned-task errors go to debug.
    """
    import asyncio

    def _on_loop_error(_loop: object, context: dict) -> None:
        log.debug(
            "background task error: %s", context.get("message"), exc_info=context.get("exception")
        )

    asyncio.get_running_loop().set_exception_handler(_on_loop_error)


async def _github_token_with_fallback(
    token: str,
    make_github: "Callable[[str], Any]",
    log: "logging.Logger",
) -> str:
    """Probe a forwarded user token, falling back to the server token only if it is dead.

    Format validation in the serve handler can't catch an expired or revoked token: it is
    well-formed but github rejects it at use time with a 401. When a user token is forwarded
    the parent stashes its own token in RELIC_SERVER_GITHUB_TOKEN, so here we do a cheap
    authenticated call to probe the user token before the heavy fetch.

    The probe is best-effort and only ever adds a fallback, it never fails a run that would
    otherwise proceed. Only a definitive 401 (bad credentials) falls back to the server
    token. A 403 is not a dead token (rate limit, sso, ip allow-list, missing scope), so we
    keep the user token and let the real fetch surface it under the user's own identity
    rather than silently re-running as the server. Any other probe error (5xx, network,
    timeout) also keeps the user token.

    Token values are never logged. With no server fallback stashed there is nothing to fall
    back to, so the original token is returned and the real fetch surfaces any error.
    """
    import os

    from githubkit.exception import RequestFailed

    server_token = os.environ.get(_SERVER_TOKEN_ENV)
    if not server_token or server_token == token:
        # no forwarded user token in play (or it already is the server token): nothing to
        # verify or fall back to. skip the extra round trip.
        return token

    try:
        async with make_github(token) as gh:
            await gh.rest.users.async_get_authenticated()
    except RequestFailed as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 401:
            log.warning("user github token rejected (401); falling back to the server token")
            return server_token
        # not a dead token: keep the user's identity and let the real fetch surface it.
        return token
    except Exception:
        # the probe is best-effort. a transient error must not fail an ingest that would
        # otherwise run, so keep the user token and let the real fetch try.
        log.debug("user github token probe failed (non-auth); proceeding with the user token")
        return token
    return token


async def _capture(
    repo: str,
    limit: int | None,
    *,
    months: int,
    settings: "Settings",
    log: "logging.Logger",
) -> "tuple[list[EpisodeSpec], str, float, float]":
    """Fetch, map, raw-store, and spool episodes: the fast, no-LLM half of an ingest.

    Returns ``(episodes, group_id, fetch_seconds, prepare_seconds)``. ``group_id`` is
    the capture key (the GitHub repo's slug); Linear episodes carry their own group but
    spool under this key, mirroring the checkpoint. ``prepare_seconds`` covers mapping,
    the raw dump, and the spool write.
    """
    import time

    from relic.ingest import (
        RepoBundle,
        dump_raw,
        fetch_issues,
        fetch_meetings,
        fetch_repo,
        granola_enabled,
        issue_to_episode,
        linear_enabled,
        make_github,
        meeting_to_episode,
        pr_to_episode,
        repo_group_id,
        resolve_github_token,
        sort_episodes,
        spool_episodes,
    )

    owner, name = repo.split("/", 1)
    fetch_start = time.monotonic()
    token = await _github_token_with_fallback(resolve_github_token(settings), make_github, log)
    async with make_github(token) as gh:
        bundle = await fetch_repo(
            gh, owner, name, concurrency=settings.fetch_concurrency, limit=limit, months=months
        )
    fetch_seconds = time.monotonic() - fetch_start
    log.info(
        "fetched %d PRs, %d issues from %s in %.1fs",
        len(bundle.pull_requests),
        len(bundle.issues),
        repo,
        fetch_seconds,
    )

    prepare_start = time.monotonic()
    raw_count = 0
    for pr in bundle.pull_requests:
        dump_raw(pr.raw, source="github", ident=f"pr-{pr.number}")
        raw_count += 1
    for issue in bundle.issues:
        dump_raw(issue.raw, source="github", ident=_safe_ident(issue.identifier))
        raw_count += 1

    episodes = [pr_to_episode(pr, bundle) for pr in bundle.pull_requests]
    episodes += [issue_to_episode(issue, bundle) for issue in bundle.issues]
    group_id = repo_group_id(bundle.full_name)

    if linear_enabled(settings) and settings.linear_api_key:
        linear_issues = await fetch_issues(settings.linear_api_key)
        linear_repo = RepoBundle(full_name="linear", url="https://linear.app", default_branch="")
        for issue in linear_issues:
            dump_raw(issue.raw, source="linear", ident=_safe_ident(issue.identifier))
            raw_count += 1
        episodes += [issue_to_episode(issue, linear_repo) for issue in linear_issues]
        log.info("fetched %d Linear issues", len(linear_issues))
    else:
        log.info("Linear skipped (LINEAR_API_KEY not set)")

    if granola_enabled(settings) and settings.granola_api_key:
        meetings = await fetch_meetings(settings.granola_api_key, months=months, limit=limit)
        for meeting in meetings:
            dump_raw(meeting.raw, source="granola", ident=_safe_ident(meeting.id))
            raw_count += 1
        # Meetings carry their own per-owner group_id (granola__<email>); like Linear they
        # spool under this capture key but land in their own partition at load time.
        episodes += [meeting_to_episode(meeting) for meeting in meetings]
        log.info("fetched %d Granola meetings", len(meetings))
    else:
        log.info("Granola skipped (GRANOLA_API_KEY not set)")

    log.debug("wrote %d raw payloads under data/raw", raw_count)
    # Canonical load order, so the combined ingest feeds the order-sensitive loader the
    # same sequence `relic load` reads back from the spool (both oldest-first).
    episodes = sort_episodes(episodes)
    if episodes:
        spool_episodes(episodes, group_id)
        log.debug("spooled %d episodes under data/spool/%s", len(episodes), group_id)
    prepare_seconds = time.monotonic() - prepare_start
    return episodes, group_id, fetch_seconds, prepare_seconds


async def _extract(
    repo: str,
    episodes: "list[EpisodeSpec]",
    group_id: str,
    *,
    settings: "Settings",
    bulk: bool,
    fresh: bool,
    no_progress: bool,
    limit: int | None,
    progress_label: str,
    log: "logging.Logger",
) -> "tuple[LoadStats, bool]":
    """Run Graphiti extraction over episodes: the LLM-heavy half. Returns (stats, used_bulk).

    ``fresh`` clears the checkpoint first so every episode reloads. ``limit`` caps how
    many not-yet-loaded episodes extract this run, for case-by-case loading; ``None``
    loads all pending (the loader skips checkpointed names internally).
    """
    import logging
    from collections.abc import Callable

    from relic.graph import LoadStats, load_episodes, load_episodes_bulk, open_memory
    from relic.ingest import checkpoint_path, clear, compact, load_done, record_done
    from relic.obs import stderr_console

    ledger = checkpoint_path(group_id)
    if fresh:
        clear(ledger)
    done = load_done(ledger)

    # A limited run pre-filters to pending and slices, so N means N freshly loaded; an
    # unbounded run hands the loader the full list plus the skip set, so it reports the
    # resume count.
    if limit is not None:
        to_load = [spec for spec in episodes if spec.name not in done][:limit]
        skip: dict[str, str | None] = {}
    else:
        to_load, skip = episodes, done

    engram = open_memory(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=settings.falkordb_database,
        api_key=settings.openai_api_key,
        max_coroutines=settings.graphiti_max_coroutines,
    )
    use_bulk = bulk or settings.bulk_load

    async def _run(on_progress: Callable[[LoadStats], None] | None) -> LoadStats:
        # progress=on_progress is None: the bar replaces the "loaded x/y" heartbeat logs
        # when active, so they aren't emitted twice.
        common = {
            "group_id": group_id,
            "skip": skip,
            "on_loaded": lambda name, token: record_done(ledger, name, token),
            "on_progress": on_progress,
            "progress": on_progress is None,
        }
        if use_bulk:
            return await load_episodes_bulk(
                engram, to_load, batch_size=settings.bulk_batch_size, **common
            )
        return await load_episodes(engram, to_load, **common)

    # A live bar only on a real terminal, off under --verbose (DEBUG logs would churn it)
    # and --no-progress; otherwise fall back to the heartbeat log lines.
    verbose = logging.getLogger("relic").getEffectiveLevel() <= logging.DEBUG
    bar_console = stderr_console()
    show_bar = bar_console.is_terminal and not no_progress and not verbose

    try:
        if show_bar:
            from rich.progress import (
                BarColumn,
                MofNCompleteColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )

            with Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("{task.fields[counts]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=bar_console,
            ) as bar:
                task = bar.add_task(progress_label, total=len(to_load), counts="")

                def _on_progress(s: LoadStats) -> None:
                    bar.update(
                        task,
                        completed=s.loaded + s.superseded + s.skipped + s.failed,
                        counts=(
                            f"[green]{s.loaded}✓[/] [blue]{s.superseded}↻[/] "
                            f"[yellow]{s.skipped}⤳[/] [red]{s.failed}✗[/]"
                        ),
                    )

                stats = await _run(_on_progress)
        else:
            stats = await _run(None)
    finally:
        await engram.close()

    # Supersession re-records a name per content change; keep the append-only ledger small.
    compact(ledger)

    summary = (
        f"extracted {stats.loaded} episodes from {repo} in {stats.duration_s:.1f}s "
        f"({stats.superseded} refreshed, {stats.skipped} skipped, {stats.failed} failed)"
    )
    if stats.failed:
        log.warning(summary)
    else:
        log.info(summary)
    return stats, use_bulk


async def _ingest(
    repo: str,
    limit: int | None = None,
    *,
    months: int = 12,
    bulk: bool = False,
    fresh: bool = False,
    no_progress: bool = False,
    no_load: bool = False,
) -> None:
    import time

    from relic.config import get_settings
    from relic.graph import falkordb_reachable
    from relic.ingest import format_ingest_timing
    from relic.obs import get_logger, stderr_console

    log = get_logger("ingest")
    _quiet_background_errors(log)

    if "/" not in repo:
        err_console.print("[red]--repo must be owner/name[/]")
        raise typer.Exit(code=2)
    settings = get_settings()
    ingest_start = time.monotonic()

    # The graph is only needed for extraction. A capture-only run (--no-load) needs no
    # FalkorDB, so the preflight probe is skipped; a full run keeps the early-fail.
    if not no_load and not falkordb_reachable(settings.falkordb_host, settings.falkordb_port):
        log.error(
            "FalkorDB not reachable at %s:%s. Start it with `just up`.",
            settings.falkordb_host,
            settings.falkordb_port,
        )
        raise typer.Exit(code=1)

    episodes, group_id, fetch_seconds, prepare_seconds = await _capture(
        repo, limit, months=months, settings=settings, log=log
    )
    if not episodes:
        log.warning("nothing to ingest")
        return

    if no_load:
        total_seconds = time.monotonic() - ingest_start
        log.info(
            "captured %d episodes from %s in %.1fs (fetch %.1fs, map + spool %.1fs); "
            "extract them with `relic load --repo %s`",
            len(episodes),
            repo,
            total_seconds,
            fetch_seconds,
            prepare_seconds,
            repo,
        )
        return

    stats, use_bulk = await _extract(
        repo,
        episodes,
        group_id,
        settings=settings,
        bulk=bulk,
        fresh=fresh,
        no_progress=no_progress,
        limit=None,
        progress_label=f"ingesting {repo}",
        log=log,
    )
    _record_run(stats, repo)

    extracted = stats.loaded + stats.failed
    total_seconds = time.monotonic() - ingest_start
    # The honest remainder: FalkorDB probe, token resolve, engram build, and the
    # index build (which the loader's duration_s deliberately excludes).
    setup_seconds = max(0.0, total_seconds - fetch_seconds - prepare_seconds - stats.duration_s)
    stderr_console().print(
        format_ingest_timing(
            repo,
            [
                ("fetch", fetch_seconds),
                ("map + raw store + spool", prepare_seconds),
                ("load (extraction)", stats.duration_s),
                ("setup + index", setup_seconds),
            ],
            total_seconds,
            loaded=stats.loaded,
            skipped=stats.skipped,
            failed=stats.failed,
            per_episode_seconds=stats.duration_s / extracted if extracted else 0.0,
            bulk=use_bulk,
        ),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )

    if stats.loaded == 0 and stats.failed:
        raise typer.Exit(code=1)


async def _load(
    repo: str,
    limit: int | None = None,
    *,
    bulk: bool = False,
    fresh: bool = False,
    no_progress: bool = False,
) -> None:
    import time

    from relic.config import get_settings
    from relic.graph import falkordb_reachable
    from relic.ingest import format_ingest_timing, read_spool, repo_group_id
    from relic.obs import get_logger, stderr_console

    log = get_logger("load")
    _quiet_background_errors(log)

    if "/" not in repo:
        err_console.print("[red]--repo must be owner/name[/]")
        raise typer.Exit(code=2)
    settings = get_settings()
    load_start = time.monotonic()

    if not falkordb_reachable(settings.falkordb_host, settings.falkordb_port):
        log.error(
            "FalkorDB not reachable at %s:%s. Start it with `just up`.",
            settings.falkordb_host,
            settings.falkordb_port,
        )
        raise typer.Exit(code=1)

    group_id = repo_group_id(repo)
    read_start = time.monotonic()
    episodes = read_spool(group_id)
    read_seconds = time.monotonic() - read_start
    if not episodes:
        log.warning("spool empty for %s; run `relic ingest --no-load --repo %s` first", repo, repo)
        return
    log.info("read %d spooled episodes for %s", len(episodes), repo)

    stats, use_bulk = await _extract(
        repo,
        episodes,
        group_id,
        settings=settings,
        bulk=bulk,
        fresh=fresh,
        no_progress=no_progress,
        limit=limit,
        progress_label=f"extracting {repo}",
        log=log,
    )
    _record_run(stats, repo)

    extracted = stats.loaded + stats.failed
    total_seconds = time.monotonic() - load_start
    setup_seconds = max(0.0, total_seconds - read_seconds - stats.duration_s)
    stderr_console().print(
        format_ingest_timing(
            repo,
            [
                ("read spool", read_seconds),
                ("load (extraction)", stats.duration_s),
                ("setup + index", setup_seconds),
            ],
            total_seconds,
            loaded=stats.loaded,
            skipped=stats.skipped,
            failed=stats.failed,
            per_episode_seconds=stats.duration_s / extracted if extracted else 0.0,
            bulk=use_bulk,
        ),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )

    if stats.loaded == 0 and stats.failed:
        raise typer.Exit(code=1)


@app.command()
def resolve() -> None:
    """Unify people across GitHub and Linear into single Person nodes (Phase 3)."""
    _todo("Phase 3")


@app.command("compile")
def compile_skill(skill: Annotated[str, typer.Option(help="archetype to compile")]) -> None:
    """Detect a procedure and compile it into a grounded SkillIR (Phase 4)."""
    _todo("Phase 4")


@app.command()
def verify(skill_id: Annotated[str, typer.Argument(help="skill id to promote")]) -> None:
    """Promote a draft skill to verified and stamp last_verified_at (Phase 5)."""
    from relic.config import get_settings
    from relic.registry import connect, mark_verified

    conn = connect(get_settings().registry_db_path)
    try:
        skill = mark_verified(conn, skill_id)
    except KeyError:
        console.print(f"[red]no such skill:[/] {skill_id}")
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    finally:
        conn.close()
    stamp = skill.last_verified_at.strftime("%Y-%m-%d %H:%M") if skill.last_verified_at else "?"
    console.print(f"[green]verified[/] [bold]{skill.skill_id}[/] v{skill.semver} ({stamp})")


@app.command()
def emit(repo: Annotated[str, typer.Option(help="target repo for .claude/skills/")]) -> None:
    """Write verified skills and a catalog index to a repo's .claude/skills/ folder."""
    from relic.config import get_settings
    from relic.registry import connect
    from relic.serve import emit_catalog, emit_verified, prune_unverified

    conn = connect(get_settings().registry_db_path)
    try:
        paths = emit_verified(conn, repo)
        pruned = prune_unverified(conn, repo)
        index = emit_catalog(conn, repo)
    finally:
        conn.close()
    if not paths and not pruned:
        console.print("[yellow]no verified skills to emit[/]")
        return
    if paths:
        console.print(f"[green]emitted[/] {len(paths)} skill(s) to [bold]{repo}[/]")
        for path in paths:
            console.print(f"  [dim]{path}[/]")
        if index is not None:
            console.print(f"  [dim]{index} (index)[/]")
    if pruned:
        console.print(f"[yellow]pruned[/] {len(pruned)} deprecated skill(s) from [bold]{repo}[/]")
        for directory in pruned:
            console.print(f"  [dim]{directory}[/]")


@app.command()
def catalog() -> None:
    """Print a browsable markdown index of verified skills."""
    from relic.config import get_settings
    from relic.registry import connect, list_skills
    from relic.serve import render_catalog

    conn = connect(get_settings().registry_db_path)
    try:
        skills = list_skills(conn, status="verified")
    finally:
        conn.close()
    print(render_catalog(skills))


@app.command()
def serve(
    repo: Annotated[
        str | None,
        typer.Option(help="owner/name to scope recall to; defaults to TARGET_REPO"),
    ] = None,
) -> None:
    """Serve verified skills and memory recall as MCP tools over stdio."""
    import asyncio

    asyncio.run(_serve(repo))


@lru_cache(maxsize=512)
def _repo_from_cwd(cwd: str) -> str | None:
    """owner/name (lowercased) from a working dir's git origin, or None.

    A session's cwd tells us which repo it belongs to; the origin remote maps to the
    owner/name ingest scopes by. Lowercased because host routing is case-insensitive, so
    the group is stable regardless of how the remote was typed. Best-effort: no dir, no
    git, no origin, or an unparseable URL all return None and the caller falls back to
    the daemon default. Cached, so the git shell-out runs at most once per distinct cwd.
    """
    import os
    import re
    import subprocess

    cwd = (cwd or "").strip()
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, cwd via -C, no shell
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return None
    # git@host:owner/name.git  or  https://host/owner/name(.git)
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def _scope_for_cwd(cwd: str, default_db: str) -> "tuple[str, str]":
    """Resolve (engram database, group_id) for a session's working dir.

    A cwd that maps to a repo scopes to that repo's slug for BOTH the database and the
    group_id (matching how ingest writes). An unresolved cwd falls back to the daemon's
    concrete default for both, so recall and capture always agree on a concrete group
    and recall never reads the whole graph unfiltered.
    """
    from relic.ingest import repo_group_id

    repo = _repo_from_cwd(cwd)
    if repo:
        slug = repo_group_id(repo)
        return slug, slug
    return default_db, default_db


class _EngramPool:
    """Lazily builds and caches one engram per FalkorDB database (per repo).

    A session can touch any repo, so the daemon can't pin a single engram at boot. The
    pool builds one on first use and reuses it. Building is deferred, so the daemon boots
    even with FalkorDB down (the failure surfaces per-request and degrades there).

    Each database gets its OWN build lock, so a slow cold-build for one repo never
    serializes requests for another. Bounded by an LRU cap so a long-lived daemon that
    visits many repos doesn't grow without limit.
    """

    def __init__(self, settings: "Settings", max_size: int = 32) -> None:
        import asyncio
        from collections import OrderedDict

        self._settings = settings
        self._max = max_size
        self._engrams = OrderedDict()  # database -> engram, in LRU order
        self._locks = {}  # database -> its build lock
        self._meta = asyncio.Lock()  # guards lazy per-database lock creation

    async def _lock_for(self, database: str):
        import asyncio

        lock = self._locks.get(database)
        if lock is None:
            async with self._meta:
                lock = self._locks.get(database)
                if lock is None:
                    lock = asyncio.Lock()
                    self._locks[database] = lock
        return lock

    async def get(self, database: str) -> "GraphitiMemory":
        eng = self._engrams.get(database)
        if eng is not None:
            self._engrams.move_to_end(database)  # LRU touch
            return eng
        async with await self._lock_for(database):
            eng = self._engrams.get(database)
            if eng is None:
                from relic.graph import open_memory

                eng = open_memory(
                    host=self._settings.falkordb_host,
                    port=self._settings.falkordb_port,
                    password=self._settings.falkordb_password,
                    database=database,
                    api_key=self._settings.openai_api_key,
                )
                self._engrams[database] = eng
                self._engrams.move_to_end(database)
                await self._evict_over_cap()
            return eng

    async def _evict_over_cap(self) -> None:
        while len(self._engrams) > self._max:
            db, eng = self._engrams.popitem(last=False)  # least-recently-used
            self._locks.pop(db, None)
            with suppress(Exception):  # eviction close is best-effort
                await eng.close()

    async def close_all(self) -> None:
        for eng in self._engrams.values():
            with suppress(Exception):  # shutdown close is best-effort
                await eng.close()
        self._engrams.clear()
        self._locks.clear()


def _make_recall_fn(
    engram: "GraphitiMemory", group_id: str | None
) -> "Callable[[str, int], Awaitable[str]]":
    async def recall_fn(query: str, num_results: int = 10) -> str:
        from relic.graph import format_answer, recall

        return format_answer(
            await recall(engram, query, group_id=group_id, num_results=num_results)
        )

    return recall_fn


# A coding session is captured as one Session episode (docs/adr/0001, the Session
# amendment). Clip to the recent chars, where the decisions land: 200k would be a
# single huge add_episode call.
_MAX_SESSION_CHARS = 24_000


def _distill_claude_transcript(raw: str) -> str:
    """Turn a Claude Code transcript (JSONL) into clean User/Assistant prose.

    Keeps real user prompts and assistant text; drops thinking, tool calls, tool
    results, and the bookkeeping line types, so extraction sees a conversation rather
    than tool-call noise (the engram ontology has no Decision/ActionItem types yet, so
    clean prose is what gives it a chance). Falls back to the raw text when the input
    is not the expected JSONL (an inline transcript). Clipped to the recent tail.
    """
    import json

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
    return distilled[-_MAX_SESSION_CHARS:]


def _resolve_session_transcript(payload: dict[str, Any]) -> str:
    """Read and distill the session text from a capture payload.

    Prefer the transcript file the hook points at, so the size/curation policy lives
    server-side (tunable without reinstalling hooks) and the HTTP payload stays small;
    fall back to an inline transcript.
    """
    import os

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
    return _distill_claude_transcript(raw)


async def _write_session_episode(
    engram: "GraphitiMemory", group: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Write one finished session into ``group`` as a Session episode.

    The session is distilled to clean prose and run through the same loader ingest uses,
    so existing extraction turns it into typed facts with no new write path. Idempotent
    per session via the group's checkpoint ledger: the episode name is the session id,
    so a repeated SessionEnd (Claude Code can fire it more than once) is skipped, not
    re-extracted into a fork.

    The deterministic ``files_touched`` / ``references`` links (docs/adr/0001 Session
    amendment) stay empty until the capture hook enriches the payload with git metadata;
    a transcript-only capture still lands.
    """
    from datetime import datetime

    from relic.contracts import EpisodeSpec, SessionEpisodeBody
    from relic.graph import load_episodes
    from relic.ingest import checkpoint_path, load_done, record_done

    session_id = str(payload.get("session_id", "")).strip()
    summary = payload.get("summary") or None
    transcript = _resolve_session_transcript(payload)

    ledger = checkpoint_path(group)
    done = load_done(ledger)

    body = SessionEpisodeBody(
        url=f"session://{session_id}",
        title=f"Coding session {session_id[:8]}",
        agent="claude-code",
        cwd=str(payload.get("cwd", "")).strip() or None,
        ended_at=datetime.now(UTC).isoformat(),
        transcript=transcript or None,
        summary=summary,
    )
    spec = EpisodeSpec(
        name=f"Session {session_id}",
        body=body.model_dump_json(),
        source_description="Claude Code session (relic daemon)",
        reference_time=datetime.now(UTC),
        group_id=group,
    )
    stats = await load_episodes(
        engram,
        [spec],
        group_id=group,
        skip=done,
        on_loaded=lambda name, token: record_done(ledger, name, token),
        progress=False,
    )
    return {
        "status": "captured",
        "session_id": session_id,
        "loaded": stats.loaded,
        "skipped": stats.skipped,
        "failed": stats.failed,
    }


def _make_pool_recall_fn(
    pool: "_EngramPool", default_db: str
) -> "Callable[[str, int, str], Awaitable[str]]":
    """Daemon recall: resolve the repo from the session's cwd, recall in that group."""

    async def recall_fn(query: str, num_results: int, cwd: str) -> str:
        from relic.graph import format_answer, recall

        database, group_id = _scope_for_cwd(cwd, default_db)
        engram = await pool.get(database)
        return format_answer(
            await recall(engram, query, group_id=group_id, num_results=num_results)
        )

    return recall_fn


def _make_pool_capture_fn(
    pool: "_EngramPool", default_db: str
) -> "Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]":
    """Daemon write-back: resolve the repo from the session's cwd, write to that group."""

    async def capture_fn(payload: dict[str, Any]) -> dict[str, Any]:
        database, group = _scope_for_cwd(str(payload.get("cwd", "")), default_db)
        engram = await pool.get(database)
        return await _write_session_episode(engram, group, payload)

    return capture_fn


async def _serve(repo: str | None = None) -> None:
    from relic.config import get_settings
    from relic.ingest import repo_group_id
    from relic.obs import get_logger
    from relic.registry import connect
    from relic.serve import build_server

    log = get_logger("serve")
    settings = get_settings()
    # Recall reads one FalkorDB graph: the repo's group_id partition that ingest wrote
    # to. Without this scope serve reads the empty default graph and recall_memory
    # returns nothing even with data ingested. Mirrors `relic recall --repo`.
    repo = repo or settings.target_repo
    group_id = repo_group_id(repo) if repo else None
    conn = connect(settings.registry_db_path)
    engram = None
    recall_fn = None
    try:
        from relic.graph import open_memory

        engram = open_memory(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password,
            database=group_id or settings.falkordb_database,
            api_key=settings.openai_api_key,
        )
        recall_fn = _make_recall_fn(engram, group_id)
        log.info("memory recall scoped to %s", group_id or settings.falkordb_database)
    except Exception as exc:  # noqa: BLE001 - recall is optional; still serve skills
        # The logger writes to stderr: stdout is the MCP transport and any bytes on it
        # would corrupt the stream.
        log.warning("recall disabled: %s", exc)

    server = build_server(conn, recall_fn=recall_fn)
    try:
        await server.run_stdio_async()
    finally:
        conn.close()
        if engram is not None:
            await engram.close()


@app.command()
def daemon(
    repo: Annotated[
        str | None,
        typer.Option(help="owner/name to scope the loop to; defaults to TARGET_REPO"),
    ] = None,
    host: Annotated[str, typer.Option(help="bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="bind port")] = 8788,
    token: Annotated[
        str | None,
        typer.Option(help="require Authorization: Bearer <token>; defaults to $RELIC_DAEMON_TOKEN"),
    ] = None,
) -> None:
    """Run the closed-loop daemon: recall on inject, write-back on capture.

    A loopback HTTP surface the Claude Code hooks call every turn: POST
    /v1/daemon/inject reflects the engram onto the prompt, POST /v1/daemon/capture
    writes the finished session back, GET /v1/daemon/status reports liveness and loop
    counters. Scoped per session: each call resolves the repo from the session's cwd
    and uses that repo's engram, falling back to --repo (or TARGET_REPO) otherwise.
    Install the hooks with `relic install-hooks`.
    """
    import asyncio

    asyncio.run(_daemon(repo, host, port, token))


async def _daemon(repo: str | None, host: str, port: int, token: str | None) -> None:
    import os

    import uvicorn

    from relic.config import get_settings
    from relic.ingest import repo_group_id
    from relic.obs import get_logger
    from relic.serve import build_daemon_app

    log = get_logger("daemon")
    settings = get_settings()
    repo = (repo or settings.target_repo or "").strip().lower() or None
    # The daemon scopes per session: each inject/capture resolves the repo from the
    # session's cwd (git origin) and uses that repo's engram, falling back to this
    # concrete default when the cwd is not a known repo. Engrams are built lazily per
    # repo by the pool, so the daemon boots even with FalkorDB down (recall/capture then
    # error per-request, caught by the surface and shown as degraded in the app).
    default_db = repo_group_id(repo) if repo else settings.falkordb_database
    pool = _EngramPool(settings)
    app_ = build_daemon_app(
        recall=_make_pool_recall_fn(pool, default_db),
        capture=_make_pool_capture_fn(pool, default_db),
        # empty/blank falls through to None (no auth), never the literal empty string
        token=token or os.environ.get("RELIC_DAEMON_TOKEN") or None,
    )
    # Pre-warm the default engram so the common case (a session in the --repo repo) is
    # hot on the first inject instead of paying the cold build inside the 2s hook.
    try:
        await pool.get(default_db)
    except Exception as exc:  # noqa: BLE001 - best-effort; a down engram still boots degraded
        log.warning("engram pre-warm failed, daemon degraded until it recovers: %s", exc)
    log.info("daemon on http://%s:%d, default scope %s, per-session by cwd", host, port, default_db)
    config = uvicorn.Config(app_, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await pool.close_all()


@app.command(name="install-hooks")
def install_hooks(
    settings_path: Annotated[
        Path | None,
        typer.Option(
            "--settings", help="Claude Code settings.json (default: ~/.claude/settings.json)"
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(help="daemon token to bake into the hook env; default $RELIC_DAEMON_TOKEN"),
    ] = None,
) -> None:
    """Wire the Relic daemon into Claude Code: inject on prompt, capture on session end.

    Merges two command hooks into your Claude Code settings.json — UserPromptSubmit ->
    inject, SessionEnd -> capture — both pointing at the stdlib shim. Idempotent:
    re-running does not duplicate entries. Backs up an existing file to <name>.bak
    first. If the daemon runs with a token, it is baked into the hook command's env so
    the hooks can authenticate (loopback-only, so the plaintext is acceptable). After
    this, start the loop with `relic daemon --repo owner/name`.
    """
    import json
    import os
    import sys

    path = settings_path or Path.home() / ".claude" / "settings.json"
    shim = Path(__file__).resolve().parent / "hooks" / "relic_hook.py"
    # One source of truth for the token: whatever the daemon will use, the hook gets
    # too. Baked into the command env so a --token daemon still authenticates.
    hook_token = token or os.environ.get("RELIC_DAEMON_TOKEN") or None

    settings: dict[str, Any] = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]could not parse {path}: {exc}[/]")
            raise typer.Exit(code=1) from exc
        path.with_suffix(path.suffix + ".bak").write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )

    env_prefix = f"RELIC_DAEMON_TOKEN={hook_token} " if hook_token else ""
    hooks = settings.setdefault("hooks", {})
    added: list[str] = []
    for event, mode in (("UserPromptSubmit", "inject"), ("SessionEnd", "capture")):
        command = f'{env_prefix}{sys.executable} "{shim}" {mode}'
        # Match on the shim+mode tail, not the whole command, so a re-run after the venv
        # python path or token changes refreshes the existing hook in place instead of
        # doubling it.
        if _ensure_hook(hooks, event, command, marker=f'"{shim}" {mode}'):
            added.append(event)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    if added:
        console.print(f"[green]installed[/] Relic hooks: {', '.join(added)}")
    else:
        console.print("Relic hooks already installed; nothing to change.")
    console.print(f"settings: {path}")
    console.print("start the loop with [bold]relic daemon --repo owner/name[/].")


def _ensure_hook(hooks: dict[str, Any], event: str, command: str, *, marker: str) -> bool:
    """Add or refresh our command hook for ``event``. Returns True if anything changed.

    An existing entry matching ``marker`` (our shim+mode) is refreshed in place when the
    full command differs (interpreter path or baked token changed), so re-running never
    leaves a stale duplicate. Mirrors Claude Code's hook shape:
    ``hooks[event] = [{"hooks": [{"type": "command", "command": ...}]}]``.
    """
    groups = hooks.setdefault(event, [])
    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("type") == "command" and marker in str(entry.get("command", "")):
                if entry.get("command") == command:
                    return False  # already exactly right
                entry["command"] = command  # refresh a stale interpreter / token
                return True
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return True


# Run-history ledger: ingest/load append a record here as they complete;
# serve-http's /v1/ingest/runs reads it. JSONL, one run per line.
_RUNS_LEDGER = Path("data/ingest/runs.jsonl")


def _record_run(stats: "LoadStats", repo: str, *, source: str = "GitHub") -> None:
    """Append a run record to the run-history ledger, for the web app's Ingest view.

    Called after extraction completes (so a capture-only --no-load run records
    nothing). Shapes the LoadStats into the JSON the web app's adapter expects.
    """
    import json
    from datetime import UTC, datetime, timedelta

    finished = datetime.now(UTC)
    started = finished - timedelta(seconds=stats.duration_s)
    note: str | None = None
    if stats.failures:
        first = stats.failures[0]
        reason = first[1] if len(first) > 1 else ""
        note = f"{stats.failed} failed: {reason}".strip().rstrip(": ")
    record = {
        "id": f"{stats.group_id}-{int(finished.timestamp() * 1000)}",
        "repo": repo,
        "source": source,
        "startedAt": started.isoformat(),
        "durationSeconds": round(stats.duration_s, 1),
        "attempted": stats.attempted,
        "loaded": stats.loaded,
        "skipped": stats.skipped,
        "failed": stats.failed,
        "status": "failed" if stats.failed and stats.loaded == 0 else "success",
        "note": note,
    }
    _RUNS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with _RUNS_LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _connector_status() -> dict[str, Any]:
    """Synthesize per-source sync status from the on-disk ingest checkpoints.

    Reads the per-repo checkpoint ledgers under data/ingest/ (the only persisted
    ingest state) plus which source keys are configured. No graph connection: the
    connector control plane only needs what has landed and when. Episode names
    carry the source and type: "PR owner/name#42" is a github pull request,
    "Issue owner/name#7" a github issue, "Issue REL-10" a Linear issue. Counts,
    repos, and last-sync are tracked per source so one source's activity never
    bleeds into the other's connector card.
    """
    from datetime import datetime

    from relic.config import get_settings
    from relic.ingest import load_done

    settings = get_settings()
    base = Path("data/ingest")
    ledgers = sorted(base.glob("*.log")) if base.exists() else []

    github_prs = 0
    linear_issues = 0
    github_repos: list[str] = []
    linear_repos: list[str] = []
    github_mtime = 0.0
    linear_mtime = 0.0
    for ledger in ledgers:
        repo = ledger.stem.replace("__", "/")
        mtime = ledger.stat().st_mtime
        has_github = False
        has_linear = False
        for name in load_done(ledger):
            if name.startswith("PR "):
                github_prs += 1
                has_github = True
            elif "#" in name:  # "Issue owner/name#7": a github issue
                has_github = True
            elif name.startswith("Issue "):  # "Issue REL-10": a linear issue
                linear_issues += 1
                has_linear = True
        if has_github:
            github_repos.append(repo)
            github_mtime = max(github_mtime, mtime)
        if has_linear:
            linear_repos.append(repo)
            linear_mtime = max(linear_mtime, mtime)

    def _iso(mtime: float) -> str | None:
        return datetime.fromtimestamp(mtime, tz=UTC).isoformat() if mtime else None

    return {
        "connectors": [
            {
                "source": "github",
                "connected": bool(settings.github_token) or bool(github_repos),
                "itemCount": github_prs,
                "itemLabel": "pull requests",
                "lastSyncAt": _iso(github_mtime),
                "repos": github_repos,
            },
            {
                "source": "linear",
                "connected": bool(settings.linear_api_key) or bool(linear_repos),
                "itemCount": linear_issues,
                "itemLabel": "issues",
                "lastSyncAt": _iso(linear_mtime),
                "repos": linear_repos,
            },
        ]
    }


def _ingest_runs(repo: str | None, limit: int) -> dict[str, Any]:
    """Ingest run history plus totals.

    Run records are appended to data/ingest/runs.jsonl as ingests complete; this
    returns the most recent ``limit`` (filtered by repo when given), newest first.
    ``totals`` is synthesized from the checkpoints so the page has live numbers
    even before the first recorded run.
    """
    import json
    from datetime import datetime

    from relic.config import get_settings
    from relic.ingest import checkpoint_path, load_done, repo_group_id

    runs: list[dict[str, Any]] = []
    runs_path = _RUNS_LEDGER
    if runs_path.exists():
        for raw in runs_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if repo and record.get("repo") != repo:
                continue
            runs.append(record)
    runs = runs[-limit:][::-1]

    # totals: when a repo is given, scope to that repo's ledger only. the web app
    # fans out one /v1/ingest/runs call per connected repo and sums totals.memories,
    # so a global total here would overcount once more than one repo is connected.
    if repo:
        repo_ledger = checkpoint_path(repo_group_id(repo))
        ledgers = [repo_ledger] if repo_ledger.exists() else []
    else:
        base = Path("data/ingest")
        ledgers = sorted(base.glob("*.log")) if base.exists() else []
    total = 0
    last_mtime = 0.0
    for ledger in ledgers:
        total += len(load_done(ledger))
        last_mtime = max(last_mtime, ledger.stat().st_mtime)

    settings = get_settings()
    sources_connected = sum(
        1 for key in (settings.github_token, settings.linear_api_key) if key
    ) or (1 if total else 0)
    last_run = datetime.fromtimestamp(last_mtime, tz=UTC).isoformat() if last_mtime else None
    return {
        "runs": runs,
        "totals": {
            "memories": total,
            "sourcesConnected": sources_connected,
            "lastRunAt": last_run,
        },
    }


# Repo -> the running `relic ingest` subprocess, so a second trigger for the same
# repo while one is in flight is a conflict, not a duplicate run.
_INGEST_PROCS: dict[str, Any] = {}

# When the parent forwards a user token in GITHUB_TOKEN, it stashes its own server
# token here so the child can fall back to it if the user token is expired/revoked.
# A separate var because the env GITHUB_TOKEN now holds the user token, shadowing
# the server's .env value (env vars beat .env in pydantic-settings).
_SERVER_TOKEN_ENV = "RELIC_SERVER_GITHUB_TOKEN"


def _resolve_server_token() -> str | None:
    """The server's own github token from settings (its GITHUB_TOKEN env or .env value).

    Used to stash a fallback for a forwarded user token. A raw os.environ read would miss a
    token that lives only in .env, so read it through settings like the rest of the app.
    Returns None when the server has no token of its own: then there is nothing to fall back
    to. We deliberately do not shell out to `gh auth token` here. this runs on the ingest
    trigger path, and a `gh` login is a local-dev convenience, not a server's fallback
    identity.
    """
    from relic.config import get_settings

    return get_settings().github_token or None


def _trigger_ingest(repo: str, token: str | None = None) -> dict[str, Any]:
    """Kick off a background ingest for ``repo`` and return its status.

    Spawns `relic ingest --repo <repo>` as a subprocess, isolated from the
    server's event loop. The checkpoint makes it effectively incremental: a
    re-run only extracts episodes that are new, so a webhook can call this on
    every push and only the new PR or issue gets loaded. A single-item fetch is a
    later optimization that would avoid re-fetching the whole repo each time.

    ``token`` is the connecting user's GitHub token, so ingest reads their repos
    and not just ours. It goes to the child through its environment, never argv,
    so it does not leak to the process list. The child's settings.github_token
    picks it up; no token means the child inherits the server's own credentials
    (its GITHUB_TOKEN, else its `gh auth` login).

    When a user token is forwarded, the server's own GITHUB_TOKEN is stashed in
    RELIC_SERVER_GITHUB_TOKEN so the child can fall back to it if the user token is
    expired or revoked (a runtime auth failure that format validation can't catch).
    """
    import os
    import subprocess
    import sys

    running = _INGEST_PROCS.get(repo)
    if running is not None and running.poll() is None:
        return {"status": "already_running", "repo": repo}
    if token:
        # GITHUB_TOKEN carries the user token (settings.github_token reads it); the
        # server's own token rides along under a separate var as the fallback, read
        # through settings so it is found even when it lives only in .env.
        env = {**os.environ, "GITHUB_TOKEN": token}
        # drop any inherited value first so a stale RELIC_SERVER_GITHUB_TOKEN in the parent
        # env can never ride into the child posing as the fallback identity.
        env.pop(_SERVER_TOKEN_ENV, None)
        server_token = _resolve_server_token()
        if server_token and server_token != token:
            env[_SERVER_TOKEN_ENV] = server_token
    else:
        env = None
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "relic", "ingest", "--repo", repo], env=env
    )
    _INGEST_PROCS[repo] = proc
    return {"status": "running", "repo": repo}


@app.command(name="serve-http")
def serve_http(
    host: Annotated[str, typer.Option(help="bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="bind port")] = 8787,
    token: Annotated[
        str | None,
        typer.Option(help="require Authorization: Bearer <token>; defaults to $RELIC_HTTP_TOKEN"),
    ] = None,
) -> None:
    """Serve connector status and the ingest trigger over HTTP for the web app.

    GET /v1/connectors, GET /v1/ingest/runs, GET /v1/status, POST /v1/ingest.
    Status reads the ingest checkpoints on disk (no graph needed); the trigger
    spawns `relic ingest`, so a full run still needs FalkorDB and the source keys.
    """
    import os

    import uvicorn

    from relic.obs import get_logger
    from relic.serve import build_http_app

    log = get_logger("serve-http")

    async def connectors() -> dict[str, Any]:
        return _connector_status()

    async def ingest_runs(repo: str | None, limit: int) -> dict[str, Any]:
        return _ingest_runs(repo, limit)

    async def ingest_trigger(repo: str, token: str | None) -> dict[str, Any]:
        return _trigger_ingest(repo, token)

    api = build_http_app(
        connectors=connectors,
        ingest_runs=ingest_runs,
        ingest_trigger=ingest_trigger,
        token=token or os.environ.get("RELIC_HTTP_TOKEN"),
    )
    log.info("serving connector status + ingest trigger on http://%s:%d", host, port)
    uvicorn.run(api, host=host, port=port, log_level="info")


@app.command()
def register(
    path: Annotated[
        Path, typer.Argument(help="path to a SkillIR JSON file", exists=True, dir_okay=False)
    ],
) -> None:
    """Load a SkillIR JSON file into the registry as a skill (manual authoring path)."""
    from pydantic import ValidationError

    from relic.config import get_settings
    from relic.contracts import SkillIR
    from relic.registry import connect, upsert_skill

    try:
        skill = SkillIR.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        console.print(f"[red]invalid SkillIR in {path}:[/]\n{exc}")
        raise typer.Exit(code=1) from None

    conn = connect(get_settings().registry_db_path)
    try:
        upsert_skill(conn, skill)
    finally:
        conn.close()
    console.print(
        f"[green]registered[/] [bold]{skill.skill_id}[/] v{skill.semver} ([dim]{skill.status}[/])"
    )


@app.command("list")
def list_skills_command(
    status: Annotated[str | None, typer.Option(help="filter by draft|verified|deprecated")] = None,
) -> None:
    """List registered skills."""
    from rich.table import Table

    from relic.config import get_settings
    from relic.registry import connect, list_skills

    conn = connect(get_settings().registry_db_path)
    try:
        skills = list_skills(conn, status=status)
    finally:
        conn.close()
    if not skills:
        console.print("[yellow]no skills registered[/]")
        return
    colors = {"verified": "green", "draft": "yellow", "deprecated": "dim"}
    table = Table(box=None)
    for column in ("skill_id", "ver", "status", "scope", "title"):
        table.add_column(column)
    for skill in skills:
        color = colors.get(skill.status, "")
        status_cell = f"[{color}]{skill.status}[/]" if color else skill.status
        table.add_row(skill.skill_id, skill.semver, status_cell, skill.scope, skill.title)
    console.print(table)


@app.command()
def show(
    skill_id: Annotated[str, typer.Argument(help="skill id to show")],
    as_json: Annotated[bool, typer.Option("--json", help="output raw SkillIR JSON")] = False,
) -> None:
    """Show a skill's rendered SKILL.md, or its raw SkillIR JSON with --json."""
    from relic.config import get_settings
    from relic.registry import connect, get_skill
    from relic.serve import render

    conn = connect(get_settings().registry_db_path)
    try:
        skill = get_skill(conn, skill_id)
    finally:
        conn.close()
    if skill is None:
        console.print(f"[red]no such skill:[/] {skill_id}")
        raise typer.Exit(code=1)
    print(skill.model_dump_json(indent=2) if as_json else render(skill))


@app.command()
def deprecate(skill_id: Annotated[str, typer.Argument(help="skill id to deprecate")]) -> None:
    """Mark a skill as deprecated so it is no longer emitted or served."""
    from relic.config import get_settings
    from relic.registry import connect, set_status

    conn = connect(get_settings().registry_db_path)
    try:
        skill = set_status(conn, skill_id, "deprecated")
    except KeyError:
        console.print(f"[red]no such skill:[/] {skill_id}")
        raise typer.Exit(code=1) from None
    finally:
        conn.close()
    console.print(f"[yellow]deprecated[/] [bold]{skill.skill_id}[/] v{skill.semver}")


@app.command("recall")
def recall_command(
    query: Annotated[str, typer.Argument(help="what to recall from team memory")],
    repo: Annotated[
        str | None, typer.Option(help="owner/name to scope the recall, defaults to TARGET_REPO")
    ] = None,
    num_results: Annotated[int, typer.Option(help="max facts to return")] = 10,
) -> None:
    """Recall facts from the memory graph, with their sources."""
    import asyncio

    asyncio.run(_recall(query, repo, num_results))


async def _recall(query: str, repo: str | None, num_results: int) -> None:
    from relic.config import get_settings
    from relic.graph import format_answer, open_memory, recall
    from relic.ingest import repo_group_id

    settings = get_settings()
    repo = repo or settings.target_repo
    group_id = repo_group_id(repo) if repo else None
    engram = open_memory(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=group_id or settings.falkordb_database,
        api_key=settings.openai_api_key,
    )
    try:
        answer = await recall(engram, query, group_id=group_id, num_results=num_results)
    finally:
        await engram.close()
    print(format_answer(answer))


@app.command("eval")
def eval_recall(
    path: Annotated[Path, typer.Option(help="scorecard gold set JSON")] = Path(
        "eval/github_recall.json"
    ),
    num_results: Annotated[int, typer.Option(help="facts per question")] = 10,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="also write machine-readable results here")
    ] = None,
) -> None:
    """Score recall against a gold set: does it cite the PR that holds each answer?"""
    import asyncio

    asyncio.run(_eval(path, num_results, json_out))


async def _eval(path: Path, num_results: int, json_out: Path | None) -> None:
    import json

    from relic.config import get_settings
    from relic.graph import open_memory, recall
    from relic.ingest import repo_group_id
    from relic.scorecard import load_gold, score_case, summarize, to_payload

    try:
        gold = load_gold(path)
    except (OSError, ValueError) as exc:
        err_console.print(f"[red]bad gold set:[/] {exc}")
        raise typer.Exit(code=1) from exc
    settings = get_settings()
    group_id = repo_group_id(gold.repo)
    engram = open_memory(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=group_id or settings.falkordb_database,
        api_key=settings.openai_api_key,
    )
    results = []
    try:
        for case in gold.cases:
            answer = await recall(engram, case.question, group_id=group_id, num_results=num_results)
            results.append(score_case(case, gold.repo, answer))
    finally:
        await engram.close()
    print(summarize(results))
    if json_out:
        json_out.write_text(json.dumps(to_payload(results), indent=2) + "\n", encoding="utf-8")


@app.command()
def doctor() -> None:
    """Report registry, graph, and key status for this setup. Reads only, changes nothing."""
    from relic.config import get_settings
    from relic.doctor import diagnose, format_report

    print(format_report(diagnose(get_settings())))


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="graph query string")],
    repo: Annotated[
        str | None, typer.Option(help="owner/name to scope the query, defaults to TARGET_REPO")
    ] = None,
) -> None:
    """Query the graph, e.g. reviewers of a path (Phase 2)."""
    import asyncio

    asyncio.run(_query(text, repo))


async def _query(text: str, repo: str | None) -> None:
    from relic.config import get_settings
    from relic.graph import open_memory, reviewers_of
    from relic.ingest import repo_group_id

    settings = get_settings()
    # FalkorDB partitions each repo into its own graph named by group_id, so the
    # client must target that graph. Without a repo we query the default database,
    # which only holds ungrouped data.
    repo = repo or settings.target_repo
    group_id = repo_group_id(repo) if repo else None
    engram = open_memory(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=group_id or settings.falkordb_database,
        api_key=settings.openai_api_key,
    )
    try:
        hits = await reviewers_of(engram, text, group_id=group_id)
    finally:
        await engram.close()

    if not hits:
        console.print("[yellow]no matches[/]")
        return
    seen: set[str] = set()
    for hit in hits:
        if hit.name in seen:
            continue
        seen.add(hit.name)
        console.print(f"[bold]{hit.name}[/] {hit.relation}: {hit.fact}")
        if hit.profile_url:
            console.print(f"  [dim]{hit.profile_url}[/]")
        if hit.episodes:
            console.print(f"  [dim]source: {', '.join(hit.episodes)}[/]")


if __name__ == "__main__":
    app()
