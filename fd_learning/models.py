"""Shapes of the learning service; contract mocks: ../contracts/mocks/{feedback,profile}.demo.json."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .rules import ALLOWED_TAGS


class FeedbackIn(BaseModel):
    project_id: str = "default"
    ticker: str
    brief_generated_at: str | None = Field(None, description="generated_at of the rated brief.")
    rating: Literal["up", "down"]
    note: str | None = Field(None, max_length=1000)
    tags: list[str] = Field(default_factory=list, description=f"Any of: {', '.join(sorted(ALLOWED_TAGS))}")

    @field_validator("tags")
    @classmethod
    def _known_tags(cls, tags: list[str]) -> list[str]:
        unknown = sorted(set(tags) - ALLOWED_TAGS)
        if unknown:
            raise ValueError(f"unknown tags {unknown}; allowed: {sorted(ALLOWED_TAGS)}")
        return tags

    @field_validator("ticker")
    @classmethod
    def _upper(cls, t: str) -> str:
        return t.strip().upper()


class Feedback(FeedbackIn):
    id: str
    created_at: str


class FeedbackAck(Feedback):
    """Contract (contracts/learning.md): {"ok": true, "feedback_count": n}; the stored feedback is extra."""

    ok: bool = True
    feedback_count: int


class Style(BaseModel):
    length: Literal["short", "long"] | None = None
    tone: Literal["simple", "numbers"] | None = None


class Profile(BaseModel):
    project_id: str
    instructions: str = Field(description='Ready-made text for the agent prompt; "" means no preferences yet.')
    label: str = Field(description='Short human summary for the UI ("Naučeno: …"); "" when empty.')
    focus: list[str]
    avoid: list[str]
    style: Style
    feedback_count: int
    recent_notes: list[str] = Field(description="Up to 3 latest non-empty notes, newest last.")
    updated_at: str | None
    mode: Literal["rules", "llm"]


class ProfileAck(Profile):
    """DELETE /profile: contract {"ok": true}; the (now empty) profile is extra."""

    ok: bool = True


class ErrorBody(BaseModel):
    error: str
    detail: str
