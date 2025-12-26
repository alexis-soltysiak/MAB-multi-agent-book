import orjson
from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.llm.client import LLMClient
from book_agents.llm.schemas import OutlineOut
from book_agents.llm.prompts import planner_instructions
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    return m


class PlannerAgent(Agent):
    name = "planner"

    def __init__(self):
        self.llm = LLMClient()

    @property
    def subscriptions(self) -> set[str]:
        return {"book.created"}

    async def handle(self, env: Envelope) -> list[Envelope]:
        trace_step_id = (env.meta or {}).get("trace_step_id")

        async with SessionLocal() as s:
            repo = Repo(s)
            book = await repo.get_book(env.book_id)
            if not book:
                return []
            spec = book.spec or {}

        input_text = _planner_input(spec)
        outline = await self.llm.structured(
            OutlineOut,
            instructions=planner_instructions(spec.get("language", "fr")),
            input_text=input_text,
            temperature=0.5,
            trace_step_id=trace_step_id,
            agent=self.name,
        )

        async with SessionLocal() as s:
            repo = Repo(s)
            await repo.set_book_outline(env.book_id, outline.model_dump())
            for ch in outline.chapters:
                await repo.set_chapter_plan(env.book_id, ch.chapter_number, ch.model_dump())

        return [
            Envelope(
                event_type="book.outline.created",
                book_id=env.book_id,
                payload={"outline": outline.model_dump()},
                meta=_child_meta(env.meta),
            )
        ]


def _planner_input(spec: dict) -> str:
    return "\n".join(
        [
            "SPEC(JSON):",
            orjson.dumps(spec, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "TÂCHE:",
            "Génère un Outline complet avec chapters[]. Chaque chapitre doit être actionnable (goals + scene_beats).",
        ]
    )
