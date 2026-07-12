"""Registry: store SkillIR + semver + status + owner + last_verified_at.

Public API for the skill registry. The interface to the rest of the pipeline is
``SkillIR`` (from ``relic.contracts``) plus these functions; callers import
``from relic.registry import ...`` rather than reaching into ``store``.
"""

from relic.registry.store import (
    SkillStatus,
    ZoneGrantSource,
    connect,
    count_by_status,
    get_skill,
    grant_zone,
    list_skills,
    mark_verified,
    revoke_zone,
    set_status,
    upsert_skill,
    zones_for_person,
)

__all__ = [
    "SkillStatus",
    "ZoneGrantSource",
    "connect",
    "count_by_status",
    "get_skill",
    "grant_zone",
    "list_skills",
    "mark_verified",
    "revoke_zone",
    "set_status",
    "upsert_skill",
    "zones_for_person",
]
