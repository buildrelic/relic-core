"""Relic command-line interface.

Only typer/rich are imported at module load so `relic --help` stays fast and
import-clean. Heavier work is imported inside each command as it lands in its
phase.
"""

from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="relic",
    help="Relic: ingest engineering history, compile it into grounded skills.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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
    _todo("Phase 5")


@app.command()
def emit(repo: Annotated[str, typer.Option(help="target repo for .claude/skills/")]) -> None:
    """Write verified skills to a repo's .claude/skills/ folder (Phase 6)."""
    _todo("Phase 6")


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
