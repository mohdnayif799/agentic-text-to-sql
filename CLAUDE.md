# AgentCrew

Text-to-SQL router agent (Streamlit, SQLite, Gemini). See README.md and docs/.

## Current work: v3.1

Version 3 (branch `v3-production`, Phases 1 to 6) was merged to `main` as PR
13 (merge commit `7a7600d`) on 2026-11-14 and deployed (revision
`agentcrew-00016-kw5`). Version 3.1 is on branch `v3.1`, cut from that
merge: chat history, a data source per chat, and the opt-in CD step for the
API. CI runs on every push to a PR; only a merge to `main` deploys.

### Ground rules
- Never push to `main`: CD auto-deploys it to the live demo. Work on a branch (now `v3.1`).
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
- Phase 5, romanized Hindi and Telugu (eval/results/romanized_report.md):
  `eval/romanized_hi.yaml` and `eval/romanized_te.yaml`, the same 10 NorthStar
  questions typed in English letters, none from the frozen 50. Gold verified
  by a second SQL and frozen first (`eval/FREEZE_v4.sha256`,
  `scripts/build_romanized_gold.py --check`). Registered in `evalrun/sets.py`
  as `ROMANIZED_SET_FILES`; kept out of the example pool's `POOL_SETS`.
  Live result: Hindi A 10/10, D 10/10; Telugu A 10/10, D 10/10. Nothing
  failed, so NO prompt changed and there is no "after" run. Scope: all
  single-query questions, D never escalated; the Telugu wording has not been
  checked by a native speaker.
- Phase 6:
  - No live rerun of the frozen 50 (no prompt changed); the offline replay is
    still D 12 + 19 + 14 = 45/50 with 0 live calls.
  - CI: the test job has a `postgres:17` service container and sets
    `AGENTCREW_TEST_POSTGRES_URL`, so the 6 live PostgreSQL tests run there.
  - pr-review-toolkit over the whole branch diff. Fixed from it: the API gate
    now requires a key for every path but health and decides on the
    root_path-stripped path; a per-key limit on requests in progress
    (`AGENTCREW_API_MAX_CONCURRENT_PER_KEY`, 4) and an async health endpoint;
    `DailyCap.reserve_sql_run`; upload refusals before the body is read;
    PostgreSQL results are streamed so the row cap bounds memory; the
    PostgreSQL function denylist matches whole families by prefix; SELECT INTO
    and row locks refused by the guard; absolute caps on what an Excel file
    may unzip to; the store reserves room for a conversion in progress; a
    deeply nested model reply is an invalid reply, not a crash; the
    self-hosted compose file binds 127.0.0.1. From the type, error-handling,
    test and docs reviews: `EditorRun.status` and `Turn.code` are closed sets
    (`EditorStatus`, `TurnCode`) and a test keeps every value mapped to an API
    error; `ToolContext` is frozen; keys and key digests are out of every
    repr; cap settings refuse negative values; `DatasetStore.update` takes only
    notes and joins; the UI upload releases its daily slot on any failure and
    a failed description draft is a notice; `deployment_cap` keeps a broken
    configuration out of the real cap and logs it; docs corrected against the
    code. New tests call the gate as raw ASGI and count how often it asks for
    the body (0 for a refused request, 9 chunks for a 64 KB limit).
  - A refused upload no longer deletes the session's previous dataset: the
    new file is converted first and the old dataset is removed only once the
    new one is stored (`data_panel.handle_upload`).
  - Rechecked in a real browser after the fixes: CSV and Excel uploads, a
    question on each, the SQL editor (edit, run, DELETE refused), and refused
    uploads that left the previous dataset in place.
  - README rewritten for version 3 with every score scoped.
- 1803 tests pass offline; 6 more need a live PostgreSQL server (run in
  CI) and 2 need a local evaluation cache. Decisions D16 to D29 are in
  docs/DECISIONS.md.

- Version 3.1 (branch `v3.1`, decision D30):
  - Chats (`agentcrew/chats.py`): "New chat" keeps the old chat in a list in
    the sidebar (newest first, the open one primary, "Untitled chat" before
    its first question); a click switches back with its turns, answers,
    tables and SQL editors (the editor's last SQL and result live in the
    chat). Up to 10 chats per session; an 11th removes the oldest with a
    notice. "Delete this chat" deletes it and its dataset. Chats live in the
    session only (reload or idle guard ends them; `app.end_session` resets
    them). The list is drawn once the run's question is known, before the
    agent runs, so it stays visible during a run.
  - Per chat: turns, data source choice, dataset id, uploader file id, SQL
    editor results (`chats.CHAT_KEYS`). Per session: everything else, so
    the 5 demo runs, the SQL run cap, the 3 uploads and the owner id never
    reset with a new chat. `chats.ChatState` (`chats.view`) routes each key;
    data_panel works on that view. Widgets in a chat are keyed by the chat
    id (`c1`, `c2`, ..., never reused in a session).
  - Data per chat: a new chat starts on the default source, never on
    another chat's upload; switching back restores the chat's dataset; an
    upload replaces only its own chat's dataset; a full store refuses with
    a message (plus a hint when the session's other chats hold data), never
    evicting. Each dataset still expires 15 minutes after its last use.
  - tests/test_chats.py: the chat list, the routing, and the app through
    AppTest (histories kept, dataset restored, new chat on NorthStar,
    editors on their own chat's source or read-only once the data is gone,
    delete removes the dataset, the 10-chat limit, a full store, and the
    demo, SQL and upload limits driven across new chats).
  - CI: the opt-in Phase 4 CD step is applied as proposed: when the
    repository variable `DEPLOY_API` is `true`, the deploy job runs
    `gcloud run services update agentcrew-api --image=<same digest>` and
    checks its `/v1/health`. It never creates the service.

### Remaining plan
- After v3.1 is merged and deployed: create the `agentcrew-api-keys` secret
  and the `agentcrew-api` service (docs/API.md), give `github-deployer` the
  same role on it as on `agentcrew`, set `DEPLOY_API=true`, and rerun the
  main workflow so CD deploys both services.

### Open items
- `st.components.v1.html` (idle guard) is deprecated; move to `st.iframe` before a Streamlit upgrade.
- MySQL is deferred; self-hosted mode is PostgreSQL only (dialect parameter kept).
- The daily cap resets on a cold start (no store behind it).
- On uploaded data D adds nothing over A so far; D's metric tools are NorthStar-only.
- Live PostgreSQL tests need `AGENTCREW_TEST_POSTGRES_URL`: skipped locally
  without it, run in CI. Locally: `docker run -d -e POSTGRES_PASSWORD=test
  -p 55432:5432 postgres:17`.
- Review findings left open (see the Phase 6 summary): the daily run cap can
  overshoot by the runs in flight; a DELETE racing a request can leave a
  small cache entry behind; process-wide locks are held while a new source
  is profiled; the model name is an alias, not a dated version.
- The romanized test covers single-query questions only, 10 per language.
- pytest warns (PytestRemovedIn10Warning) that the class-scoped `seeded`
  fixture in `tests/test_postgres.py::TestLiveServer` is an instance method;
  make it a `@classmethod` (or module-scoped) before upgrading to pytest 10.
- The API's rate limiter, caps and datasets are in memory: the service must
  stay at one instance and one worker.
- app.py still re-exports `_log_run` and `trace_content_enabled` from the
  service because older tests reach them there.
- A dataset in a chat left unopened for 15 minutes expires (the store's
  per-dataset TTL); its SQL editors then show the SQL read-only.
