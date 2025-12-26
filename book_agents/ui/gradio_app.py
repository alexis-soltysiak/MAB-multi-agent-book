import json
from uuid import uuid4
from datetime import datetime
import gradio as gr
import redis.asyncio as redis
from pathlib import Path

from book_agents.config import settings
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo

AGENTS = ["coordinator", "planner", "writer", "continuity", "editor", "bible"]

STREAM_NAME = getattr(settings, "redis_stream_name", None) or getattr(settings, "redis_stream", None) or "book_agents:events"
PAUSE_KEY = getattr(settings, "ui_pause_key", None) or "book_agents:paused"

def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _json_pre(obj) -> str:
    try:
        return _escape(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception:
        return _escape(str(obj))

def _badge(text: str, kind: str) -> str:
    return f"<span class='badge badge-{kind}'>{_escape(text)}</span>"

def _agent_badge(agent: str) -> str:
    return f"<span class='agent agent-{_escape(agent)}'>{_escape(agent)}</span>"

def _short(s: str, n: int = 160) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"

def _pct(a: int, b: int) -> int:
    if b <= 0:
        return 0
    return int((a / b) * 100)

def _split_prompt_sections(user_text: str) -> list[tuple[str, str]]:
    markers = [
        "SPEC(JSON):",
        "OUTLINE(JSON):",
        "STORY_BIBLE(JSON):",
        "BIBLE(JSON):",
        "BIBLE_ACTUELLE(JSON):",
        "PLAN_CHAPITRE(JSON):",
        "RESUMES_PRECEDENTS:",
        "NOTES_DE_REECRITURE_A_RESPECTER:",
        "TACHE:",
        "TÂCHE:",
    ]

    lines = (user_text or "").splitlines()
    cur_title = "USER"
    cur_buf: list[str] = []
    out: list[tuple[str, str]] = []

    def flush():
        nonlocal cur_title, cur_buf
        if cur_buf:
            out.append((cur_title, "\n".join(cur_buf).strip()))
            cur_buf = []

    for ln in lines:
        hit = None
        for m in markers:
            if ln.strip().startswith(m):
                hit = m.replace(":", "")
                break
        if hit:
            flush()
            cur_title = hit
            continue
        cur_buf.append(ln)

    flush()
    if not out:
        return [("USER", user_text or "")]
    return out

async def _redis_conn():
    url = getattr(settings, "redis_url", None) or getattr(settings, "redis_dsn", None) or "redis://localhost:6379/0"
    return redis.from_url(url, decode_responses=True)

async def _publish_event(*, event_type: str, book_id: str, payload: dict, meta: dict | None = None):
    r = await _redis_conn()
    env = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "book_id": book_id,
        "created_at": datetime.utcnow().isoformat(),
        "payload": payload or {},
        "meta": meta or {},
    }
    await r.xadd(STREAM_NAME, {"data": json.dumps(env, ensure_ascii=False)})
    await r.close()

async def _set_paused(paused: bool) -> str:
    r = await _redis_conn()
    if paused:
        await r.set(PAUSE_KEY, "1")
    else:
        await r.delete(PAUSE_KEY)
    await r.close()
    return "PAUSED" if paused else "RUNNING"

async def list_books_choices():
    async with SessionLocal() as s:
        repo = Repo(s)
        books = await repo.list_books()
    return [f"{bid} | {title} | {_fmt_dt(created_at)}" for bid, title, created_at in books]

def _parse_book_id(choice: str) -> str:
    if not choice:
        return ""
    return choice.split("|")[0].strip()

