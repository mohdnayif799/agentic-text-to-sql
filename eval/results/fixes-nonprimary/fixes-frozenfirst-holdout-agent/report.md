# Run fixes-frozenfirst-holdout-agent

Set `holdout_agent`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `f9e34a9`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| D full | 18/21 | 0.65-0.95 | 4.0 | 1.4 (6.09) | ambiguous 3/3, multi_hop 3/3, recovery 3/3, segment_comparison 2/3, statistics 3/3, term_matching 3/3, why 1/3 |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | multi_hop | D full | PASS | - | 3 (0) | 0.04 (4.10) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | D full | PASS | - | 3 (0) | 0.01 (3.52) | direct | run_sql | 0: 0/0 |
| ha03 | multi_hop | D full | PASS | - | 6 (4) | 5.97 (8.86) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| ha04 | term_matching | D full | PASS | - | 3 (0) | 0.01 (4.69) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | D full | PASS | - | 3 (0) | 0.01 (3.93) | direct | run_sql | 0: 0/0 |
| ha06 | term_matching | D full | PASS | - | 4 (0) | 0.07 (5.00) | escalated | run_sql+repair | 1: 1/0 |
| ha07 | why | D full | FAIL | driver named; judge fail | 7 (4) | 4.46 (9.01) | escalated | run_sql+repair > compute:decompose_change > stack | 1: 0/0 |
| ha08 | why | D full | FAIL | driver named; judge fail | 5 (3) | 3.84 (6.23) | escalated | run_sql > compute:contribution_by_dimension > compute:period_history > stack | 0: 0/0 |
| ha09 | why | D full | PASS | - | 4 (2) | 3.03 (10.87) | escalated | run_sql > compute:decompose_change > compute:period_history > stack | 0: 0/0 |
| ha10 | statistics | D full | PASS | - | 4 (0) | 0.02 (5.31) | escalated | run_sql+repair | 1: 1/0 |
| ha11 | statistics | D full | PASS | - | 4 (0) | 0.10 (8.87) | escalated | run_sql+repair > run_sql > compute:zscore_anomalies | 1: 0/0 |
| ha12 | statistics | D full | PASS | - | 3 (0) | 0.06 (5.22) | escalated | run_sql > run_sql > compute:linear_trend_forecast | 0: 0/0 |
| ha13 | recovery | D full | PASS | - | 3 (0) | 0.00 (3.14) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.04) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | D full | PASS | - | 3 (0) | 0.01 (3.15) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | D full | PASS | - | 3 (0) | 0.01 (4.01) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | D full | PASS | - | 3 (0) | 0.01 (3.38) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | D full | PASS | - | 3 (0) | 0.01 (3.16) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | D full | PASS | - | 5 (3) | 4.66 (11.63) | escalated | run_sql > run_sql > run_sql | 0: 0/0 |
| ha20 | segment_comparison | D full | FAIL | expected 4 row(s), got 5 | 8 (5) | 6.94 (13.25) | escalated | run_sql+repair > run_sql > assemble | 2: 0/0 |
| ha21 | segment_comparison | D full | PASS | - | 4 (0) | 0.02 (7.55) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| D full | 10 | 0 | 0 | 0 | 10 | ha03 unknown, ha06 unknown, ha07 unknown, ha08 unknown, ha09 unknown, ha10 unknown, ha11 unknown, ha12 unknown, ha19 unknown, ha20 unknown |

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
