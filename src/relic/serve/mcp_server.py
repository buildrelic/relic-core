"""FastMCP server exposing verified skills, and optionally memory recall, over MCP.

Each verified skill becomes one MCP tool: its input schema is built directly from
``SkillIR.inputs`` (the same typed contract rendered into SKILL.md), and calling
the tool returns the grounded skill document for an agent to follow.

When a recall function is supplied, a ``recall_memory`` tool is added too, so the
same server is the single MCP surface for skills and memory both. The recall
function is injected rather than imported, to keep this module free of any graph
or network dependency.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

from relic.compile.render import render
from relic.ontology.skill_ir import SkillIR
from relic.registry.store import list_skills


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


RecallFn = Callable[[str, int], Awaitable[str]]

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


def build_server(
    conn: sqlite3.Connection, *, recall_fn: RecallFn | None = None, name: str = "relic"
) -> FastMCP:
    """Build a FastMCP server: verified skills as tools, plus recall when provided."""
    mcp = FastMCP(name)
    for skill in list_skills(conn, status="verified"):
        mcp.add_tool(_skill_tool(skill))
    if recall_fn is not None:
        mcp.add_tool(_recall_tool(recall_fn))
    return mcp
