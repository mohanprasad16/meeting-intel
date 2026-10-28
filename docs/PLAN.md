# Meeting Intelligence — locked design

Rule everywhere: **the AI suggests, code verifies and saves.** No agent, no tools: code runs a fixed pipeline.

## 1. Upload & parse
- Safety first: size ≤ 2 MB (checked before reading), UTF-8 text, not empty, duplicate = content hash. Filenames never used as paths. Rate limits on upload/ask.
- Known formats parsed by code: `.txt` (`[00:02:15] Name: text`, brackets optional, `MM:SS` ok; optional `Title:` / `Date:` / `Project:` header) and `.vtt` (Zoom `Name:` and Teams `<v Name>`; end time kept).
- Line rules: no time + no name → continuation of previous speaker; name + no time → new speaker, previous time, marked approximate; time + no name → `Unknown` + warning; `[music]` / `(inaudible)` / `--- break ---` skipped; times going backwards → warning, order kept.
- Unknown format → AI sees first ~40 lines and returns only a regex ("stencil") → code applies it with a timeout + length cap → < 80% match = retry with the failed lines (max 2 retries) → else reject with "export as .txt or .vtt". ≥ 80% → preview, user must click Import. Nothing saved before approval.
- UI: ✅ loaded / ⚠️ loaded with N skipped (viewable) / ❌ rejected with reason. Never silent.

## 2. Chunking
- Speaker-turn windows: ~5 turns or 300 words (whichever first), overlap 1 turn. Long turn → own chunk, split at sentence ends.
- Final size picked by eval (3 / 5 / 8 turns).

## 3. Embedding & search (hybrid)
- One vector per chunk; embedded text = meeting header (title, date, speakers) + chunk. Document vs query task types. Batched.
- Keyword (SQLite FTS5, BM25) top 20 + vector (cosine) top 20 → Reciprocal Rank Fusion → top 8 to the AI, labelled with id, meeting, date, time.
- Scoped to a project / meeting when selected or named.
- No reranker in v1; added only if the eval shows retrieval misses (local cross-encoder).

## 4. Extraction at upload
- One call over the whole, line-numbered transcript → structured JSON: summary, decisions, action items (owner, task, due words, line, quote). Only explicit statements; never guess owner/date.
- Code checks: line exists; quote fuzzy-matches the line (difflib ≥ 0.9, else ±2 lines, else drop + log); time & speaker taken from parsed data; owner not a speaker → ⚠️ tag.
- Due words → real date by code, relative to the meeting date ("by Friday" on a Friday = next Friday). Unparseable → keep words.
- Follow-up: open items from the same project (both directions by meeting date) + new transcript → done / updated / cancelled / no_mention + quote. Code verifies the quote, writes `action_item_updates`. Unsure = stays open.
- Failure → retry once → meeting still saved & searchable, "Retry extraction" shown.
- Names matched case/space-insensitive only.

## 5. Routing
- Rules first (clear words), then a small AI call → `search | action_items | decisions | summary | off_topic` + person / meeting / status filters. Unknown person → drop filter. Any error → search.

## 6. Answer & guard
- Answer only from the material; cite `[c12]`; `NOT_FOUND` if absent; later date wins on conflicts, mention the change.
- Code: every cited id must be in the sent set (else drop that sentence); zero citations → "Not discussed in these meetings." Ids rendered as clickable chips (speaker · meeting · time).
- Known limit: a real id can still be cited for a wrong claim → clickable proof + evals; "more time": per-claim support check.

### 6b. Prompt-injection defence
1. Rules only in the system instruction; data wrapped in `<transcript>` / `<question>`; delimiter strings stripped from data.
2. The AI has no powers (no DB writes, no tools).
3. Fixed reply shapes (enums, JSON schema), validated by code.
4. All code guards above.
5. Upload scan flags injection-like lines ⚠️; flagged lines can't be evidence.
6. Canary token in the system instruction; leaked → answer blocked.
7. Fixed refusal for rule-breaking requests.

## 7. Observability
- Trace per ask/upload: route, retrieved ids (keyword / vector), sent / cited ids, guard result, per-step timings, tokens. Stored in `traces`, logged as JSON, shown in UI as "How I answered".

## 8. Evals
- ~20 golden questions over the samples: facts, tasks/status, changed decision, cross-meeting, not-found, injection/off-topic.
- Scores: retrieval hit, answer correct, correct NOT_FOUND, traps blocked, latency. Used to choose chunk size, k, reranker. Tests (pytest, fake LLM) ≠ evals (real key).

## 9. UI
- One page, 3 panels: projects/meetings + upload | chat with scope picker | action items (filter person/status).
- Citation chip → transcript drawer at the highlighted line. Meeting view: summary, decisions, tasks, transcript. Starter questions; "Load sample meetings"; loading / error / not-found states; light + dark; keyboard.
- Plain HTML/CSS/JS served by FastAPI.

## 10. Engineering
- Python 3.12, FastAPI, Gemini (`llm.py` is the only provider file), SQLite, pytest, ruff, type hints, `.env` settings, Docker + compose (DB on a volume), CLAUDE.md.
- Tables: projects, meetings, turns, chunks (+ embedding), chunks_fts, decisions, action_items, action_item_updates, traces.
- Production (README only): Postgres + pgvector, migrations, org/user ids, background jobs, auth, managed cloud.

## 11. Samples & README
- 5 "Pilot Launch" meetings (txt, Zoom vtt, Teams vtt, txt, odd format) + 1 other-project meeting; one trap line. Synthetic, stated in README.
- README written by the author from bullet notes.

## Build order
1 → 2 → 3 → 6 (working Q&A) → 4 → 5 → 9 → 7 → 8 → Docker → README. AI stencil last.
