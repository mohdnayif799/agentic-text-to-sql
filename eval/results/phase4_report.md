# Phase 4 report (final)

Final code: `b07f4f1` on branch `agent-v2`. The final results below are an
offline replay at that commit: every call is served from the responses that
the live development runs cached, and every question/architecture pair hit
the cache. No holdout question has been run.

## Final results (development sets only)

| set | A baseline | B v1 | C router | D full |
| --- | --- | --- | --- | --- |
| showcase (5) | 4/5 | 2/5 | 4/5 | **5/5** |
| dev2 (6) | 4/6 | not run | 4/6 | **6/6** |

- **Escalation against A:**
  - D escalated 3 questions (sc03, dv01, dv03) and **helped on all 3**,
    harming none.
  - C escalated the same 3 (identical escalation rule) and changed none of
    them.
- **Missed escalations:** none. A failed sc03, dv01 and dv03, and C and D
  escalated all three.
- **Paired against A:** D passes 3 questions A fails and fails none that A
  passes. That gives exact McNemar p = 1.0 on the showcase (1-0) and 0.5 on
  dev2 (2-0): development sets, far too small for significance and tuned
  on, so they say nothing about the holdouts.
- **Repairs:** 2 on dv01 for C and D (neither accepted), so no fixes and no
  breaks.
- **Errors:** none in the final runs. One provider 503 during development
  (D, dv03) led to ERROR recording, retries and amendment 3.
- **Latency, as if live, average per question:**

  | set | A | B | C | D |
  | --- | --- | --- | --- | --- |
  | showcase | 3.1 s | 8.7 s | 4.9 s | 5.1 s |
  | dev2 | 3.0 s | not run | 6.2 s | 7.3 s |

- **Live calls spent in Phase 4:** 57 over nine runs, including 9 judge
  calls.

Per-question tables for both final runs are in section F at the end.

## What D is at the end of Phase 4

D is C until escalation (same step 0, signals, repairs and follow-up
decision; one shared escalation rule, `Router.escalated`). When it
escalates:

- **Why-questions** (`is_why_question`) open with a validated MetricPlan.
  `decompose_change` splits the metric into components, each labelled
  higher, lower or about the same (within 5%). The final table is the
  stacked metric analyses, the answer gets reading guidance, and a
  deterministic driver sentence is added if the answer omits the main
  driver.
- **Forecast and trend-fit questions** (the forecast signal) open with a
  validated TrendPlan. The model writes the series query; the deterministic
  `linear_trend_forecast` (named periods) or `zscore_anomalies` (against the
  trend) does the fit. x is evenly spaced period numbers, stated in the
  output. The fit's table is the final result.
- **Everything else** goes through the `decide_next_action` loop:
  lookups, run_sql, compute functions, metric tools, and verification on
  every query.
- **Never worse than A when escalation fails:** A's retry comes first on an
  SQL error at step 1. An invalid reply ends only that part of the work.
  With nothing usable, the final table is step 1's. A final table that is
  A's own result is answered with A's answer call, and a failed escalated
  answer falls back to A's.
- **Budgets:** the demo's limits, 3 steps (step 0 counts) and 3 attempts per
  step.

**Added after a development failure** (recorded so Phase 5 can report it):

