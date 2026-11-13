# AgentCrew

Text-to-SQL router agent (Streamlit, SQLite, Gemini). See README.md and docs/.

## Current work: v3-production

Branch `v3-production`, last code commit `e0c5cc0`. Phases 1 and 2 are done
and pushed; Phase 3 is next. A draft PR to `main` is open and CI runs on every
push to it (it does not deploy; only a merge to `main` does).

### Ground rules
- Never push to `main`: CD auto-deploys it to the live demo. Work on `v3-production`.
- Ask before every commit and every push.
- Extend existing modules. Do not rewrite the router agent or anything that works.
- Every SQL path (agent, SQL editor, API) goes through the same layers:
  read-only connection, sqlglot guard, resource caps. No path may bypass them.
- Do not change the Cloud Run service, Secret Manager or the CD workflow without asking.
- ruff and the full pytest suite must pass at the end of every phase.
- Stop after each phase with a short summary and wait for approval.
- NorthStar prompts must stay byte-identical. Prove it with the offline replay:
  `python scripts/run_eval_v2.py --set <set> --arch baseline full --run NAME --offline --cache-dir eval/runs/postfix50-cache`
  for holdout_easy, holdout_agent, holdout_agent_v2 (complete, D 12 + 19 + 14 = 45/50).
- Windows: use `.venv/Scripts/python.exe`; pytest needs `--basetemp=<scratch dir>`.

### Built so far
- Phase 1 (`847ee22`): idle guard, process-wide daily cap (`agentcrew/caps.py`),
  JSON run log on stdout (`agentcrew/runlog.py`), optional Langfuse,
  `AGENTCREW_TRACE_CONTENT`, docs/OPERATIONS.md.
- Phase 2 (`dd3c8b6`, `132acd2`, `e0c5cc0`): CSV, Excel and SQLite uploads
  (`agentcrew/datasets/`, `agentcrew/data_panel.py`), self-hosted PostgreSQL via
  `AGENTCREW_DATABASE_URL`, model-drafted descriptions with a row-meaning line,
  inferred joins, upload security hardening, docs/DATA_SOURCES.md, docs/SELF_HOSTED.md.
- Unseen-schema test (`9b9f759`): college data, clean and messy copies, 15 frozen
  questions. Clean A 15/15, D 15/15. Messy 14/15 before the grain fix, 15/15 after
  (same 15 questions, not a holdout).
- 1505 tests pass. Decisions D16 to D23 are in docs/DECISIONS.md.

### Remaining plan
- Phase 3, SQL editor: collapsed expander, prefilled with the agent's last SQL,
  run through `tools.execute_readonly_sql`, result table with the row cap.
- Phase 4, FastAPI under `/v1`: health, ask, run read-only SQL, upload a dataset,
  get its schema. Shared core with Streamlit. API key header (keys from env),
  per-key rate limit, request size limits, clear errors. Tests for auth, rate
  limiting and write SQL refused. A separate Cloud Run service from the same
  image; ask before touching CD.
- Phase 5: 10 NorthStar questions in Hindi and Telugu typed in English letters
  only, no native script, no translation step. Add one prompt instruction only
  if several fail; report before and after.
- Phase 6: rerun the frozen 50 if prompts or schema selection changed (must stay
  at 45/50 or better; show failures before changing anything), run
  pr-review-toolkit on the whole branch diff, run the live PostgreSQL tests in
  CI with a postgres service container, update README and docs, final resume
  summary.

### Open items
- `st.components.v1.html` (idle guard) is deprecated; move to `st.iframe` before a Streamlit upgrade.
- MySQL is deferred; self-hosted mode is PostgreSQL only (dialect parameter kept).
- The daily cap resets on a cold start (no store behind it).
- On uploaded data D adds nothing over A so far; D's metric tools are NorthStar-only.
- Live PostgreSQL tests need `AGENTCREW_TEST_POSTGRES_URL` (skipped in CI).
