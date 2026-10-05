# Phase 5 report: final evaluation on the frozen holdouts

Protocol v2 (`eval/PROTOCOL.md`, with pre-run amendments 1-3). Plan:
`eval/results/phase5_plan.md`. Raw per-question outcomes (results files,
redacted traces, generated reports): `eval/results/phase5/`.

## Runs

| run | set | archs | code | started (UTC) | complete | live calls |
| --- | --- | --- | --- | --- | --- | --- |
| `phase5-holdout-agent` | holdout_agent (21) | A, C, D | b868f38 | 2026-10-05 01:55 | yes | 103 + 16 judge |
| `phase5-holdout-easy` | holdout_easy (12) | A, C, D | b868f38 | 2026-10-05 02:03 | yes | 36 |
| `phase5-holdout-agent-v1` | holdout_agent (21) | B | adff209 | 2026-10-05 05:02 | yes | 124 + 6 judge |
| `phase5-holdout-easy-v1` | holdout_easy (12) | B | adff209 | 2026-10-05 05:11 | yes | 48 |

- **Primary result:** one primary run of A, C and D on each set, at a clean
  commit. B ran afterwards in its own folders, at a commit that differs
  only in documents (no change under `agentcrew/`, `evalrun/`, `evalkit/`,
  `scripts/` or the run configuration).
- **Errors:** no ERROR, no provider error, no resumes in any run.
- **Cost:** 333 live calls in total (155 for A, C and D; 178 for B).
- **Configuration:** gemini-3.5-flash-lite, temperature 0, today
  2026-10-04, at most 3 steps and 3 attempts per step, for every
  architecture.

## Pre-registered rules, as applied

Quoted from `eval/PROTOCOL.md`:

> **Primary comparison:** D vs A on `holdout_agent`, using the two-sided
> exact McNemar test on the primary metric.

> **Easy set:** D must not be worse than A on `holdout_easy`. That means
> the number of questions only A passes is no larger than the number only
> D passes. McNemar p is reported as well.

> **Power:** with 21 questions, significance needs a lopsided split of
> discordant pairs: at least 6-0 (p = 0.031) or 8-1 (p = 0.039). A smaller
> win is reported as a win in this sample without significance, in those
> words.

> These answer-text conditions are part of the primary grade for every
> architecture, A included [...] naming the gold item(s) on which/top-N
> questions, and stating the assumption and naming the metric on ambiguous
> questions.

> A run that errors or produces no selected result fails.

> A question counts only when every architecture in the comparison has an
> evaluated result for it.

> one primary run of A, C and D on both holdouts, with B only if quota
> allows. [...] the primary result is never replaced by a better run.

Every architecture was evaluated on every question, so all 21 + 12 count.
B's one ordinary failure (ha12: its loop gave up after its attempts, "no
selected result") is scored as a fail under the rule above.

## Verdict

**Primary: on holdout_agent, D beats A in this sample, 16/21 vs 13/21,
without significance.** D passes 3 questions A fails (ha06, ha09, ha11) and
fails none that A passes. The split is 3-0 and the exact McNemar p is 0.25,
short of the 6-0 or 8-1 needed.

**Easy set: D is not worse than A.** A, B, C and D all score 12/12 (no
discordant pairs, p = 1.0).

**Secondary comparisons** (not pre-registered as decisions; reported for
context):

- **C vs A:** 2-0, p = 0.50.
- **D vs C:** 1-0 (ha09), p = 1.0. Two of D's three wins over A come from
  C's repair step, which D shares.
- **B vs A:** B 9/21 vs A 13/21. A passes 5 that B fails, B passes 1 that A
  fails (ha20), p = 0.22.
- **D vs B:** D passes 8 that B fails, B passes 1 that D fails (ha20),
  p = 0.039. This 8-1 split meets the protocol's significance threshold,
  but B is a reference arm and this comparison was not the pre-registered
  decision. Read it as secondary.

## Primary comparison: holdout_agent

| arch | primary | Wilson 95% | avg LLM calls | avg latency, as if live |
| --- | --- | --- | --- | --- |
| A baseline | 13/21 | 0.41-0.79 | 2.14 | 3.8 s |
| B v1 (reference) | 9/21 | 0.24-0.63 | 5.90 | 11.0 s |
| C router | 15/21 | 0.50-0.86 | 3.86 | 6.1 s |
| D full | 16/21 | 0.55-0.89 | 3.67 | 5.7 s |