| addition | after |
| --- | --- |
| `decompose_change`, `contribution_by_dimension` | showcase sc03 (why-question gap, before Phase 4 was built) |
| why-questions open with MetricPlan; stacked tables; every component labelled; answer guidance | showcase sc03 (D computed AOV by hand, assembly dropped it) |
| plain-language component names; driver sentence | showcase sc03 (answer named the driver only as `order_count`) |
| forecast signal (C and D) | dev2 dv03 (A's one-query fit wrong, not escalated) |
| TrendPlan; period-number x; labelled forecasts | dev2 dv03 (planner trusted A's fit) |
| ERROR records, 503 retries, amendment 3 | dev2 dv03 (provider 503 scored as a failure) |
| deterministic metric SQL | found while building the parity test (set order changed per process) |
| fallback safety | review of failure paths (4 gaps proven by tests on the old code) |

## Known limitations going into Phase 5

- **Small samples.** Development n = 11. Every improvement above was
  measured on the questions that motivated it.
- **Few tools exercised live.** Only run_sql, search_values,
  decompose_change and (in the last run) linear_trend_forecast have run with
  model-chosen arguments. get_distinct_values, sample_rows,
  contribution_by_dimension, the other compute functions and the
  conditional-branch path are covered offline only (`tests/test_dispatch.py`).
  The forced smoke test showed that Flash-Lite's planner answers straight
  away when step 1 looks right.
- **C has no compute tools.** On a forecast question it escalates but keeps
  A's result.
- **Answer-text check for conditional branches.** On dv05, A's query takes
  the right branch but its answer names the other one, and still passes
  (the check only needs the word "Partner"). The holdout's ha19-ha21 may
  share this; Phase 5 analyses it separately (see the Phase 5 plan).
- **The date-format check runs only after escalation.** It stayed there
  because dv06 passed for every architecture.
- **Authorship.** The questions, gold and agent share an author (as
  recorded in the protocol).

## Development log

The sections below are the record of each round, in order. Section numbers
are kept as written at the time.

### 1. Results of `phase4-showcase` (before the fix)

A 4/5, B 2/5, C 4/5, D 4/5. Every architecture fails sc03 (why-question) on
the same judge fact: none states that average order value is about the same
in every channel. Full tables (generated by `scripts/report_v2.py`) are in
section 12.

- **Escalation against A:** C and D each escalated one question (sc03):
  helped 0, harmed 0, no change 1.
- **Missed escalations:** A failed one question (sc03); both C and D
  escalated it, so no escalation was missed.
- **Latency:** this run replayed almost every call from the cache, so the
  measured latency is about 0 s. The "as if live" column counts each replayed
  call with the latency of the live call that filled the cache (backfilled
  from the traces of the runs that made those calls). It is the comparable
  figure: A 3.1 s, B 8.6 s, C 4.9 s, D 5.0 s per question on average.

### 2. Diagnosis of D on sc03

Question: why does Online bring in so much more completed revenue than Direct
and Partner over 2024 and 2025? Trace: `eval/runs/phase4-showcase/traces/full/sc03.json`.

| step | what happened | source |
| --- | --- | --- |
| 1 (step 0) | A's SQL: completed revenue and completed order count by channel (Online 10.20M / 3,458 orders, Direct 7.11M / 2,366, Partner 5.83M / 1,922). Verification found no problem. | A's cache |
| decision | needs_followup = true: "the question asks why ... requiring further analysis such as average order value or product mix." | C's cache |
| 2 | decide_next_action chose **run_sql**, reason "check the average order value and order items per channel". Result: orders, revenue, AOV and units per order by channel: AOV 2,948 (Online), 3,003 (Direct), 3,034 (Partner). **The decomposition data was right**, computed by hand, not by the tool. | live |
| 3 | decide_next_action chose **run_sql** for revenue by channel and category (product mix). | live |
| final | The assembly call chose `base_step: 1, columns: []`: the final table was step 1's, and the AOV column from step 2 was dropped. | live |
| answer | Written from step 1's table only: names order volume with the right figures, says nothing about order value (it never saw it). | live |
| judge | Fact 1 (more orders) YES with a verified quote; fact 2 (AOV about the same) NO; no disqualifying claim. Fail. | live |

**Root causes:**

- **D never called `decompose_change` or `contribution_by_dimension`.** The
  planner prompt suggested splitting the metric, but Flash-Lite wrote the
  split as plain SQL.
- **Assembly picked the step with the least evidence.** It dropped the
  decisive AOV comparison, and the answer step only sees the assembled
  table.
- **Nothing asked the answer to state components that did not move.** The
  answer named the one that did (orders).

C (Phase 3 and 4) failed the same way. Its follow-up computed only product
mix, and its assembly also kept step 1.

### 3. Fix (general rules, no question-set facts)

- **Why-questions start with a metric analysis.** When D escalates on a
  question that asks for a cause (`is_why_question`, an English-phrasing
  rule: why, how come, what explains / drove / caused / led to, reasons
  for), the first exploration step is one validated `MetricPlan`: a metric
  from the allowlist and a comparison. `decompose_change` runs it, or
  `contribution_by_dimension` for a metric without components. If no valid
  plan comes back, the ordinary loop continues. The remaining step stays
  the planner's choice.
- **Comparisons of several groups.** `groups: [explained, reference, ...]`
  compares the first group with each of the others (sc03-like questions name
  three channels). Two periods and two groups work as before.
- **Every component is labelled.** Both metric tools return one shared
  schema (`side_a, side_b, analysis, item, value_a, value_b, change,
  pct_change, share_of_change_pct, comparison, largest_contributor`).
  `comparison` is "higher", "lower" or "about the same" (within 5%,
  `SIMILAR_PCT`), so components that stay flat are stated by the tool,
  not left to the answer model.
- **The final table of a why-question is its metric analyses, stacked.**
  There is no assembly call that could drop one. Other questions are
  assembled as in C.
- **Answer guidance for that table.** The answer prompt gets a short "how to
  read this result" block. It asks the answer to name the largest
  contributor and to state every component's comparison with figures,
  including components that are about the same. It is appended only for
  stacked metric tables, so A's and C's answer prompts are byte-identical
  (tested), and their cached responses still replay (verified offline: A and
  C reproduce the showcase from cache with 0 misses).

No-LLM preview of the tool on a three-channel comparison over 2024-2025:
orders account for about 105% of the gap to each other channel, and AOV,
average unit price and discount are all "about the same". This is the
evidence the judge's fact 2 needs, now in the table the answer is written
from.

### 4. Safety of the metric layer (confirmed, tests added)

- **Names are allowlisted, not just quoted.**
  - A metric name must be a key of `METRICS`, which a test pins to the
    data dictionary's metric names. The one extra name is `tickets`, a plain
    row count.
  - `MetricPlan` rejects unknown metrics at JSON validation, before any SQL
    exists.
  - Dimension names, filter keys and `group_dimension` must be keys of the
    source's dimension map. A new test checks that every dimension expression
    uses only data-dictionary columns.
  - Model-supplied values (groups, filters) are quoted as SQL literals by
    sqlglot. Periods are validated dates.
  - A new test confirms that hostile names are refused before any SQL runs:
    `revenue; DROP TABLE orders`, `region) OR (1=1`, `o.status`, `channel; --`
    and `1=1 OR channel` as a filter key.
- **Every generated statement passes the same read-only guard as model SQL.**
  - D runs metric SQL only through `execute_readonly_sql`, which calls
    `guard_sql` (sqlglot read-only check plus a row limit).
  - New test: all 177 statements the layer can build (every metric x
    dimension x mode, with a hostile filter value) pass `guard_sql` and
    execute.
  - New test: during a D run, a spy on `guard_sql` sees every metric query
    of the final table.

### 5. dev2 (development set, not frozen; tuning allowed)

`eval/dev2.yaml`, gold `eval/gold/dev2.json`, built by
`scripts/build_dev_gold.py`. Gold is SQL run through the frozen builder's
`build_entry` (unchanged). The two statistics answers are re-computed with
pandas and must agree; dv04 must also give the same month under SD ddof 0, 1
and 2. Customers are graded by ID.

| id | type | D path it targets | gold |
| --- | --- | --- | --- |
| dv01 | term_matching | search_values: misspelled customer name ("Idirs Rosis"; customer names are not in the context's value lists) | 1133 Idris Rossi, 33,475.85 |
| dv02 | term_matching | unknown label ("Professional Services" is category 'Services'); get_distinct_values or a repair | 1,269 orders |
| dv03 | statistics | linear_trend_forecast, three labelled months | 46.04, 46.16, 46.28 signups |
| dv04 | statistics | zscore_anomalies against the mean | 2024-08, 9.15 days |
| dv05 | segment_comparison | conditional branch on growth (Partner 28.5% vs Online 25.4%: Partner branch) | Partner 2025 revenue by segment |
| dv06 | recovery | date-format check (`churn_date` against 'YYYY-MM' literals drops February) | 20 customers |

**Screening:**

- The same rules as for the showcase and the example pool, run by
  `build_dev_gold.py --check` and a test.
- TF-IDF cosine < 0.5 to every showcase and holdout question (the highest is
  0.42, dv01 against sc01).
- No shared gold table or distinctive value. The first dv01 draft used
  customer 193, which equals he01's gold count, so it was replaced.
- Manual notes per question under `screening`: ha06, ha12, ha13 and
  ha19-ha21 cover the same failure classes with different entities, series,
  periods and templates.
- The example pool now also screens against dev sets. Its 20 examples are
  unchanged; one excluded item's similarity moved from 0.78 to 0.79.

**Known gaps dev2 is expected to show** (not fixed, because they should be
measured first):

- **dv06:** a silently wrong date filter is not one of C's step-0 signals,
  and D is C until escalation. If the follow-up decision says "no", D's
  date-format check never runs (a missed escalation).
- **dv03:** `linear_trend_forecast` returns steps ahead without month labels,
  so a compute table cannot match a gold that has a month column.

### 6. Runs on d3275c5: `phase4b-showcase` and `phase4-dev2`

Live calls spent: 4 on the showcase (D on sc03; everything else replayed
from the cache) and 22 on dev2. No judge call was live.

**Showcase:** A 4/5, C 4/5, D 4/5. sc03 still fails for all three, but D now
fails earlier:

- **D on sc03 failed the driver check ("driver not named")**, so the judge
  never ran.
- **Escalation against A:** C and D escalated sc03, no change.
- **Missed escalations:** none.

D's sc03 path ran as designed:

- step 0 (A's query) and the follow-up decision came from the cache;
- the MetricPlan call needed one schema repair (its first reply mixed period
  and group fields);
- `decompose_change` compared Online with Direct and with Partner;
- the stacked table went to the answer step.

D's full answer:

> Comparing the Online channel to Direct over 2024 and 2025, Online's higher
> revenue of 10195563.464 compared to Direct's 7106104.9285 is driven
> primarily by order_count, which is the largest contributor with 3458.0
> orders for Online versus 2366.0 for Direct. Similarly, units_sold is higher
> at 25661.0 for Online compared to 17902.0 for Direct. Meanwhile,
> average_order_value (2948.398919606709 vs 3003.4255826289095),
> average_unit_price (418.23862865827516 vs 418.230787062898), and
> one_minus_average_discount (0.9499779254382523 vs 0.9491045687230547) are
> about the same and do not explain the difference. Comparing Online to the
> Partner channel (revenue of 5831252.8015), order_count is again the largest
> contributor at 3458.0 orders for Online compared to 1922.0 for Partner, and
> units_sold is higher at 25661.0 compared to 14593.0. Partner's
> average_order_value (3033.950469042664), average_unit_price
> (420.33407044473375), and one_minus_average_discount (0.9506544682884801)
> are also about the same and do not explain the difference.

Driver patterns (any one must match): `more\s+(completed\s+)?orders`,
`order\s+(volume|count|counts|numbers)`,
`(number|count|volume|share)\s+of\s+(completed\s+)?orders`.

**Verdict:** the answer genuinely fails to state the driver in plain words.

- **The driver is only the column name `order_count`.** The order figures
  are there, but nothing calls them a larger number of orders; every
  component is named by its column.
- **The facts are right.** The "about the same" components are correct and
  complete. That is the round-1 fix working.
- **Pattern unchanged.** Broadening it to accept `order_count` would reward
  answers that speak in column names, so I fixed the answer step instead
  (section 7).
- **Holdout patterns not opened.** Because the answer, not the pattern, was
  at fault, I did not open the ha07-ha09 driver patterns.

**dev2:** A 4/6, C 4/6, D 5/6. As-if-live latency per question: A 3.1 s,
C 4.4 s, D 4.8 s.

| id | A | C | D | what happened |
| --- | --- | --- | --- | --- |
| dv01 | FAIL | FAIL | **PASS** | A's exact name match returned no rows. C's two repairs tried LIKE '%Idris Rosis%' (first name corrected, surname still misspelled), which also returned none. D escalated on the empty result, called `search_values` (match: Idris Rossi) and re-queried by the corrected name. D helped; C did not change the result. |
| dv02 | PASS | PASS | PASS | A mapped "Professional Services" to 'Services' itself (the context lists the categories). Direct for all; get_distinct_values was not exercised. |
| dv03 | FAIL | FAIL | FAIL | A fitted the trend in one SQL query with x = year*12 + month - 24021 (256..279) but forecast at x = 25..27: 15.5 signups instead of 46.0, with no error. C's follow-up decision accepted it, so C and D stayed direct: a **missed escalation**. |
| dv04 | PASS | PASS | PASS | A computed the z-scores correctly in SQL. Direct. |
| dv05 | PASS | PASS | PASS | A evaluated the branch in one query. Direct. |
| dv06 | PASS | PASS | PASS | A filtered `churn_date` correctly. Direct, so D's date-format check never ran. |

- **Escalation against A:** C escalated 1 (dv01: no change); D escalated 1
  (dv01: helped).
- **Missed escalations:** A failed dv01 and dv03. Both C and D escalated
  dv01; neither escalated dv03.
- **Repairs:** 2 on dv01 for both C and D (shared calls; neither accepted, both
  still empty), so no fixes and no breaks.
- **D paths now run live:** `search_values` (dv01) and `decompose_change`
  (sc03).
- **D paths still not run live:** `get_distinct_values`,
  `linear_trend_forecast`, `zscore_anomalies`, and exploration on a
  conditional branch. All the questions that target them were answered
  correctly by A directly.

**dv06, date-format check:** A, C and D all passed, so the check stays where
it is: on D's escalated path only, not a step-0 signal. If a holdout shows a
silent date-format error that is not escalated, that will show up as a
missed escalation.

### 7. Round-2 fixes (after the d3275c5 runs)

- **The driver is always stated in plain words (sc03, answer step).**
  - The metric tools now name items in plain words: "number of orders",
    "average order value", "units sold", "1 - average discount", and so on.
    These come from a fixed `LABELS` map with one label per factor, tested.
  - The answer guidance asks for the main driver (the largest contributor)
    in the first sentence, no column names, and rounded figures.
  - If the answer still does not name the main driver, a sentence built
    deterministically from the table is put first, for example "Main driver
    (largest contributor): number of orders, Online 3,458 vs Direct 2,366
    (+46.2%); ...". The trace records `driver_sentence_added`.
  - This applies only to D's stacked metric tables; A's and C's prompts are
    unchanged.
- **Forecast escalation signal (dv03 missed escalation).**
  - `checks.forecast_signal` is a deterministic, zero-LLM signal read from
    the question.
  - It fires when the question asks for a forecast, a projection, an
    extrapolation, a trend line (linear or straight-line), a regression or a
    least-squares / best-fit line. Bare "trend", correlations and z-scores
    do not fire it.
  - It replaces the follow-up decision call and escalates directly. C and D
    share it, so they stay identical up to escalation; C, which has no
    compute tools, follows up with SQL.
  - **It was added on 2026-10-05 after a development failure (dev2 dv03);**
    this is recorded in `agentcrew/archs/checks.py`.
  - It fires on dv03 only among the showcase and dev2 questions (not sc04,
    not dv04). On the holdouts it fires on ha11 and ha12, which both ask for
    a straight-line trend fit, and on no easy question.
  - **Disclosure:** I had read the holdout texts while screening dev2. The
    rule was written from your list of categories, and ha11/ha12 are routed
    by "trend line" and "forecast" respectively, independent of the
    "straight-line trend" alternative in the rule.
- **Forecast rows carry their period.** `linear_trend_forecast` labels each
  forecast row with its period, in a column named after the order column. It
  understands 'YYYY-MM', 'YYYY-Qn', 'YYYY-MM-DD' (monthly or daily steps) and
  integers/years; when the column holds something else, the label is
  omitted.
- **Integration bug found and fixed.** For a forecast question, D's
  date-range check flagged the query of the fitting window (for example
  2023-2024) because it does not overlap the forecast period (2025). That
  would have triggered a bogus repair. The check is now skipped when the
  forecast signal is on (tested). The other three checks still run.
- **Cache check (offline, no API calls).** After these changes only three
  question/architecture pairs would make live calls: D on sc03, and C and D
  on dv03. Every other pair replays from the cache with the same result as
  before.

### 8. Runs on 70d5089: `phase4c-showcase` and `phase4b-dev2`

**Showcase: A 4/5, C 4/5, D 5/5.**

- D passes sc03. Its answer names the driver in plain words ("primarily due
  to a higher number of orders ... the largest contributor") and says the
  average order value is about the same. The judge passed both facts, and the
  driver sentence was not needed.
- **Escalation against A:** C and D escalated sc03; C no change, D **helped**.
- **Missed escalations:** none.

**dev2: A 4/6, C 4/6, D 5/6, with one ERROR (D on dv03).**

- **Escalation against A:** C escalated dv01 and dv03 (no change on either);
  D escalated dv01 (helped).
- **Missed escalations:** A failed dv01 and dv03. C escalated both. D
  escalated dv01; on dv03 it hit an ERROR.
- **C on dv03:** escalated on the forecast signal. Its one follow-up call
  judged step 1 sufficient (no new query), so it answered from A's wrong
  one-query fit (15.5 instead of 46.0). C has no compute tools; this is a
  plain failure, not a bug.

**What happened to D on dv03.** The run recorded `status failed, path error`
with:

> LLMError: Gemini call failed: 503 UNAVAILABLE ... This model is currently
> experiencing high demand.

- **Where it failed:** step 0 (A's cached call) ran, then D's first own live
  call raised the 503.
- **D did escalate:** an offline replay (A's real cached step 0, later calls
  scripted) shows `path escalated`, step 1 marked
  `escalation_signal: forecast`, no follow-up-decision call, and
  `decide_next_action` as D's first own call. So D handled the signal; the
  failure was the provider, on that call.
- **What was wrong was the recording, not D's logic:**
  - **No retry:** the runner did not retry a transient 503.
  - **Ordinary failure:** it recorded the exception as an ordinary failure
    ("no selected result").
  - **Labelled "direct":** my missed-escalations table counted every path
    other than "escalated" as "direct".
- **Answer to item 3:** yes, it was a swallowed exception.

### 9. Round-3 fixes (after the 70d5089 runs)

- **Exceptions are recorded as ERROR.**
  - Any exception that escapes an architecture is recorded as status `ERROR`,
    with its type, the stage it happened in (for example "step 2:
    decide_next_action"), the innermost `agentcrew/` code location, and the
    redacted message.
  - It is never shown as an ordinary failure or as "direct": the report,
    help/harm, missed escalations and the CLI all say ERROR.
  - Per protocol v2 ("a run that errors ... fails") it still scores as not
    passing; that rule was not changed.
  - Architectures name their stage as they go (`agentcrew/archs/progress.py`,
    a ContextVar). Quota stops and cache misses are re-raised and still stop
    and resume the run.
- **Transient 503 retries.** A 503 / UNAVAILABLE / "high demand" error is
  retried after 10, 30 and 60 s, and becomes an ERROR only if it persists. Any
  other provider error is raised at once.
- **One escalation rule for C and D.**
  - `Router.escalated(decision, first)` is now the only place escalation is
    decided, used by both C and D.
  - A test checks that D escalates on exactly the same questions as C:
    scripted scenarios, plus both development sets run offline from the
    cache. In the cache run, step 0 and the decision must come from the cache
    (it fails otherwise); only later calls are scripted.
- **D exploration reason.** D used to pass the follow-up decision's reason to
  exploration even when the decision said no follow-up was needed. It now
  uses it only when one was asked for. No cached prompt depended on it.
- **Nondeterministic metric SQL (found while building the parity test).**
  - `decompose_change` ordered its SELECT columns by iterating a Python set,
    and set order depends on per-process string hashing. The SQL text shown
    to the model, and so D's next prompts and their cache keys, changed from
    run to run: 4 hash seeds gave 3 different queries on the old code.
  - It now uses declaration order. A test runs the generator under 3 hash
    seeds and requires identical SQL.
  - **Cost:** D's sc03 prompts change once, so sc03 needs one more D run.
  - **Why it mattered:** without this fix, D's holdout why-question prompts
    would not have been reproducible.
- **Forced escalation (development smoke test only).**
  - `run_eval_v2.py --force-escalation` makes D escalate on every question.
  - It is refused on `holdout_agent` and `holdout_easy` (and on any set file
    listed in `FREEZE_v2.sha256`), refused for architectures other than D,
    and refused in a run folder that already holds normal results.
  - The results file is marked "development smoke test: results do not
    count", and so is the report.
  - The app never sets the switch; it exists only as a runner option.

### 10. Runs on 9d23f5f: `phase4c-dev2`, `phase4d-showcase`, `phase4-dev2-forced`

**Showcase: A 4/5, C 4/5, D 5/5.**

- D still passes sc03 with the deterministic SQL order (one more live run).
- **Escalation against A:** C escalated sc03 (no change); D escalated sc03
  (**helped**).
- **Missed escalations:** none.

**dev2: A 4/6, C 4/6, D 5/6, no ERROR.**

- **Escalation against A:** C escalated dv01 and dv03 (no change on either);
  D escalated dv01 (**helped**) and dv03 (no change).
- **Missed escalations:** none; C and D escalated both questions A failed.

**dv03, D (the question from item 1).**

- **D called no tool after step 1.** The forecast signal escalated it.
  `decide_next_action` then saw step 1, A's one-query fit (already labelled
  2025-01..03, "projected_signups" 15.5), and chose `answer` ("the
  projections ... have been successfully computed").
- **So nothing to report on D's fit.** `linear_trend_forecast` never ran,
  and there is no series, window or x convention of D's to report.
- **A's wrong value comes from an x offset.** A's SQL fitted
  x = year*12 + month - 24021 (256..279 for 2023-01..2024-12) but forecast
  at x = 25..27. That extrapolates the line back to about 15.5 instead of
  46.04.
- **The cause is not the tool's x convention.** It is that the planner
  trusts a first result that already looks like the answer. Fixed with a
  general rule (section 11).

**dv05, an answer-grading weakness (dev set; not changed).**

- **What happened:** A's query picks the Partner branch correctly (the rows
  are Partner's), but the table does not show which branch was taken. A's
  answer then says "Assuming Partner did not grow faster ... the Online
  channel's 2025 completed revenue ...".
- **Why it still passes:** the answer check only requires the word
  "Partner", which that wrong sentence contains. The result is right; the
  prose is wrong.
- **Holdouts not checked:** I have not opened ha19-ha21's answer checks.
  They may share this weakness, and any change there would need an
  amendment.

**Smoke test (forced escalation; results do not count): tools D actually
called.**

| question | tools by step | what the planner did |
| --- | --- | --- |
| dv01 | run_sql+repair > search_values > run_sql | replayed from the normal run (it escalates anyway) |
| dv02 | run_sql | answered straight away |
| dv03 | run_sql | replayed from the normal run: answered straight away |
| dv04 | run_sql | answered straight away |
| dv05 | run_sql | answered straight away |
| dv06 | run_sql | answered straight away |

Forced escalation did not get the live planner to choose any new tool. With
a step-1 result that looks right, Flash-Lite answers.

**Tools never run live with model-chosen arguments, and their offline
coverage** (`tests/test_dispatch.py`: every case goes through D's
dispatcher `Full._act`, from a model-style reply parsed the way the
structured-output path parses it):

| tool | live, model-chosen args | offline through the dispatcher |
| --- | --- | --- |
| run_sql (exploration loop) | yes (dv01) | yes |
| search_values | yes (dv01) | yes, wrong-case table/column filters |
| decompose_change | yes (sc03, via MetricPlan) | yes, wrong-case metric and dimension |
| get_distinct_values | **no** | yes, fenced reply with nulls, an extra field and wrong case |
| sample_rows | **no** | yes, wrong-case table |
| contribution_by_dimension | **no** | yes, wrong-case dimension |
| pct_change, yoy_growth, compare_periods, share_of_total, correlation | **no** | yes, wrong-case columns |
| zscore_anomalies | **no** | yes, against the trend, wrong-case options |
| linear_trend_forecast | **no** (the dv03 re-run is its first live use, via TrendPlan) | yes, a named period given as a string |
| conditional-branch path (a branch query in the loop) | **no** | yes, scripted D run: condition in step 1, branch query in step 2, branch table selected |

- **Bad arguments:** 15 cases, each returned as a failed step with a
  readable message and never an exception. They cover an unknown step,
  unknown params, a non-numeric column, a bad direction, a period inside the
  series, too many periods ahead, an unknown metric or dimension, a
  wrong-case data value, an empty comparison, an unknown table or column,
  and an injection-shaped table name.
- **Invalid replies:** 5 malformed replies are rejected at validation, so
  the structured-output path asks the model once to fix them.

### 11. Round-4 fixes (after the 9d23f5f runs)

- **Trend-fit questions open with a TrendPlan (D).**
  - When the forecast signal fires, D's escalated path starts with one
    validated plan: the series query (one row per period over the fitting
    window, labelled 'YYYY-MM' / 'YYYY-Qn' / 'YYYY'), the period and value
    columns, and the fit. The fit is `linear_trend_forecast` with the periods
    the question names, or `zscore_anomalies` against the trend.
  - The series query runs, is verified and is repaired like any query; the
    deterministic tool fits it; and the fit's table is the final result (no
    assembly call). If the plan is invalid or the series query fails, the
    ordinary loop continues.
  - **Recorded:** added after a development failure (dev2 dv03).
  - **Dry run:** with A's real cached step 0 and a plan of the expected
    shape, D returns 2025-01..03 = 46.036, 46.156, 46.276, which matches the
    gold exactly.
- **x convention.**
  - Trend fits use evenly spaced period numbers, 1 = the first period, for
    months, quarters and years; a missing period keeps its place. Other
    labels fall back to 1..n in period order.
  - Every trend output states the convention, for example
    `x_convention: "x = month number, 1 = 2023-01"`.
  - `linear_trend_forecast` also accepts the exact periods to forecast
    (`periods`) and returns only those rows.
  - dv03's wording already states the convention, so it is unchanged.
- **Model-style arguments.**
  - Lookups resolve table and column names case-insensitively, using the
    catalog's own spelling in the SQL.
  - Metric and dimension names are matched case-insensitively and are still
    allowlisted.
  - `zscore_anomalies` options are case-insensitive.
  - The compute dispatcher turns any argument-shaped error (including a
    plain ValueError or KeyError) into a failed step rather than an
    exception.
- **Pre-run amendment 3 (approved).**
  - Recorded in `eval/PROTOCOL.md` (Failures, Run configuration, Changes
    after pre-registration). `FREEZE_v2.sha256` was regenerated: only
    PROTOCOL.md's hash changed.
  - **Implementation:** a provider error that persists after the 10/30/60 s
    retries (an HTTP 5xx status code anywhere in the cause chain, "5xx
    STATUS" or UNAVAILABLE / high demand / overloaded in the message, or a
    transport timeout) marks the pair not evaluated. The run continues with
    the next pair. Each attempt is recorded with its date and stage.
  - **Resumes:** a later run resumes the pair. Every pair that hit such an
    error is listed in the results (`provider_resumes`) and in the report.
  - **Two-day rule:** if the error recurs on resumes made on two different
    calendar dates (the first run is not a resume), the pair is scored as a
    fail (status ERROR).
  - A run with pairs still waiting is not `complete` (the CLI exits 2), and
    the summary counts them as `not_evaluated`.
  - 4xx errors, invalid model output and exceptions in our code still count
    as fails.

### 12. Run tables

#### phase4-showcase (code 1cfa440, before the fix)

Set `showcase`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `1cfa440`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/5 | 0.38-0.96 | 2.0 | 0.03 (3.11) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| B v1 | 2/5 | 0.12-0.77 | 6.8 | 0.06 (8.64) | multi_hop 1/1, recovery 0/1, statistics 0/1, term_matching 1/1, why 0/1 |
| C router | 4/5 | 0.38-0.96 | 3.6 | 0.03 (4.85) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| D full | 4/5 | 0.38-0.96 | 3.6 | 1.15 (4.96) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sc01 | multi_hop | A baseline | PASS | - | 2 (0) | 0.06 (3.20) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | B v1 | PASS | - | 6 (0) | 0.05 (6.89) | v1 | run_sql > run_sql | 0: 0/0 |
| sc01 | multi_hop | C router | PASS | - | 3 (0) | 0.02 (4.42) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | D full | PASS | - | 3 (0) | 0.04 (4.43) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.64) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | B v1 | PASS | - | 4 (0) | 0.03 (4.71) | v1 | run_sql | 0: 0/0 |
| sc02 | term_matching | C router | PASS | - | 3 (0) | 0.01 (3.56) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | D full | PASS | - | 3 (0) | 0.01 (3.56) | direct | run_sql | 0: 0/0 |
| sc03 | why | A baseline | FAIL | driver named; judge fail | 2 (0) | 0.01 (2.38) | direct | run_sql | 0: 0/0 |
| sc03 | why | B v1 | FAIL | driver named; judge fail | 11 (0) | 0.15 (15.51) | v1 | run_sql > run_sql > run_sql | 0: 0/0 |
| sc03 | why | C router | FAIL | driver named; judge fail | 6 (0) | 0.04 (7.14) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| sc03 | why | D full | FAIL | driver named; judge fail | 6 (4) | 5.62 (7.69) | escalated | run_sql > run_sql > run_sql > assemble | 0: 0/0 |
| sc04 | statistics | A baseline | PASS | - | 2 (0) | 0.04 (4.16) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | B v1 | FAIL | expected 1 row(s), got 24; answer missing ['number: 0.1400200523481476'] | 7 (0) | 0.03 (9.13) | v1 | run_sql | 0: 0/0 |
| sc04 | statistics | C router | PASS | - | 3 (0) | 0.04 (5.06) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | D full | PASS | - | 3 (0) | 0.05 (5.07) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | A baseline | PASS | - | 2 (0) | 0.01 (3.15) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | B v1 | FAIL | result has 1 column(s); gold needs 2 | 6 (0) | 0.04 (6.97) | v1 | run_sql > run_sql | 0: 0/0 |
| sc05 | recovery | C router | PASS | - | 3 (0) | 0.02 (4.08) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | D full | PASS | - | 3 (0) | 0.02 (4.08) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | sc03 no change |
| D full | 1 | 0 | 0 | 1 | 0 | sc03 no change |

##### Missed escalations (questions A failed)

A failed 1 question(s). C escalated 1, did not escalate 0, not run 0; D escalated 1, did not escalate 0, not run 0.

| question | C | D |
| --- | --- | --- |
| sc03 | escalated (fail) | escalated (fail) |

#### phase4b-showcase (code d3275c5)
Set `showcase`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `d3275c5`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/5 | 0.38-0.96 | 2.0 | 0.02 (3.10) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| C router | 4/5 | 0.38-0.96 | 3.6 | 0.03 (4.86) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| D full | 4/5 | 0.38-0.96 | 3.6 | 1.53 (5.34) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sc01 | multi_hop | A baseline | PASS | - | 2 (0) | 0.05 (3.20) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | C router | PASS | - | 3 (0) | 0.02 (4.41) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | D full | PASS | - | 3 (0) | 0.03 (4.42) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.64) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | C router | PASS | - | 3 (0) | 0.01 (3.56) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | D full | PASS | - | 3 (0) | 0.01 (3.56) | direct | run_sql | 0: 0/0 |
| sc03 | why | A baseline | FAIL | driver named; judge fail | 2 (0) | 0.01 (2.38) | direct | run_sql | 0: 0/0 |
| sc03 | why | C router | FAIL | driver named; judge fail | 6 (0) | 0.04 (7.13) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| sc03 | why | D full | FAIL | driver not named: ["any_of: [{'pattern': 'more\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'order\\\\s+(volume/count/counts/numbers)'}, {'pattern': '(number/count/volume/share)\\\\s+of\\\\s+(completed\\\\s+)?orders'}]"] | 6 (4) | 7.54 (9.60) | escalated | run_sql > compute:decompose_change > stack | 0: 0/0 |
| sc04 | statistics | A baseline | PASS | - | 2 (0) | 0.04 (4.16) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | C router | PASS | - | 3 (0) | 0.09 (5.11) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | D full | PASS | - | 3 (0) | 0.04 (5.07) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | A baseline | PASS | - | 2 (0) | 0.01 (3.15) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | C router | PASS | - | 3 (0) | 0.02 (4.07) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | D full | PASS | - | 3 (0) | 0.02 (4.08) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | sc03 no change |
| D full | 1 | 0 | 0 | 1 | 0 | sc03 no change |

##### Missed escalations (questions A failed)

A failed 1 question(s). C escalated 1, did not escalate 0, not run 0; D escalated 1, did not escalate 0, not run 0.

| question | C | D |
| --- | --- | --- |
| sc03 | escalated (fail) | escalated (fail) |

#### phase4-dev2 (code d3275c5)
Set `dev2`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `d3275c5`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/6 | 0.30-0.90 | 2.0 | 3.12 (3.12) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| C router | 4/6 | 0.30-0.90 | 3.17 | 1.23 (4.39) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| D full | 5/6 | 0.44-0.97 | 3.5 | 0.59 (4.80) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 2/2 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dv01 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 2 (2) | 3.48 (3.48) | direct | run_sql | 0: 0/0 |
| dv01 | term_matching | C router | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 4 (2) | 2.88 (6.64) | escalated | run_sql+repair | 2: 0/0 |
| dv01 | term_matching | D full | PASS | - | 6 (3) | 3.43 (9.21) | escalated | run_sql+repair > search_values > run_sql | 2: 0/0 |
| dv02 | term_matching | A baseline | PASS | - | 2 (2) | 2.23 (2.23) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | C router | PASS | - | 3 (1) | 0.84 (3.06) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | D full | PASS | - | 3 (0) | 0.01 (3.06) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | A baseline | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 2 (2) | 3.65 (3.65) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | C router | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (1) | 0.93 (4.56) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | D full | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (0) | 0.02 (4.52) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | A baseline | PASS | - | 2 (2) | 3.43 (3.43) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | C router | PASS | - | 3 (1) | 0.98 (4.40) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | D full | PASS | - | 3 (0) | 0.02 (4.39) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | A baseline | PASS | - | 2 (2) | 3.56 (3.56) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | C router | PASS | - | 3 (1) | 0.89 (4.43) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | D full | PASS | - | 3 (0) | 0.03 (4.42) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | A baseline | PASS | - | 2 (2) | 2.37 (2.37) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | C router | PASS | - | 3 (1) | 0.87 (3.23) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.23) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | dv01 no change |
| D full | 1 | 1 | 0 | 0 | 0 | dv01 helped |

##### Missed escalations (questions A failed)

A failed 2 question(s). C escalated 1, did not escalate 1, not run 0; D escalated 1, did not escalate 1, not run 0.

| question | C | D |
| --- | --- | --- |
| dv01 | escalated (fail) | escalated (pass) |
| dv03 | direct (fail) | direct (fail) |

#### phase4c-showcase (code 70d5089)
Set `showcase`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `70d5089`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/5 | 0.38-0.96 | 2.0 | 0.11 (3.19) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| C router | 4/5 | 0.38-0.96 | 3.6 | 0.09 (4.92) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| D full | 5/5 | 0.57-1.00 | 3.6 | 1.27 (5.65) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 1/1 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sc01 | multi_hop | A baseline | PASS | - | 2 (0) | 0.25 (3.39) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | C router | PASS | - | 3 (0) | 0.12 (4.51) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | D full | PASS | - | 3 (0) | 0.17 (4.56) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | A baseline | PASS | - | 2 (0) | 0.03 (2.67) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | C router | PASS | - | 3 (0) | 0.04 (3.60) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | D full | PASS | - | 3 (0) | 0.04 (3.59) | direct | run_sql | 0: 0/0 |
| sc03 | why | A baseline | FAIL | driver named; judge fail | 2 (0) | 0.05 (2.42) | direct | run_sql | 0: 0/0 |
| sc03 | why | C router | FAIL | driver named; judge fail | 6 (0) | 0.17 (7.26) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| sc03 | why | D full | PASS | - | 6 (2) | 6.00 (10.92) | escalated | run_sql > compute:decompose_change > stack | 0: 0/0 |
| sc04 | statistics | A baseline | PASS | - | 2 (0) | 0.18 (4.30) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | C router | PASS | - | 3 (0) | 0.10 (5.12) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | D full | PASS | - | 3 (0) | 0.08 (5.11) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | A baseline | PASS | - | 2 (0) | 0.03 (3.16) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | C router | PASS | - | 3 (0) | 0.04 (4.09) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | D full | PASS | - | 3 (0) | 0.04 (4.09) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | sc03 no change |
| D full | 1 | 1 | 0 | 0 | 0 | sc03 helped |

##### Missed escalations (questions A failed)

A failed 1 question(s). C escalated 1, did not escalate 0, ERROR 0, not run 0; D escalated 1, did not escalate 0, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| sc03 | escalated (fail) | escalated (pass) |

#### phase4b-dev2 (code 70d5089)
Set `dev2`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `70d5089`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/6 | 0.30-0.90 | 2.0 | 0.03 (3.03) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| C router | 4/6 | 0.30-0.90 | 3.17 | 3.64 (7.68) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| D full | 5/6 | 0.44-0.97 | 3.17 | 0.39 (4.86) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 2/2 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dv01 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 2 (0) | 0.07 (2.86) | direct | run_sql | 0: 0/0 |
| dv01 | term_matching | C router | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 4 (0) | 0.06 (6.66) | escalated | run_sql+repair | 2: 0/0 |
| dv01 | term_matching | D full | PASS | - | 6 (0) | 0.12 (9.27) | escalated | run_sql+repair > search_values > run_sql | 2: 0/0 |
| dv02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.23) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | C router | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | D full | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | A baseline | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 2 (0) | 0.02 (3.66) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | C router | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (2) | 21.66 (24.30) | escalated | run_sql | 0: 0/0 |
| dv03 | statistics | D full | FAIL | no selected result; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 1 (0) | 2.15 (4.79) | error | - | 0: 0/0 |
| dv04 | statistics | A baseline | PASS | - | 2 (0) | 0.04 (3.46) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | C router | PASS | - | 3 (0) | 0.04 (4.41) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | D full | PASS | - | 3 (0) | 0.02 (4.40) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | A baseline | PASS | - | 2 (0) | 0.04 (3.58) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | C router | PASS | - | 3 (0) | 0.05 (4.44) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | D full | PASS | - | 3 (0) | 0.06 (4.45) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | A baseline | PASS | - | 2 (0) | 0.01 (2.37) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | C router | PASS | - | 3 (0) | 0.01 (3.22) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.22) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 2 | 0 | 0 | 2 | 0 | dv01 no change, dv03 no change |
| D full | 1 | 1 | 0 | 0 | 0 | dv01 helped |

##### Missed escalations (questions A failed)

A failed 2 question(s). C escalated 2, did not escalate 0, ERROR 0, not run 0; D escalated 1, did not escalate 1, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| dv01 | escalated (fail) | escalated (pass) |
| dv03 | escalated (fail) | direct (fail) |

#### phase4d-showcase (code 9d23f5f)
Set `showcase`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `9d23f5f`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/5 | 0.38-0.96 | 2.0 | 0.08 (3.16) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| C router | 4/5 | 0.38-0.96 | 3.6 | 0.06 (4.88) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| D full | 5/5 | 0.57-1.00 | 3.6 | 1.05 (5.43) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 1/1 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sc01 | multi_hop | A baseline | PASS | - | 2 (0) | 0.13 (3.27) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | C router | PASS | - | 3 (0) | 0.05 (4.44) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | D full | PASS | - | 3 (0) | 0.07 (4.46) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.65) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | C router | PASS | - | 3 (0) | 0.02 (3.57) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | D full | PASS | - | 3 (0) | 0.02 (3.57) | direct | run_sql | 0: 0/0 |
| sc03 | why | A baseline | FAIL | driver named; judge fail | 2 (0) | 0.03 (2.40) | direct | run_sql | 0: 0/0 |
| sc03 | why | C router | FAIL | driver named; judge fail | 6 (0) | 0.10 (7.20) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| sc03 | why | D full | PASS | - | 6 (2) | 5.01 (9.94) | escalated | run_sql > compute:decompose_change > stack | 0: 0/0 |
| sc04 | statistics | A baseline | PASS | - | 2 (0) | 0.19 (4.31) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | C router | PASS | - | 3 (0) | 0.08 (5.11) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | D full | PASS | - | 3 (0) | 0.09 (5.11) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | A baseline | PASS | - | 2 (0) | 0.02 (3.16) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | C router | PASS | - | 3 (0) | 0.05 (4.10) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | D full | PASS | - | 3 (0) | 0.04 (4.10) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | sc03 no change |
| D full | 1 | 1 | 0 | 0 | 0 | sc03 helped |

