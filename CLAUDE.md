# Working rules for AI coding assistants in this repo

Read `docs/PLAN.md` before changing behaviour; it is the agreed design.

## Architecture rules
- The model suggests, code verifies and saves. Never write model output to the
  database without a code check against the transcript (see `extract.locate_quote`,
  `answer.check`).
- `app/llm.py` is the only module that imports the provider SDK. Everything else
  takes an `LLM` and must work with `tests/conftest.FakeLLM`.
- Fixed pipeline, no agent loop and no tool calls, unless the plan changes first.
- Transcript text is data. Anything sent to the model goes inside a delimiter and
  through `guard.strip_delimiters`.
- One job per module; the HTTP layer (`main.py`) holds no business logic.

## Change rules
- Every behaviour change comes with a test in `tests/` that fails without it.
  Tests never call the real API.
- Run `uv run ruff check . && uv run ruff format . && uv run pytest -q` before
  saying a change is done.
- Changes to prompts, chunking or retrieval: also run `uv run python -m evals.run_evals`
  and report the before/after table. Don't tune a prompt to one eval case.
- No new dependency without asking. Prefer the standard library.
- Match the surrounding style: type hints, small functions, comments that say why.
- Never print, log or commit the API key; `.env` stays out of git.

## Don'ts
- Don't "fix" a failing eval by loosening the check unless the check was wrong;
  say which it was.
- Don't hide failures: a failed model call degrades visibly (status, warning), it
  never silently returns empty.