def _compute_overview(steps):
    per_agent = {a: {"last": None, "active": False, "running_llm": 0, "ok": 0, "error": 0} for a in AGENTS}
    for st in steps:
        a = st.agent
        if a not in per_agent:
            per_agent[a] = {"last": None, "active": False, "running_llm": 0, "ok": 0, "error": 0}
        if per_agent[a]["last"] is None or st.created_at > per_agent[a]["last"].created_at:
            per_agent[a]["last"] = st
        if (st.status == "started") and (st.finished_at is None):
            per_agent[a]["active"] = True
        if st.status == "ok":
            per_agent[a]["ok"] += 1
        if st.status == "error":
            per_agent[a]["error"] += 1
        for c in (st.llm_calls or []):
            if getattr(c, "status", "ok") == "running" and getattr(c, "finished_at", None) is None:
                per_agent[a]["running_llm"] += 1

    boxes = []
    for a in AGENTS:
        info = per_agent.get(a) or {}
        last = info.get("last")
        last_txt = ""
        last_ts = ""
        if last:
            last_ts = _fmt_dt(last.created_at)
            last_txt = last.summary or last.in_event_type
        active = bool(info.get("active")) or int(info.get("running_llm") or 0) > 0
        cls = f"node node-{a} active" if active else f"node node-{a}"
        meta = []
        meta.append(_badge(f"ok {info.get('ok', 0)}", "ok"))
        meta.append(_badge(f"err {info.get('error', 0)}", "err"))
        if info.get("running_llm", 0) > 0:
            meta.append(_badge(f"LLM {info.get('running_llm')}", "run"))
        meta_html = " ".join(meta)

        boxes.append(
            "\n".join(
                [
                    f"<div class='{cls}'>",
                    f"<div class='node-title'>{_agent_badge(a)}</div>",
                    f"<div class='node-meta'>{meta_html}</div>",
                    f"<div class='node-last'>{_escape(_short(last_txt, 120))}</div>",
                    f"<div class='node-ts'>{_escape(last_ts)}</div>",
                    "</div>",
                ]
            )
        )

    flow = "\n".join(
        [
            "<div class='pipeline'>",
            "".join([f"<div class='pipe-item'>{b}</div>" for b in boxes]),
            "</div>",
        ]
    )

    lanes = []
    for a in AGENTS:
        info = per_agent.get(a) or {}
        active = bool(info.get("active")) or int(info.get("running_llm") or 0) > 0
        dot = "<span class='dot dot-active'></span>" if active else "<span class='dot'></span>"
        last = info.get("last")
        last_txt = (last.summary or last.in_event_type) if last else ""
        last_ts = _fmt_dt(last.created_at) if last else ""
        lanes.append(
            "\n".join(
                [
                    f"<div class='lane lane-{a}'>",
                    f"<div class='lane-left'>{dot}<span class='lane-agent'>{_escape(a)}</span></div>",
                    f"<div class='lane-right'><span class='lane-ts'>{_escape(last_ts)}</span> {_escape(_short(last_txt, 140))}</div>",
                    "</div>",
                ]
            )
        )

    lanes_html = "<div class='lanes'>" + "\n".join(lanes) + "</div>"
    return flow + lanes_html

def _build_stream(steps):
    items = []
    for st in steps:
        items.append(
            {
                "ts": st.created_at,
                "kind": "step",
                "agent": st.agent,
                "title": f"{st.in_event_type}",
                "status": st.status,
                "detail": {
                    "step_id": st.id,
                    "in_event": st.in_event,
                    "out_events": st.out_events,
                    "summary": st.summary,
                    "finished_at": st.finished_at,
                },
            }
        )
        if st.finished_at:
            items.append(
                {
                    "ts": st.finished_at,
                    "kind": "step_done",
                    "agent": st.agent,
                    "title": f"{st.in_event_type}",
                    "status": st.status,
                    "detail": {"step_id": st.id, "summary": st.summary},
                }
            )

        for c in (st.llm_calls or []):
            c_status = getattr(c, "status", "ok")
            items.append(
                {
                    "ts": c.created_at,
                    "kind": "llm_start",
                    "agent": c.agent,
                    "title": f"LLM {c.model}",
                    "status": c_status,
                    "detail": {"call_id": c.id, "trace_step_id": st.id},
                }
            )
            if getattr(c, "finished_at", None):
                items.append(
                    {
                        "ts": c.finished_at,
                        "kind": "llm_done",
                        "agent": c.agent,
                        "title": f"LLM {c.model}",
                        "status": c_status,
                        "detail": {"call_id": c.id, "trace_step_id": st.id, "latency_ms": c.latency_ms},
                    }
                )

    def _rank(kind: str) -> int:
        order = {"step": 10, "llm_start": 20, "llm_done": 30, "step_done": 40}
        return order.get(kind, 99)

    items.sort(key=lambda x: (x["ts"] or datetime.utcnow(), -_rank(x["kind"])), reverse=True)

    return items

