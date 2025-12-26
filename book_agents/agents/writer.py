import orjson
from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.llm.client import LLMClient
from book_agents.llm.schemas import ChapterDraftOut
from book_agents.llm.prompts import writer_instructions
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None, **overrides) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    m.update(overrides)
    return m


class WriterAgent(Agent):
    name = "writer"

    def __init__(self):
        self.llm = LLMClient()

    @property
    def subscriptions(self) -> set[str]:
        return {"chapter.write.requested", "chapter.rewrite.requested"}

    async def handle(self, env: Envelope) -> list[Envelope]:
        chapter_number = int(env.payload.get("chapter_number"))
        rewrite_notes = env.payload.get("rewrite_instructions") or []
        attempt = int((env.meta or {}).get("attempt", 0))
        trace_step_id = (env.meta or {}).get("trace_step_id")

        async with SessionLocal() as s:
            repo = Repo(s)
            book = await repo.get_book(env.book_id)
            if not book or not book.outline:
                return []
            chapter = await repo.get_chapter(env.book_id, chapter_number)
            if not chapter or not chapter.plan:
                return []
            summaries = await repo.list_chapter_summaries(env.book_id)
            bible = book.bible or {}
            spec = book.spec or {}
            outline = book.outline or {}
            plan = chapter.plan or {}

        input_text = _writer_input(spec, outline, bible, plan, summaries, chapter_number, rewrite_notes)
        draft = await self.llm.structured(
            ChapterDraftOut,
            instructions=writer_instructions(spec.get("language", "fr")),
            input_text=input_text,
            temperature=0.85,
            trace_step_id=trace_step_id,
            agent=self.name,
        )

        async with SessionLocal() as s:
            repo = Repo(s)
            await repo.set_chapter_draft(env.book_id, chapter_number, draft.text, draft.summary)

        out_env = Envelope(
            event_type="chapter.draft.created",
            book_id=env.book_id,
            payload={"chapter_number": chapter_number, "draft": draft.model_dump()},
            meta=_child_meta(env.meta, attempt=attempt),
        )
        return [out_env]


def _writer_input(
    spec: dict,
    outline: dict,
    bible: dict,
    chapter_plan: dict,
    summaries: list[tuple[int, str]],
    chapter_number: int,
    rewrite_notes: list[str],
) -> str:
    prior = "\n".join([f"- Chapitre {n}: {s}" for n, s in summaries if n < chapter_number]) or "(aucun)"
    rewrite = "\n".join([f"- {x}" for x in rewrite_notes]) or "(aucune)"
    return "\n".join(
        [
            "SPEC(JSON):",
            orjson.dumps(spec, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "OUTLINE(JSON):",
            orjson.dumps(outline, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "STORY_BIBLE(JSON):",
            orjson.dumps(bible, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "PLAN_CHAPITRE(JSON):",
            orjson.dumps(chapter_plan, option=orjson.OPT_INDENT_2).decode("utf-8"),
            "",
            "RESUMES_PRECEDENTS:",
            prior,
            "",
            "NOTES_DE_REECRITURE_A_RESPECTER:",
            rewrite,
            "",
            f"CONTRAINTE: Ecris le chapitre {chapter_number} en visant environ {spec.get('target_words_per_chapter', 1500)} mots. Retourne aussi summary fidèle.",
        ]
    )
