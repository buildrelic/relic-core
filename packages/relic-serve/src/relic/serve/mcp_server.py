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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.resources import TextResource
from fastmcp.tools.function_tool import FunctionTool

from relic.contracts import RecallFn, SkillIR
from relic.registry import list_skills
from relic.serve.render import render


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """A JSON Schema ``object`` from its properties and required keys.

    The one place the tool input-schema shape is built, so every tool spells it the
    same way instead of hand-rolling the dict and forgetting ``required``.
    """
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything needed to register one MCP tool: name, description, schema, handler.

    The single deep interface tools are built from. A new tool is one ``ToolSpec``, not
    another hand-rolled ``FunctionTool`` + schema dict + handler triple.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]


def _function_tool(spec: ToolSpec) -> FunctionTool:
    """Turn a :class:`ToolSpec` into the FastMCP tool. The only ``FunctionTool`` call site."""
    return FunctionTool(
        name=spec.name,
        description=spec.description,
        parameters=spec.parameters,
        fn=spec.handler,
    )


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
    return _object_schema(properties, tuple(required))


def _skill_spec(skill: SkillIR) -> ToolSpec:
    document = render(skill)

    def handler(**_kwargs: Any) -> str:
        return document

    return ToolSpec(
        name=skill.skill_id,
        description=skill.description,
        parameters=input_schema(skill),
        handler=handler,
    )


def _recall_spec(recall_fn: RecallFn) -> ToolSpec:
    async def recall_memory(query: str, num_results: int = 10) -> str:
        return await recall_fn(query, num_results)

    parameters = _object_schema(
        {
            "query": {"type": "string", "description": "what to recall from team memory"},
            "num_results": {
                "type": "integer",
                "description": "max facts to return",
                "default": 10,
            },
        },
        required=("query",),
    )
    return ToolSpec(
        name="recall_memory",
        description="Recall facts from the team's memory graph, with their sources.",
        parameters=parameters,
        handler=recall_memory,
    )


def _search_skills_spec(skills: list[SkillIR]) -> ToolSpec:
    def search_skills(query: str = "", scope: str | None = None) -> str:
        return _format_matches([s for s in skills if _match(s, query, scope)])

    parameters = _object_schema(
        {
            "query": {
                "type": "string",
                "description": "text to match in a skill's id, title, description, or tags",
                "default": "",
            },
            "scope": {
                "type": "string",
                "description": "filter by scope: org, team, repo, project, or person",
            },
        }
    )
    return ToolSpec(
        name="search_skills",
        description="Find verified skills by text or scope. Returns id, title, and description.",
        parameters=parameters,
        handler=search_skills,
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

    specs: list[ToolSpec] = [_skill_spec(skill) for skill in skills]
    specs.append(_search_skills_spec(skills))
    if recall_fn is not None:
        specs.append(_recall_spec(recall_fn))

    for spec in specs:
        mcp.add_tool(_function_tool(spec))
    for skill in skills:
        mcp.add_resource(_skill_resource(skill))
    return mcp
