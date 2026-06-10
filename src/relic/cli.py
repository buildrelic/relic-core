"""Relic command-line interface.

Only typer/rich are imported at module load so `relic --help` stays fast and
import-clean. Heavier work is imported inside each command as it lands in its
phase.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from graphiti_core import Graphiti

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
        int | None, typer.Option(help="cap merged PRs and issues pulled, most recent first")
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
) -> None:
    """Pull merged PRs, reviews, and issues into the graph (Phase 2)."""
    import asyncio

    from relic.obs import get_logger

    try:
        asyncio.run(
            _ingest(repo, limit, months=months, bulk=bulk, fresh=fresh, no_progress=no_progress)
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


def _safe_ident(identifier: str) -> str:
    return identifier.replace("/", "_").replace("#", "-")


async def _ingest(
    repo: str,
    limit: int | None = None,
    *,
    months: int = 12,
    bulk: bool = False,
    fresh: bool = False,
    no_progress: bool = False,
) -> None:
    import asyncio
    import logging
    import time
    from collections.abc import Callable

    from relic.config import get_settings
    from relic.graph.engram import falkordb_reachable, make_engram
    from relic.graph.load import LoadStats, load_episodes, load_episodes_bulk
    from relic.ingest.checkpoint import checkpoint_path, clear, load_done, record_done
    from relic.ingest.github import fetch_repo, make_github, resolve_github_token
    from relic.ingest.linear import fetch_issues, linear_enabled
    from relic.ingest.mappers import RepoBundle, issue_to_episode, pr_to_episode, repo_group_id
    from relic.ingest.raw_store import dump_raw
    from relic.obs import get_logger, stderr_console

    log = get_logger("ingest")

    # The FalkorDB driver schedules an index build in its constructor as a detached
    # task. If the graph is unhealthy it fails there too, and asyncio dumps a full
    # traceback to stderr. Route those orphaned-task errors to debug: the run's own
    # error reporting already covers the failure on the foreground path.
    def _on_loop_error(_loop: object, context: dict) -> None:
        log.debug(
            "background task error: %s", context.get("message"), exc_info=context.get("exception")
        )

    asyncio.get_running_loop().set_exception_handler(_on_loop_error)

    if "/" not in repo:
        err_console.print("[red]--repo must be owner/name[/]")
        raise typer.Exit(code=2)
    settings = get_settings()
    owner, name = repo.split("/", 1)

    if not falkordb_reachable(settings.falkordb_host, settings.falkordb_port):
        log.error(
            "FalkorDB not reachable at %s:%s. Start it with `just up`.",
            settings.falkordb_host,
            settings.falkordb_port,
        )
        raise typer.Exit(code=1)

    fetch_start = time.monotonic()
    token = resolve_github_token(settings)
    async with make_github(token) as gh:
        bundle = await fetch_repo(
            gh, owner, name, concurrency=settings.fetch_concurrency, limit=limit, months=months
        )
    log.info(
        "fetched %d merged PRs, %d issues from %s in %.1fs",
        len(bundle.pull_requests),
        len(bundle.issues),
        repo,
        time.monotonic() - fetch_start,
    )

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

    log.debug("wrote %d raw payloads under data/raw", raw_count)

    if not episodes:
        log.warning("nothing to ingest")
        return

    ledger = checkpoint_path(group_id)
    if fresh:
        clear(ledger)
    done = load_done(ledger)

    engram = make_engram(
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
            "skip": done,
            "on_loaded": lambda name: record_done(ledger, name),
            "on_progress": on_progress,
            "progress": on_progress is None,
        }
        if use_bulk:
            return await load_episodes_bulk(
                engram, episodes, batch_size=settings.bulk_batch_size, **common
            )
        return await load_episodes(engram, episodes, **common)

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
                task = bar.add_task(f"ingesting {repo}", total=len(episodes), counts="")

                def _on_progress(s: LoadStats) -> None:
                    bar.update(
                        task,
                        completed=s.loaded + s.skipped + s.failed,
                        counts=f"[green]{s.loaded}✓[/] [yellow]{s.skipped}⤳[/] [red]{s.failed}✗[/]",
                    )

                stats = await _run(_on_progress)
        else:
            stats = await _run(None)
    finally:
        await engram.close()

    summary = (
        f"ingested {stats.loaded} episodes from {repo} in {stats.duration_s:.1f}s "
        f"({stats.skipped} skipped, {stats.failed} failed)"
    )
    if stats.failed:
        log.warning(summary)
    else:
        log.info(summary)
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
    from relic.registry.store import connect, mark_verified

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
    from relic.registry.store import connect
    from relic.serve.emit_files import emit_catalog, emit_verified, prune_unverified

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
    from relic.registry.store import connect, list_skills
    from relic.serve.catalog import render_catalog

    conn = connect(get_settings().registry_db_path)
    try:
        skills = list_skills(conn, status="verified")
    finally:
        conn.close()
    print(render_catalog(skills))


@app.command()
def serve() -> None:
    """Serve verified skills and memory recall as MCP tools over stdio."""
    import asyncio

    asyncio.run(_serve())


def _make_recall_fn(engram: "Graphiti") -> "Callable[[str, int], Awaitable[str]]":
    async def recall_fn(query: str, num_results: int = 10) -> str:
        from relic.graph.recall import format_answer, recall

        return format_answer(await recall(engram, query, num_results=num_results))

    return recall_fn


async def _serve() -> None:
    from relic.config import get_settings
    from relic.obs import get_logger
    from relic.registry.store import connect
    from relic.serve.mcp_server import build_server

    log = get_logger("serve")
    settings = get_settings()
    conn = connect(settings.registry_db_path)
    engram = None
    recall_fn = None
    try:
        from relic.graph.engram import make_engram

        engram = make_engram(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password,
            database=settings.falkordb_database,
            api_key=settings.openai_api_key,
        )
        recall_fn = _make_recall_fn(engram)
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
def register(
    path: Annotated[
        Path, typer.Argument(help="path to a SkillIR JSON file", exists=True, dir_okay=False)
    ],
) -> None:
    """Load a SkillIR JSON file into the registry as a skill (manual authoring path)."""
    from pydantic import ValidationError

    from relic.config import get_settings
    from relic.ontology.skill_ir import SkillIR
    from relic.registry.store import connect, upsert_skill

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
    from relic.registry.store import connect, list_skills

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
    from relic.compile.render import render
    from relic.config import get_settings
    from relic.registry.store import connect, get_skill

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
    from relic.registry.store import connect, set_status

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
    from relic.graph.engram import make_engram
    from relic.graph.recall import format_answer, recall
    from relic.ingest.mappers import repo_group_id

    settings = get_settings()
    repo = repo or settings.target_repo
    group_id = repo_group_id(repo) if repo else None
    engram = make_engram(
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
    from relic.graph.engram import make_engram
    from relic.graph.recall import recall
    from relic.ingest.mappers import repo_group_id
    from relic.scorecard import load_gold, score_case, summarize, to_payload

    try:
        gold = load_gold(path)
    except (OSError, ValueError) as exc:
        err_console.print(f"[red]bad gold set:[/] {exc}")
        raise typer.Exit(code=1) from exc
    settings = get_settings()
    group_id = repo_group_id(gold.repo)
    engram = make_engram(
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
    from relic.graph.engram import make_engram
    from relic.graph.queries import reviewers_of
    from relic.ingest.mappers import repo_group_id

    settings = get_settings()
    # FalkorDB partitions each repo into its own graph named by group_id, so the
    # client must target that graph. Without a repo we query the default database,
    # which only holds ungrouped data.
    repo = repo or settings.target_repo
    group_id = repo_group_id(repo) if repo else None
    engram = make_engram(
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