| paired comparison | both pass | neither | only first | only second | exact McNemar p | role |
| --- | --- | --- | --- | --- | --- | --- |
| D vs A | 13 | 5 | 3 (ha06, ha09, ha11) | 0 | 0.250 | **primary** |
| C vs A | 13 | 6 | 2 (ha06, ha11) | 0 | 0.500 | secondary |
| D vs C | 15 | 5 | 1 (ha09) | 0 | 1.000 | secondary |
| B vs A | 8 | 7 | 1 (ha20) | 5 (ha02, ha03, ha12, ha16, ha21) | 0.219 | secondary |
| D vs B | 8 | 4 | 8 (ha02, ha03, ha06, ha09, ha11, ha12, ha16, ha21) | 1 (ha20) | 0.039 | secondary |

**By type** (3 questions each; descriptive only):

| type | A | B | C | D |
| --- | --- | --- | --- | --- |
| multi_hop | 3/3 | 1/3 | 3/3 | 3/3 |
| term_matching | 2/3 | 2/3 | 3/3 | 3/3 |
| why | 0/3 | 0/3 | 0/3 | 1/3 |
| statistics | 2/3 | 1/3 | 3/3 | 3/3 |
| recovery | 3/3 | 3/3 | 3/3 | 3/3 |
| ambiguous | 1/3 | 0/3 | 1/3 | 1/3 |
| segment_comparison | 2/3 | 2/3 | 2/3 | 2/3 |

**Repairs** (fix = wrong to right, break = right to wrong, on the step
whose result is graded; why-questions are not counted):

| arch | holdout_agent repairs | fixes | breaks | holdout_easy repairs | fixes | breaks |
| --- | --- | --- | --- | --- | --- | --- |
| A baseline | 1 (ha10, its retry) | 1 | 0 | 0 | 0 | 0 |
| B v1 | not recorded (B retries inside its own loop) | - | - | not recorded | - | - |
| C router | 4 | 3 (ha06, ha10, ha11) | 0 | 0 | 0 | 0 |
| D full | 4 | 2 (ha06, ha10) | 0 | 0 | 0 | 0 |

D's other two repairs (ha07, ha11) were not on the graded step.

**Escalation against A:**

- C escalated 8 questions: helped 2 (ha06, ha11), harmed 0, no change 6.
- D escalated the same 8: helped 3 (ha06, ha09, ha11), harmed 0, no change 5
  (ha03, ha07, ha08, ha10, ha12).
- B has no direct path; every B run is its own loop.

**Missed escalations:** A failed 8 questions. C and D escalated 5 of them
(ha06, ha07, ha08, ha09, ha11) and missed 3 (ha17, ha18, ha20). Those three
fail for A, C and D; see the error analysis.

**How D's wins happened:**

- **ha06 (C and D):** A matched 'Cobolt Nimbus' exactly and got no rows.
  The empty-result signal triggered C's repair, which found Cobalt Nimbus.
- **ha09 (D only):** `decompose_change` plus `contribution_by_dimension`
  gave units 2,741 to 778, with unit price (1,003 to 1,009) and discount
  "about the same". The judge passed both facts; spot-checked, and the
  quotes are right.
- **ha11 (C and D):** A's trend-residual query returned no rows. C repaired
  it in SQL. D's repair did the same, and D's trend plan with
  `zscore_anomalies` against the trend returned exactly the gold months
  2025-07..09 (x = month number, 1 = 2024-01).
- **ha12 (no change):** all of A, C and D pass. D used its trend plan and
  `linear_trend_forecast`, which agrees with A's SQL.
- **ha10:** A's own retry fixed an SQL error. C and D took A's retry first
  (the fallback-safety rule) and got the same result.

**B's failures, briefly:**

- **Wrong figures:** ha02 reports July's marketing spend (145,145) as the
  revenue, ha03 reports 1,542 units instead of 1,075, and ha06 reports 2,739
  units instead of 913.
- **Never reached the answering query:** on ha21 B ran only the
  condition query. On ha11 its attempts degenerated into schema probes
  (`SELECT SQLITE_VERSION()`, a list of tables) and it ended on a table
  list.
