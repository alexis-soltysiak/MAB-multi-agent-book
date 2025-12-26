import orjson
from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.llm.client import LLMClient
from book_agents.llm.schemas import EditedChapterOut
from book_agents.llm.prompts import editor_instructions
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None, **overrides) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    m.update(overrides)
    return m


class EditorAgent(Agent):
    name = "editor"

    def __init__(self):
        self.llm = LLMClient()

    @property
    def subscriptions(self) -> set[str]:
        return {"chapter.edit.requested"}

    async def handle(self, env: Envelope) -> list[Envelope]:
        chapter_number = int(env.payload.get("chapter_number"))
        instructions = env.payload.get("instructions") or []
        attempt = int((env.meta or {}).get("attempt", 0))
        trace_step_id = (env.meta or {}).get("trace_step_id")

        async with SessionLocal() as s:
            repo = Repo(s)
            book = await repo.get_book(env.book_id)
            if not book:
                return []
            chapter = await repo.get_chapter(env.book_id, chapter_number)
            if not chapter or not chapter.draft_text:
                return []
            spec = book.spec or {}
            bible = book.bible or {}
            outline = book.outline or {}
            plan = chapter.plan or {}
            draft_text = chapter.draft_text

        input_text = _editor_input(spec, outline, bible, plan, draft_text, instructions, chapter_number)

        edited = await self.llm.structured(
            EditedChapterOut,
            instructions=editor_instructions(spec.get("language", "fr")),
            input_text=input_text,
            temperature=0.4,
            trace_step_id=trace_step_id,
            agent=self.name,
        )

        async with SessionLocal() as s:
            repo = Repo(s)
            await repo.set_chapter_edited(env.book_id, chapter_number, edited.text)

        out = Envelope(
            event_type="chapter.finalized",
            book_id=env.book_id,
            payload={"chapter_number": chapter_number, "chapter": edited.model_dump()},
            meta=_child_meta(env.meta, attempt=attempt),
        )
        return [out]


def _editor_input(
    spec: dict,
    outline: dict,
    bible: dict,
    plan: dict,
    draft_text: str,
    instructions: list[str],
    chapter_number: int,
) -> str:
    return "\n".join(
        [
            "SPEC(JSON):",
            orjson.dumps(spec, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "OUTLINE(JSON):",
            orjson.dumps(outline, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "BIBLE(JSON):",
            orjson.dumps(bible, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "PLAN_CHAPITRE(JSON):",
            orjson.dumps(plan, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "INSTRUCTIONS_RELECTURE:",
            orjson.dumps(instructions, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            f"DRAFT_TEXTE_CHAPITRE_{chapter_number}:",
            draft_text,
            "",
            "TACHE: Réécris en améliorant style/rythme/clarté, sans casser la continuité.",
        ]
    )
