from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookSpec(BaseModel):
    title: str
    genre: str
    premise: str
    language: str = "fr"
    n_chapters: int = Field(ge=1, le=100)
    target_words_per_chapter: int = Field(ge=200, le=20000)
    style_guide: str = ""
    constraints: list[str] = Field(default_factory=list)
    seed_elements: dict = Field(default_factory=dict)


class CastMember(StrictModel):
    name: str
    role: str = ""
    traits: list[str] = Field(default_factory=list)
    description: str = ""
    arc: str = ""


class Location(StrictModel):
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class ChapterPlan(StrictModel):
    chapter_number: int
    title: str
    goals: list[str]
    scene_beats: list[str]
    continuity_notes: list[str] = Field(default_factory=list)


class Outline(StrictModel):
    logline: str
    themes: list[str] = Field(default_factory=list)
    cast: list[CastMember] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    chapters: list[ChapterPlan] = Field(default_factory=list)


class TimelineEvent(StrictModel):
    what: str
    when: str = ""
    chapter_number: Optional[int] = None


class Relationship(StrictModel):
    with_name: str
    kind: str = ""
    notes: str = ""


class CharacterProfile(StrictModel):
    name: str
    description: str = ""
    traits: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class GlossaryEntry(StrictModel):
    term: str
    definition: str
    notes: str = ""


class StoryBible(StrictModel):
    global_summary: str = ""
    timeline: list[TimelineEvent] = Field(default_factory=list)
    characters: list[CharacterProfile] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    closed_threads: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)


class ChapterDraft(StrictModel):
    chapter_number: int
    text: str
    summary: str


class ContinuityReview(StrictModel):
    chapter_number: int
    approved: bool
    issues: list[str] = Field(default_factory=list)
    rewrite_instructions: list[str] = Field(default_factory=list)


class EditedChapter(StrictModel):
    chapter_number: int
    text: str
    edit_notes: list[str] = Field(default_factory=list)


class Envelope(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    book_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    payload: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
