"""SkillIR: the typed contract for a compiled skill.

Ported verbatim from the Relic system-design doc. This same schema becomes the
MCP tool contract, so it must not drift from the rendered SKILL.md. Do not
redesign it.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class FieldSpec(BaseModel):
    """A single typed input or output field."""

    type: str
    description: str
    required: bool = True
    default: Optional[object] = None


class Citation(BaseModel):
    """Grounded source for a skill or one of its fields."""

    label: str
    url: str
    source_type: Literal["pr", "issue", "comment", "doc", "message"]


class SkillIR(BaseModel):
    # Identity + versioning
    skill_id: str
    semver: str

    # Human-facing metadata
    title: str
    description: str

    # Where the skill applies
    scope: Literal["org", "team", "repo", "project", "person"]

    # Typed contract (also emitted directly as the MCP tool schema)
    inputs: dict[str, FieldSpec] = Field(default_factory=dict)
    outputs: dict[str, FieldSpec] = Field(default_factory=dict)

    # Execution guardrails
    preconditions: list[str] = Field(default_factory=list)
    safety_checks: list[str] = Field(default_factory=list)

    # Grounding + governance
    citations: list[Citation] = Field(default_factory=list)
    status: Literal["draft", "verified", "deprecated"] = "draft"
    owner: str
    last_verified_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)
