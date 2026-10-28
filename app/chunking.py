"""Group speaker turns into overlapping windows for search."""

import re
from dataclasses import dataclass

from app.parsing import Turn

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    start_line: int
    end_line: int
    start_time: int | None
    end_time: int | None
    speakers: list[str]
    text: str  # "Speaker: words" lines, what the answer model reads


def _words(text: str) -> int:
    return len(text.split())


def _split_long(turn: Turn, max_words: int) -> list[Turn]:
    """One monologue over the word limit becomes several pieces, cut at sentence ends."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(turn.text):
        if current and _words(current) + _words(sentence) > max_words:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    pieces.append(current)
    return [
        Turn(turn.line_no, turn.time, turn.speaker, p, turn.end_time, turn.time_approx)
        for p in pieces
    ]


def _to_chunk(window: list[Turn]) -> Chunk:
    speakers = list(dict.fromkeys(t.speaker for t in window))
    times = [t.time for t in window if t.time is not None]
    ends = [t.end_time or t.time for t in window if (t.end_time or t.time) is not None]
    return Chunk(
        start_line=window[0].line_no,
        end_line=window[-1].line_no,
        start_time=min(times) if times else None,
        end_time=max(ends) if ends else None,
        speakers=speakers,
        text="\n".join(f"{t.speaker}: {t.text}" for t in window),
    )


def chunk_turns(turns: list[Turn], max_turns: int = 5, max_words: int = 300) -> list[Chunk]:
    """Windows of up to `max_turns` turns / `max_words` words, overlapping by one turn,
    so a question and its reply are never only on opposite sides of a boundary."""
    units = [piece for t in turns for piece in _split_long(t, max_words)]
    chunks: list[Chunk] = []
    start = 0
    while start < len(units):
        end, words = start, 0
        while end < len(units) and end - start < max_turns:
            if end > start and words + _words(units[end].text) > max_words:
                break
            words += _words(units[end].text)
            end += 1
        chunks.append(_to_chunk(units[start:end]))
        if end >= len(units):
            break
        start = end - 1 if end - start > 1 else end  # overlap one turn
    return chunks
