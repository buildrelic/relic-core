"""Registry: store SkillIR + semver + status + owner + last_verified_at.

Public API for the skill registry. The interface to the rest of the pipeline is
``SkillIR`` (from ``relic.contracts``) plus these functions; callers import
``from relic.registry import ...`` rather than reaching into ``store``.
"""

from relic.registry.store import (
    SkillStatus,
    connect,
    count_by_status,
    get_skill,
    list_skills,
    mark_verified,
    set_status,
    upsert_skill,
)

__all__ = [
    "SkillStatus",
    "connect",
    "count_by_status",
    "get_skill",
    "list_skills",
    "mark_verified",
    "set_status",
    "upsert_skill",
]
