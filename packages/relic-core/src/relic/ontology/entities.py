"""Engineering-subset entities.

`Person` and the primitives are ported from the system-design doc. `Repo`,
`PullRequest`, `Review`, `Issue`, and `Procedure` are not in the doc; they are
designed here to fit Phase 2 GitHub/Linear ingestion. Every entity carries a
canonical `url` for provenance and an `audit` record.
"""

from typing import Literal

from pydantic import BaseModel, Field

from relic.ontology.primitives import (
    AuditInfo,
    ContactInfo,
    Id,
    Link,
    Taggable,
    TextBlob,
    TimePoint,
)


class Person(Taggable):
    id: Id
    full_name: str
    preferred_name: str | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)
    external_org: str | None = None
    profiles: list[Link] = Field(default_factory=list)
    audit: AuditInfo


class Repo(BaseModel):
    id: Id
    full_name: str
    url: str
    description: str | None = None
    default_branch: str = "main"
    tags: list[str] = Field(default_factory=list)
    audit: AuditInfo


class PullRequest(BaseModel):
    id: Id
    repo_id: Id
    number: int
    title: str
    body: TextBlob | None = None
    state: Literal["open", "closed", "merged"]
    author_id: Id
    url: str
    created_at: TimePoint
    merged_at: TimePoint | None = None
    touched_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    audit: AuditInfo


class Review(BaseModel):
    id: Id
    pull_request_id: Id
    reviewer_id: Id
    state: Literal["approved", "changes_requested", "commented", "dismissed"]
    body: TextBlob | None = None
    url: str
    submitted_at: TimePoint
    audit: AuditInfo


class Issue(BaseModel):
    id: Id
    source: Literal["github", "linear"]
    identifier: str
    title: str
    body: TextBlob | None = None
    state: str
    assignee_ids: list[Id] = Field(default_factory=list)
    url: str
    created_at: TimePoint
    closed_at: TimePoint | None = None
    tags: list[str] = Field(default_factory=list)
    audit: AuditInfo


class ProcedureEvidence(BaseModel):
    source_url: str
    label: str
    kind: Literal["pr", "issue", "review", "comment", "doc", "message"]


class Procedure(BaseModel):
    id: Id
    title: str
    archetype: str
    description: TextBlob
    confidence: float = Field(ge=0.0, le=1.0)
    support: int = Field(ge=0)
    evidence: list[ProcedureEvidence] = Field(default_factory=list)
    recommended_reviewer_ids: list[Id] = Field(default_factory=list)
    scope: Literal["org", "team", "repo", "project", "person"] = "team"
    status: Literal["draft", "verified", "deprecated"] = "draft"
    tags: list[str] = Field(default_factory=list)
    audit: AuditInfo
