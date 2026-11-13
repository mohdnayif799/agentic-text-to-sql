# Data sources

AgentCrew answers from one of three sources. Every one is read through the
same three layers: a read-only connection, the sqlglot guard, and the row
cap and timeout (`tools.execute_readonly_sql`).

| Source | Where | Engine |
| --- | --- | --- |
| NorthStar (default) | `data/northstar.db`, built in the image | SQLite, `mode=ro` |
| An upload | the session's folder under `AGENTCREW_UPLOAD_DIR` | SQLite, `mode=ro`, `trusted_schema=OFF`, defensive mode |
| Your database (self-hosted only) | `AGENTCREW_DATABASE_URL` | PostgreSQL, read-only session and transactions; see SELF_HOSTED.md |

NorthStar keeps its exact path through the app: its hand-written data
dictionary, its verified examples and D's metric tools. The frozen
evaluation's offline replay is complete with no live call after these
changes, so its prompts did not change.

## Uploads

| Kind | Files | Limit | Becomes |
| --- | --- | --- | --- |
| CSV | one or more at once | 10 MB in all | one table per file, named after it |
| Excel (.xlsx) | one | 10 MB | one table per non-empty sheet |
| SQLite (.db, .sqlite) | one | 10 MB | its tables, after the checks below |

Also: at most 500,000 rows and 200 columns per table, 30 tables, 3 uploads
per browser session, 50 per day across the demo (`AGENTCREW_DAILY_UPLOAD_CAP`),
and 150 MB of converted data held at once (`AGENTCREW_UPLOAD_STORE_MAX_MB`).

**Conversion.** Names become lower-case identifiers (`Student ID` ->
`student_id`; reserved words get a trailing underscore). Excel dates become
ISO text, the format NorthStar uses. CSV dates that are already ISO stay as
they are; other spellings (`03/04/2025`) stay text, because guessing day or
month first would silently change the data.

**An uploaded SQLite file is untrusted.** Its header is checked before it is
parsed; it is opened read-only with defensive mode and `trusted_schema=OFF`;
`PRAGMA integrity_check` must pass; virtual tables are refused; and the agent
reads a copy SQLite writes itself with its backup API, never the bytes that
were uploaded.

**Lifetime.** One dataset per session. It is deleted when the visitor
removes it, uploads another, leaves it unused for 15 minutes
(`AGENTCREW_DATASET_TTL_SECONDS`), or the session goes idle (the idle guard);
and with everything else when the instance scales to zero. It is never
written to a log, a bucket or a database.

**Memory, measured.** In the production image with `--memory=1g`, one
conversion each, peak resident memory of a fresh process:

| File | Size | Rows | Converted | Peak RSS above baseline | Time |
| --- | --- | --- | --- | --- | --- |
| CSV | 9.99 MB | 154,815 | 11.0 MB | +109 MB | 0.5 s |
| Excel | 10.00 MB | 207,729 | 14.8 MB | +64 MB | 16.4 s |

The running app with one session holds about 230 MB. A pessimistic total for
the 1 GiB instance (three conversions at once, a full 150 MB store) is about
710 MB, so the 10 MB limits stay. Excel is read in openpyxl's streaming mode
and written in chunks, which is why it peaks lower than CSV despite being
larger unzipped; it is slower, so a 10 MB workbook takes about 16 seconds. While a
file converts, the panel shows the step in progress (which sheet, how many
rows so far).

## Descriptions and joins

After an upload the model drafts one short description per table and column
(one call per 12 tables). The visitor can correct them, and untick, fix or
add joins, in "Your data". Both go into the shared context as the dataset's
data dictionary; schema selection also scores tables by their descriptions'
words when a database has more tables than fit in context.

Joins are proposed only when the name and the data agree (a matching key
column, or a column naming another table, with a unique referenced side and
90% of values found). Generic columns (`id`, `name`, `date`, `status`,
`created_at`, ...) never make a join, and a hand-entered join between two
columns of the same generic name is refused.

