import orjson
from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.llm.client import LLMClient
from book_agents.llm.schemas import StoryBibleOut
from book_agents.llm.prompts import bible_instructions
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None, **overrides) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    m.update(overrides)
    return m


class BibleAgent(Agent):
    name = "bible"

    def __init__(self):
        self.llm = LLMClient()

    @property
    def subscriptions(self) -> set[str]:
        return {"chapter.finalized"}

    async def handle(self, env: Envelope) -> list[Envelope]:
        chapter_number = int(env.payload.get("chapter_number"))
        chapter_obj = env.payload.get("chapter") or {}
        trace_step_id = (env.meta or {}).get("trace_step_id")

        async with SessionLocal() as s:
            repo = Repo(s)
            book = await repo.get_book(env.book_id)
            if not book:
                return []
            bible = book.bible or {}
            spec = book.spec or {}
            outline = book.outline or {}

        input_text = _bible_input(spec, outline, bible, chapter_obj, chapter_number)

        new_bible = await self.llm.structured(
            StoryBibleOut,
            instructions=bible_instructions(spec.get("language", "fr")),
            input_text=input_text,
            temperature=0.25,
            trace_step_id=trace_step_id,
            agent=self.name,
        )

        async with SessionLocal() as s:
            repo = Repo(s)
            await repo.set_book_bible(env.book_id, new_bible.model_dump())

        out_env = Envelope(
            event_type="story.bible.updated",
            book_id=env.book_id,
            payload={"chapter_number": chapter_number, "bible": new_bible.model_dump()},
            meta=_child_meta(env.meta),
        )
        return [out_env]


def _bible_input(spec: dict, outline: dict, bible: dict, chapter_obj: dict, chapter_number: int) -> str:
    return "\n".join(
        [
            "SPEC(JSON):",
            orjson.dumps(spec, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "OUTLINE(JSON):",
            orjson.dumps(outline, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "BIBLE_ACTUELLE(JSON):",
            orjson.dumps(bible, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            f"CHAPITRE_{chapter_number}(JSON):",
            orjson.dumps(chapter_obj, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "TACHE: Mets à jour la story bible en cohérence stricte. Normalise les noms et déduplique.",
        ]
    )
