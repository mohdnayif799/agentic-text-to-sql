# REST API

The same agent as the Streamlit app, over HTTP, under `/v1`. It is a second
entry point on one core: a question goes through `agentcrew/service.py` (the
UI's ask flow) and SQL through `agentcrew/sql_editor.py`, so the read-only
connection, the sqlglot guard, the row cap, the timeout, the daily caps and
the run log are the ones the UI has. The package `agentcrew/api/` adds only
what HTTP needs.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/health` | Liveness. No key. |
| `POST /v1/ask` | A question, optionally about an uploaded dataset. |
| `POST /v1/sql` | One read-only SQL statement, on NorthStar or a dataset. |
| `POST /v1/datasets` | Upload a CSV, Excel or SQLite file. |
| `GET /v1/datasets/{id}/schema` | Tables, columns, descriptions, row meaning, joins. |
| `DELETE /v1/datasets/{id}` | Delete a dataset now. |

## Run it

```bash
export AGENTCREW_API_KEYS="$(openssl rand -hex 32)"      # one or more, comma separated
export AGENTCREW_PROVIDER=gemini AGENTCREW_GEMINI_API_KEY=...   # the model key, as for the UI
uvicorn agentcrew.api.app:create_app --factory --host 0.0.0.0 --port 8080 --no-access-log
```

Or the image, with its command overridden (the default command is the UI):

```bash
docker run --rm -p 8080:8080 -e HEALTH_PATH=/v1/health \
  -e AGENTCREW_API_KEYS -e AGENTCREW_GEMINI_API_KEY \
  agentcrew uvicorn agentcrew.api.app:create_app --factory \
  --host 0.0.0.0 --port 8080 --no-access-log
```

One process, one worker, one instance. The daily caps, the rate limiter and
the dataset store live in memory, so a second worker or instance would be a
second set of limits and would not see the first one's datasets.

## Authentication

Every endpoint except `/v1/health` needs a key in the `X-API-Key` header.
Keys are read from `AGENTCREW_API_KEYS` (commas or whitespace between them;
on Cloud Run, a Secret Manager secret). Keys shorter than 24 characters are
ignored. With no usable key, every request is refused.

- The comparison is constant-time: both sides are hashed with SHA-256 and
  compared with `hmac.compare_digest`, against every configured key.
- A key is never logged. Log lines name a caller by `client`, the first 12
  hex characters of its key's SHA-256.
- A key in the query string is not accepted, so it cannot end up in a URL
  log. A request with two `X-API-Key` headers is refused.
- A request without a valid key is answered before its body is read.

## Errors

Every failure, from every endpoint, is this envelope and nothing else:

```json
{"error": {"code": "rate_limited", "message": "Too many requests for this API key. Try again shortly.", "request_id": "5d0c1b0f6c0e4f0a"}}
```

`code` is stable; `message` is one plain sentence. No stack trace, exception
name or provider error text is ever returned. `request_id` is also in the
`X-Request-ID` response header and in the request's log line.

| Status | Code | When |
| --- | --- | --- |
| 400 | `sql_error` | The statement failed (unknown column, syntax, timeout). |
| 400 | `upload_refused` | The file failed one of the upload checks. |
| 401 | `unauthorized` | Missing or wrong key. |
| 403 | `sql_refused` | The guard refused the statement (anything but one read-only query). |
| 403 | `uploads_disabled` | `AGENTCREW_UPLOADS_ENABLED=false`. |
| 404 | `not_found`, `dataset_not_found` | No such endpoint, or no such dataset for this key. |
| 405 | `method_not_allowed` | |
| 409 | `dataset_limit` | The key already holds its 2 datasets. |
| 410 | `dataset_expired` | The key's own dataset expired. Upload it again. |
| 413 | `body_too_large` | The body is over the endpoint's limit. |
| 422 | `invalid_request` | A field is missing, too long, unknown or of the wrong type. |
| 429 | `rate_limited` | Over the key's rate limit. `Retry-After` gives the seconds to wait. |
| 429 | `daily_cap` | The service's daily cap is reached. |
| 500 | `agent_error`, `internal_error` | The run or the server failed. |
| 503 | `not_configured`, `unavailable`, `model_quota`, `store_full` | No model key, a dependency is down, the provider's quota is used up, or the dataset store is full. |
| 504 | `model_timeout` | The model was too slow. |

## Limits

| Limit | Default | Setting |
| --- | --- | --- |
| Requests per key | 30 a minute, sliding | `AGENTCREW_API_RATE_LIMIT_PER_MINUTE` (0 = none) |
| JSON body | 64 KB | `AGENTCREW_API_MAX_JSON_KB` |
| Upload body | 10 MB | `AGENTCREW_MAX_UPLOAD_MB`, `AGENTCREW_MAX_EXCEL_UPLOAD_MB` (the UI's) |
| Question | 2,000 characters | fixed |
| SQL statement | 20,000 characters | fixed |
| Result rows | 500 | `AGENTCREW_MAX_RESULT_ROWS` (the agent's row cap) |
| Statement time | 15 s | `AGENTCREW_SQL_TIMEOUT_SECONDS` |
| Datasets per key | 2 | `AGENTCREW_API_DATASETS_PER_KEY` |
| Dataset lifetime | 30 minutes after last use | `AGENTCREW_API_DATASET_TTL_SECONDS` |
| All datasets together | 150 MB | `AGENTCREW_UPLOAD_STORE_MAX_MB` (the UI's) |

The body limit is enforced while the body is read. A declared
`Content-Length` over the limit is refused before reading; a body with no
declared length is cut off at the chunk that crosses the line.

### Daily caps

Every request charges the same counters as the UI (`agentcrew/caps.py`):

| Request | Charged to |
| --- | --- |
| `POST /v1/ask` | the daily run cap (150) when the run completes, and the daily model-call cap (1,000) per call |
| `POST /v1/sql` | the daily SQL-run counter (1,000) |
| `POST /v1/datasets` | the daily upload cap (50), and the model-call cap for the calls that draft descriptions |

Questions run on the server's model key with the demo's fixed budgets
(3 steps, 3 attempts per step). The caller cannot choose the model or the
budgets.

**Each service has its own counters.** The caps live in the process, and the
API is a separate Cloud Run service from the UI, so each counts its own day.
The ceiling on total spend is the sum of both: with the defaults, up to
2 × 150 runs and 2 × 1,000 model calls a day across the UI and the API. Set
each service's `AGENTCREW_DAILY_RUN_CAP` and `AGENTCREW_DAILY_LLM_CALL_CAP`
with that in mind. The model provider's own daily quota on the key stays the
hard stop behind both.

## Endpoints

The examples assume:

```bash
export API=http://localhost:8080/v1
export KEY=...          # one of AGENTCREW_API_KEYS
```

### GET /v1/health

```bash
curl -s $API/health
```

```json
{"status":"ok"}
```

### POST /v1/ask

```bash
curl -s $API/ask -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"question": "How many orders were placed in each channel in 2025?"}'
```

```json
{
  "columns": ["channel", "order_count"],
  "rows": [["Direct", 2108], ["Online", 3048], ["Partner", 1660]],
  "row_count": 3,
  "rows_capped": false,
  "status": "done",
  "answer": "In 2025, there were 2108 orders placed in the Direct channel, 3048 orders in the Online channel, and 1660 orders in the Partner channel.",
  "sql": "SELECT channel, COUNT(*) AS order_count FROM orders WHERE order_date >= '2025-01-01' AND order_date <= '2025-12-31' GROUP BY channel LIMIT 500",
  "path": "direct",
  "llm_calls": 3,
  "caveats": [],
  "dataset_id": null,
  "seconds": 4.426
}
```

- `dataset_id` (optional) answers from one of your datasets.
- `status` is `done`, `failed` (the agent ran and could not answer; `answer`
  says so), `refused` (the question asked to change data; no model call is
  made) or `needs_clarification`. All four are HTTP 200: the run happened
  and this is its outcome. A run that could not happen is an error envelope.
- `path` is `direct` or `escalated`; `llm_calls` is the model calls it made.
- `rows_capped` is true when the table stopped at the row cap.

### POST /v1/sql

```bash
curl -s $API/sql -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"sql": "SELECT segment, COUNT(*) AS n FROM customers GROUP BY segment"}'
```

```json
{"columns":["segment","n"],"rows":[["Enterprise",315],["Mid-Market",534],["SMB",551]],"row_count":3,"rows_capped":false,"sql":"SELECT segment, COUNT(*) AS n FROM customers GROUP BY segment LIMIT 500","dataset_id":null,"seconds":0.001}
```

`sql` in the response is the statement as it ran, with the row limit the
guard added. Add `"dataset_id"` to run it on a dataset. No model is called.
Anything but a single read-only query is refused:

```bash
curl -s $API/sql -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"sql": "DELETE FROM customers"}'
```

```json
{"error":{"code":"sql_refused","message":"Refused: DELETE is not permitted: this agent is read-only. Rewrite the request as a SELECT.","request_id":"..."}}
```

### POST /v1/datasets

The file is the raw request body. Its name, which decides whether it is read
as CSV, Excel or SQLite, is the `filename` query parameter.

```bash
curl -s -X POST "$API/datasets?filename=students.csv" -H "X-API-Key: $KEY" \
  -H "Content-Type: application/octet-stream" --data-binary @students.csv
