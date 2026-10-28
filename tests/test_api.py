import pytest
from fastapi.testclient import TestClient

from app import main
from tests.conftest import FakeLLM
from tests.test_extract import EMPTY, KICKOFF, KICKOFF_EXTRACT


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(main.settings, "gemini_api_key", "test")
    fake = FakeLLM(
        {
            "Extraction": lambda p: KICKOFF_EXTRACT if "Kickoff" in p else EMPTY,
            "FollowUps": {"updates": []},
            "Proposal": {
                "pattern": r"^(?P<speaker>\w+) \((?P<time>[\d:]+)\) - (?P<text>.+)$",
                "title": "Retro",
            },
            "text": "Launch is March 20 [c1].",
        }
    )
    monkeypatch.setattr(main, "llm", fake)
    main._hits.clear()
    with TestClient(main.app) as client:  # runs the startup hook that creates tables
        yield client


def test_upload_then_ask(client):
    r = client.post("/api/upload", files={"file": ("kickoff.txt", KICKOFF)})
    assert r.status_code == 200, r.text
    assert r.json()["extraction"]["action_items"] == 2
    r = client.post("/api/ask", json={"question": "When is launch?"})
    body = r.json()
    assert body["status"] == "answered" and body["citations"][0]["meeting"] == "Kickoff"
    assert client.get("/api/action-items?status=open").json()[0]["owner"] == "Priya"


def test_duplicate_is_409_and_bad_type_is_400(client):
    client.post("/api/upload", files={"file": ("k.txt", KICKOFF)})
    assert client.post("/api/upload", files={"file": ("k2.txt", KICKOFF)}).status_code == 409
    r = client.post("/api/upload", files={"file": ("notes.pdf", b"%PDF-1.4")})
    assert (r.status_code, r.json()["detail"]) == (
        400,
        "Only .txt and .vtt transcripts are supported.",
    )


def test_unknown_format_previews_and_saves_nothing_until_confirm(client):
    raw = b"Ravi (00:00:10) - Hello there.\nArun (00:00:20) - Dashboard is done.\n"
    preview = client.post("/api/upload", files={"file": ("retro.txt", raw)}).json()["preview"]
    assert preview["match_ratio"] == 1.0 and preview["turns"][1]["speaker"] == "Arun"
    assert client.get("/api/projects").json() == []
    r = client.post(
        "/api/upload/confirm",
        json={"token": preview["token"], "title": "Retro", "date": "2026-03-22"},
    )
    assert r.status_code == 200
    assert (
        client.post(
            "/api/upload/confirm", json={"token": preview["token"], "title": "x"}
        ).status_code
        == 404
    )


def test_path_in_filename_is_ignored(client):
    r = client.post("/api/upload", files={"file": ("../../etc/kickoff.txt", KICKOFF)})
    assert r.json()["title"] == "Kickoff"


def test_rate_limit(client, monkeypatch):
    monkeypatch.setitem(main.LIMITS, "ask", 2)
    for _ in range(2):
        client.post("/api/ask", json={"question": "hi"})
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 429