##### Missed escalations (questions A failed)

A failed 1 question(s). C escalated 1, did not escalate 0, ERROR 0, not run 0; D escalated 1, did not escalate 0, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| sc03 | escalated (fail) | escalated (pass) |

##### Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.

#### phase4c-dev2 (code 9d23f5f)
Set `dev2`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `9d23f5f`; complete: True.

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/6 | 0.30-0.90 | 2.0 | 0.03 (3.03) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| C router | 4/6 | 0.30-0.90 | 3.17 | 0.03 (7.46) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| D full | 5/6 | 0.44-0.97 | 3.5 | 0.74 (5.21) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 2/2 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dv01 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 2 (0) | 0.07 (2.86) | direct | run_sql | 0: 0/0 |
| dv01 | term_matching | C router | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 4 (0) | 0.05 (6.65) | escalated | run_sql+repair | 2: 0/0 |
| dv01 | term_matching | D full | PASS | - | 6 (0) | 0.12 (9.27) | escalated | run_sql+repair > search_values > run_sql | 2: 0/0 |
| dv02 | term_matching | A baseline | PASS | - | 2 (0) | 0.02 (2.24) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | C router | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | D full | PASS | - | 3 (0) | 0.01 (3.06) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | A baseline | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 2 (0) | 0.02 (3.65) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | C router | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (0) | 0.04 (22.99) | escalated | run_sql | 0: 0/0 |
| dv03 | statistics | D full | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (2) | 4.22 (6.87) | escalated | run_sql | 0: 0/0 |
| dv04 | statistics | A baseline | PASS | - | 2 (0) | 0.02 (3.44) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | C router | PASS | - | 3 (0) | 0.04 (4.41) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | D full | PASS | - | 3 (0) | 0.03 (4.41) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | A baseline | PASS | - | 2 (0) | 0.05 (3.59) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | C router | PASS | - | 3 (0) | 0.05 (4.44) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | D full | PASS | - | 3 (0) | 0.05 (4.44) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | A baseline | PASS | - | 2 (0) | 0.01 (2.37) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | C router | PASS | - | 3 (0) | 0.01 (3.23) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.23) | direct | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 2 | 0 | 0 | 2 | 0 | dv01 no change, dv03 no change |
| D full | 2 | 1 | 0 | 1 | 0 | dv01 helped, dv03 no change |

