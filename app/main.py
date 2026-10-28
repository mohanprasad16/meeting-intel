"""HTTP layer only: validation, rate limits and JSON shapes. The work lives in the modules."""

import json
import logging
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.ask import ask
from app.config import settings
from app.extract import extract_meeting
from app.ingest import read_upload, store
from app.llm import GeminiLLM, Usage
from app.parsing import (
    MAX_BYTES,
    ParseError,
    UnknownFormat,
    decode_upload,
    format_time,
    parse,
    parse_date,
)
from app.search import Scope
from app.stencil import infer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meeting_intel")

ROOT = Path(__file__).parent
SAMPLES = ROOT.parent / "data" / "samples"
ALLOWED = (".txt", ".vtt", ".md")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect(init=True).close()
    yield


app = FastAPI(title="Meeting Intelligence", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
llm = GeminiLLM()

# ponytail: in-process state; one container is the whole deployment. Redis when it scales out.
_previews: dict[str, dict] = {}
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
LIMITS = {"ask": 30, "upload": 20}  # requests per minute per client
PREVIEW_TTL = 15 * 60


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def rate_limit(request: Request, bucket: str) -> None:
    key = (request.client.host if request.client else "local", bucket)
    window, now = _hits[key], time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= LIMITS[bucket]:
        raise HTTPException(429, "Too many requests. Please wait a minute.")
    window.append(now)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "ai_configured": bool(settings.gemini_api_key)}


# --- upload ------------------------------------------------------------------------


