"""FastMCP Skills provider exposing only verified skills over MCP.

Each verified skill becomes one MCP tool: its input schema is built directly from
``SkillIR.inputs`` (the same typed contract rendered into SKILL.md), and calling
the tool returns the grounded skill document for an agent to follow. Reads from
the registry only; no graph or network dependency.
"""

from __future__ import annotations

import sqlite3
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


def build_server(conn: sqlite3.Connection, *, name: str = "relic") -> FastMCP:
    """Build a FastMCP server exposing every verified skill in the registry as a tool."""
    mcp = FastMCP(name)
    for skill in list_skills(conn, status="verified"):
        mcp.add_tool(_skill_tool(skill))
    return mcp
