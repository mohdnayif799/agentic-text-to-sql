# AgentCrew

Text-to-SQL router agent (Streamlit, SQLite, Gemini). See README.md and docs/.

## Current work: v3-production

Branch `v3-production`. Phases 1 to 4 are done and pushed (Phase 4, the REST
API, is `60da5cb`). Phase 5 is next. A draft PR to `main` is open and CI runs on every
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
- Phase 3, SQL editor: `agentcrew/sql_editor.py` (`run_sql`, the shared core
  Phase 4's API should reuse) and an "Edit and run the SQL" expander in app.py,
  collapsed, prefilled with the final SQL. Runs on the agent's own cached
  `ToolContext` (`build_runtime(...).tools`), so guard, LIMIT, row cap and
  timeout are the agent's on NorthStar, uploads and PostgreSQL. No model calls.
  Limits: `AGENTCREW_SESSION_SQL_RUN_CAP` (30) and its own daily counter
  `AGENTCREW_DAILY_SQL_RUN_CAP` (1000), not the agent's daily run cap. Refused
  statements count; empty input does not. One run-log line per run
  (surface `sql_editor`, status, rows, seconds, failure kind; SQL text only
  with `AGENTCREW_TRACE_CONTENT`). Errors are one plain line. "Capped" is
  inferred from row count == cap (the guard injects LIMIT, so `truncated` stays
  False). An answer from another data source shows its SQL read-only. The
  expander is keyed with `on_change="rerun"` so a Run click does not collapse
  it. v1 uses the shared NorthStar context, not its own deps. Checked in a real
  browser on NorthStar and an uploaded CSV.
- Structured replies (`agentcrew/llm.py`): `json_candidates()` finds every JSON
  object in a reply (any fenced block, an unclosed fence, stray text around
  it) and `complete_structured` validates each in turn before its one repair
  retry, which CountingClient counts and charges. Prompts unchanged: offline
  replay of the frozen 50 is still D 45/50 with 0 live calls.
- A failed run's summary reads "Stopped" and "It stopped before it could
  answer."; a refused one "Refused". Never "answered directly".
- Phase 4, REST API (docs/API.md, decisions D24 to D29):
  - `agentcrew/service.py` holds the ask flow moved out of app.py unchanged
    (`answer`, `execute`, `log_run`, `describer`, `run_sql_on`, `store`);
    app.py and the API are callers. It looks the agent's entry points up on
    their modules at call time (`app_runner.build_runtime`), so tests that
    patch the module reach every caller. `Turn.code` says why a run did not
    happen; `Caller(surface, client)` goes to the run log.
  - `agentcrew/api/`: `app.py` (`create_app`, the `Gate` ASGI middleware),
    `auth.py`, `ratelimit.py`, `schemas.py`, `routes.py`, `errors.py`.
    Endpoints under /v1: health, ask, sql, datasets (POST), datasets/{id}/schema,
    datasets/{id} (DELETE). Run: `uvicorn agentcrew.api.app:create_app --factory
    --host 0.0.0.0 --port 8080 --no-access-log`.
  - The gate runs before the framework reads a body: key (401), per-key rate
    limit (429), body limit while reading (413), last-resort 500 envelope,
    one `event: request` log line. Keys: `AGENTCREW_API_KEYS`, SHA-256 digests
    compared with `hmac.compare_digest`; logs carry a 12-hex `client` hash.
  - An upload is the raw request body with `?filename=`, one file, through
    `datasets.ingest`. Datasets are owned by the key's digest: 404 for any
    other key, 410 for the owner once expired (30 min after last use), 2 per
    key. The API process builds the store with `api_dataset_ttl_seconds`.
  - /ask runs on the server key with the demo budgets and charges the daily
    run and call caps; /sql goes through `sql_editor.run_sql` (daily SQL
    counter); uploads charge the upload cap. Each Cloud Run service has its
    own counters, so the total ceiling is the sum of both (docs say so).
  - No CORS; /docs off unless `AGENTCREW_API_DOCS=true`. New pins: fastapi,
    uvicorn (httpx in dev). Dockerfile: HEALTHCHECK honours `HEALTH_PATH`.
  - Verified: tests/test_api.py (real stack, scripted model, fake clock); the
    image run with the uvicorn command and every endpoint called with curl;
    offline replay of the frozen 50 still D 45/50 with 0 live calls.
  - CI's docker job also runs the image with the uvicorn command and checks
    /v1/health returns 200 (no keys needed).
  - NOT done, by decision: the `agentcrew-api` Cloud Run service and the
    `agentcrew-api-keys` secret. The owner creates them by hand (console
    steps in docs/API.md) only AFTER the PR is merged to `main`, so the
    deployed image includes Phase 4. The deploy job is untouched; the opt-in
    CD step for the API (repository variable DEPLOY_API) was proposed as a
    diff only and is not applied.
- 1687 tests pass, 7 skipped (live PostgreSQL). Decisions D16 to D29 are in
  docs/DECISIONS.md.

### Remaining plan
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
- Live PostgreSQL tests need `AGENTCREW_TEST_POSTGRES_URL` (skipped in CI),
  including the SQL editor's.
- The API's rate limiter, caps and datasets are in memory: the service must
  stay at one instance and one worker.
- app.py still re-exports `_log_run` and `trace_content_enabled` from the
  service because older tests reach them there.