## What differs from NorthStar

* No verified examples: NorthStar's would only mislead.
* D's metric analyses (`decompose_change`, `contribution_by_dimension`) are
  built from NorthStar's metric definitions, so they are off. A why-question
  goes through D's general exploration loop instead.
* The original agent (B, `AGENTCREW_ARCH=v1`) only knows NorthStar; on any
  other source the app uses D.

## Unseen-schema test

A schema no part of the agent was built or tuned on, asked as a visitor's
upload. Separate from the frozen 50.

**Data.** `scripts/build_college_dataset.py` writes a seeded college dataset
(240 students, 20 courses, 2,400 exam marks, 18,000 attendance records) as
CSV files with no keys declared, in two copies of the same rows:

| Copy | Tables | What makes it hard |
| --- | --- | --- |
| clean, `eval/college/` | students, courses, marks, attendance | nothing: readable names |
| messy, `eval/college_messy/` | stu_mst, crs_mst, exm_mk, att_log, att_sum | cryptic names (`stu_nm`, `mk_obt`, `crd`), codes for values (CSE/ECE/ME/CE, MT/FN, P/A/L) with no lookup table, and a derived summary table (`att_sum` with `att_pct`) beside the log |

**Questions.** 15 in `eval/college_questions.yaml`, the same text for both
copies: 10 single-query questions and 5 that need more than one step (for
example, students under 85% attendance who also failed two or more courses).

**Verified and frozen before any model call.**
`scripts/check_college_gold.py` checks that the two copies hold the same
rows, that each question's gold SQL gives the same answer on both, that
pandas reproduces every answer from the CSV files, and that no answer rests
on a tie; it then writes `eval/college_FREEZE.sha256`. The runner refuses to
start if a frozen file has changed.

**Run.** `scripts/run_unseen_schema.py` uploads each copy through the real
ingest path, lets the model draft the descriptions, and uses them and the
inferred joins exactly as produced, with no hand edits. A and D answer every
question; the frozen strict grader scores the final table.

| Run college2, Gemini 3.5 Flash-Lite, 2026-11-13, agent code dd3c8b6 | A (baseline) | D (full) |
| --- | --- | --- |
| Clean, all 15 | 15/15 | 15/15 |
| Clean, 5 multi-step | 5/5 | 5/5 |
| Messy, all 15 | 14/15 | 14/15 |
| Messy, 5 multi-step | 4/5 | 4/5 |

What the run shows, and what it does not:

- **The one failure is silent and shared.** On the messy copy, "by how many
  points does the average final score of students with overall attendance of
  at least 85% exceed the others'" was answered from `att_sum`, grouped by
  student and course, so it compared per-course attendance instead of each
  student's overall attendance: 2.20 instead of 1.95. The query ran, returned
  one plausible number, and no signal fired. On the clean copy, which has no
  summary table, the same question passed. The cryptic names did not cause
  it; the extra table at a different grain did.
- **D added nothing here.** D's first query is A's first query, so on a
  direct answer the two are the same call. D escalated once per copy (cm02)
  and passed where A already passed. Equal scores are the expected result
  when nothing triggers an escalation, not evidence that D is as good on
  unseen data as on NorthStar.
- **Join inference missed the course key on the messy copy.** `stu_id` was
  recognised and its three joins proposed; `crs_cd` was not, because `_cd`
  is not among the endings treated as a key. The model joined on it anyway.
- **The drafted descriptions were thin.** For the messy copy they read
  "Attendance status code", "Exam type code", "Department code". The codes
  were resolved from the value lists in the context, not the descriptions.
- **Limits of the test.** 15 questions on one synthetic dataset, written by
  the same author as the agent, on small tables with clean values, one run,
  one model. It shows the upload path works end to end and that cryptic
  names and coded values alone did not break single-query or multi-step
  questions. It does not show robustness on real messy data.

Results: `eval/results/college/college2.json`. The first version of this
test (10 questions, clean copy only) is `college-p2.json`: A 10/10, D 10/10.
