"""Score the full pipeline against evals/golden.yaml with the real model.

    uv run python -m evals.run_evals            # full run -> evals/results.md
    uv run python -m evals.run_evals --sweep    # retrieval only: chunk size x top-k

Needs GEMINI_API_KEY. Uses a throwaway database, never data/meetings.db.
"""

import argparse
import logging
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

from app import db
from app.ask import ask
from app.config import settings
from app.extract import extract_meeting
from app.ingest import read_upload, store
from app.llm import GeminiLLM
from app.parsing import UnknownFormat, decode_upload
from app.search import Scope, search
from app.stencil import infer

HERE = Path(__file__).parent
SAMPLES = HERE.parent / "data" / "samples"


def load_samples(conn, llm, extract=True) -> None:
    for path in sorted(SAMPLES.iterdir()):
        raw = path.read_bytes()
        try:
            text, transcript = read_upload(raw, path.name)
        except UnknownFormat:
            text = decode_upload(raw)
            transcript = infer(llm, text).transcript
            transcript.project = "Smart Shelf Pilot"
        meeting_id = store(conn, llm, text, transcript, path.name).meeting_id
        print(f"loaded {path.name}", file=sys.stderr)
        if extract:
            extract_meeting(conn, llm, meeting_id)


def evidence_meetings(conn, ids: list[str]) -> set[str]:
    tables = {"c": "chunks", "a": "action_items", "d": "decisions"}
    titles = set()
    for ev in ids:
        kind, n = ev[0], int(ev[1:])
        if kind == "s":
            row = conn.execute("SELECT title FROM meetings WHERE id = ?", (n,)).fetchone()
        elif kind == "u":
            row = conn.execute(
                "SELECT m.title FROM action_item_updates u JOIN meetings m ON m.id = u.meeting_id WHERE u.id = ?",
                (n,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT m.title FROM {tables[kind]} t JOIN meetings m ON m.id = t.meeting_id WHERE t.id = ?",
                (n,),
            ).fetchone()
        if row:
            titles.add(row[0])
    return titles


def scope_for(conn, case) -> Scope:
    if not case.get("project"):
        return Scope()
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (case["project"],)).fetchone()
    return Scope(project_id=row[0])


def grade(case, answer, citations, sent_meetings) -> tuple[bool, list[str], bool | None]:
    """Returns (passed, reasons, retrieval_hit). retrieval_hit is None when the case
    doesn't name the meetings it needs."""
    reasons = []
    text = answer.text.lower()
    expect = case.get("expect")
    if expect == "declined":
        if answer.status not in ("not_found", "refused", "blocked"):
            reasons.append(f"expected a decline, got {answer.status}")
    elif expect and answer.status != expect:
        reasons.append(f"expected {expect}, got {answer.status}")
    for need in case.get("contains", []):
        options = need if isinstance(need, list) else [need]
        if not any(o.lower() in text for o in options):
            reasons.append(f"missing {' / '.join(options)}")
    for bad in case.get("not_contains", []):
        if bad.lower() in text:
            reasons.append(f"contains '{bad}'")
    cited = {c["meeting"] for c in citations}
    needs = [n if isinstance(n, list) else [n] for n in case.get("cites", [])]
    for options in needs:
        if not cited & set(options):
            reasons.append(f"no citation from {' / '.join(options)}")
    hit = all(sent_meetings & set(options) for options in needs) if needs else None
    return not reasons, reasons, hit


