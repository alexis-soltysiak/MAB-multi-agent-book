import json
import asyncio
from uuid import uuid4
import typer
import structlog
from rich import print as rprint
from redis.asyncio import Redis

from book_agents.config import settings
from book_agents.logging_setup import configure_logging
from book_agents.bus.redis_streams import RedisStreamsBus
from book_agents.domain.types import Envelope, BookSpec
from book_agents.db.engine import engine, SessionLocal
from book_agents.db.models import Base
from book_agents.db.repo import Repo

from book_agents.agents.planner import PlannerAgent
from book_agents.agents.writer import WriterAgent
from book_agents.agents.bible import BibleAgent
from book_agents.agents.continuity import ContinuityAgent
from book_agents.agents.editor import EditorAgent
from book_agents.agents.coordinator import CoordinatorAgent

from book_agents.ui.gradio_app import launch_ui

PAUSE_KEY = "book_agents:paused"
PAUSE_POLL_SECONDS = 0.5

app = typer.Typer(no_args_is_help=True)


def _make_agent(name: str):
    mapping = {
        "planner": PlannerAgent,
        "writer": WriterAgent,
        "bible": BibleAgent,
        "continuity": ContinuityAgent,
        "editor": EditorAgent,
        "coordinator": CoordinatorAgent,
    }
    if name not in mapping:
        raise typer.BadParameter(f"Agent inconnu: {name}. Options: {list(mapping.keys())}")
    return mapping[name]()


def _event_excerpt(env: Envelope) -> dict:
    payload = env.payload or {}
    keep = {}
    for k in ("chapter_number", "total_chapters", "where", "message"):
        if k in payload:
            keep[k] = payload[k]
    return {"event_type": env.event_type, "book_id": env.book_id, "meta": env.meta, "payload_excerpt": keep}


def _derive_summary(agent: str, in_env: Envelope, out_envs: list[Envelope]) -> str:
    if agent == "planner":
        for e in out_envs:
            if e.event_type == "book.outline.created":
                outline = (e.payload or {}).get("outline") or {}
                logline = outline.get("logline") or ""
                if logline:
                    return f"Outline créé: {logline}"
        return "Outline créé"

    if agent == "writer":
        for e in out_envs:
            if e.event_type == "chapter.draft.created":
                n = (e.payload or {}).get("chapter_number")
                draft = (e.payload or {}).get("draft") or {}
                summ = draft.get("summary") or ""
                if summ:
                    return f"Draft chapitre {n}: {summ}"
                return f"Draft chapitre {n} généré"
        n = (in_env.payload or {}).get("chapter_number")
        return f"Draft chapitre {n} généré"

    if agent == "continuity":
        for e in out_envs:
            if e.event_type == "chapter.continuity.reviewed":
                review = (e.payload or {}).get("review") or {}
                n = (e.payload or {}).get("chapter_number")
                approved = bool(review.get("approved"))
                issues = review.get("issues") or []
                if approved:
                    return f"Continuité OK (chapitre {n})"
                return f"Continuité KO (chapitre {n}) : {len(issues)} problème(s)"
        return "Revue continuité"

    if agent == "editor":
        for e in out_envs:
            if e.event_type == "chapter.finalized":
                n = (e.payload or {}).get("chapter_number")
                return f"Chapitre {n} finalisé"
        return "Chapitre finalisé"

    if agent == "bible":
        for e in out_envs:
            if e.event_type == "story.bible.updated":
                n = (e.payload or {}).get("chapter_number")
                return f"Story bible mise à jour (après chapitre {n})"
        return "Story bible mise à jour"

    if agent == "coordinator":
        for e in out_envs:
            if e.event_type == "book.created":
                return "Livre créé"
            if e.event_type == "chapter.write.requested":
                n = (e.payload or {}).get("chapter_number")
                return f"Orchestration: lancement chapitre {n}"
            if e.event_type == "book.completed":
                return "Livre terminé"
        return "Orchestration"

    return f"{in_env.event_type} traité"


