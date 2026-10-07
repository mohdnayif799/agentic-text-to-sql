# Phase 5 plan: final evaluation on the frozen holdouts

Proposed for approval. Nothing runs on a holdout before the go-ahead. Rules
come from `eval/PROTOCOL.md` (protocol v2, with pre-run amendments 1-3).

## Before the first holdout call

- **Code:** frozen at the commit that adds this plan (D as in the final
  Phase 4 report). The working tree must be clean; each results file
  records `code_commit`. No code change between the two holdout runs. A bug
  found mid-run is reported and waits for the author's decision; the
  primary result is never replaced.
- **Configuration:** `eval/run_config.yaml` unchanged: gemini-3.5-flash-lite,
  temperature 0, today 2026-10-04, at most 3 steps and 3 attempts per step.
- **Freeze:** `FREEZE.sha256` and `FREEZE_v2.sha256` pass (checked in a
  temporary worktree outside OneDrive).
- **Cache:** it holds no holdout entries (checked: 0), so every holdout call
  is live. Within a run, C and D reuse A's step 0 and answers, and D reuses
  C's decision and repairs, through the cache.

## Commands and planned live calls

Run in the project PowerShell window. The runner's printed estimate ignores
the cache (it prints 198 / 618 for holdout_agent and 96 / 336 for
holdout_easy). The numbers below count it.

| # | command | typical live calls | at most |
| --- | --- | --- | --- |
| 1 | `.venv\Scripts\python scripts\run_eval_v2.py --set holdout_agent --arch baseline router full --run phase5-holdout-agent` | about 150 | about 550 |
| 2 | `.venv\Scripts\python scripts\run_eval_v2.py --set holdout_easy --arch baseline router full --run phase5-holdout-easy` | about 50 | about 290 |
| 3 (optional, B) | `.venv\Scripts\python scripts\run_eval_v2.py --set holdout_agent --arch v1 --run phase5-holdout-agent-b` | about 140 | about 430 |
| 4 (optional, B) | `.venv\Scripts\python scripts\run_eval_v2.py --set holdout_easy --arch v1 --run phase5-holdout-easy-b` | about 70 | about 240 |

**How the estimates are built (per question):**

- **A:** 2 calls (3 with its retry).
- **C:** 1 follow-up decision, none on the two trend-fit questions, where
  the forecast signal skips it. Escalated: about 3 more (follow-up queries,
  assembly, answer; the answer costs nothing when it ends on A's own
  result).
- **D:** 0 when direct. Escalated: about 3.5 (why-question: plan, next step,
  answer; trend fit: plan, answer; other: two next-step calls, assembly,
  answer).
- **Judge:** why-questions only: one call per required fact plus one, for
  each answer that names the driver; at most 30 on holdout_agent.
- **Escalation rate assumed:** about half of holdout_agent and about 2 of 12
  on holdout_easy. On the development sets it was 3 of 11.
- **"At most":** every question escalates, with every repair and schema
  retry. **B:** 5 to 7 calls per question, as on the showcase.

**Split across days** (free tier, about 500 requests a day; a daily-limit
stop saves progress, and the same command resumes it later):

- **Day 1:** command 1, the primary comparison, first. If it completes with
  at least about 300 calls of the day left (the CLI prints live calls per
  question), run command 2 the same day.
- **Day 2:** rerun any command that did not complete. It resumes, and
  cached responses cost nothing. Pairs left "not evaluated" by a persistent
  provider error (amendment 3) resume here too. Then run command 2 if it
  has not run yet.
- **Day 3 (only if quota allows):** commands 3 and 4 (B). They use separate
  run folders, because a run folder holds one architecture set; B's paired
  comparison with A combines the two results files (same questions, same
  gold).
- **Two-day rule:** a pair that still hits a persistent provider error on
  resumes made on two different days is scored as a fail, as recorded in
  the protocol.

At most two full runs: the second only to finish an interrupted run, or as a
cache-bypassed repeat to measure run-to-run variance. Both would be
reported, and neither replaces the primary result.

## What the Phase 5 report contains

All of it is produced by `scripts/report_v2.py` (its analysis code is fixed
now, before the run), plus the two analyses below.

- **Primary, D vs A on holdout_agent:**
  - accuracy with Wilson 95% intervals;
  - the paired table (both, neither, only A, only D) and the exact McNemar
    p;
  - results per type.
- **The verdict, stated plainly:** whether D beats A. Significance needs at
  least 6-0 or 8-1 discordant pairs (p < 0.05); a smaller win is reported
  as a win in this sample, without significance.
- **Easy set:** D must not be worse than A (questions only A passes are no
  more than questions only D passes), with McNemar p.
- **Secondary:** C vs A (and B vs A if run), on the same terms.
- **Per question and architecture:** pass or fail with the first failing
  check, LLM calls (live), latency (measured and as if live), the direct or
  escalated path, tools by step, and repair fix/break counts. Average LLM
  calls and latency per question for each architecture.
- **Escalation and errors:** escalation help/harm against A, missed
  escalations, ERRORs with their type and stage, and every question resumed
  after a provider error.
- **Disclosures:**
  - every change after pre-registration (amendments 1-3);
  - the parts of D added after a development failure, from the table in
    the final Phase 4 report. That includes `decompose_change` and
    `contribution_by_dimension`, added after the showcase revealed the
    why-question gap; they are general methods.
- **Raw per-question outcomes:** under `eval/results/phase5/`: each run's
  `results.json`, its redacted traces and the generated report.

## Separate post-run analysis: conditional branches (ha19-ha21)

The answer-text check of a conditional question can pass an answer that
names the wrong branch (seen on dev2 dv05). This analysis is reported
separately from the score, as a known limitation of that check. It does not
change any grade, and no holdout check is opened or amended for it.

The method is fixed now and applied after the primary runs are saved:

1. **Supported branch:** the condition part of each question's gold query
   is run on the database, and the branch it selects is recorded with its
   numbers.
2. **Branch each answer names,** for every architecture (A, C, D; B if run),
   classified as one of:
   - names the supported branch;
   - names the other branch;
   - names no branch;
   - names both or contradicts itself.

   Each classification is recorded with the verbatim sentence it rests on.
   The adjudicator is Claude (the assistant that built the agent), as in
   protocol v1's refusal rubric.
3. **The graded table:** whether the result table is the supported branch's
   table. That part is already graded by the score.
4. **Reported as one table:** question x architecture with the answer's
   branch, the supported branch, match yes/no and the quote, next to (not
   inside) the scores.

## After Phase 5

Phase 6 (integration) as planned: Streamlit uses D and shows the path and
the tools of each step; the demo limits stay enforced on the server; ruff,
pytest and docker build pass; every changed or added file on the branch is
listed. No push, no deploy.
