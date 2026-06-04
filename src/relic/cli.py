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


@app.command()
def ingest(repo: Annotated[str, typer.Option(help="owner/name to ingest")]) -> None:
    """Pull merged PRs, reviews, and issues into the graph (Phase 2)."""
    import asyncio

    asyncio.run(_ingest(repo))


def _safe_ident(identifier: str) -> str:
    return identifier.replace("/", "_").replace("#", "-")


async def _ingest(repo: str) -> None:
    from relic.config import get_settings
    from relic.graph.engram import make_engram
    from relic.graph.load import load_episodes
    from relic.ingest.github import fetch_repo, make_github, resolve_github_token
    from relic.ingest.linear import fetch_issues, linear_enabled
    from relic.ingest.mappers import RepoBundle, issue_to_episode, pr_to_episode, repo_group_id
    from relic.ingest.raw_store import dump_raw

    if "/" not in repo:
        console.print("[red]--repo must be owner/name[/]")
        raise typer.Exit(code=2)
    settings = get_settings()
    owner, name = repo.split("/", 1)

    token = resolve_github_token(settings)
    async with make_github(token) as gh:
        bundle = await fetch_repo(gh, owner, name, concurrency=settings.semaphore_limit)
    console.print(f"fetched {len(bundle.pull_requests)} merged PRs, {len(bundle.issues)} issues")

    for pr in bundle.pull_requests:
        dump_raw(pr.raw, source="github", ident=f"pr-{pr.number}")
    for issue in bundle.issues:
        dump_raw(issue.raw, source="github", ident=_safe_ident(issue.identifier))

    episodes = [pr_to_episode(pr, bundle) for pr in bundle.pull_requests]
    episodes += [issue_to_episode(issue, bundle) for issue in bundle.issues]
    group_id = repo_group_id(bundle.full_name)

    if linear_enabled(settings) and settings.linear_api_key:
        linear_issues = await fetch_issues(settings.linear_api_key)
        linear_repo = RepoBundle(full_name="linear", url="https://linear.app", default_branch="")
        for issue in linear_issues:
            dump_raw(issue.raw, source="linear", ident=_safe_ident(issue.identifier))
        episodes += [issue_to_episode(issue, linear_repo) for issue in linear_issues]
        console.print(f"fetched {len(linear_issues)} Linear issues")
    else:
        console.print("[dim]Linear skipped (LINEAR_API_KEY not set)[/]")

    if not episodes:
        console.print("[yellow]nothing to ingest[/]")
        return

    engram = make_engram(
        settings.engram_db_path,
        api_key=settings.openai_api_key,
        max_coroutines=settings.semaphore_limit,
    )
    try:
        stats = await load_episodes(engram, episodes, group_id=group_id)
    finally:
        await engram.close()
    console.print(f"[green]ingested[/] {stats.episodes} episodes from [bold]{repo}[/]")


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
    from relic.serve.emit_files import emit_catalog, emit_verified

    conn = connect(get_settings().registry_db_path)
    try:
        paths = emit_verified(conn, repo)
        index = emit_catalog(conn, repo)
    finally:
        conn.close()
    if not paths:
        console.print("[yellow]no verified skills to emit[/]")
        return
    console.print(f"[green]emitted[/] {len(paths)} skill(s) to [bold]{repo}[/]")
    for path in paths:
        console.print(f"  [dim]{path}[/]")
    if index is not None:
        console.print(f"  [dim]{index} (index)[/]")


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
    from relic.registry.store import connect
    from relic.serve.mcp_server import build_server

    settings = get_settings()
    conn = connect(settings.registry_db_path)
    engram = None
    recall_fn = None
    try:
        from relic.graph.engram import make_engram

        engram = make_engram(settings.engram_db_path, api_key=settings.openai_api_key)
        recall_fn = _make_recall_fn(engram)
    except Exception as exc:  # noqa: BLE001 - recall is optional; still serve skills
        # stderr, not stdout: stdout is the MCP transport and any bytes on it corrupt the stream
        err_console.print(f"[yellow]recall disabled: {exc}[/]")

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
    num_results: Annotated[int, typer.Option(help="max facts to return")] = 10,
) -> None:
    """Recall facts from the memory graph, with their sources."""
    import asyncio

    asyncio.run(_recall(query, num_results))


async def _recall(query: str, num_results: int) -> None:
    from relic.config import get_settings
    from relic.graph.engram import make_engram
    from relic.graph.recall import format_answer, recall

    settings = get_settings()
    engram = make_engram(settings.engram_db_path, api_key=settings.openai_api_key)
    try:
        answer = await recall(engram, query, num_results=num_results)
    finally:
        await engram.close()
    print(format_answer(answer))


@app.command()
def query(text: Annotated[str, typer.Argument(help="graph query string")]) -> None:
    """Query the graph, e.g. reviewers of a path (Phase 2)."""
    import asyncio

    asyncio.run(_query(text))


async def _query(text: str) -> None:
    from relic.config import get_settings
    from relic.graph.engram import make_engram
    from relic.graph.queries import reviewers_of

    settings = get_settings()
    engram = make_engram(settings.engram_db_path, api_key=settings.openai_api_key)
    try:
        hits = await reviewers_of(engram, text)
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
