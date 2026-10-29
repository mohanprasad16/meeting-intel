import pytest

from app import audio, main
from app.llm import LLMError
from app.parsing import ParseError
from tests.conftest import FakeLLM

HEARD = """```
[00:00:00] Ravi: Morning everyone. Priya, the demo deck?
[00:00:04] Speaker 2: It's done, I sent it to the client yesterday.
[00:00:09] Ravi: Great, thanks.
```"""


def test_transcribe_strips_fences_and_parses_our_format():
    text, t = audio.transcribe(FakeLLM({"transcribe": HEARD}), b"RIFF....", "standup.m4a")
    assert "```" not in text
    assert [x.speaker for x in t.turns] == ["Ravi", "Speaker 2", "Ravi"]
    assert (t.format, t.turns[1].time) == ("audio", 4)


def test_rename_rewrites_turns_and_stored_text():
    _, t = audio.transcribe(FakeLLM({"transcribe": HEARD}), b"x", "a.wav")
    text = audio.rename(t, {"Speaker 2": "  Priya ", "Ravi": ""})  # blank keeps the old name
    assert [x.speaker for x in t.turns] == ["Ravi", "Priya", "Ravi"]
    assert "[00:00:04] Priya: It's done" in text


@pytest.mark.parametrize(
    "raw, reply, message",
    [
        (b"", HEARD, "empty"),
        (b"x" * (audio.MAX_AUDIO_BYTES + 1), HEARD, "too large"),
        (b"x", "I'm sorry, I can't help with that.", "Couldn't hear a conversation"),
        (b"x", LLMError("503: busy"), "Couldn't transcribe"),
    ],
)
def test_transcribe_failures_are_clear(raw, reply, message):
    with pytest.raises(ParseError, match=message):
        audio.transcribe(FakeLLM({"transcribe": reply}), raw, "a.mp3")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main.settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(main.settings, "gemini_api_key", "test")
    empty = {"summary": "Standup.", "decisions": [], "action_items": []}
    monkeypatch.setattr(
        main,
        "llm",
        FakeLLM({"transcribe": HEARD, "Extraction": empty, "FollowUps": {"updates": []}}),
    )
    main._hits.clear()
    with TestClient(main.app) as c:
        yield c


def test_audio_upload_previews_then_confirm_applies_names(client):
    r = client.post("/api/upload", files={"file": ("standup.m4a", b"fake-audio", "audio/mp4")})
    preview = r.json()["preview"]
    assert (preview["source"], preview["speakers"]) == ("audio", ["Ravi", "Speaker 2"])
    assert client.get("/api/projects").json() == []  # nothing stored before confirm

    r = client.post(
        "/api/upload/confirm",
        json={
            "token": preview["token"],
            "title": "Standup",
            "date": "2026-03-26",
            "speakers": {"Speaker 2": "Priya"},
        },
    )
    assert r.status_code == 200, r.text
    meeting = client.get(f"/api/meetings/{r.json()['meeting_id']}").json()
    assert meeting["format"] == "audio"
    assert [t["speaker"] for t in meeting["turns"]] == ["Ravi", "Priya", "Ravi"]


def test_unsupported_audio_type_rejected(client):
    r = client.post("/api/upload", files={"file": ("call.flac", b"x")})
    assert r.status_code == 400 and "audio recordings" in r.json()["detail"]


def test_garbage_transcription_is_a_400(client, monkeypatch):
    monkeypatch.setattr(main, "llm", FakeLLM({"transcribe": "no speech detected"}))
    r = client.post("/api/upload", files={"file": ("a.wav", b"x")})
    assert r.status_code == 400 and "Couldn't hear" in r.json()["detail"]
