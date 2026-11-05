# Root-cause fixes for D: non-primary holdout rerun

**NON-PRIMARY. This does not replace the Phase 5 result.** The held-out result stays
as published: on `holdout_agent` A 13/21, D 16/21, D vs A 3-0, p = 0.25
([phase5_report.md](phase5_report.md)). The protocol allows one primary run on the
holdouts, and these fixes were written after reading these exact failures, so these
questions cannot measure them without bias. This rerun checks for regressions and
whether the fixes work as built. A clean number needs a new holdout.

The frozen holdouts, gold answers, grader and Phase 5 result folders are unchanged.

## Changes after this measurement

**Everything below this section measures commit `f9e34a9` and was left as written.** These
commits came later, and **none of them has been run on the holdouts**. The planned
50-question evaluation is what will measure them.

| Commit | Change | Effect on what this report describes |
| --- | --- | --- |
| `eca0692` | The conditional signal reads a decimal as part of the sentence | ha20 and ha21 should now take the conditional path, which they did not here |
| `807a027` | A table with a hollow row is left out of selection when a clean one exists | Closes problem 2 below, on ha20's path |
| `46a9aef` | Fix 6's keyword hint removed | The disclosure about the hint no longer applies; the planner now gets no wording-based help |
| `1415a3f` | A breakdown leads with the dimension that concentrates the change most | Addresses problem 3 below; ha08's all-live answer should no longer lead with segment |
| `43c7eab` | A request to change the data is refused before any LLM call or SQL (C and D, and the app) | No question in these sets is affected, as a test checks |

They were checked on the dev sets and dev probes only, from a new empty cache: D 11/11 on
`showcase` and `dev2`, A 8/11, no errors, and 63 live calls for about $0.07. Raw results
are in [fixes-nonprimary/after-measurement/](fixes-nonprimary/after-measurement/).

**That check does not exercise all of them.** No dev question has a decimal in a condition,
a table with a hollow row or a request to change data, and on the spike probe the planner
had already chosen the most concentrated dimension. Those paths are covered by offline
tests only. The hint's removal was exercised: the planner still chose the across-groups
mode on the probe, and `sc03` did not use it.

Still open: across-groups answers can overstate how alike other components moved (the
probe says average unit price changed similarly, at -13% against -7%), and the judge's false
positives on ha07 and ha08, which are recorded as graded.

## Runs

All on `gemini-3.5-flash-lite`, the pinned date and the same budgets. Raw results and
traces are in [fixes-nonprimary/](fixes-nonprimary/).

| Run | Code | What | First queries | Live calls |
| --- | --- | --- | --- | --- |
| `fixes-dev-*`, `fixes-dev2-*`, `fixes-dev-probes` | `470a70f` to `f9e34a9` | A and D on `showcase` and `dev2`, then 3 ungraded probes | new | 71 |
| `fixes-nonprimary-holdout-*` | `f9e34a9` | A and D on all 33, empty cache | new | 128 |
| `fixes-frozenfirst-holdout-*` | `f9e34a9` | D on all 33, from a copy of the Phase 5 cache | Phase 5's | 31 |

Two views of D, because the model is not deterministic (see Noise):

- **D, fixes only** reuses Phase 5's replies wherever a prompt is unchanged, so its
  first query is byte-identical to Phase 5's. Results can change only through the
  fixes, including fresh model replies to the prompts the fixes changed.
- **D, all live** starts from an empty cache, so it also has new first queries.

## Scores

| | A frozen | A live | D frozen | D fixes only | D all live |
| --- | --- | --- | --- | --- | --- |
| `holdout_agent` (21) | 13 | 14 | 16 | **18** | **20** |
| `holdout_easy` (12) | 12 | 12 | 12 | 12 | 12 |

**The fixes account for +2 (ha17, ha18).** The other +2 in the all-live run is not
reliable: ha20 passed because of a new first query, and ha07 passed in one run of two.

## Per question

Path is D's before and after the fixes, on the frozen first queries.