def _finish(conn, text, transcript, filename, project, kind="upload"):
    started, usage = time.perf_counter(), Usage()
    result = store(conn, llm, text, transcript, filename, project)
    report = extract_meeting(conn, llm, result.meeting_id, usage)
    payload = {
        **asdict(result),
        "format": transcript.format,
        "extraction": asdict(report),
        "seconds": round(time.perf_counter() - started, 2),
        "usage": {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    }
    conn.execute("INSERT INTO traces (kind, payload) VALUES (?, ?)", (kind, json.dumps(payload)))
    conn.commit()
    log.info("upload %s", json.dumps({k: v for k, v in payload.items() if k != "skipped"}))
    return payload


@app.post("/api/upload")
async def upload(
    request: Request,
    conn: Conn,
    file: Annotated[UploadFile, File()],
    project: Annotated[str | None, Form()] = None,
):
    rate_limit(request, "upload")
    filename = Path(file.filename or "transcript.txt").name  # never used as a path
    if not filename.lower().endswith(ALLOWED):
        raise HTTPException(400, "Only .txt and .vtt transcripts are supported.")
    try:
        text = decode_upload(await file.read(MAX_BYTES + 1))
        transcript = parse(text, filename)
    except UnknownFormat:
        return _preview(text, filename, project)
    except ParseError as e:
        raise HTTPException(400, str(e))
    try:
        return _finish(conn, text, transcript, filename, project)
    except ParseError as e:
        raise HTTPException(409, str(e))


def _preview(text: str, filename: str, project: str | None):
    """Unknown layout: show what the AI stencil would import; save nothing yet."""
    try:
        result = infer(llm, text)
    except ParseError as e:
        raise HTTPException(400, str(e))
    now = time.monotonic()
    for token in [t for t, p in _previews.items() if now - p["at"] > PREVIEW_TTL]:
        _previews.pop(token)
    token = secrets.token_urlsafe(16)
    _previews[token] = {"at": now, "text": text, "filename": filename, "result": result}
    t = result.transcript
    return {
        "preview": {
            "token": token,
            "filename": filename,
            "pattern": result.pattern,
            "match_ratio": round(result.match_ratio, 3),
            "attempts": result.attempts,
            "title": t.title or Path(filename).stem,
            "date": t.meeting_date.isoformat() if t.meeting_date else None,
            "project": project,
            "total_turns": len(t.turns),
            "turns": [
                {
                    "time": format_time(x.time) if x.time is not None else None,
                    "speaker": x.speaker,
                    "text": x.text,
                }
                for x in t.turns[:10]
            ],
            "skipped": [{"line": s.source_line, "text": s.text} for s in t.skipped],
        }
    }


class Confirm(BaseModel):
    token: str
    title: str = Field(min_length=1, max_length=200)
    date: str | None = None
    project: str | None = None


@app.post("/api/upload/confirm")
def confirm(body: Confirm, request: Request, conn: Conn):
    rate_limit(request, "upload")
    pending = _previews.pop(body.token, None)
    if not pending:
        raise HTTPException(404, "This preview expired. Please upload the file again.")
    transcript = pending["result"].transcript
    transcript.title = body.title.strip()
    transcript.meeting_date = parse_date(body.date) if body.date else None
    try:
        return _finish(conn, pending["text"], transcript, pending["filename"], body.project)
    except ParseError as e:
        raise HTTPException(409, str(e))


@app.get("/api/samples")
def samples():
    return [p.name for p in sorted(SAMPLES.iterdir()) if p.suffix in (".txt", ".vtt")]


@app.post("/api/samples/{name}")
def load_sample(name: str, request: Request, conn: Conn):
    rate_limit(request, "upload")
    path = SAMPLES / Path(name).name
    if not path.is_file():
        raise HTTPException(404, "No such sample.")
    raw = path.read_bytes()
    try:
        text, transcript = read_upload(raw, path.name)
    except UnknownFormat:
        # Samples are ours, so the odd-format one is imported straight from its preview.
        text = decode_upload(raw)
        result = infer(llm, text)
        transcript = result.transcript
        transcript.project = "Smart Shelf Pilot"
    except ParseError as e:
        raise HTTPException(400, str(e))
    try:
        return _finish(conn, text, transcript, path.name, None, kind="sample")
    except ParseError as e:
        if "already" in str(e):
            return {"already_loaded": True, "detail": str(e)}
        raise HTTPException(400, str(e))


# --- reading ---------------------------------------------------------------------


@app.get("/api/projects")
def projects(conn: Conn):
    rows = conn.execute(
        """SELECT p.id AS project_id, p.name AS project, m.id, m.title, m.meeting_date,
                  m.source_format, m.extraction_status, m.warnings,
                  (SELECT count(*) FROM action_items a WHERE a.meeting_id = m.id) AS tasks
           FROM projects p JOIN meetings m ON m.project_id = p.id
           ORDER BY p.name, COALESCE(m.meeting_date, m.uploaded_at)"""
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        project = out.setdefault(
            r["project_id"], {"id": r["project_id"], "name": r["project"], "meetings": []}
        )
        project["meetings"].append(
            {
                "id": r["id"],
                "title": r["title"],
                "date": r["meeting_date"],
                "format": r["source_format"],
                "extraction": r["extraction_status"],
                "warnings": json.loads(r["warnings"]),
                "tasks": r["tasks"],
            }
        )
    return list(out.values())


@app.get("/api/meetings/{meeting_id}")
def meeting(meeting_id: int, conn: Conn):
    m = conn.execute(
        """SELECT m.*, p.name AS project FROM meetings m JOIN projects p ON p.id = m.project_id
           WHERE m.id = ?""",
        (meeting_id,),
    ).fetchone()
    if not m:
        raise HTTPException(404, "Meeting not found.")
    turns = conn.execute(
        "SELECT line_no, time, speaker, text, time_approx, flagged FROM turns WHERE meeting_id = ? ORDER BY line_no",
        (meeting_id,),
    ).fetchall()
    return {
        "id": m["id"],
        "title": m["title"],
        "date": m["meeting_date"],
        "project": m["project"],
        "format": m["source_format"],
        "summary": m["summary"],
        "extraction": m["extraction_status"],
        "warnings": json.loads(m["warnings"]),
        "decisions": [
            dict(d)
            for d in conn.execute(
                "SELECT id, text, line_no, quote FROM decisions WHERE meeting_id = ?", (meeting_id,)
            )
        ],
        "turns": [
            {
                "line": t["line_no"],
                "time": format_time(t["time"]) if t["time"] is not None else None,
                "speaker": t["speaker"],
                "text": t["text"],
                "approx": bool(t["time_approx"]),
                "flagged": bool(t["flagged"]),
            }
            for t in turns
        ],
    }


@app.post("/api/meetings/{meeting_id}/extract")
def retry_extraction(meeting_id: int, conn: Conn):
    if not conn.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone():
        raise HTTPException(404, "Meeting not found.")
    return asdict(extract_meeting(conn, llm, meeting_id))


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: int, conn: Conn):
    with db.transaction(conn):
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.execute("DELETE FROM projects WHERE id NOT IN (SELECT project_id FROM meetings)")
    return {"deleted": meeting_id}


@app.get("/api/action-items")
def action_items(conn: Conn, project_id: int | None = None, status: str | None = None):
    sql = """SELECT a.*, m.title AS meeting, m.meeting_date, m.project_id FROM action_items a
             JOIN meetings m ON m.id = a.meeting_id WHERE 1=1"""
    params: list = []
    if project_id:
        sql += " AND m.project_id = ?"
        params.append(project_id)
    if status == "open":
        sql += " AND a.status IN ('open', 'updated')"
    elif status in ("done", "cancelled"):
        sql += " AND a.status = ?"
        params.append(status)
    sql += " ORDER BY a.status IN ('done', 'cancelled'), COALESCE(a.due_date, '9999'), a.id"
    items = []
    for a in conn.execute(sql, params).fetchall():
        history = conn.execute(
            """SELECT u.new_status, u.note, u.line_no, u.quote, u.meeting_id, m.title, m.meeting_date
               FROM action_item_updates u JOIN meetings m ON m.id = u.meeting_id
               WHERE u.action_item_id = ? ORDER BY COALESCE(m.meeting_date, m.uploaded_at)""",
            (a["id"],),
        ).fetchall()
        items.append(
            {
                "id": a["id"],
                "owner": a["owner"],
                "owner_in_meeting": bool(a["owner_in_meeting"]),
                "task": a["task"],
                "due_text": a["due_text"],
                "due_date": a["due_date"],
                "status": a["status"],
                "meeting_id": a["meeting_id"],
                "meeting": a["meeting"],
                "meeting_date": a["meeting_date"],
                "line_no": a["line_no"],
                "quote": a["quote"],
                "history": [
                    {
                        "status": h["new_status"],
                        "note": h["note"],
                        "line_no": h["line_no"],
                        "quote": h["quote"],
                        "meeting_id": h["meeting_id"],
                        "meeting": h["title"],
                        "date": h["meeting_date"],
                    }
                    for h in history
                ],
            }
        )
    return items


# --- asking ----------------------------------------------------------------------


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    project_id: int | None = None
    meeting_id: int | None = None


@app.post("/api/ask")
def ask_question(body: Question, request: Request, conn: Conn):
    rate_limit(request, "ask")
    if not settings.gemini_api_key:
        raise HTTPException(503, "No GEMINI_API_KEY configured. Add it to .env and restart.")
    result = ask(conn, llm, body.question.strip(), Scope(body.project_id, body.meeting_id))
    log.info("ask %s", json.dumps(result.trace))
    return {
        "answer": result.answer.text,
        "status": result.answer.status,
        "citations": result.answer.citations,
        "trace": result.trace,
    }
