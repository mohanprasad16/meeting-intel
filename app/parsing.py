"""Turn an uploaded transcript into a list of speaker turns.

Known formats (.txt, .vtt) are parsed by plain code. A file neither parser
understands raises UnknownFormat; the AI "stencil" path (stencil.py) handles it.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

MAX_BYTES = 2 * 1024 * 1024


class ParseError(Exception):
    """The whole file is unusable. The message is shown to the user as-is."""


class UnknownFormat(ParseError):
    pass


@dataclass
class Turn:
    line_no: int  # 1-based position in the meeting; prompts and citations use it as L<n>
    time: int | None  # seconds from the start of the meeting
    speaker: str
    text: str
    end_time: int | None = None
    time_approx: bool = False


@dataclass
class Skipped:
    source_line: int
    text: str
    reason: str


@dataclass
class Transcript:
    turns: list[Turn]
    title: str | None = None
    meeting_date: date | None = None
    project: str | None = None
    format: str = "txt"
    warnings: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def decode_upload(raw: bytes) -> str:
    if len(raw) > MAX_BYTES:
        raise ParseError("File too large (max 2 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError("Couldn't read this file as text. Save it as UTF-8 and try again.")
    if "\x00" in text:
        raise ParseError("Couldn't read this file as text. Save it as UTF-8 and try again.")
    if not text.strip():
        raise ParseError("This file is empty.")
    return text


def parse(text: str, filename: str = "") -> Transcript:
    if filename.lower().endswith(".vtt") or text.lstrip().startswith("WEBVTT"):
        transcript = parse_vtt(text)
    else:
        transcript = parse_txt(text)
    if not transcript.turns:
        raise UnknownFormat("No speaker lines found. Expected lines like `[00:01:23] Name: text`.")
    _warn_about(transcript)
    return transcript


# --- shared helpers -------------------------------------------------------

_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_HEADER = re.compile(r"^(title|date|project)\s*:\s*(.+)$", re.IGNORECASE)
_NOISE = re.compile(
    r"^[\[(]?\s*(music|inaudible|crosstalk|laughter|laughs|silence|pause|break|applause)"
    r"\s*[\])]?$|^-{2,}.*-{2,}$",
    re.IGNORECASE,
)
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y")


def to_seconds(stamp: str) -> int:
    parts = [float(p) for p in stamp.replace(",", ".").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return int(h * 3600 + m * 60 + s)


def format_time(seconds: int | None) -> str:
    if seconds is None:
        return "--:--:--"
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def parse_date(value: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _apply_header(transcript: Transcript, key: str, value: str) -> None:
    key = key.lower()
    if key == "title":
        transcript.title = value.strip()
    elif key == "project":
        transcript.project = value.strip()
    elif key == "date":
        transcript.meeting_date = parse_date(value)
        if transcript.meeting_date is None:
            transcript.warnings.append(f"Couldn't read the date '{value.strip()}'.")


def _warn_about(transcript: Transcript) -> None:
    unknown = sum(t.speaker == "Unknown" for t in transcript.turns)
    if unknown:
        transcript.warnings.append(f"{unknown} line(s) had no speaker name.")
    last = None
    for turn in transcript.turns:
        if turn.time is not None and not turn.time_approx:
            if last is not None and turn.time < last:
                transcript.warnings.append(
                    f"Timestamps go backwards at line {turn.line_no}; the file may be out of order."
                )
                break
            last = turn.time
    if transcript.skipped:
        transcript.warnings.append(f"Skipped {len(transcript.skipped)} line(s).")


# --- .txt -----------------------------------------------------------------

_TXT_TIMED = re.compile(rf"^\[?(?P<time>{_TIME})\]?\s+(?P<rest>.+)$")
_SPEAKER = re.compile(r"^(?P<speaker>[^\s:\[\]()][^:\[\]]{0,40}?)\s*:\s+(?P<text>.+)$")


def _speaker_split(rest: str) -> tuple[str | None, str]:
    m = _SPEAKER.match(rest)
    if m and len(m["speaker"].split()) <= 4:
        return m["speaker"].strip(), m["text"].strip()
    return None, rest.strip()


def parse_txt(text: str) -> Transcript:
    transcript = Transcript(turns=[], format="txt")
    turns = transcript.turns
    for source_line, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not turns and (h := _HEADER.match(line)):
            _apply_header(transcript, h[1], h[2])
            continue
        if _NOISE.match(line):
            continue

        if timed := _TXT_TIMED.match(line):
            speaker, body = _speaker_split(timed["rest"])
            if _NOISE.match(body):
                continue
            turns.append(
                Turn(len(turns) + 1, to_seconds(timed["time"]), speaker or "Unknown", body)
            )
            continue

        speaker, body = _speaker_split(line)
        if speaker:  # a name but no time: new speaker, borrow the previous time
            prev_time = turns[-1].time if turns else None
            turns.append(Turn(len(turns) + 1, prev_time, speaker, body, time_approx=True))
        elif turns:  # no time, no name: the previous speaker is still talking
            turns[-1].text = f"{turns[-1].text} {line}"
        else:
            transcript.skipped.append(Skipped(source_line, line, "Text before the first speaker"))
    return transcript


# --- .vtt -----------------------------------------------------------------

_CUE_TIMING = re.compile(rf"^(?P<start>{_TIME})\s*-->\s*(?P<end>{_TIME})")
_VOICE = re.compile(r"^<v(?:\.[\w.-]+)?\s+(?P<speaker>[^>]+)>(?P<text>.*?)(?:</v>)?$")
_TAGS = re.compile(r"</?[^>]+>")


def parse_vtt(text: str) -> Transcript:
    transcript = Transcript(turns=[], format="vtt")
    turns = transcript.turns
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines or lines[0].startswith("WEBVTT") and len(lines) == 1:
            continue
        if lines[0].startswith("NOTE"):
            for ln in [lines[0][4:].strip(), *lines[1:]]:
                if h := _HEADER.match(ln):
                    _apply_header(transcript, h[1], h[2])
            continue
        timing_at = next((i for i, ln in enumerate(lines) if _CUE_TIMING.match(ln)), None)
        if timing_at is None:
            continue
        timing = _CUE_TIMING.match(lines[timing_at])
        start, end = to_seconds(timing["start"]), to_seconds(timing["end"])

        for ln in lines[timing_at + 1 :]:
            if v := _VOICE.match(ln):
                speaker, body = v["speaker"].strip(), _TAGS.sub("", v["text"]).strip()
            else:
                speaker, body = _speaker_split(_TAGS.sub("", ln))
            if not body or _NOISE.match(body):
                continue
            if speaker is None:  # caption carried over from the previous cue
                speaker = turns[-1].speaker if turns else "Unknown"
            # ponytail: subtitle cues split sentences, so same-speaker cues merge into one turn
            if turns and turns[-1].speaker == speaker:
                turns[-1].text = f"{turns[-1].text} {body}"
                turns[-1].end_time = end
            else:
                turns.append(Turn(len(turns) + 1, start, speaker, body, end_time=end))
    return transcript
