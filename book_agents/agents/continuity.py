import orjson
from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.config import settings
from book_agents.llm.client import LLMClient
from book_agents.llm.schemas import ContinuityReviewOut
from book_agents.llm.prompts import continuity_instructions
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None, **overrides) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    m.update(overrides)
    return m


class ContinuityAgent(Agent):
    name = "continuity"

    def __init__(self):
        self.llm = LLMClient()

    @property
    def subscriptions(self) -> set[str]:
        return {"chapter.draft.created"}

    async def handle(self, env: Envelope) -> list[Envelope]:
        chapter_number = int(env.payload.get("chapter_number"))
        draft = env.payload.get("draft") or {}
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
            bible = book.bible or {}
            spec = book.spec or {}
            outline = book.outline or {}
            plan = chapter.plan or {}
            summaries = await repo.list_chapter_summaries(env.book_id)

        input_text = _continuity_input(spec, outline, bible, plan, summaries, draft, chapter_number)

        review = await self.llm.structured(
            ContinuityReviewOut,
            instructions=continuity_instructions(spec.get("language", "fr")),
            input_text=input_text,
            temperature=0.2,
            trace_step_id=trace_step_id,
            agent=self.name,
        )

        reviewed = Envelope(
            event_type="chapter.continuity.reviewed",
            book_id=env.book_id,
            payload={"chapter_number": chapter_number, "review": review.model_dump()},
            meta=_child_meta(env.meta, attempt=attempt),
        )

        if review.approved:
            proceed = Envelope(
                event_type="chapter.edit.requested",
                book_id=env.book_id,
                payload={"chapter_number": chapter_number, "instructions": []},
                meta=_child_meta(env.meta, attempt=attempt),
            )
            return [reviewed, proceed]

        next_attempt = attempt + 1
        if next_attempt > int(settings.max_rewrites_per_chapter):
            proceed = Envelope(
                event_type="chapter.edit.requested",
                book_id=env.book_id,
                payload={"chapter_number": chapter_number, "instructions": review.rewrite_instructions},
                meta=_child_meta(env.meta, attempt=attempt),
            )
            err = Envelope(
                event_type="error.raised",
                book_id=env.book_id,
                payload={
                    "where": "continuity",
                    "message": "Max rewrites atteint, passage en édition forcée",
                    "details": {"chapter_number": chapter_number, "issues": review.issues},
                },
                meta=_child_meta(env.meta, attempt=attempt),
            )
            return [reviewed, err, proceed]

        rewrite = Envelope(
            event_type="chapter.rewrite.requested",
            book_id=env.book_id,
            payload={"chapter_number": chapter_number, "rewrite_instructions": review.rewrite_instructions},
            meta=_child_meta(env.meta, attempt=next_attempt),
        )
        return [reviewed, rewrite]


def _continuity_input(
    spec: dict,
    outline: dict,
    bible: dict,
    plan: dict,
    summaries: list[tuple[int, str]],
    draft: dict,
    chapter_number: int,
) -> str:
    prior = "\n".join([f"- Chapitre {n}: {s}" for n, s in summaries if n < chapter_number]) or "(aucun)"
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
            "RESUMES_PRECEDENTS:",
            prior,
            "",
            f"DRAFT_CHAPITRE_{chapter_number}(JSON):",
            orjson.dumps(draft, option=orjson.OPT_INDENT_2).decode("utf-8"),
        ]
    )
