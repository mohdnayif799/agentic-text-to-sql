# Evaluation

## What is measured

| Metric | Meaning |
|---|---|
| Task success | Agent's result matches the reference query's result |
| SQL execution success | A query ran without error and returned rows |
| Repairs | Times the agent recovered from a failure |
| Median latency | Wall clock per question |
| Total LLM calls / tokens | Cost of the architecture |
| Tables mutated | Must be zero; asserted, not just reported |

## How grading works

Each question in `eval/questions.yaml` carries a **reference SQL query**. The
harness runs that query itself against the same database and compares the
agent's returned rows against it.

Four check types:

- `scalar` — the agent's result must contain the reference number, with an
  optional relative tolerance for floating-point aggregates.
- `top_label` — the highest-ranked label must match. Order-sensitive, which is
  what makes "which region lost the *most*" a real test.
- `label_set` — the reference's label set must be present in the agent's.
- `nonempty` — used for open-ended diagnostics where many shapes are valid.

Two things are deliberately *not* done:

- **No hardcoded expected values.** They rot the moment the seed data changes
  and nobody can verify a number typed into YAML.
- **No LLM judge.** Grading is execution-based and deterministic, so the score
  is reproducible and auditable.

Two questions (`a01`, ambiguity handling) have no machine-checkable ground
truth. They are run and reported under **manual review** rather than being
silently counted as passes.

## The baseline

`agentcrew/baseline.py` is a single-agent text-to-SQL implementation, and
it is deliberately strong:

- same model, same temperature;
- the same deterministically-selected schema card (schema selection is
  infrastructure, not part of the orchestration claim, so both arms get it);
- the same read-only guard and row cap;
- one retry if its query fails, so it is not penalised for a typo.

What it lacks is exactly what is under test: multi-step planning,
deterministic failure triage, and semantic verification.

Weakening the baseline would make the agent look better and teach nothing.
If the orchestration does not pay for itself on a question class, the harness
should say so — that finding is more useful than a flattering number.

## Running it

```bash
python scripts/run_eval.py                      # both arms, 18 questions
python scripts/run_eval.py --arm agent
python scripts/run_eval.py --ids q04 q11 q12
python scripts/run_eval.py --provider fake      # harness self-test, offline
```

Results are written to `eval/results.json` with per-question detail.

### About `--provider fake`

This replays the reference SQL through the full graph. A green run proves the
**harness** works — grading, comparison, budget accounting, the safety
re-check. It proves **nothing** about SQL quality, because no model was
consulted. Real numbers require a real provider.

The self-test run does surface one real signal, though: with both arms
producing identical SQL, the agent consumed **68 LLM calls and ~42.9k tokens**
against the baseline's **17 calls and ~11.7k tokens** — a 4× call overhead
and 3.7× token overhead that buys nothing when the first query is already
correct. That is the cost the orchestration has to earn back on harder
questions, and it is the honest framing for any comparison.

## Expected shape of results

Predictions written before running with a real model, so they can be checked:

- **Easy questions (q01–q03, q13).** The baseline should match the agent. One
  correct query is one correct query; verification and planning add cost and
  nothing else. If the agent "wins" here, something is wrong with the baseline.
- **Medium questions (q04–q08, q14).** Roughly comparable, with the agent
  ahead where the first query is likely to be subtly wrong — filtering on
  `status`, or getting the churn date window right.
- **Hard and diagnostic questions (q09–q12, q15).** Where the agent should
  win, for two reasons: multi-step decomposition on q12, and the verifier
  catching wrong-metric answers on q09 and q11 (churn *rate* vs churn *count*
  is the classic failure).
- **Safety probes (s01, s02).** Both arms should score clean, because the
  guard is shared. This tests the guard, not the orchestration.

If the hard-question gap turns out to be small, the honest conclusion is that
the extra stages are not worth their cost for this workload — and the
architecture should shrink accordingly.

## Reproducibility

- The database is generated from a fixed RNG seed (`SEED = 20260215`), so
  every developer gets identical data.
- Reference queries are versioned with the questions.
- Model temperature is 0 by default.
- Agent runs remain non-deterministic despite temperature 0, because providers
  do not guarantee it. Treat single-run differences of one or two questions as
  noise; run the set more than once before drawing conclusions.
