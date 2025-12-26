from pydantic import BaseModel, Field
from book_agents.domain.types import Outline, ChapterDraft, StoryBible, ContinuityReview, EditedChapter

class OutlineOut(Outline):
    pass

class ChapterDraftOut(ChapterDraft):
    pass

class StoryBibleOut(StoryBible):
    pass

class ContinuityReviewOut(ContinuityReview):
    pass

class EditedChapterOut(EditedChapter):
    pass

class SimpleTextOut(BaseModel):
    text: str = Field(default="")