- **Gave up:** ha12 ended after B's attempts with no result.
- **Ambiguous questions:** ha16, ha17 and ha18 state no assumption (ha18
  also uses a different reading: quantity ordered across all statuses).
- **Why-questions:** ha07's driver is not named, and ha08 and ha09 fail the
  judge.

## Easy set: holdout_easy

| arch | primary | Wilson 95% | avg LLM calls | avg latency, as if live |
| --- | --- | --- | --- | --- |
| A baseline | 12/12 | 0.76-1.00 | 2.0 | 3.9 s |
| B v1 (reference) | 12/12 | 0.76-1.00 | 4.0 | 5.1 s |
| C router | 12/12 | 0.76-1.00 | 3.0 | 4.9 s |
| D full | 12/12 | 0.76-1.00 | 3.0 | 4.9 s |

Nothing escalated, so C and D answered every question with A's own result
and answer. Their extra cost is the one follow-up decision call per question
(as if live, +1.1 s).

## Conditional branches (ha19-ha21): what the answers name

A known limitation of the answer-text check. This is reported separately and
changes no primary score. The method was fixed in the plan before the run,
and no holdout check was opened or amended.

The supported branch comes from running the condition part of each gold
query on the database:

- **ha19:** Enterprise AOV 2,854.42 vs SMB 2,838.89 (+0.5%, not over 20%),
  so the **SMB** branch applies.
- **ha20:** Online 16.99% vs Direct 14.99% (a 2.00-point gap), so **Online,
  by region**.
- **ha21:** APAC 741 tickets vs EMEA 397 (1.87 times), so **APAC, by month**.

C and D answered all three directly with A's answer call, so their answers
are word for word A's.

| question | supported branch | A = C = D: branch named | match | B: branch named | match | graded (A, C, D / B) |
| --- | --- | --- | --- | --- | --- | --- |
| ha19 | SMB | Enterprise: "Assuming Enterprise's average completed order value was more than 20% higher than SMB's, Enterprise's 2025 completed revenue by product category consists of ..." | **no** | SMB: "Because Enterprise's average order value is not more than 20% higher than SMB's, SMB's 2025 completed revenue is broken down by product category ..." | yes | PASS / PASS |
| ha20 | Online, by region | by region, channel unnamed: "... the 2025 refund rate for the higher channel is broken down by region ..." | branch yes, channel not named | Online by region: "Because the gap between them was 2.00 percentage points ... the refund rate for the higher channel (Online) is broken down by region ..." | yes | FAIL (extra NULL row) / PASS |
| ha21 | APAC, by month | APAC by month, as an assumption: "Assuming APAC opened more than 1.5 times as many tickets as EMEA in 2025, APAC's tickets are broken down by month ..." | yes (as an assumption) | APAC by month: "... APAC opened more than 1.5 times as many tickets as EMEA. Consequently, APAC's 2025 tickets are broken down by month ..." | yes, but its table is the condition, not the breakdown | PASS / FAIL |

On these three questions B names every branch correctly, because it
evaluates the condition in a separate step before answering. A, C and D
answer from one query whose table does not carry the condition, and on ha19
the answer guesses the wrong branch while the table is right.

### Sensitivity: strict branch naming on ha19

Not part of the primary scores, which stay as pre-registered and graded
above. If ha19 required the answer to name the supported branch (SMB), A,
C and D would fail it and B would still pass:

| | A | B | C | D |
| --- | --- | --- | --- | --- |
| holdout_agent, as graded | 13/21 | 9/21 | 15/21 | 16/21 |
| with strict branch naming on ha19 | 12/21 (0.37-0.76) | 9/21 (0.24-0.63) | 14/21 (0.45-0.83) | 15/21 (0.50-0.86) |

| comparison | as graded | with strict ha19 |
| --- | --- | --- |
| D vs A | 3-0, p = 0.250 | 3-0, p = 0.250 (unchanged: both lose ha19) |
| D vs B | 8-1, p = 0.039 | 8-2, p = 0.109 (no longer under 0.05) |
| C vs A | 2-0, p = 0.500 | 2-0, p = 0.500 |
| B vs A | 1-5, p = 0.219 | 2-5, p = 0.453 |

