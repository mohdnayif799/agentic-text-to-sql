# Operating the public demo

How the deployed demo is kept cheap and observable, and what an operator sets
or checks. Everything here is configuration or console work; the Cloud Run
service, Secret Manager and the CD workflow are described in the README.

## Cost and abuse guard

The demo answers on the server's Gemini key. Three limits bound what that key
can spend, from the inside out.

| Limit | Where | Default | Setting |
| --- | --- | --- | --- |
| Per browser session | `app.py`, `st.session_state` | 5 runs | fixed (`DEMO_RUN_LIMIT`) |
| Per UTC day, every session | `agentcrew/caps.py`, process-wide | 150 runs, 1,000 model calls | `AGENTCREW_DAILY_RUN_CAP`, `AGENTCREW_DAILY_LLM_CALL_CAP` (0 = no cap) |
| Provider quota | Google, on the key | the key's daily quota | AI Studio or the Generative Language API quotas page |

- The session allowance is a courtesy: a page reload starts a new session. The
  daily cap is the ceiling. It counts runs when they complete and model calls
  as they are made, so a run that would cross the day's calls stops at that
  call with a notice and is not counted as a run.
- A visitor's own key is never counted.
- The cap lives in the process. The service runs at most one instance, so one
  process is the whole deployment, and there is no store behind it: a cold
  start (scale-to-zero, a deploy) starts the day's count at zero. The provider
  quota is the hard stop behind this one.
- Both caps also apply to uploads (Phase 2) and the API (Phase 4), which
  charge the same counters.

## Idle guard

Streamlit holds one websocket per open tab and reconnects by itself, so an
idle tab keeps a request in flight and the instance never scales to zero.
`AGENTCREW_IDLE_TIMEOUT_SECONDS` (default 600) ends such a session:

- in the browser, a small script watches input and after the timeout leaves
  for `static/idle.html`, which closes the connection for good;
- on the server, each script run stamps the session's last activity; a run
  that arrives after the timeout finds the conversation cleared and a notice.

To confirm it works in production: Cloud Run, the `agentcrew` service,
Metrics, "Billable instance time". With no visitor the line must drop to zero
within about 15 minutes of the last tab going idle.

## Run log (always on)

Every run writes one JSON line to stdout (`agentcrew/runlog.py`). Cloud Run
stores it as a structured entry in Cloud Logging, so each field is filterable.

```text
{"severity":"INFO","time":"...","event":"run","message":"run status=done path=escalated tools=run_sql>compute:decompose_change llm_calls=5 seconds=9.3",
 "surface":"app","key":"server","provider":"gemini","model":"gemini-3.5-flash-lite","arch":"full","status":"done","path":"escalated",
 "tools":"run_sql>compute:decompose_change","llm_calls":5,"input_tokens":14210,"output_tokens":612,"thoughts_tokens":0,"seconds":9.31,"error":null}
```

Useful queries in Logs Explorer:

```text
jsonPayload.event="run" AND jsonPayload.status!="done"
jsonPayload.event="run" AND jsonPayload.path="escalated"
jsonPayload.event="run" AND jsonPayload.key="server"
```

What a line never holds: prompts, completions, result rows, previews, data
from an uploaded dataset, keys, or an error's message text (the error field is
the exception's type name; the redacted message goes to a separate stderr
line). With `AGENTCREW_TRACE_CONTENT=true` the line also holds the question
and the final SQL. That is for a self-hosted operator who needs to see real
failures; it is off by default and off on the public demo.

## Langfuse (optional extra)

Langfuse is on as soon as both keys are set; nothing else is needed.

| Variable | Meaning |
| --- | --- |
| `AGENTCREW_LANGFUSE_PUBLIC_KEY`, `AGENTCREW_LANGFUSE_SECRET_KEY` | Set both to switch tracing on |
| `AGENTCREW_LANGFUSE_HOST` | Default `https://cloud.langfuse.com` |
| `AGENTCREW_LANGFUSE_ENABLED` | Leave unset. `false` switches tracing off while keeping the keys |
| `AGENTCREW_TRACE_CONTENT` | As for the run log: adds the question and the final SQL |

One trace per run (an `agent` observation) with one `generation` per model
call: role, model, tokens, seconds, status. The same policy as the run log on
what is never recorded. Tracing cannot break or slow a run: every call into
the client is guarded, the client exports on its own background thread, and
nothing on the request path waits on it. The first failure is logged once as
`langfuse: tracing failed and is being skipped`; at startup the log says
`langfuse: enabled ...` followed by the result of a credential check, or why
tracing is off.

On Cloud Run the keys belong in Secret Manager, exposed as the two variables
above, the way the Gemini key is. That is a change to the service and is not
made by the code.

## Billing budget alert (console)

A budget emails; it never stops spend. The stops are the caps above and the
key's quota.

1. console.cloud.google.com, project `text-to-sql-demo`, menu, **Billing**,
   choose the billing account, **Budgets & alerts**, **Create budget**.
2. Scope: name `agentcrew-monthly`; time range **Monthly**; projects
   `text-to-sql-demo`; all services. Under Credits untick **Promotions and
   others**, so spend counts even while credits cover it.
3. Amount: **Specified amount**, your figure.
4. Actions: thresholds **50%**, **90%** and **100% of Actual**, plus **100%
   of Forecasted**. Tick **Email alerts to billing admins and users**. Leave
   Pub/Sub off.
5. Finish.

Optional second stop: **APIs & Services**, **Generative Language API**,
**Quotas**, lower the requests-per-day quota for the project.
