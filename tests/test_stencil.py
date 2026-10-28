import pytest

from app.parsing import ParseError
from app.stencil import apply_pattern, infer
from tests.conftest import FakeLLM

RETRO = """Smart Shelf Pilot - Retro (2026-03-22)
Ravi (00:00:10) - Let's do a quick check.
Arun (00:00:25) - The dashboard is done.
Priya (00:00:48) - The deck is halfway.
"""
GOOD = r"^(?P<speaker>[^()]+?) \((?P<time>\d{2}:\d{2}:\d{2})\) - (?P<text>.+)$"


def test_apply_pattern_cuts_text_from_the_file():
    transcript, failed = apply_pattern(GOOD, RETRO)
    assert next((t.speaker, t.time, t.text) for t in transcript.turns) == (
        "Ravi",
        10,
        "Let's do a quick check.",
    )
    assert failed == [(1, "Smart Shelf Pilot - Retro (2026-03-22)")]


def test_infer_accepts_good_stencil_heading_lines_dont_count():
    llm = FakeLLM({"Proposal": {"pattern": GOOD, "title": "Retro", "date": "2026-03-22"}})
    r = infer(llm, RETRO)
    assert (r.match_ratio, r.attempts, r.transcript.title) == (1.0, 1, "Retro")
    assert str(r.transcript.meeting_date) == "2026-03-22"


def test_infer_retries_with_failed_lines_then_succeeds():
    replies = iter([{"pattern": r"^(?P<speaker>\w+): (?P<text>.+)$"}, {"pattern": GOOD}])
    llm = FakeLLM({"Proposal": lambda p: next(replies)})
    body = "\n".join(RETRO.splitlines()[1:])  # all spoken lines
    r = infer(llm, body)
    assert r.attempts == 2 and r.match_ratio == 1.0
    assert "did not match" in llm.calls[1][1]


def test_infer_gives_up_after_two_retries():
    llm = FakeLLM({"Proposal": {"pattern": r"^(?P<speaker>\w+): (?P<text>.+)$"}})
    with pytest.raises(ParseError, match="export the transcript"):
        infer(llm, RETRO)
    assert len(llm.calls) == 3


def test_slow_pattern_times_out_instead_of_freezing():
    # The regex library defuses the textbook (a+)+ case; alternation still explodes.
    evil = r"^(?P<speaker>(a|aa)+)x(?P<text>.*)$"
    with pytest.raises(ParseError, match="too slow"):
        apply_pattern(evil, "a" * 40 + "b")


@pytest.mark.parametrize("pattern", [r"^(.+)$", "(", "a" * 400])
def test_unusable_patterns_rejected(pattern):
    with pytest.raises(ParseError):
        apply_pattern(pattern, RETRO)