def _render_stream(items, agent_filter: str, kind_filter: str, status_filter: str, text_filter: str):
    def _ok_item(it):
        if agent_filter and agent_filter != "ALL" and it["agent"] != agent_filter:
            return False
        if kind_filter and kind_filter != "ALL":
            if kind_filter == "STEP" and not it["kind"].startswith("step"):
                return False
            if kind_filter == "LLM" and not it["kind"].startswith("llm"):
                return False
        if status_filter and status_filter != "ALL":
            if (it.get("status") or "") != status_filter:
                return False
        if text_filter:
            blob = f'{it.get("agent","")} {it.get("title","")} {it.get("kind","")} {it.get("status","")}'.lower()
            if text_filter.lower() not in blob:
                return False
        return True

    blocks = []
    for it in items:
        if not _ok_item(it):
            continue

        ts = _fmt_dt(it["ts"])
        kind = it["kind"]
        status = it.get("status") or ""
        agent = it["agent"]
        title = it["title"]

        if kind.startswith("llm"):
            b = _badge(status or "ok", "run" if status == "running" else ("err" if status == "error" else "ok"))
            label = _badge("LLM", "llm")
        else:
            b = _badge(status or "ok", "run" if status == "started" else ("err" if status == "error" else "ok"))
            label = _badge("STEP", "step")

        detail = it.get("detail") or {}

        blocks.append(
            "\n".join(
                [
                    f"<div class='card card-{_escape(agent)}'>",
                    f"<details>",
                    f"<summary><b>{_escape(ts)} · {agent} · {_escape(title)}</b> {label} {b}</summary>",
                    f"<pre>{_json_pre(detail)}</pre>",
                    "</details>",
                    "</div>",
                ]
            )
        )

    return "<div class='panel'>" + ("\n".join(blocks) if blocks else "<div class='empty'>Aucun item</div>") + "</div>"


def _render_llm_panel(steps, agent_filter: str, status_filter: str):
    calls = []
    for st in steps:
        for c in (st.llm_calls or []):
            calls.append((st, c))
    calls.sort(key=lambda x: (x[1].created_at or datetime.utcnow()), reverse=True)

    blocks = []
    for st, c in calls[: settings.trace_max_steps]:
        if agent_filter and agent_filter != "ALL" and c.agent != agent_filter:
            continue
        c_status = getattr(c, "status", "ok")
        if status_filter and status_filter != "ALL" and c_status != status_filter:
            continue

        head = f"{_fmt_dt(c.created_at)} · {c.agent} · {c.model}"
        if c_status == "running":
            head += " · EN COURS"
        elif c_status == "error":
            head += " · ERREUR"
        else:
            head += f" · {c.latency_ms}ms"

        req = c.request or {}
        sys_txt = ""
        user_txt = ""
        try:
            inp = (req.get("input") or [])
            for it in inp:
                if (it or {}).get("role") == "system":
                    sys_txt = it.get("content") or ""
                if (it or {}).get("role") == "user":
                    user_txt = it.get("content") or ""
        except Exception:
            pass

        sections = _split_prompt_sections(user_txt)
        sections_html = "\n".join(
            [
                "\n".join(
                    [
                        "<details class='sec'>",
                        f"<summary>{_escape(title)}</summary>",
                        f"<pre class='pre-dark'>{_escape(body)}</pre>",
                        "</details>",
                    ]
                )
                for title, body in sections
                if (body or "").strip()
            ]
        )

        outp = c.output_parsed
        out_txt = c.output_text or ""

        blocks.append(
            "\n".join(
                [
                    f"<div class='card card-{_escape(c.agent)}'>",
                    f"<details><summary><b>{_escape(head)}</b></summary>",
                    f"<div class='mini'>trace_step_id: {_escape(st.id)}</div>",
                    "<div class='section-title'>system</div>",
                    f"<pre class='pre-dark'>{_escape(sys_txt)}</pre>",
                    "<div class='section-title'>user (découpé)</div>",
                    f"<div class='sections'>{sections_html or '<div class=empty>Prompt vide</div>'}</div>",
                    "<div class='section-title'>output_parsed</div>",
                    f"<pre>{_json_pre(outp)}</pre>",
                    "<div class='section-title'>output_text</div>",
                    f"<pre>{_escape(out_txt)}</pre>",
                    "</details>",
                    "</div>",
                ]
            )
        )

    return "<div class='panel'>" + ("\n".join(blocks) if blocks else "<div class='empty'>Aucun call LLM</div>") + "</div>"