The D-vs-B secondary result depends on how ha19's answer text is graded.
The primary D-vs-A result does not.

## Error analysis: questions A, C and D all failed

ha07, ha08, ha17, ha18 and ha20. B also fails ha07, ha08, ha17 and ha18;
it passes ha20, because it evaluates the condition in its own step and
then runs only the branch query. **The fixes below are future work. They
were not applied to this holdout, and they would have to be measured on a
new holdout.**

### ha07 (why): why APAC's revenue fell more than other regions' from Q2 to Q3 2025

**What each answer did:**

- **A:** a product x region x month query hit the 500-row cap; the answer
  listed product revenues and named no driver.
- **C:** the row-cap signal led to a repair (region x quarter revenue), then
  follow-ups by category and segment. The answer blamed a Hardware drop in
  APAC, which is not the driver and is a disqualified claim (the Hardware
  mix).
- **D:** same repaired step 1. Its MetricPlan decomposed revenue Q2 vs Q3
  within APAC: average order value -35% (largest contributor), orders -29%.
  Its `contribution_by_dimension` call asked for dimension `region_name` (a
  column name; the dimension is `region`), was refused, and no step was
  left. The answer named average order value as the largest contributor:
  disqualified, with the other regions' comparison missing.

**Why it failed:** the question compares a change across groups ("more than
other regions"). That needs each component's change in every region and the
component whose change differs most between APAC and the rest: orders -29%
in APAC vs -2% to -12% elsewhere, with average order value -32% to -39%
everywhere. D's decomposition compares two periods within one group, so it
answered "why did APAC fall", not "why more than the others".

**General fixes (future work):**

1. A "difference of changes" comparison: decompose the change for the
   subject group and for each reference group, and rank components by how
   much their change differs.
2. Accept a dimension's underlying column name as an alias (`region_name`
   for `region`), or validate dimension names when the plan is parsed, so a
   bad name costs one schema retry instead of a step.

### ha08 (why): why customer churn spiked in Q3 2025

**What each answer did:**

- **A:** churn by segment x region for Q3. The answer said churn was
  concentrated in APAC, then gave segment counts. There was no comparison
  with earlier quarters, so fact 2 was missing.
- **C:** follow-ups on APAC tickets and churn by month x region x segment.
  Same answer, same missing fact.
- **D:** its MetricPlan asked for `churn_rate` split by period, which the
  tool refuses, so the step was wasted. Then a Q2 vs Q3 query by segment x
  region. The answer led with "the Mid-Market segment in APAC" as the
  driver, which is a disqualified claim.

**Why it failed:**

1. **No baseline:** nothing compared Q3 with APAC's usual churn (1 to 5 a
   quarter).
2. **Cross-tabs first:** every analysis cross-tabbed two dimensions, so the
   answers led with the finest cell instead of the one dimension that
   concentrates the spike (region: 44 of 67).
3. **Late refusal:** D's plan combined a metric and a comparison mode that
   the tool refuses, and found out only at run time.

**Judge spot-check:** for A and C, the "segment as main driver" verdict is
debatable, since both name APAC first. It does not change the outcome:
fact 2 is missing from every answer.

**General fixes (future work):**

1. Check metric/mode compatibility while validating the MetricPlan, so a
   refusal triggers the one schema retry instead of costing a step.
2. For a count without components, run `contribution_by_dimension` over
   every available dimension and report the dimension whose largest
   contributor holds the biggest share, before any cross-tab.
3. For a spike or dip question, add the previous periods as a baseline.

### ha17 and ha18 (ambiguous): "most valuable segment", "most popular product"

**What each answer did:** all three architectures chose an accepted
reading, with the correct table, the metric named and the item named:

- **ha17:** all-time completed revenue, giving SMB, 9,174,303.86.
- **ha18:** all-time units, giving product 14 Vertex Nimbus, 1,926.