def full_run(llm, cases) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(str(Path(tmp) / "eval.db"), init=True)
        started = time.perf_counter()
        load_samples(conn, llm)
        ingest_seconds = time.perf_counter() - started

        rows, latencies, tokens = [], [], []
        for case in cases:
            result = ask(conn, llm, case["q"], scope_for(conn, case))
            sent = evidence_meetings(conn, result.trace.get("sent", []))
            ok, reasons, hit = grade(case, result.answer, result.answer.citations, sent)
            latencies.append(result.trace["timings"]["total"])
            tokens.append(
                result.trace["usage"]["input_tokens"] + result.trace["usage"]["output_tokens"]
            )
            rows.append((case, ok, reasons, hit, result))
            mark = "PASS" if ok else "FAIL"
            print(f"{mark} {case['id']}: {'; '.join(reasons)}", file=sys.stderr)

    def rate(subset):
        return f"{sum(r[1] for r in subset)}/{len(subset)}"

    groups = {
        "Facts": "fact-",
        "Why": "why-",
        "Tasks & status": "task-",
        "Decisions": "decision-",
        "Summary": "summary-",
        "Across meetings": "cross-",
        "Not discussed": "notfound-",
        "Prompt injection": "trap-",
        "Project scope": "scope-",
    }
    hits = [r[3] for r in rows if r[3] is not None]
    out = [
        "# Eval results",
        "",
        (
            f"Run {datetime.now():%Y-%m-%d %H:%M} · answer model `{settings.answer_model}` · "
            f"router `{settings.fast_model}` · embeddings `{settings.embedding_model}` · "
            f"chunks {settings.chunk_max_turns} turns · top-k {settings.top_k}"
        ),
        "",
        "| Group | Passed |",
        "|---|---|",
        *[
            f"| {g} | {rate([r for r in rows if r[0]['id'].startswith(p)])} |"
            for g, p in groups.items()
        ],
        f"| **All answers** | **{rate(rows)}** |",
        f"| Right meeting in the evidence sent | {sum(hits)}/{len(hits)} |",
        "",
        (
            f"Median answer time {statistics.median(latencies):.2f}s · slowest {max(latencies):.2f}s · "
            f"median {int(statistics.median(tokens))} tokens per question · "
            f"loading + extracting 6 meetings took {ingest_seconds:.0f}s"
        ),
        "",
        "| Case | Result | Route | Why it failed |",
        "|---|---|---|---|",
    ]
    for case, ok, reasons, _, result in rows:
        route = f"{result.trace['route']['kind']} ({result.trace['route']['by']})"
        out.append(f"| {case['id']} | {'✅' if ok else '❌'} | {route} | {'; '.join(reasons)} |")
    return "\n".join(out) + "\n"


def sweep(llm, cases) -> str:
    """Retrieval only, no answer calls: does search put the right meeting in the top k?"""
    targets = [c for c in cases if c.get("cites")]
    out = [
        "# Retrieval sweep",
        "",
        "Share of cited-meeting questions whose meetings all appear in the chunks sent.",
        "",
        "| Chunk size (turns) | top-k 5 | top-k 8 | top-k 12 |",
        "|---|---|---|---|",
    ]
    for turns in (3, 5, 8):
        settings.chunk_max_turns = turns
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "sweep.db"), init=True)
            load_samples(conn, llm, extract=False)
            cells = []
            for k in (5, 8, 12):
                hit = 0
                for case in targets:
                    hits, _ = search(conn, llm, case["q"], scope_for(conn, case), top_k=k)
                    found = {h.meeting_title for h in hits}
                    hit += all(
                        found & set(n if isinstance(n, list) else [n]) for n in case["cites"]
                    )
                cells.append(f"{hit}/{len(targets)}")
            out.append(f"| {turns} | " + " | ".join(cells) + " |")
            print(out[-1], file=sys.stderr)
    return "\n".join(out) + "\n"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--runs", type=int, default=1, help="repeat the full run; models vary")
    args = parser.parse_args()
    if not settings.gemini_api_key:
        sys.exit("Set GEMINI_API_KEY in .env first.")
    cases = yaml.safe_load((HERE / "golden.yaml").read_text())
    llm = GeminiLLM()
    if args.sweep:
        report = sweep(llm, cases)
        (HERE / "sweep.md").write_text(report)
    else:
        report = "\n".join(full_run(llm, cases) for _ in range(args.runs))
        (HERE / "results.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
