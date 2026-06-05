"""Org-domain entities: the broader company model around the engineering subset.

`Person` and the shared primitives live in `relic.ontology.entities` /
`relic.ontology.primitives`. This module ports the rest of the system-design
ontology: `Employee`, `Team`, `Customer`, `Product`, `Project`, `Document`,
`Meeting`, `Decision`, `Policy`, and the OKR/goal types. As with the rest of the
ontology these are nodes only; relationships are expressed as `Id` reference
fields, and enums are `Literal` aliases with lowercase values to match the
existing idiom.
"""

from typing import Literal

from pydantic import BaseModel, Field

from relic.ontology.entities import Person
from relic.ontology.primitives import (
    AuditInfo,
    Id,
    Link,
    Money,
    Taggable,
    TextBlob,
    TimePoint,
    TimeRange,
)

# --- Employee ---------------------------------------------------------------

EmploymentType = Literal["full_time", "part_time", "contractor", "advisor", "intern"]
EmploymentStatus = Literal["active", "on_leave", "inactive"]


class Employee(Person):
    type: EmploymentType
    status: EmploymentStatus
    title: str
    manager_employee_id: Id | None = None
    team_id: Id | None = None
    start_date: TimePoint | None = None
    end_date: TimePoint | None = None


# --- Team -------------------------------------------------------------------

TeamType = Literal["engineering", "product", "sales", "marketing", "operations", "other"]


class Team(Taggable):
    id: Id
    name: str
    type: TeamType
    lead_employee_id: Id | None = None
    member_employee_ids: list[Id] = Field(default_factory=list)
    charter: TextBlob
    audit: AuditInfo


# --- Customer ---------------------------------------------------------------

CustomerStage = Literal["lead", "prospect", "trial", "active", "churned"]


class Customer(Taggable):
    id: Id
    name: str
    stage: CustomerStage
    industry: str | None = None
    employee_count: int | None = None
    website: str | None = None
    owner_employee_id: Id | None = None
    contacts: list[Id] = Field(default_factory=list)
    arr: Money | None = None
    start_date: TimePoint | None = None
    notes: TextBlob
    audit: AuditInfo


# --- Product ----------------------------------------------------------------

ProductLifecycle = Literal["concept", "beta", "ga", "deprecated"]


class Product(Taggable):
    id: Id
    name: str
    description: str | None = None
    lifecycle: ProductLifecycle
    owner_employee_id: Id | None = None
    artifacts: list[Link] = Field(default_factory=list)
    audit: AuditInfo


# --- Project ----------------------------------------------------------------

ProjectStatus = Literal["planned", "in_progress", "blocked", "done", "canceled"]
ProjectPriority = Literal["low", "medium", "high", "critical"]


class Project(Taggable):
    id: Id
    name: str
    status: ProjectStatus
    priority: ProjectPriority
    owning_team_id: Id | None = None
    owner_employee_id: Id | None = None
    product_id: Id | None = None
    customer_id: Id | None = None
    timeline: TimeRange
    brief: TextBlob
    links: list[Link] = Field(default_factory=list)
    audit: AuditInfo


# --- Document ---------------------------------------------------------------

DocumentType = Literal["prd", "adr", "srs", "design_doc", "runbook", "spec", "notes", "other"]
DocumentStatus = Literal["draft", "in_review", "approved", "deprecated"]


class Document(Taggable):
    id: Id
    title: str
    type: DocumentType
    status: DocumentStatus
    owner_employee_id: Id | None = None
    related_project_id: Id | None = None
    related_product_id: Id | None = None
    related_customer_id: Id | None = None
    content: TextBlob
    sources: list[Link] = Field(default_factory=list)
    audit: AuditInfo


# --- Meeting ----------------------------------------------------------------

MeetingType = Literal[
    "standup", "one_on_one", "planning", "retro", "customer_call", "incident_review", "other"
]


class Meeting(Taggable):
    id: Id
    title: str
    type: MeetingType
    when: TimeRange
    attendee_person_ids: list[Id] = Field(default_factory=list)
    organizer_person_id: Id | None = None
    agenda: TextBlob
    notes: TextBlob
    transcript: TextBlob
    decisions: list[Id] = Field(default_factory=list)
    documents: list[Id] = Field(default_factory=list)
    recordings: list[Link] = Field(default_factory=list)
    audit: AuditInfo


# --- Decision ---------------------------------------------------------------

DecisionStatus = Literal["proposed", "accepted", "rejected", "reversed"]


class Decision(Taggable):
    id: Id
    title: str
    status: DecisionStatus
    context: TextBlob
    decision: TextBlob
    consequences: TextBlob
    made_by_person_id: Id | None = None
    made_at: TimePoint | None = None
    meeting_id: Id | None = None
    related_document_ids: list[Id] = Field(default_factory=list)
    audit: AuditInfo


# --- Policy -----------------------------------------------------------------

PolicyStatus = Literal["draft", "active", "retired"]


class Policy(Taggable):
    id: Id
    name: str
    status: PolicyStatus
    owner_employee_id: Id | None = None
    effective_date: TimePoint | None = None
    review_by_date: TimePoint | None = None
    policy_text: TextBlob
    references: list[Link] = Field(default_factory=list)
    audit: AuditInfo


# --- OKR / Goal -------------------------------------------------------------

GoalStatus = Literal["not_started", "on_track", "at_risk", "off_track", "done", "canceled"]
Cadence = Literal["weekly", "monthly", "quarterly", "yearly", "custom"]


class Metric(BaseModel):
    name: str
    unit: str | None = None
    baseline: float
    target: float
    current: float


class KeyResult(Taggable):
    id: Id
    title: str
    status: GoalStatus
    window: TimeRange | None = None
    metric: Metric
    notes: TextBlob
    audit: AuditInfo


class OKRGoal(Taggable):
    id: Id
    objective: str
    status: GoalStatus
    cadence: Cadence
    window: TimeRange
    owner_employee_id: Id | None = None
    owning_team_id: Id | None = None
    related_project_id: Id | None = None
    related_product_id: Id | None = None
    key_results: list[KeyResult] = Field(default_factory=list)
    narrative: TextBlob
    audit: AuditInfo
