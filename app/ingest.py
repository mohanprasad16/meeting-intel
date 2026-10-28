"""Upload pipeline: bytes -> parsed turns -> stored meeting, chunks and vectors."""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePath

from app import db
from app.chunking import chunk_turns
from app.config import settings
from app.guard import looks_like_injection
from app.llm import LLM, LLMError
from app.parsing import ParseError, Transcript, decode_upload, parse

log = logging.getLogger(__name__)
DEFAULT_PROJECT = "General"


@dataclass
class IngestResult:
    meeting_id: int
    title: str
    turns: int
    chunks: int
    warnings: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def content_hash(text: str) -> str:
    normalised = "\n".join(line.strip() for line in text.strip().splitlines())
    return hashlib.sha256(normalised.encode()).hexdigest()


def project_id(conn: sqlite3.Connection, name: str | None) -> int:
    name = (name or "").strip() or DEFAULT_PROJECT
    conn.execute("INSERT OR IGNORE INTO projects(name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()[0]


def embed_header(title: str, meeting_date: date | None, speakers: list[str]) -> str:
    when = meeting_date.isoformat() if meeting_date else "unknown date"
    return f"Meeting: {title} | Date: {when} | Speakers: {', '.join(speakers)}"


def read_upload(raw: bytes, filename: str) -> tuple[str, Transcript]:
    text = decode_upload(raw)
    return text, parse(text, filename)


def store(
    conn: sqlite3.Connection,
    llm: LLM,
    text: str,
    transcript: Transcript,
    filename: str,
    project: str | None = None,
) -> IngestResult:
    digest = content_hash(text)
    if conn.execute("SELECT 1 FROM meetings WHERE content_hash = ?", (digest,)).fetchone():
        raise ParseError("This meeting is already uploaded.")

    title = transcript.title or PurePath(filename).stem.replace("_", " ").replace("-", " ")
    title = title.strip() or "Untitled meeting"
    warnings = list(transcript.warnings)
    if transcript.meeting_date is None:
        warnings.append("No meeting date found; due dates and 'latest decision' can't use it.")

    chunks = chunk_turns(transcript.turns, settings.chunk_max_turns, settings.chunk_max_words)
    try:
        vectors = llm.embed(
            [
                embed_header(title, transcript.meeting_date, c.speakers) + "\n" + c.text
                for c in chunks
            ]
        )
    except LLMError as e:
        log.warning("embedding failed for %s: %s", filename, e)
        vectors = [None] * len(chunks)
        warnings.append("Meaning search is unavailable for this meeting (embedding failed).")

    flagged = sum(looks_like_injection(t.text) for t in transcript.turns)
    if flagged:
        warnings.append(f"{flagged} line(s) look like instructions to an AI; they're marked ⚠️.")

    with db.transaction(conn):
        meeting_id = conn.execute(
            """INSERT INTO meetings
               (project_id, title, meeting_date, source_format, content_hash, raw_text, warnings)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id(conn, project or transcript.project),
                title,
                transcript.meeting_date.isoformat() if transcript.meeting_date else None,
                transcript.format,
                digest,
                text,
                json.dumps(warnings),
            ),
        ).lastrowid
        conn.executemany(
            """INSERT INTO turns
               (meeting_id, line_no, time, end_time, speaker, text, time_approx, flagged)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    meeting_id,
                    t.line_no,
                    t.time,
                    t.end_time,
                    t.speaker,
                    t.text,
                    t.time_approx,
                    looks_like_injection(t.text),
                )
                for t in transcript.turns
            ],
        )
        conn.executemany(
            """INSERT INTO chunks
               (meeting_id, start_line, end_line, start_time, end_time, speakers, text, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    meeting_id,
                    c.start_line,
                    c.end_line,
                    c.start_time,
                    c.end_time,
                    ",".join(c.speakers),
                    c.text,
                    db.to_blob(v),
                )
                for c, v in zip(chunks, vectors)
            ],
        )

    return IngestResult(
        meeting_id=meeting_id,
        title=title,
        turns=len(transcript.turns),
        chunks=len(chunks),
        warnings=warnings,
        skipped=[s.__dict__ for s in transcript.skipped],
    )