```

```json
{"dataset_id":"e8bf20d31a0e4840a0d41c4b7adb4fe2","label":"students.csv","kind":"csv","tables":["students"],"size_bytes":8192,"descriptions":"drafted","expires_after_seconds":1800}
```

- The file goes through the UI's converter and checks
  (`agentcrew/datasets/ingest.py`): the kind and size before parsing, the
  row, column and table limits, the SQLite checks, the store's total limit.
- One file per upload. For several related tables, upload an Excel workbook
  (one table per sheet) or a SQLite file; joins are inferred between them.
- `descriptions` is `drafted` when the model described the tables and
  columns, `empty` when it could not. Questions work either way.
- What the model sees of an upload is the same as in the UI
  (docs/DATA_SOURCES.md). Do not upload sensitive or personal data to the
  public demo.

### GET /v1/datasets/{id}/schema

```bash
curl -s $API/datasets/$DATASET/schema -H "X-API-Key: $KEY"
```

```json
{
  "dataset_id": "a88b3b9c454a4befafb40d0d8b795f1d",
  "label": "college.xlsx",
  "kind": "excel",
  "tables": [
    {"name": "marks", "description": "Holds student course marks and scores.",
     "row_meaning": "One row per mark.", "row_count": 4,
     "columns": [
       {"name": "mark_id", "type": "INTEGER", "description": "Unique identifier for the mark."},
       {"name": "student_id", "type": "INTEGER", "description": "Identifier that refers to students."},
       {"name": "course", "type": "TEXT", "description": "Course name."},
       {"name": "score", "type": "INTEGER", "description": "Numerical score achieved."}]},
    {"name": "students", "description": "Holds student personal details and department.",
     "row_meaning": "One row per student.", "row_count": 3,
     "columns": [
       {"name": "student_id", "type": "INTEGER", "description": "Unique identifier for the student."},
       {"name": "name", "type": "TEXT", "description": "Name of the student."},
       {"name": "dept", "type": "TEXT", "description": "Department name."}]}
  ],
  "joins": [{"from_table": "marks", "from_column": "student_id", "to_table": "students",
             "to_column": "student_id", "reason": "same key column"}],
  "expires_after_seconds": 1800
}
```

### DELETE /v1/datasets/{id}

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE $API/datasets/$DATASET -H "X-API-Key: $KEY"
```