| Question | Type | A frozen | A live | D frozen | D fixes only | D all live | D path |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | multi_hop | pass | pass | pass | pass | pass | direct > direct |
| ha02 | multi_hop | pass | pass | pass | pass | pass | direct > direct |
| ha03 | multi_hop | pass | pass | pass | pass | pass | escalated > escalated |
| ha04 | term_matching | pass | pass | pass | pass | pass | direct > direct |
| ha05 | term_matching | pass | pass | pass | pass | pass | direct > direct |
| ha06 | term_matching | **FAIL** | **FAIL** | pass | pass | pass | escalated > escalated |
| ha07 | why | **FAIL** | **FAIL** | **FAIL** | **FAIL** | pass | escalated > escalated |
| ha08 | why | **FAIL** | **FAIL** | **FAIL** | **FAIL** | **FAIL** | escalated > escalated |
| ha09 | why | **FAIL** | **FAIL** | pass | pass | pass | escalated > escalated |
| ha10 | statistics | pass | pass | pass | pass | pass | escalated > escalated |
| ha11 | statistics | **FAIL** | **FAIL** | pass | pass | pass | escalated > escalated |
| ha12 | statistics | pass | pass | pass | pass | pass | escalated > escalated |
| ha13 | recovery | pass | pass | pass | pass | pass | direct > direct |
| ha14 | recovery | pass | pass | pass | pass | pass | direct > direct |
| ha15 | recovery | pass | pass | pass | pass | pass | direct > direct |
| ha16 | ambiguous | pass | pass | pass | pass | pass | direct > direct |
| ha17 | ambiguous | **FAIL** | **FAIL** | **FAIL** | pass | pass | direct > direct |
| ha18 | ambiguous | **FAIL** | **FAIL** | **FAIL** | pass | pass | direct > direct |
| ha19 | segment_comparison | pass | pass | pass | pass | pass | direct > escalated |
| ha20 | segment_comparison | **FAIL** | pass | **FAIL** | **FAIL** | pass | direct > escalated |
| ha21 | segment_comparison | pass | pass | pass | pass | pass | direct > direct |
| he01 | easy | pass | pass | pass | pass | pass | direct > direct |
| he02 | easy | pass | pass | pass | pass | pass | direct > direct |
| he03 | easy | pass | pass | pass | pass | pass | direct > direct |
| he04 | easy | pass | pass | pass | pass | pass | direct > direct |
| he05 | easy | pass | pass | pass | pass | pass | direct > direct |
| he06 | easy | pass | pass | pass | pass | pass | direct > direct |
| he07 | easy | pass | pass | pass | pass | pass | direct > direct |
| he08 | easy | pass | pass | pass | pass | pass | direct > direct |
| he09 | easy | pass | pass | pass | pass | pass | direct > direct |
| he10 | easy | pass | pass | pass | pass | pass | direct > direct |
| he11 | easy | pass | pass | pass | pass | pass | direct > direct |
| he12 | easy | pass | pass | pass | pass | pass | direct > direct |

## Regressions

**None.** No question D passed in Phase 5 failed in either run. No errors, and A and D
stay tied on `holdout_easy`.

- **ha09:** passed in both. Fix 5 used the step that the contribution breakdown used
  before, and the answer still names the units and price facts the rubric requires.
- **ha19:** passed in both, on the new conditional path. It is better than the score
  shows: the answer now gives both values and names SMB, where Phase 5's named
  Enterprise over SMB's figures.
- **ha21:** passed in both, but on the old path, because fix 4 did not fire (see the
  first problem below). Treat it as untested by fix 4.

## The five failures

| Question | Fixes only | All live | Status | What did it |
| --- | --- | --- | --- | --- |
| ha17 | pass | pass | **Resolved** | Fix 2: same table as before, plus the stated reading |
| ha18 | pass | pass | **Resolved** | Fix 2: same table as before, plus the stated reading |
| ha07 | FAIL | pass | **Partly** | Fix 6 finds the right driver in both runs; the prose decides the verdict |
| ha08 | FAIL | FAIL | **Not resolved** | Fixes 3 and 5 work; the planner's choice of dimension and the judge decide it |
| ha20 | FAIL | pass | **Not resolved** | The pass is noise; fix 1 fired but did not fix it, and fix 4 never fired |

ha17 and ha18 say little about the model: code adds a sentence to tables that were
already correct, and the grader's rule is that the reading must be stated.

**ha07.** The planner chose the across-groups mode in both runs. Its first plan used
`region_name`, which fix 3 rejected, and the retry cost no step. Both answers name
number of orders as the driver, which Phase 5's did not. The all-live answer gives each
region's fall and passes all three facts. The fixes-only answer gives only the others'
average (-8.18%), so fact 2 is fairly missed. The judge also marks "number of orders"
as a disqualified claim about order size, which is a wrong reading.

**ha08.** Fix 3 worked in both runs: the planner chose `churned_customers`, not
`churn_rate`, and a rejected first plan cost no step. Fix 5 put the earlier quarters in
the evidence. The rest differs by run:

- **Fixes only:** the planner chose region, and the answer states APAC, 44 of 67, the
  earlier range of 1 to 5, and that other regions did not move comparably. The judge
  passes both required facts, then marks "the main driver ... was APAC" as naming
  "another region". That is a false positive; a fair reader would pass this answer. The
  recorded verdict stands, and neither the rubric nor the judge was changed.
- **All live:** the planner chose segment, so the answer leads with SMB and never names
  APAC. This is a genuine failure. Region against segment is the model's choice, and the
  tool's own figures favour region (81% against 44% of the rise).

**ha20.** The all-live pass came from A's first query failing to parse and A's retry
returning a clean table, so A passed too. On Phase 5's first query, fix 1's signal fired
on the `OVERALL` row, but both repairs kept the row and were correctly refused. The
planner then ran a clean 4-row query, and the assembly step chose the first table anyway.

## Noise

A's code is unchanged and its prompts are byte-identical on all 33 (the prompt hashes
match Phase 5's, and a replay from the Phase 5 cache reproduced 33/33), so any change in
A is the model.

