from book_agents.agents.base import Agent
from book_agents.domain.types import Envelope
from book_agents.domain.events import BookStartRequested
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo


def _child_meta(meta: dict | None) -> dict:
    m = dict(meta or {})
    parent = m.pop("trace_step_id", None)
    if parent:
        m["parent_trace_step_id"] = parent
    return m


class CoordinatorAgent(Agent):
    name = "coordinator"

    @property
    def subscriptions(self) -> set[str]:
        return {
            "book.start.requested",
            "book.outline.created",
            "chapter.finalized",
        }

    async def handle(self, env: Envelope) -> list[Envelope]:
        async with SessionLocal() as s:
            repo = Repo(s)

            if env.event_type == "book.start.requested":
                req = BookStartRequested.model_validate(env.payload)
                spec_dict = req.spec.model_dump()

                book = await repo.get_book(env.book_id)
                if not book:
                    await repo.create_book(env.book_id, spec_dict)

                await repo.ensure_chapters(env.book_id, int(spec_dict.get("n_chapters", 1)))

                return [
                    Envelope(
                        event_type="book.created",
                        book_id=env.book_id,
                        payload={"spec": spec_dict},
                        meta=_child_meta(env.meta),
                    )
                ]

            book = await repo.get_book(env.book_id)
            if not book:
                return []

            spec = book.spec or {}
            total_chapters = int(spec.get("n_chapters", 1))

            if env.event_type == "book.outline.created":
                next_ch = await repo.get_next_unfinished_chapter(env.book_id)
                if not next_ch:
                    return [
                        Envelope(
                            event_type="book.completed",
                            book_id=env.book_id,
                            payload={"total_chapters": total_chapters},
                            meta=_child_meta(env.meta),
                        )
                    ]

                return [
                    Envelope(
                        event_type="chapter.write.requested",
                        book_id=env.book_id,
                        payload={"chapter_number": next_ch},
                        meta=_child_meta(env.meta),
                    )
                ]

            if env.event_type == "chapter.finalized":
                finalized = await repo.count_final_chapters(env.book_id)
                if finalized >= total_chapters:
                    return [
                        Envelope(
                            event_type="book.completed",
                            book_id=env.book_id,
                            payload={"total_chapters": total_chapters},
                            meta=_child_meta(env.meta),
                        )
                    ]

                next_ch = await repo.get_next_unfinished_chapter(env.book_id)
                if not next_ch:
                    return [
                        Envelope(
                            event_type="book.completed",
                            book_id=env.book_id,
                            payload={"total_chapters": total_chapters},
                            meta=_child_meta(env.meta),
                        )
                    ]

                return [
                    Envelope(
                        event_type="chapter.write.requested",
                        book_id=env.book_id,
                        payload={"chapter_number": next_ch},
                        meta=_child_meta(env.meta),
                    )
                ]

        return []