`204`, no body. The file is deleted at once.

## Datasets

- A dataset belongs to the key that uploaded it. To any other key it does
  not exist: every endpoint answers `404 dataset_not_found`, the same as for
  an id that was never issued.
- It is deleted 30 minutes after its last use (a question, a query or a
  schema read each count as a use), or when you delete it. After that its
  owner gets `410 dataset_expired`; everyone else still gets 404.
- A key holds at most 2 at a time; a third upload is `409 dataset_limit`.
- Datasets are held in the instance's memory and vanish when it restarts or
  scales to zero. Nothing is written to a bucket, a database or a log.

## Logs

Two kinds of JSON line on stdout (`agentcrew/runlog.py`), both with
`"surface":"api"` and the caller's `client` hash:

- `"event":"request"`: one per HTTP request, whatever became of it: method,
  route template (never the raw path or the query string), status, seconds,
  the error `code` and the `request_id`.
- `"event":"run"`: one per agent run, SQL run or description draft, the same
  record the UI writes (docs/OPERATIONS.md).

```text
jsonPayload.surface="api" AND jsonPayload.event="request" AND jsonPayload.status>=400
jsonPayload.surface="api" AND jsonPayload.client="bd7e4552b8af"
```

The question and SQL text appear only with `AGENTCREW_TRACE_CONTENT=true`.
Result rows, uploaded data and keys never do.

