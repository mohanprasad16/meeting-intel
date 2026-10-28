"""Hybrid retrieval: keyword (FTS5 / BM25) + meaning (vectors), merged with RRF."""

import re
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from app import db
from app.config import settings
from app.llm import LLM, LLMError

_RRF_K = 60  # standard damping constant: rank 1 and rank 5 differ, rank 50 and 55 barely
_WORD = re.compile(r"[\w']+")
_STOPWORDS = set(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "s",
        "so",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "about",
        "any",
    ]
)


@dataclass
class Scope:
    project_id: int | None = None
    meeting_id: int | None = None

    def where(self, alias: str = "m") -> tuple[str, list]:
        clauses, params = [], []
        if self.project_id is not None:
            clauses.append(f"{alias}.project_id = ?")
            params.append(self.project_id)
        if self.meeting_id is not None:
            clauses.append(f"{alias}.id = ?")
            params.append(self.meeting_id)
        return (" AND ".join(clauses) or "1=1"), params


@dataclass
class Hit:
    chunk_id: int
    meeting_id: int
    meeting_title: str
    meeting_date: str | None
    start_line: int
    end_line: int
    start_time: int | None
    speakers: str
    text: str
    score: float = 0.0
    found_by: list[str] = field(default_factory=list)


def _fts_query(question: str) -> str:
    words = [w for w in _WORD.findall(question.lower()) if w not in _STOPWORDS and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def keyword_search(conn: sqlite3.Connection, question: str, scope: Scope, limit: int) -> list[int]:
    query = _fts_query(question)
    if not query:
        return []
    where, params = scope.where()
    rows = conn.execute(
        f"""SELECT c.id FROM chunks_fts f
            JOIN chunks c ON c.id = f.rowid JOIN meetings m ON m.id = c.meeting_id
            WHERE chunks_fts MATCH ? AND {where}
            ORDER BY bm25(chunks_fts) LIMIT ?""",
        [query, *params, limit],
    ).fetchall()
    return [r[0] for r in rows]


def vector_search(
    conn: sqlite3.Connection, query_vector: list[float], scope: Scope, limit: int
) -> list[int]:
    where, params = scope.where()
    rows = conn.execute(
        f"""SELECT c.id, c.embedding FROM chunks c JOIN meetings m ON m.id = c.meeting_id
            WHERE c.embedding IS NOT NULL AND {where}""",
        params,
    ).fetchall()
    if not rows:
        return []
    matrix = np.vstack([db.from_blob(r[1]) for r in rows])
    scores = matrix @ np.asarray(query_vector, dtype=np.float32)
    order = np.argsort(-scores)[:limit]
    return [rows[i][0] for i in order]


def rrf(rankings: dict[str, list[int]]) -> list[tuple[int, float, list[str]]]:
    """Reciprocal Rank Fusion: a chunk ranked high by both lists wins; scores from
    different methods never need to be on the same scale."""
    scores: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    for name, ids in rankings.items():
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            sources.setdefault(chunk_id, []).append(name)
    ordered = sorted(scores, key=lambda cid: -scores[cid])
    return [(cid, scores[cid], sources[cid]) for cid in ordered]


def load_hits(conn: sqlite3.Connection, ranked: list[tuple[int, float, list[str]]]) -> list[Hit]:
    if not ranked:
        return []
    ids = [cid for cid, _, _ in ranked]
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT c.id, c.meeting_id, m.title, m.meeting_date, c.start_line, c.end_line,
                       c.start_time, c.speakers, c.text
                FROM chunks c JOIN meetings m ON m.id = c.meeting_id
                WHERE c.id IN ({",".join("?" * len(ids))})""",
            ids,
        )
    }
    return [
        Hit(
            chunk_id=cid,
            meeting_id=rows[cid]["meeting_id"],
            meeting_title=rows[cid]["title"],
            meeting_date=rows[cid]["meeting_date"],
            start_line=rows[cid]["start_line"],
            end_line=rows[cid]["end_line"],
            start_time=rows[cid]["start_time"],
            speakers=rows[cid]["speakers"],
            text=rows[cid]["text"],
            score=score,
            found_by=found_by,
        )
        for cid, score, found_by in ranked
        if cid in rows
    ]


def search(
    conn: sqlite3.Connection, llm: LLM, question: str, scope: Scope, top_k: int | None = None
) -> tuple[list[Hit], dict]:
    """Returns the top chunks plus what each method found, for the trace."""
    top_k = top_k or settings.top_k
    n = settings.search_candidates
    keyword_ids = keyword_search(conn, question, scope, n)
    try:
        vector_ids = vector_search(conn, llm.embed([question], query=True)[0], scope, n)
        vector_error = None
    except LLMError as e:  # keyword search alone still answers
        vector_ids, vector_error = [], str(e)
    hits = load_hits(conn, rrf({"keyword": keyword_ids, "meaning": vector_ids})[:top_k])
    info = {"keyword": keyword_ids[:top_k], "meaning": vector_ids[:top_k]}
    if vector_error:
        info["meaning_error"] = vector_error
    return hits, info