def _render_progress(book, finalized: int, total: int) -> str:
    p = _pct(finalized, total)
    bar = f"""
    <div class="progress">
      <div class="progress-top">
        <div class="progress-title">{_escape(getattr(book, "title", "") or "")}</div>
        <div class="progress-meta">{_escape(book.id)} · {finalized}/{total} chapitres · {p}%</div>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" style="width:{p}%"></div>
      </div>
    </div>
    """
    return bar

async def load_dashboard(choice: str, agent_filter: str, kind_filter: str, status_filter: str, text_filter: str):
    book_id = _parse_book_id(choice)
    if not book_id:
        empty = "<div class='empty'>Aucun book_id sélectionné</div>"
        return empty, empty, empty, empty

    async with SessionLocal() as s:
        repo = Repo(s)
        book = await repo.get_book(book_id)
        steps = await repo.get_timeline(book_id, limit=settings.trace_max_steps)
        finalized = await repo.count_final_chapters(book_id)

    spec = (book.spec or {}) if book else {}
    total = int(spec.get("n_chapters", 0) or 0)
    prog = _render_progress(book, finalized, total) if book else "<div class='empty'>Livre introuvable</div>"

    overview = _compute_overview(steps)
    stream_items = _build_stream(steps)
    stream_html = _render_stream(stream_items, agent_filter, kind_filter, status_filter, text_filter)
    llm_html = _render_llm_panel(steps, agent_filter, "ALL" if status_filter == "started" else status_filter)
    return prog, overview, stream_html, llm_html


def _book_to_md(book, chapters: list[tuple[int, str]]) -> str:
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
    return "\n".join(lines)