##### Missed escalations (questions A failed)

A failed 2 question(s). C escalated 2, did not escalate 0, ERROR 0, not run 0; D escalated 2, did not escalate 0, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| dv01 | escalated (fail) | escalated (pass) |
| dv03 | escalated (fail) | escalated (fail) |

##### Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.

#### phase4-dev2-forced (code 9d23f5f; smoke test, does not count)
Set `dev2`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `9d23f5f`; complete: True.

**Forced escalation (development smoke test): these results do not count.**

##### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| D full | 5/6 | 0.44-0.97 | 4.17 | 1.82 (6.06) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 2/2 |

##### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dv01 | term_matching | D full | PASS | - | 6 (0) | 0.20 (9.34) | escalated | run_sql+repair > search_values > run_sql | 2: 0/0 |
| dv02 | term_matching | D full | PASS | - | 4 (2) | 3.72 (5.98) | escalated | run_sql | 0: 0/0 |
| dv03 | statistics | D full | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (0) | 0.12 (5.36) | escalated | run_sql | 0: 0/0 |
| dv04 | statistics | D full | PASS | - | 4 (2) | 2.54 (5.68) | escalated | run_sql | 0: 0/0 |
| dv05 | segment_comparison | D full | PASS | - | 4 (2) | 2.31 (5.56) | escalated | run_sql | 0: 0/0 |
| dv06 | recovery | D full | PASS | - | 4 (2) | 2.07 (4.44) | escalated | run_sql | 0: 0/0 |

##### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| D full | 6 | 0 | 0 | 0 | 6 | dv01 unknown, dv02 unknown, dv03 unknown, dv04 unknown, dv05 unknown, dv06 unknown |

##### Missed escalations (questions A failed)

A failed no question.

##### Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.

#### dv03 re-run (`phase4d-dev2`, code d2cf7e0)

D escalated on the forecast signal and its validated TrendPlan was:
`series_sql` = monthly signups for 2023-01..2024-12 (strftime('%Y-%m'),
one row per month, 24 rows), `period` = period, `value` = value,
`function` = linear_trend_forecast, `periods` = ["2025-01", "2025-02",
"2025-03"]. The tool fitted slope 0.11957, intercept 43.04710 with
x = month number (1 = 2023-01) and returned 46.0362, 46.1558, 46.2754 for
x = 25, 26, 27: equal to the gold (46.03623188405797, 46.155797101449274,
46.27536231884058). **PASS** (3 rows match; answer checks pass). A and C
still fail (A's x offset). 2 live calls.

## F. Final run tables (code b07f4f1, offline replay)

### showcase: A, B, C, D
Set `showcase`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `b07f4f1`; complete: True.

#### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/5 | 0.38-0.96 | 2.0 | 0.03 (3.11) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| B v1 | 2/5 | 0.12-0.77 | 6.8 | 0.1 (8.68) | multi_hop 1/1, recovery 0/1, statistics 0/1, term_matching 1/1, why 0/1 |
| C router | 4/5 | 0.38-0.96 | 3.6 | 0.04 (4.86) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 0/1 |
| D full | 5/5 | 0.57-1.00 | 3.6 | 0.05 (5.10) | multi_hop 1/1, recovery 1/1, statistics 1/1, term_matching 1/1, why 1/1 |

#### Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B v1 | 5 | 2 | 1 | 2 | 0 | 0.500 | no |
| C router | 5 | 4 | 1 | 0 | 0 | 1.000 | yes |
| D full | 5 | 4 | 0 | 0 | 1 | 1.000 | yes |

#### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sc01 | multi_hop | A baseline | PASS | - | 2 (0) | 0.03 (3.18) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | B v1 | PASS | - | 6 (0) | 0.08 (6.92) | v1 | run_sql > run_sql | 0: 0/0 |
| sc01 | multi_hop | C router | PASS | - | 3 (0) | 0.03 (4.42) | direct | run_sql | 0: 0/0 |
| sc01 | multi_hop | D full | PASS | - | 3 (0) | 0.05 (4.44) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.65) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | B v1 | PASS | - | 4 (0) | 0.05 (4.73) | v1 | run_sql | 0: 0/0 |
| sc02 | term_matching | C router | PASS | - | 3 (0) | 0.01 (3.56) | direct | run_sql | 0: 0/0 |
| sc02 | term_matching | D full | PASS | - | 3 (0) | 0.02 (3.57) | direct | run_sql | 0: 0/0 |
| sc03 | why | A baseline | FAIL | driver named; judge fail | 2 (0) | 0.03 (2.39) | direct | run_sql | 0: 0/0 |
| sc03 | why | B v1 | FAIL | driver named; judge fail | 11 (0) | 0.23 (15.59) | v1 | run_sql > run_sql > run_sql | 0: 0/0 |
| sc03 | why | C router | FAIL | driver named; judge fail | 6 (0) | 0.06 (7.15) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| sc03 | why | D full | PASS | - | 6 (0) | 0.08 (8.33) | escalated | run_sql > compute:decompose_change > stack | 0: 0/0 |
| sc04 | statistics | A baseline | PASS | - | 2 (0) | 0.06 (4.18) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | B v1 | FAIL | expected 1 row(s), got 24; answer missing ['number: 0.1400200523481476'] | 7 (0) | 0.04 (9.15) | v1 | run_sql | 0: 0/0 |
| sc04 | statistics | C router | PASS | - | 3 (0) | 0.06 (5.08) | direct | run_sql | 0: 0/0 |
| sc04 | statistics | D full | PASS | - | 3 (0) | 0.07 (5.09) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | A baseline | PASS | - | 2 (0) | 0.02 (3.15) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | B v1 | FAIL | result has 1 column(s); gold needs 2 | 6 (0) | 0.09 (7.01) | v1 | run_sql > run_sql | 0: 0/0 |
| sc05 | recovery | C router | PASS | - | 3 (0) | 0.03 (4.09) | direct | run_sql | 0: 0/0 |
| sc05 | recovery | D full | PASS | - | 3 (0) | 0.03 (4.09) | direct | run_sql | 0: 0/0 |

#### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 1 | 0 | 0 | 1 | 0 | sc03 no change |
| D full | 1 | 1 | 0 | 0 | 0 | sc03 helped |

#### Missed escalations (questions A failed)

A failed 1 question(s). C escalated 1, did not escalate 0, ERROR 0, not run 0; D escalated 1, did not escalate 0, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| sc03 | escalated (fail) | escalated (pass) |

#### Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.

### dev2: A, C, D
Set `dev2`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `b07f4f1`; complete: True.

#### Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 4/6 | 0.30-0.90 | 2.0 | 0.02 (3.01) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| C router | 4/6 | 0.30-0.90 | 3.17 | 0.03 (6.22) | recovery 1/1, segment_comparison 1/1, statistics 1/2, term_matching 1/2 |
| D full | 6/6 | 0.61-1.00 | 3.5 | 0.04 (7.29) | recovery 1/1, segment_comparison 1/1, statistics 2/2, term_matching 2/2 |

#### Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C router | 6 | 4 | 2 | 0 | 0 | 1.000 | yes |
| D full | 6 | 4 | 0 | 0 | 2 | 0.500 | yes |

#### Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dv01 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 2 (0) | 0.01 (2.80) | direct | run_sql | 0: 0/0 |
| dv01 | term_matching | C router | FAIL | expected 1 row(s), got 0; answer missing ['text: Idris Rossi', 'text: 1133', 'number: 33475.845'] | 4 (0) | 0.04 (6.88) | escalated | run_sql+repair | 2: 0/0 |
| dv01 | term_matching | D full | PASS | - | 6 (0) | 0.10 (9.24) | escalated | run_sql+repair > search_values > run_sql | 2: 0/0 |
| dv02 | term_matching | A baseline | PASS | - | 2 (0) | 0.01 (2.23) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | C router | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| dv02 | term_matching | D full | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | A baseline | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 2 (0) | 0.02 (3.65) | direct | run_sql | 0: 0/0 |
| dv03 | statistics | C router | FAIL | no result column matches gold column(s) [1]; first gold row ['2025-01', 46.03623188405797]; answer missing ['number: 46.03623188405797', 'number: 46.155797101449274', 'number: 46.27536231884058'] | 3 (0) | 0.03 (15.30) | escalated | run_sql | 0: 0/0 |
| dv03 | statistics | D full | PASS | - | 3 (0) | 0.05 (19.40) | escalated | run_sql > run_sql > compute:linear_trend_forecast | 0: 0/0 |
| dv04 | statistics | A baseline | PASS | - | 2 (0) | 0.01 (3.43) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | C router | PASS | - | 3 (0) | 0.02 (4.39) | direct | run_sql | 0: 0/0 |
| dv04 | statistics | D full | PASS | - | 3 (0) | 0.02 (4.40) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | A baseline | PASS | - | 2 (0) | 0.03 (3.57) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | C router | PASS | - | 3 (0) | 0.04 (4.43) | direct | run_sql | 0: 0/0 |
| dv05 | segment_comparison | D full | PASS | - | 3 (0) | 0.04 (4.43) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | A baseline | PASS | - | 2 (0) | 0.01 (2.37) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | C router | PASS | - | 3 (0) | 0.01 (3.22) | direct | run_sql | 0: 0/0 |
| dv06 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.22) | direct | run_sql | 0: 0/0 |

#### Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 2 | 0 | 0 | 2 | 0 | dv01 no change, dv03 no change |
| D full | 2 | 2 | 0 | 0 | 0 | dv01 helped, dv03 helped |

#### Missed escalations (questions A failed)

A failed 2 question(s). C escalated 2, did not escalate 0, ERROR 0, not run 0; D escalated 2, did not escalate 0, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| dv01 | escalated (fail) | escalated (pass) |
| dv03 | escalated (fail) | escalated (pass) |

#### Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
