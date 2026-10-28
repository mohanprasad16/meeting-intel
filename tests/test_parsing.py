from datetime import date

import pytest

from app.parsing import ParseError, UnknownFormat, decode_upload, parse

TXT = """Title: Project Kickoff
Date: 2026-03-02
Project: Pilot Launch

[00:01:10] Ravi: Welcome everyone.
[00:02:15] Ravi: Let's lock the pilot launch for March 20,
because the client wants it before their board meeting.
Priya: Agreed.
[00:02:40] Priya: I'll send the pricing sheet by Friday.
[music]
[00:03:05] We should check the budget again.
02:30 Arun: Late note.
"""


def test_txt_header_and_turns():
    t = parse(TXT, "kickoff.txt")
    assert (t.title, t.meeting_date, t.project) == (
        "Project Kickoff",
        date(2026, 3, 2),
        "Pilot Launch",
    )
    assert [x.speaker for x in t.turns] == ["Ravi", "Ravi", "Priya", "Priya", "Unknown", "Arun"]
    assert t.turns[0].time == 70


def test_txt_continuation_joins_previous_speaker():
    t = parse(TXT)
    assert t.turns[1].text.endswith("before their board meeting.")


def test_txt_name_without_time_borrows_previous_time():
    agreed = parse(TXT).turns[2]
    assert (agreed.text, agreed.time, agreed.time_approx) == ("Agreed.", 135, True)


def test_txt_noise_skipped_and_warnings():
    t = parse(TXT)
    assert all("music" not in x.text for x in t.turns)
    assert "1 line(s) had no speaker name." in t.warnings
    assert any("backwards" in w for w in t.warnings)  # 02:30 after 03:05


def test_brackets_optional_and_minutes_only():
    t = parse("00:00:05 Ravi: hi\n[01:10] Priya: hello")
    assert [(x.time, x.speaker) for x in t.turns] == [(5, "Ravi"), (70, "Priya")]


ZOOM_VTT = """WEBVTT

NOTE Title: Pricing review

1
00:00:01.000 --> 00:00:04.000
Ravi: Let's look at pricing.

2
00:00:04.500 --> 00:00:07.000
Ravi: Starting with tier one.

3
00:00:08.000 --> 00:00:10.000
Priya: Sounds good.
"""

TEAMS_VTT = """WEBVTT

a1b2/12-0
00:01:00.000 --> 00:01:03.000
<v Meena Iyer>We need a buffer.</v>

a1b2/13-0
00:01:04.000 --> 00:01:06.000
<v Arun>Ten percent.</v>
"""


def test_zoom_vtt_merges_same_speaker_cues():
    t = parse(ZOOM_VTT, "pricing.vtt")
    assert t.title == "Pricing review"
    assert [(x.speaker, x.time, x.end_time) for x in t.turns] == [("Ravi", 1, 7), ("Priya", 8, 10)]
    assert t.turns[0].text == "Let's look at pricing. Starting with tier one."


def test_teams_vtt_voice_tags():
    t = parse(TEAMS_VTT, "sync.vtt")
    assert [(x.speaker, x.text) for x in t.turns] == [
        ("Meena Iyer", "We need a buffer."),
        ("Arun", "Ten percent."),
    ]


def test_unknown_format_raises():
    with pytest.raises(UnknownFormat):
        parse("Ravi (00:02:15) - Let's launch in March.\nPriya (00:02:40) - Sure.")


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"", "empty"),
        (b"   \n ", "empty"),
        (b"\xff\xfe\x00bad", "UTF-8"),
        (b"x" * (2 * 1024 * 1024 + 1), "too large"),
    ],
)
def test_decode_rejects_bad_files(raw, message):
    with pytest.raises(ParseError, match=message):
        decode_upload(raw)


def test_script_tags_stay_plain_text():
    t = parse("[00:00:01] Bob: <script>alert(1)</script>")
    assert t.turns[0].text == "<script>alert(1)</script>"  # the UI renders it with textContent