async def export_book_from_ui(choice: str):
    book_id = _parse_book_id(choice)
    if not book_id:
        return None, "Aucun book_id sélectionné"

    async with SessionLocal() as s:
        repo = Repo(s)
        book = await repo.get_book(book_id)
        if not book:
            return None, "Book introuvable"
        chapters = await repo.list_final_chapters(book_id)

    md = _book_to_md(book, chapters)

    out_dir = Path("exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{book_id}.md"
    out_path.write_text(md, encoding="utf-8")

    return str(out_path), f"Export OK → {out_path}"


async def ui_pause():
    state = await _set_paused(True)
    return f"Workers: {state}"

async def ui_resume():
    state = await _set_paused(False)
    return f"Workers: {state}"

async def ui_reset_selected(choice: str):
    book_id = _parse_book_id(choice)
    if not book_id:
        return "Reset: aucun livre sélectionné"
    async with SessionLocal() as s:
        repo = Repo(s)
        if not hasattr(repo, "delete_book"):
            return "Reset: Repo.delete_book() manquant (prochaine étape)"
        await repo.delete_book(book_id)
    return f"Reset OK: {book_id}"

async def ui_create_book(spec_json: str):
    try:
        spec = json.loads(spec_json or "{}")
    except Exception as e:
        return "Erreur spec JSON: " + str(e), ""
    book_id = str(uuid4())
    await _publish_event(event_type="book.start.requested", book_id=book_id, payload={"spec": spec}, meta={})
    return f"Livre créé: {book_id}", ""

def launch_ui(host: str = "127.0.0.1", port: int = 7860):
    css = """
    :root{
      --bg:#0b1020;
      --panel:#0f172a;
      --card:#0b1224;
      --text:#86878a;
      --muted:rgba(0,0,0,.78);
      --muted2:rgba(0,0,0,.68);
      --muted3:rgba(0,0,0,.58);
      --stroke:rgba(148,163,184,.18);
      --stroke2:rgba(148,163,184,.28);
      --ok:#22c55e;
      --err:#ef4444;
      --run:#ec4899;
      --step:#60a5fa;
      --coordinator:#3b82f6;
      --planner:#8b5cf6;
      --writer:#22c55e;
      --continuity:#f97316;
      --editor:#06b6d4;
      --bible:#eab308;
    }
    body{ background:var(--bg) !important; }
    .wrap { max-width: 1500px; margin: 0 auto; color:var(--text); }
    h2, h3 { color: var(--text); }
    .topbar { display:flex; gap:12px; align-items:flex-end; }
    .grid { display:flex; gap:14px; }
    .col { flex:1; min-width: 0; }
    .pipeline { display:flex; gap:10px; overflow-x:auto; padding: 8px 0; }
    .pipe-item { min-width: 250px; }
    .node {
      border: 1px solid var(--stroke);
      border-radius: 16px;
      padding: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01));
      box-shadow: 0 10px 24px rgba(0,0,0,.22);
    }
    .node.active { border-color: var(--stroke2); box-shadow: 0 0 0 2px rgba(255,255,255,.04), 0 10px 26px rgba(0,0,0,.30); }
    .node-title { font-weight: 900; font-size: 14px; }
    .node-meta { margin-top: 8px; display:flex; gap:6px; flex-wrap:wrap; }
    .node-last { margin-top: 10px; color: var(--muted); font-size: 12px; }
    .node-ts { margin-top: 6px; color: var(--muted3); font-size: 11px; }

    .agent{
      display:inline-flex;
      padding: 2px 10px;
      border-radius: 999px;
      font-weight: 900;
      letter-spacing:.2px;
      background: rgba(255,255,255,.06);
      border:1px solid var(--stroke);
    }
    .agent-coordinator{ background:rgba(59,130,246,.16); border-color:rgba(59,130,246,.35); }
    .agent-planner{ background:rgba(139,92,246,.16); border-color:rgba(139,92,246,.35); }
    .agent-writer{ background:rgba(34,197,94,.14); border-color:rgba(34,197,94,.33); }
    .agent-continuity{ background:rgba(249,115,22,.15); border-color:rgba(249,115,22,.35); }
    .agent-editor{ background:rgba(6,182,212,.14); border-color:rgba(6,182,212,.33); }
    .agent-bible{ background:rgba(234,179,8,.14); border-color:rgba(234,179,8,.33); }

    .node-coordinator{ border-left:4px solid var(--coordinator); }
    .node-planner{ border-left:4px solid var(--planner); }
    .node-writer{ border-left:4px solid var(--writer); }
    .node-continuity{ border-left:4px solid var(--continuity); }
    .node-editor{ border-left:4px solid var(--editor); }
    .node-bible{ border-left:4px solid var(--bible); }

    .lanes { margin-top: 12px; border: 1px solid var(--stroke); border-radius: 16px; overflow:hidden; background:rgba(255,255,255,.02); }
    .lane { display:flex; justify-content:space-between; gap:10px; padding:12px 14px; border-top:1px solid var(--stroke); }
    .lane:first-child { border-top: none; }
    .lane-left { display:flex; align-items:center; gap:10px; min-width: 220px; }
    .lane-agent { font-weight:900; }
    .lane-right { flex:1; color: var(--muted); }
    .lane-ts { color: var(--muted3); margin-right: 8px; font-size: 12px; }

    .dot { width:10px; height:10px; border-radius:50%; background: rgba(229,231,235,.25); display:inline-block; }
    .dot-active { background: rgba(229,231,235,.95); animation: pulse 1.2s infinite; }
    @keyframes pulse { 0%{ transform:scale(1); opacity:1;} 50%{ transform:scale(1.35); opacity:.55;} 100%{ transform:scale(1); opacity:1;} }

    .badge { display:inline-flex; padding: 3px 9px; border-radius: 999px; font-size: 11px; border: 1px solid var(--stroke); background: rgba(255,255,255,.04); color: var(--text); }
    .badge-ok { background: rgba(34,197,94,.14); border-color: rgba(34,197,94,.30); }
    .badge-err { background: rgba(239,68,68,.14); border-color: rgba(239,68,68,.30); }
    .badge-run { background: rgba(236,72,153,.14); border-color: rgba(236,72,153,.30); }
    .badge-step { background: rgba(96,165,250,.14); border-color: rgba(96,165,250,.30); }
    .badge-llm { background: rgba(236,72,153,.14); border-color: rgba(236,72,153,.30); }

    .progress{
      border: 1px solid var(--stroke);
      border-radius: 16px;
      padding: 14px;
      background: rgba(255,255,255,.03);
      box-shadow: 0 10px 24px rgba(0,0,0,.22);
      margin-top: 10px;
      margin-bottom: 12px;
    }
    .progress-top{ display:flex; justify-content:space-between; gap:12px; align-items:flex-end; }
    .progress-title{ font-weight: 950; font-size: 18px; color: var(--text); }
    .progress-meta{ color: var(--muted); font-size: 12px; }
    .progress-bar{ height: 10px; border-radius: 999px; background: rgba(255,255,255,.06); overflow:hidden; border:1px solid var(--stroke); margin-top: 12px; }
    .progress-fill{ height: 100%; background: linear-gradient(90deg, rgba(34,197,94,.9), rgba(236,72,153,.9)); }

    .stream { border: 1px solid var(--stroke); border-radius: 16px; overflow:hidden; max-height: 690px; overflow-y:auto; background: rgba(255,255,255,.02); }
    .stream-row { display:flex; gap:10px; padding:12px 14px; border-top:1px solid var(--stroke); }
    .stream-row:first-child { border-top:none; }
    .stream-ts { width: 175px; color: var(--muted2); font-size: 12px; flex-shrink:0; }
    .stream-main { flex:1; min-width:0; color: var(--text); }
    .sep{ color: var(--muted3);margin: 0 8px; }

    .panel { max-height: 690px; overflow-y:auto; }
    .card {
      border: 1px solid var(--stroke);
      border-radius: 16px;
      padding: 12px;
      margin: 12px 0;
      background: rgba(255,255,255,.02);
      box-shadow: 0 10px 24px rgba(0,0,0,.22);
    }
    .card-coordinator{ border-left:4px solid var(--coordinator); }
    .card-planner{ border-left:4px solid var(--planner); }
    .card-writer{ border-left:4px solid var(--writer); }
    .card-continuity{ border-left:4px solid var(--continuity); }
    .card-editor{ border-left:4px solid var(--editor); }
    .card-bible{ border-left:4px solid var(--bible); }

    .section-title { font-weight: 950; margin-top: 12px; margin-bottom: 6px; color: var(--text); }
    details > summary { cursor: pointer; color: var(--text); }
    pre { white-space: pre-wrap; word-break: break-word; background: rgba(255,255,255,.04); padding: 12px; border-radius: 14px; border:1px solid var(--stroke); color: var(--text); }
    .pre-dark{ background: rgba(0,0,0,.05); }
    .empty { padding: 14px; color: var(--muted); }
    .mini { font-size: 12px; color: var(--muted); margin-top: 8px; }
    .sections{ display:flex; flex-direction:column; gap:8px; }
    .sec{ border:1px solid var(--stroke); border-radius: 14px; padding: 8px; background: rgba(255,255,255,.02); }
    """

    with gr.Blocks(css=css) as demo:
        gr.Markdown("<div class='wrap'><h2>Book Agents — Observability Console</h2></div>")

        with gr.Row():
            book_choice = gr.Dropdown(
                label="Livre",
                choices=[],
                value=None,
                allow_custom_value=True,
                interactive=True,
                scale=2,
            )
            export_btn = gr.Button("Exporter (.md)")
            export_file = gr.File(label="Fichier exporté")
            export_status = gr.Markdown()
            refresh = gr.Button("Rafraîchir", scale=1)

        export_btn.click(
            export_book_from_ui,
            inputs=[book_choice],
            outputs=[export_file, export_status],
        )

        with gr.Row():
            pause_btn = gr.Button("Pause workers", scale=1)
            resume_btn = gr.Button("Resume workers", scale=1)
            reset_btn = gr.Button("Reset livre sélectionné", scale=2)
            control_status = gr.Textbox(label="Control", value="", interactive=False, scale=3)

        with gr.Accordion("Nouveau livre (book.start.requested)", open=False):
            default_spec = json.dumps(
                {
                    "title": "Nouveau Livre",
                    "genre": "Fantasy",
                    "premise": "…",
                    "language": "fr",
                    "n_chapters": 8,
                    "target_words_per_chapter": 1400,
                    "style_guide": "",
                    "constraints": [],
                    "seed_elements": {},
                },
                ensure_ascii=False,
                indent=2,
            )
            spec_editor = gr.Code(label="spec (JSON)", value=default_spec, language="json")
            create_btn = gr.Button("Créer et démarrer")
            create_out = gr.Textbox(label="Résultat", value="", interactive=False)

        with gr.Row():
            agent_filter = gr.Dropdown(label="Agent", choices=["ALL"] + AGENTS, value="ALL", interactive=True)
            kind_filter = gr.Dropdown(label="Type", choices=["ALL", "STEP", "LLM"], value="ALL", interactive=True)
            status_filter = gr.Dropdown(label="Status", choices=["ALL", "started", "ok", "error", "running"], value="ALL", interactive=True)
            text_filter = gr.Textbox(label="Recherche", value="", interactive=True)

        progress = gr.HTML()
        overview = gr.HTML()

        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown("### Event Stream (plus récent en haut)")
                stream = gr.HTML()
            with gr.Column(scale=2):
                gr.Markdown("### LLM Calls")
                llm_panel = gr.HTML()

        async def _refresh_choices():
            choices = await list_books_choices()
            return gr.Dropdown(choices=choices, value=(choices[0] if choices else None))


        demo.load(_refresh_choices, outputs=book_choice)

        def _bind_refresh():
            return load_dashboard, [book_choice, agent_filter, kind_filter, status_filter, text_filter], [progress, overview, stream, llm_panel]

        fn, ins, outs = _bind_refresh()

        refresh.click(fn, inputs=ins, outputs=outs)
        book_choice.change(fn, inputs=ins, outputs=outs)
        agent_filter.change(fn, inputs=ins, outputs=outs)
        kind_filter.change(fn, inputs=ins, outputs=outs)
        status_filter.change(fn, inputs=ins, outputs=outs)
        text_filter.change(fn, inputs=ins, outputs=outs)

        gr.Timer(settings.ui_poll_seconds).tick(fn, inputs=ins, outputs=outs)

        pause_btn.click(ui_pause, outputs=control_status)
        resume_btn.click(ui_resume, outputs=control_status)
        reset_btn.click(ui_reset_selected, inputs=book_choice, outputs=control_status)

        create_btn.click(ui_create_book, inputs=spec_editor, outputs=[create_out, spec_editor])

    demo.queue()
    demo.launch(server_name=host, server_port=port)
