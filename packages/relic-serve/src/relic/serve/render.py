"""Render a SkillIR to SKILL.md via the Jinja template.

Deterministic: no LLM, so identical input renders byte-identical output. The
template uses custom delimiters (<< >> for variables, <% %> for blocks) to avoid
colliding with markdown and code in skill content. The template ships inside this
package so render works wherever the package is installed.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from relic.contracts import SkillIR

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def render(skill: SkillIR) -> str:
    """Render `skill` to SKILL.md markdown."""
    return _env().get_template("skill.md.j2").render(skill=skill)
