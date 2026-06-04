"""Render a browsable markdown index of skills, the human-facing surface.

Pairs with emit: where emit writes one SKILL.md per skill, the catalog is the
index over them, linking to each. Deterministic and pure, like ``render``.
"""

from __future__ import annotations

from relic.ontology.skill_ir import SkillIR


def render_catalog(skills: list[SkillIR]) -> str:
    """Render an index of ``skills`` as markdown, linking to each SKILL.md."""
    count = len(skills)
    noun = "skill" if count == 1 else "skills"
    lines = ["# Skills", "", f"{count} {noun}.", ""]
    for skill in skills:
        lines.append(f"## [{skill.title}]({skill.skill_id}/SKILL.md)")
        lines.append("")
        lines.append(f"`{skill.skill_id}` · {skill.scope} · v{skill.semver}")
        lines.append("")
        lines.append(skill.description)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