**Why it failed:** no answer stated an assumption, and the ambiguous-
question rule requires one. C's follow-up decision did not flag the
ambiguity, so C and D stayed direct (missed escalations). The shared answer
instruction ("if the question could reasonably be read more than one way,
say which reading ...") was not followed. ha16 ("best month"), by
contrast, passed for all three.

**General fix (future work):** this belongs in the shared SQL and answer
steps (it would change A too, so it must be measured on a new holdout):

- the SQL step returns the reading it chose whenever the question leaves
  the metric or period undefined, and the answer step must state it; or
- a deterministic rule: an evaluative superlative with no metric ("most
  valuable", "most popular", "best", "top" without "by ...") requires an
  "Assuming ..." sentence built from the result's metric column and its time
  window.

### ha20 (conditional): refund-rate comparison, then a branch

**What each answer did:** all three ran one query that evaluated the
condition and both branches through a UNION. The unused branch added a row
`OVERALL | NULL`, so 5 rows against the gold's 4. The answer named the
by-region branch but not the channel. C and D stayed direct: the only NULL
signal fires when every value is NULL.

**Why it failed:** a row produced by the branch that does not apply stayed
in the result.

**General fixes (future work):**

1. A deterministic signal for a row whose measure columns are all NULL
   while other rows have values, leading to a repair that drops it.
2. For conditional questions, keep the condition's values in the final
   table, so the answer states the branch from data rather than a guess.
   The same fix covers ha19's wrong branch name.

## Disclosures

- **Holdout texts were read before the holdout run.** I (the implementing
  assistant) read every holdout question text and note on 2026-10-05, while
  screening dev2 against them (the protocol's screening rules; no gold
  values were used for tuning). The question author is also the agent's
  author, as recorded in the protocol.
  - Everything in the table below was written after that reading.
  - The forecast signal's wording includes "straight-line trend", a phrase
    also used by ha11 and ha12. Both are routed by other alternatives too
    ("trend line", "forecast"), so their routing does not depend on it.
  - No rule names a holdout question, value or fact.
- **Changes after pre-registration:** amendments 1 (ha11 x), 2 (per-fact
  judge) and 3 (persistent provider errors not evaluated), all recorded in
  `PROTOCOL.md` before the holdout run.
- **Rules and tools added after a development failure** (all before the
  holdout run):

  | addition | added after | used on the holdout |
  | --- | --- | --- |
  | `decompose_change`, `contribution_by_dimension` | showcase sc03 (the why-question gap; general methods) | ha07, ha08, ha09 |
  | why-questions open with a MetricPlan; stacked metric tables; every component labelled; answer guidance | showcase sc03 | ha07, ha08, ha09 |
  | plain-language component names; driver sentence | showcase sc03 | ha07, ha09 (sentence not needed) |
  | forecast signal (C and D) | dev2 dv03 | ha11, ha12 |
  | TrendPlan; period-number x with x_convention; labelled forecasts | dev2 dv03 | ha11 (zscore against trend), ha12 (forecast) |
  | ERROR records, 503 retries, amendment 3 | dev2 dv03 (provider 503) | not triggered |
  | deterministic metric SQL | found while building the parity test | ha07-ha09 prompts reproducible |
  | fallback safety (A's retry first, step-1 fallback, A's answer on A's result) | review of failure paths | ha10 (A's retry), ha03 (A's answer) |
  | forced-escalation switch | dev2 smoke test | refused on holdouts; not used |

## Limitations

- **Small n:** 21 + 12 questions. Per-type results (n = 3) are descriptive.
  The 3-0 win is far from significance.
- **Most of D's edge is C's repair:** on these holdouts, D's own tools
  decided one question (ha09). The other two wins came from the repair step
  shared with C.
- **Weak spots that remain:** comparing a change across groups (ha07),
  attributing a spike to one dimension against a baseline (ha08), stating
  the reading of an ambiguous question (ha17, ha18), and leftover NULL rows
  from conditional queries (ha20).
- **Answer-text checks for conditional branches:** they can pass a wrong
  branch name (ha19). The strict-naming sensitivity above shows the
  primary comparison is unaffected and the D-vs-B secondary one is not
  robust to it.
- **Same model as judge:** the judge is the model under test, behind
  rule-based driver checks. Verdicts were spot-checked (ha07, ha08, ha09);
  no verdict changed an outcome.

## Per-question tables

Generated by `scripts/report_v2.py`, one per run, in `eval/results/phase5/`:
`phase5-holdout-agent/report.md`, `phase5-holdout-easy/report.md`,
`phase5-holdout-agent-v1/report.md` and `phase5-holdout-easy-v1/report.md`.
