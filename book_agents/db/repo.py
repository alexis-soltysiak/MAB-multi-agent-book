from uuid import uuid4
from datetime import datetime
from sqlalchemy import select, update, func, desc, delete
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from book_agents.db.models import Book, Chapter, TraceStep, LLMCall


class Repo:
    def __init__(self, s: AsyncSession):
        self.s = s

    async def book_exists(self, book_id: str) -> bool:
        res = await self.s.execute(select(Book.id).where(Book.id == book_id))
        return res.scalar_one_or_none() is not None

    async def create_book(self, book_id: str, spec: dict) -> Book:
        b = Book(
            id=book_id,
            title=spec["title"],
            genre=spec["genre"],
            language=spec.get("language", "fr"),
            spec=spec,
            status="created",
            outline=None,
            bible=None,
        )
        self.s.add(b)
        try:
            await self.s.commit()
            return b
        except IntegrityError:
            await self.s.rollback()
            res = await self.s.execute(select(Book).where(Book.id == book_id))
            existing = res.scalar_one_or_none()
            if existing:
                return existing
            raise

    async def list_books(self) -> list[tuple[str, str, datetime]]:
        res = await self.s.execute(
            select(Book.id, Book.title, Book.created_at).order_by(desc(Book.created_at)).limit(50)
        )
        return [(r[0], r[1], r[2]) for r in res.all()]

    async def get_book(self, book_id: str) -> Book | None:
        res = await self.s.execute(select(Book).where(Book.id == book_id))
        return res.scalar_one_or_none()

    async def set_book_outline(self, book_id: str, outline: dict) -> None:
        await self.s.execute(update(Book).where(Book.id == book_id).values(outline=outline, status="outlined"))
        await self.s.commit()

    async def set_book_bible(self, book_id: str, bible: dict) -> None:
        await self.s.execute(update(Book).where(Book.id == book_id).values(bible=bible))
        await self.s.commit()

    async def ensure_chapters(self, book_id: str, n_chapters: int) -> None:
        res = await self.s.execute(select(Chapter.chapter_number).where(Chapter.book_id == book_id))
        existing = {r[0] for r in res.all()}
        for i in range(1, n_chapters + 1):
            if i in existing:
                continue
            c = Chapter(id=str(uuid4()), book_id=book_id, chapter_number=i, status="pending")
            self.s.add(c)
        await self.s.commit()

    async def set_chapter_plan(self, book_id: str, chapter_number: int, plan: dict) -> None:
        await self.s.execute(
            update(Chapter)
            .where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
            .values(plan=plan, status="planned")
        )
        await self.s.commit()

    async def set_chapter_draft(self, book_id: str, chapter_number: int, draft_text: str, draft_summary: str) -> None:
        await self.s.execute(
            update(Chapter)
            .where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
            .values(draft_text=draft_text, draft_summary=draft_summary, status="drafted")
        )
        await self.s.commit()

    async def set_chapter_edited(self, book_id: str, chapter_number: int, edited_text: str) -> None:
        await self.s.execute(
            update(Chapter)
            .where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
            .values(edited_text=edited_text, status="final")
        )
        await self.s.commit()

    async def get_chapter(self, book_id: str, chapter_number: int) -> Chapter | None:
        res = await self.s.execute(
            select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
        )
        return res.scalar_one_or_none()

    async def list_chapter_summaries(self, book_id: str) -> list[tuple[int, str]]:
        res = await self.s.execute(
            select(Chapter.chapter_number, Chapter.draft_summary)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.chapter_number.asc())
        )
        out: list[tuple[int, str]] = []
        for num, summ in res.all():
            if summ:
                out.append((num, summ))
        return out

    async def count_final_chapters(self, book_id: str) -> int:
        res = await self.s.execute(
            select(func.count()).select_from(Chapter).where(Chapter.book_id == book_id, Chapter.status == "final")
        )
        return int(res.scalar_one())

    async def get_next_unfinished_chapter(self, book_id: str) -> int | None:
        res = await self.s.execute(
            select(Chapter.chapter_number, Chapter.status)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.chapter_number.asc())
        )
        for num, st in res.all():
            if st != "final":
                return num
        return None

    async def list_final_chapters(self, book_id: str) -> list[tuple[int, str]]:
        res = await self.s.execute(
            select(Chapter.chapter_number, Chapter.edited_text, Chapter.draft_text)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.chapter_number.asc())
        )
        out: list[tuple[int, str]] = []
        for num, edited, draft in res.all():
            text = edited or draft or ""
            out.append((num, text))
        return out

    async def create_trace_step(self, book_id: str, agent: str, in_event_type: str, in_event: dict) -> str:
        step_id = str(uuid4())
        step = TraceStep(
            id=step_id,
            book_id=book_id,
            agent=agent,
            in_event_type=in_event_type,
            in_event=in_event,
            summary="",
            out_events=[],
            status="started",
        )
        self.s.add(step)
        await self.s.commit()
        return step_id

    async def finish_trace_step(self, step_id: str, summary: str, out_events: list[dict], status: str) -> None:
        await self.s.execute(
            update(TraceStep)
            .where(TraceStep.id == step_id)
            .values(summary=summary, out_events=out_events, status=status, finished_at=datetime.utcnow())
        )
        await self.s.commit()

    async def create_llm_call_started(
        self,
        trace_step_id: str,
        *,
        agent: str,
        model: str,
        request: dict,
    ) -> str:
        call_id = str(uuid4())
        call = LLMCall(
            id=call_id,
            trace_step_id=trace_step_id,
            agent=agent,
            model=model,
            latency_ms=0,
            status="running",
            finished_at=None,
            error=None,
            request=request,
            response={},
            output_parsed=None,
            output_text=None,
        )
        self.s.add(call)
        await self.s.commit()
        return call_id

    async def finish_llm_call(
        self,
        call_id: str,
        *,
        status: str,
        latency_ms: int,
        response: dict,
        output_parsed: dict | None,
        output_text: str | None,
        error: str | None = None,
    ) -> None:
        vals = {
            "status": status,
            "latency_ms": latency_ms,
            "response": response,
            "output_parsed": output_parsed,
            "output_text": output_text,
            "error": error,
            "finished_at": datetime.utcnow(),
        }
        await self.s.execute(update(LLMCall).where(LLMCall.id == call_id).values(**vals))
        await self.s.commit()

    async def add_llm_call(
        self,
        trace_step_id: str,
        *,
        agent: str,
        model: str,
        latency_ms: int,
        request: dict,
        response: dict,
        output_parsed: dict | None,
        output_text: str | None,
    ) -> None:
        call_id = await self.create_llm_call_started(
            trace_step_id,
            agent=agent,
            model=model,
            request=request,
        )
        await self.finish_llm_call(
            call_id,
            status="ok",
            latency_ms=latency_ms,
            response=response,
            output_parsed=output_parsed,
            output_text=output_text,
            error=None,
        )

    async def get_timeline(self, book_id: str, limit: int = 300) -> list[TraceStep]:
        q = (
            select(TraceStep)
            .where(TraceStep.book_id == book_id)
            .options(selectinload(TraceStep.llm_calls))
            .order_by(TraceStep.created_at.asc())
            .limit(limit)
        )
        res = await self.s.execute(q)
        return list(res.scalars().all())


    async def delete_book(self, book_id: str) -> None:
        book = await self.get_book(book_id)
        if not book:
            return

        await self.s.delete(book)
        await self.s.commit()