- **First queries:** 16 of 33 differ in text from Phase 5, on identical prompts.
- **A's verdicts:** 1 of 33 moved (ha20, fail to pass). The dev sets moved 2 of 11
  (sc05, dv03).
- **D between its two new runs:** 2 of 33 differ (ha07, ha20), and ha08 fails for a
  different reason in each.

So about 1 in 33 to 2 in 11 verdicts can move with no code change. With these sample
sizes, one run cannot separate a small improvement from that. Paired on `holdout_agent`,
D vs A is 6-0 (p = 0.031) all live and 5-0 (p = 0.063) on frozen first queries, against
3-0 (p = 0.25) in Phase 5. **These are not confirmatory.** Two of those wins come from
fixes written after seeing these exact failures, and the questions are no longer blind.

## Problems found and not fixed

The code is left as measured, so these results describe it. Each needs a decision.

1. **Fix 4 misses a condition with a decimal in it.** The signal's pattern stops at the
   `.` in "1.5", so ha20 and ha21 never took the conditional path. Only ha19 did.
2. **Assembly can pick a table with a known hollow row.** On ha20 a clean table existed
   at step 2, and the model chose step 1, whose `null_row` signal was still unresolved.
3. **The planner's dimension decides ha08.** Nothing checks whether another dimension
   concentrates the change more.
4. **The judge has false positives.** ha08 (fixes only) and ha07 (fixes only) above.
   Recorded as graded. The judge is the same model as the one under test, as the
   protocol discloses.

## Prose check: ha19, ha20, ha21

Every figure in D's answers matches its table or the condition's values. No answer here
repeats dv05's slip. These are not wrong figures, but they are not fully right either:

- **ha19, A live:** says Enterprise's figures exceed the threshold and gives SMB's
  revenue as Enterprise's. The grader passes it. D's answers in both runs are correct.
- **ha19, D all live:** leaks a column name, "(condition_met: 0)".
- **ha20, all three new answers:** the figures are right, but the channel is called "the
  higher channel" and is never named as Online, and the condition is assumed, not shown.
- **ha21, all three new answers:** the figures are right and sum to 741, but the condition
  is assumed, with no comparison shown, because fix 4 did not fire.

Wrong statements found outside those three, all in answers the grader passes:

- **ha07, D all live:** says units sold "changed by a similar amount in every group".
  APAC's fell 30.21% against an average of 8.91% elsewhere. It also says "falling"
  0.42% for EMEA, whose units rose.
- **dv05, D:** "28.5% compared to the Online channel's 28.5%", then the correct 0.285 and
  0.254.
- **probe-against-the-rest, D:** says units sold changed similarly (64.2% against 36.05%).

The across-groups answers tend to generalize from the first decomposition to the second.

## Dev check and probes

The dev check caught a regression fix 6 caused before any holdout call: on `sc03` the
planner gave the same period twice, and the tool returned a table of zeros. Rejecting
overlapping periods at parse time fixed it, and D went to 11/11. No wording gate was added.

The probes ([dev_probes.yaml](../dev_probes.yaml)) are ungraded, never part of a holdout,
and read by hand against the data:

- **probe-spike:** correct, with the earlier-periods table matching the data. Periods
  before the data starts were left out.
- **probe-against-the-rest:** the planner chose the across-groups mode with no keyword
  hint, and named order count. It has the units slip above.
- **probe-vague-ranking:** correct. The model stated the reading itself, so the code-built
  sentence was not needed.

## Tokens and cost

| Run | Live calls | Input tokens | Output tokens | Thinking tokens | Cost |
| --- | --- | --- | --- | --- | --- |
| Dev runs and probes | 71 | 166,753 | 13,575 | 0 | $0.084 |
| A and D, all live | 128 | 224,039 | 22,627 | 0 | $0.124 |
| D, fixes only | 31 | 87,766 | 6,413 | 0 | $0.042 |
| **Total** | **230** | **478,558** | **42,615** | **0** | **$0.25** |

Counted from the response caches, one entry per live call, judge calls included. Priced at
[$0.30 per 1M input and $2.50 per 1M output tokens](https://ai.google.dev/gemini-api/docs/pricing),
with thinking billed as output. The API reported 0 thinking tokens on every call. The
plan was about 275 calls.

On `holdout_agent`, D makes 3.67 calls per question all live (the same as Phase 5) and 4.0
on frozen first queries; latency as if live is 4.4 s and 6.1 s, against 5.7 s in Phase 5.

## Disclosures

- **The fixes came from these failures.** Each is general in its wording and tested on
  other data, but the idea for each came from reading these holdout questions.
- **One keyword hint remains.** Fix 6 adds a sentence to the planner's prompt when the
  question's wording is about one group against "the others". It is a hint, not a gate:
  the mode is offered on every why-question, and the probe chose it without the hint.
- **C changed too.** Fixes 1, 2 and 4 live in code C and D share. C's Phase 5 scores no
  longer describe current C, and C was not rerun.
- **A and B are unchanged.** A's and B's code has no diff, and both replay 33/33 from the
  Phase 5 cache with identical verdicts, answers and SQL.