## Not included, on purpose

- **CORS.** No `Access-Control` header is sent, so a web page on another
  origin cannot read a response. The API is for servers and scripts; a key
  in browser JavaScript would be public anyway.
- **Interactive docs.** `/docs` and `/openapi.json` are unauthenticated, so
  they are off unless `AGENTCREW_API_DOCS=true`.
- **Choosing the model or the budgets per request.** The server's key pays,
  so the server decides.

## Deploying as its own Cloud Run service

The API is the same image as the UI with the container command overridden to
uvicorn, as a second service, `agentcrew-api`. The CD workflow does not
create or deploy it; these are the console steps (project
`text-to-sql-demo`, region `asia-south1`).

**1. The keys, in Secret Manager.** Generate one key per caller:

```bash
openssl rand -hex 32
```

Security > Secret Manager > Create secret. Name `agentcrew-api-keys`. Secret
value: the keys, separated by commas, no spaces or quotes. Replication:
automatic. Create. Then open the secret > Permissions > Grant access: the
runtime service account of the `agentcrew` service (Cloud Run > agentcrew >
Security tab shows it), role Secret Manager Secret Accessor.

**2. The service.** Cloud Run > Deploy container > Service.

- Container image URL: Select > Artifact Registry >
  `cloud-run-source-deploy/agentcrew` > the newest image (the one the
  `agentcrew` service runs).
- Service name `agentcrew-api`, region `asia-south1`.
- Authentication: Allow unauthenticated invocations (the API authenticates
  callers itself with `X-API-Key`).
- Billing: Request-based. Scaling: minimum instances 0, maximum instances 1.
- Container(s) > Edit container:
  - Container port `8080`.
  - Container command: `uvicorn`
  - Container arguments, one per line: `agentcrew.api.app:create_app`,
    `--factory`, `--host`, `0.0.0.0`, `--port`, `8080`, `--no-access-log`
  - Memory 1 GiB, CPU 1 (as the UI service).
  - Variables & Secrets > Environment variables: none are required
    (`AGENTCREW_PROVIDER` and `AGENTCREW_MODEL` are set in the image). To
    split the daily ceiling between the two services, set
    `AGENTCREW_DAILY_RUN_CAP` and `AGENTCREW_DAILY_LLM_CALL_CAP` here.
  - Variables & Secrets > Secrets exposed as environment variables:
    `AGENTCREW_API_KEYS` = secret `agentcrew-api-keys`, version `latest`;
    `AGENTCREW_GEMINI_API_KEY` = the same secret and version the `agentcrew`
    service uses (its Variables & Secrets tab shows the name).
  - Health checks > Startup probe: HTTP, path `/v1/health`, port 8080.
- Requests: timeout 300 seconds, maximum concurrent requests per instance 8.
- Security: the same service account as the `agentcrew` service.
- Create.

**3. Check it.**

```bash
export API=https://agentcrew-api-<hash>.asia-south1.run.app/v1
curl -s $API/health
curl -s $API/sql -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"sql": "SELECT COUNT(*) FROM customers"}'
```

To rotate a key: add a new version of the secret, then deploy a new revision
of `agentcrew-api` (a running instance read the keys at start-up). Datasets
of a removed key expire on their own.

Maximum instances must stay 1: the caps, the rate limiter and the datasets
are in the instance's memory.
