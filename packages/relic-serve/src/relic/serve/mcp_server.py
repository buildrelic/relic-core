"""FastMCP server exposing a team's verified skills and its memory.

Each verified skill is exposed two ways: as a tool (call it to get the grounded
steps) and as a resource at ``skill://<id>`` (read it as a document). A
``search_skills`` tool helps find one by text or scope. When a recall function is
supplied, a ``recall_memory`` tool is added too, so one server is the single MCP
surface for skills and memory both. The recall function is injected rather than
imported, to keep this module free of any graph or network dependency.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastmcp import FastMCP
from fastmcp.resources import TextResource
from fastmcp.tools.function_tool import FunctionTool

from relic.contracts import RecallFn, SkillIR
from relic.registry import list_skills
from relic.serve.render import render


def input_schema(skill: SkillIR) -> dict[str, Any]:
    """Build a JSON Schema for ``skill``'s inputs (the MCP tool's input schema)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in skill.inputs.items():
        prop: dict[str, Any] = {"type": spec.type, "description": spec.description}
        if spec.default is not None:
            prop["default"] = spec.default
        properties[name] = prop
        if spec.required:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _skill_tool(skill: SkillIR) -> FunctionTool:
    document = render(skill)

    def handler(**_kwargs: Any) -> str:
        return document

    return FunctionTool(
        name=skill.skill_id,
        description=skill.description,
        parameters=input_schema(skill),
        fn=handler,
    )


_RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "what to recall from team memory"},
        "num_results": {"type": "integer", "description": "max facts to return", "default": 10},
    },
    "required": ["query"],
}


def _recall_tool(recall_fn: RecallFn) -> FunctionTool:
    async def recall_memory(query: str, num_results: int = 10) -> str:
        return await recall_fn(query, num_results)

    return FunctionTool(
        name="recall_memory",
        description="Recall facts from the team's memory graph, with their sources.",
        parameters=_RECALL_SCHEMA,
        fn=recall_memory,
    )


def _skill_resource(skill: SkillIR) -> TextResource:
    return TextResource(
        uri=f"skill://{skill.skill_id}",  # type: ignore[arg-type]  # pydantic coerces str to AnyUrl
        name=skill.skill_id,
        description=skill.description,
        mime_type="text/markdown",
        text=render(skill),
    )


def _match(skill: SkillIR, query: str, scope: str | None) -> bool:
    if scope is not None and skill.scope != scope:
        return False
    if not query:
        return True
    haystack = " ".join([skill.skill_id, skill.title, skill.description, *skill.tags]).lower()
    return query.lower() in haystack


def _format_matches(skills: list[SkillIR]) -> str:
    if not skills:
        return "No matching skills."
    lines: list[str] = []
    for skill in skills:
        lines.append(f"- {skill.skill_id} (v{skill.semver}, {skill.scope}): {skill.title}")
        lines.append(f"  {skill.description}")
    return "\n".join(lines)


def _search_skills_tool(skills: list[SkillIR]) -> FunctionTool:
    def search_skills(query: str = "", scope: str | None = None) -> str:
        return _format_matches([s for s in skills if _match(s, query, scope)])

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "text to match in a skill's id, title, description, or tags",
                "default": "",
            },
            "scope": {
                "type": "string",
                "description": "filter by scope: org, team, repo, project, or person",
            },
        },
    }
    return FunctionTool(
        name="search_skills",
        description="Find verified skills by text or scope. Returns id, title, and description.",
        parameters=schema,
        fn=search_skills,
    )


_INSTRUCTIONS = (
    "Relic serves a team's verified skills and its memory.\n"
    "- Each verified skill is a tool (call it for the grounded steps) and a resource "
    "at skill://<id> (read it as a document).\n"
    "- Use search_skills to find a skill by text or scope.\n"
    "- Use recall_memory to ask the team's memory a question and get facts with their sources."
)


def build_server(
    conn: sqlite3.Connection, *, recall_fn: RecallFn | None = None, name: str = "relic"
) -> FastMCP:
    """Build the server: skills as tools and resources, search, and recall when provided."""
    skills = list_skills(conn, status="verified")
    mcp = FastMCP(name, instructions=_INSTRUCTIONS)
    for skill in skills:
        mcp.add_tool(_skill_tool(skill))
        mcp.add_resource(_skill_resource(skill))
    mcp.add_tool(_search_skills_tool(skills))
    if recall_fn is not None:
        mcp.add_tool(_recall_tool(recall_fn))
    return mcp
