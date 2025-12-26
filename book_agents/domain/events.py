from pydantic import BaseModel
from book_agents.domain.types import BookSpec

class BookStartRequested(BaseModel):
    spec: BookSpec

class BookOutlineCreated(BaseModel):
    outline: dict

class ChapterWriteRequested(BaseModel):
    chapter_number: int

class ChapterDraftCreated(BaseModel):
    chapter_number: int
    draft: dict

class StoryBibleUpdated(BaseModel):
    chapter_number: int
    bible: dict

class ContinuityReviewed(BaseModel):
    chapter_number: int
    review: dict

class ChapterEditRequested(BaseModel):
    chapter_number: int
    instructions: list[str]

class ChapterFinalized(BaseModel):
    chapter_number: int
    chapter: dict

class BookCompleted(BaseModel):
    total_chapters: int

class ErrorRaised(BaseModel):
    where: str
    message: str
    details: dict = {}
