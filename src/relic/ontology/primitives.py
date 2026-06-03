"""Shared ontology primitives, ported from the Relic system-design doc."""

from datetime import datetime

from pydantic import BaseModel, Field

Id = str
TimePoint = datetime


class TimeRange(BaseModel):
    start: TimePoint
    end: TimePoint | None = None


class Money(BaseModel):
    currency: str = "USD"
    amount: float


class Link(BaseModel):
    label: str
    url: str


class ContactInfo(BaseModel):
    email: str | None = None
    phone: str | None = None
    slack_handle: str | None = None


class AuditInfo(BaseModel):
    created_by: Id
    created_at: TimePoint
    last_edited_by: Id | None = None
    last_edited_at: TimePoint | None = None


class TextBlob(BaseModel):
    markdown: str


class Taggable(BaseModel):
    tags: list[str] = Field(default_factory=list)
