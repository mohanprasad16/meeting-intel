from app.ask import ask, match_meeting, match_person
from app.search import Scope
from tests.conftest import FakeLLM
from tests.test_extract import KICKOFF, load, make_llm


def test_person_match_only_for_real_unique_names():
    people = ["Priya", "Karthik (Coldchain)", "Ravi"]
    assert match_person("Did Priya deliver?", people) == "Priya"
    assert match_person("What did karthik promise?", people) == "Karthik"
    assert match_person("What did Bob promise?", people) is None


def test_meeting_match_uses_distinctive_title_words(conn, fake):
    for title in ("Pilot Kickoff", "Pilot Review"):
        conn.execute("INSERT OR IGNORE INTO projects(name) VALUES ('P')")
        conn.execute(
            "INSERT INTO meetings (project_id, title, source_format, content_hash, raw_text) VALUES (1, ?, 'txt', ?, '')",
            (title, title),
        )
    assert match_meeting(conn, "what happened in the kickoff?", Scope()) == 1
    assert match_meeting(conn, "what happened in the pilot?", Scope()) is None  # shared word


def test_task_question_routed_by_rule_with_person_filter(conn):
    llm = make_llm()
    load(conn, llm, KICKOFF, "k.txt")
    llm.replies["text"] = lambda p: "Priya must send the pricing sheet [a1]."
    r = ask(conn, llm, "What tasks does Priya have?", Scope())
    assert r.trace["route"] == {
        "kind": "action_items",
        "by": "rule",
        "status": "any",
        "person": "Priya",
        "meeting_id": None,
    }
    assert r.answer.citations[0]["id"] == "a1"
    assert "a2" not in r.trace["sent"]  # Kumar's task filtered out
    assert conn.execute("SELECT count(*) FROM traces").fetchone()[0] == 1


def test_router_failure_falls_back_to_search(conn):
    llm = make_llm()
    load(conn, llm, KICKOFF, "k.txt")
    llm.replies["text"] = "Launch is March 20 [c1]."
    r = ask(conn, llm, "When is launch?", Scope())  # no rule matches, no Route reply -> error
    assert r.trace["route"]["by"] == "fallback"
    assert r.answer.status == "answered"


def test_off_topic_never_calls_answer_model(conn):
    llm = FakeLLM({"Route": {"kind": "off_topic"}})
    r = ask(conn, llm, "What's the weather?", Scope())
    assert r.answer.status == "refused"
    assert all(name != "text" for name, _ in llm.calls)