async def _run_single_worker(agent_obj, consumer: str):
    log = structlog.get_logger()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    bus = RedisStreamsBus(redis, settings.stream_key, settings.dlq_stream_key)
    group = f"agent:{agent_obj.name}"
    log.info("worker_started", agent=agent_obj.name, group=group, consumer=consumer, stream=settings.stream_key)

    async for msg in bus.read_forever(group=group, consumer=consumer):
        env = msg.envelope

        # Global pause (UI / ops)
        paused_logged = False
        while await redis.get(PAUSE_KEY):
            if not paused_logged:
                log.info("workers_paused", agent=agent_obj.name, book_id=env.book_id, redis_id=msg.redis_id)
                paused_logged = True
            await asyncio.sleep(PAUSE_POLL_SECONDS)


        if env.event_type not in agent_obj.subscriptions:
            await bus.ack(group, msg.redis_id)
            continue

        step_id = None
        created_trace_late = False

        try:
            async with SessionLocal() as s:
                repo = Repo(s)
                book_exists_before = await repo.book_exists(env.book_id)

                if book_exists_before:
                    step_id = await repo.create_trace_step(
                        env.book_id, agent_obj.name, env.event_type, _event_excerpt(env)
                    )

            env.meta = dict(env.meta or {})
            if step_id:
                env.meta["trace_step_id"] = step_id

            log.info(
                "received_event",
                agent=agent_obj.name,
                event_type=env.event_type,
                book_id=env.book_id,
                redis_id=msg.redis_id,
            )

            out = await agent_obj.handle(env)

            for e in out:
                await bus.publish(e)

            await bus.ack(group, msg.redis_id)

            summary = _derive_summary(agent_obj.name, env, out)
            out_events = [{"event_type": e.event_type, "payload_excerpt": _event_excerpt(e).get("payload_excerpt")} for e in out]

            if not step_id:
                async with SessionLocal() as s:
                    repo = Repo(s)
                    book_exists_after = await repo.book_exists(env.book_id)
                    if book_exists_after:
                        step_id = await repo.create_trace_step(
                            env.book_id, agent_obj.name, env.event_type, _event_excerpt(env)
                        )
                        created_trace_late = True

            if step_id:
                async with SessionLocal() as s:
                    repo = Repo(s)
                    await repo.finish_trace_step(step_id, summary=summary, out_events=out_events, status="ok")

            if created_trace_late:
                log.info("trace_step_created_late", agent=agent_obj.name, event_type=env.event_type, book_id=env.book_id)

        except Exception as e:
            err = {
                "where": agent_obj.name,
                "message": str(e),
                "event_type": env.event_type,
                "book_id": env.book_id,
            }

            await bus.publish_dlq(env, err)
            await bus.publish(
                Envelope(
                    event_type="error.raised",
                    book_id=env.book_id,
                    payload={"where": agent_obj.name, "message": str(e), "details": {"event_type": env.event_type}},
                )
            )
            await bus.ack(group, msg.redis_id)

            if step_id:
                try:
                    async with SessionLocal() as s:
                        repo = Repo(s)
                        await repo.finish_trace_step(step_id, summary=f"Erreur: {str(e)}", out_events=[], status="error")
                except Exception:
                    pass

            log.exception("agent_error", agent=agent_obj.name, event_type=env.event_type, book_id=env.book_id)


@app.command()
def init_db():
    configure_logging(settings.log_level)

    async def _run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_run())
    rprint("[green]DB initialisée[/green]")


@app.command()
def start_book(spec_path: str = typer.Argument(...), book_id: str = typer.Option("", help="ID optionnel")):
    configure_logging(settings.log_level)
    log = structlog.get_logger()

    async def _run():
        bid = book_id or str(uuid4())
        spec_dict = json.loads(open(spec_path, "r", encoding="utf-8").read())
        spec = BookSpec.model_validate(spec_dict)

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        bus = RedisStreamsBus(redis, settings.stream_key, settings.dlq_stream_key)

        env = Envelope(
            event_type="book.start.requested",
            book_id=bid,
            payload={"spec": spec.model_dump()},
            meta={"attempt": 0},
        )
        await bus.publish(env)

        await redis.aclose()
        log.info("book_started", book_id=bid)
        rprint(f"[cyan]book_id[/cyan]: {bid}")

    asyncio.run(_run())


@app.command()
def run_worker(agent: str = typer.Argument(...), consumer: str = typer.Option("c1")):
    configure_logging(settings.log_level)
    agent_obj = _make_agent(agent)
    asyncio.run(_run_single_worker(agent_obj, consumer))


@app.command()
def run_all(consumer_prefix: str = typer.Option("local")):
    configure_logging(settings.log_level)

    async def _run():
        agents = [
            CoordinatorAgent(),
            PlannerAgent(),
            WriterAgent(),
            BibleAgent(),
            ContinuityAgent(),
            EditorAgent(),
        ]
        tasks = []
        for a in agents:
            tasks.append(asyncio.create_task(_run_single_worker(a, f"{consumer_prefix}-{a.name}")))
        await asyncio.gather(*tasks)

    asyncio.run(_run())


@app.command()
def export_book(book_id: str = typer.Argument(...), out_path: str = typer.Option("book.md")):
    configure_logging(settings.log_level)

    async def _run():
        async with SessionLocal() as s:
            repo = Repo(s)
            book = await repo.get_book(book_id)
            if not book:
                raise RuntimeError("Book introuvable")
            chapters = await repo.list_final_chapters(book_id)

        lines = []
        lines.append(f"# {book.title}")
        lines.append("")
        lines.append(f"*Genre:* {book.genre}")
        lines.append("")
        for num, text in chapters:
            lines.append(f"## Chapitre {num}")
            lines.append("")
            lines.append((text or "").strip())
            lines.append("")

        open(out_path, "w", encoding="utf-8").write("\n".join(lines))

    asyncio.run(_run())
    rprint(f"[green]Export terminé[/green] -> {out_path}")

@app.command()
def pause_workers():
    async def _run():
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.set(PAUSE_KEY, "1")
        await redis.aclose()
    asyncio.run(_run())
    rprint("[yellow]Workers PAUSED[/yellow]")

@app.command()
def resume_workers():
    async def _run():
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.delete(PAUSE_KEY)
        await redis.aclose()
    asyncio.run(_run())
    rprint("[green]Workers RUNNING[/green]")



@app.command()
def ui(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(7860)):
    configure_logging(settings.log_level)
    launch_ui(host=host, port=port)
