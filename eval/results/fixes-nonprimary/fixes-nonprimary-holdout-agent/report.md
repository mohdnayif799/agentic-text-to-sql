# Run fixes-nonprimary-holdout-agent

Set `holdout_agent`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `f9e34a9`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 14/21 | 0.45-0.83 | 2.1 | 2.74 (2.74) | ambiguous 1/3, multi_hop 3/3, recovery 3/3, segment_comparison 3/3, statistics 2/3, term_matching 2/3, why 0/3 |
| D full | 20/21 | 0.77-0.99 | 3.67 | 2.12 (4.43) | ambiguous 3/3, multi_hop 3/3, recovery 3/3, segment_comparison 3/3, statistics 3/3, term_matching 3/3, why 2/3 |

## Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D full | 21 | 14 | 1 | 0 | 6 | 0.031 | yes |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | multi_hop | A baseline | PASS | - | 2 (2) | 3.36 (3.36) | direct | run_sql | 0: 0/0 |
| ha01 | multi_hop | D full | PASS | - | 3 (1) | 0.79 (3.47) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | A baseline | PASS | - | 2 (2) | 2.25 (2.25) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | D full | PASS | - | 3 (1) | 1.13 (3.36) | direct | run_sql | 0: 0/0 |
| ha03 | multi_hop | A baseline | PASS | - | 2 (2) | 2.52 (2.52) | direct | run_sql | 0: 0/0 |
| ha03 | multi_hop | D full | PASS | - | 6 (5) | 5.65 (7.29) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| ha04 | term_matching | A baseline | PASS | - | 2 (2) | 1.92 (1.92) | direct | run_sql | 0: 0/0 |
| ha04 | term_matching | D full | PASS | - | 3 (1) | 0.79 (2.70) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | A baseline | PASS | - | 2 (2) | 2.24 (2.24) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | D full | PASS | - | 3 (1) | 0.82 (3.05) | direct | run_sql | 0: 0/0 |
| ha06 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Cobalt Nimbus', 'number: 913'] | 2 (2) | 2.08 (2.08) | direct | run_sql | 0: 0/0 |
| ha06 | term_matching | D full | PASS | - | 4 (3) | 3.01 (4.30) | escalated | run_sql+repair | 1: 1/0 |
| ha07 | why | A baseline | FAIL | driver not named: ["any_of: [{'pattern': 'fewer\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'order\\\\s+(volume/count/counts/numbers)'}, {'pattern': '(number/count/volume)\\\\s+of\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'orders\\\\s+(fell/dropped/declined/decreased/shrank)'}]"] | 2 (2) | 3.26 (3.26) | direct | run_sql | 0: 0/0 |
| ha07 | why | D full | PASS | - | 6 (5) | 5.76 (7.76) | escalated | run_sql > compute:decompose_change > stack | 0: 0/0 |
| ha08 | why | A baseline | FAIL | driver not named: ['text: APAC'] | 2 (2) | 2.66 (2.66) | direct | run_sql | 0: 0/0 |
| ha08 | why | D full | FAIL | driver not named: ['text: APAC'] | 5 (4) | 3.98 (5.12) | escalated | run_sql > compute:contribution_by_dimension > compute:period_history > stack | 0: 0/0 |
| ha09 | why | A baseline | FAIL | driver not named: ['pattern: \\b(units?/volume/quantit(y/ies))\\b/fewer\\s+(hardware\\s+)?(items/products/sales)'] | 2 (2) | 2.55 (2.55) | direct | run_sql | 0: 0/0 |
| ha09 | why | D full | PASS | - | 4 (3) | 3.77 (5.18) | escalated | run_sql > compute:decompose_change > compute:period_history > stack | 0: 0/0 |
| ha10 | statistics | A baseline | PASS | - | 2 (2) | 2.12 (2.12) | direct | run_sql | 0: 0/0 |
| ha10 | statistics | D full | PASS | - | 3 (1) | 0.85 (2.96) | direct | run_sql | 0: 0/0 |
| ha11 | statistics | A baseline | FAIL | expected 3 row(s), got 0; answer missing ["any_of: [{'text': '2025-07'}, {'pattern': 'jul(y)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-08'}, {'pattern': 'aug(ust)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-09'}, {'pattern': 'sep(t/tember)?\\\\.?\\\\s+2025'}]"] | 2 (2) | 3.43 (3.43) | direct | run_sql | 0: 0/0 |
| ha11 | statistics | D full | PASS | - | 4 (3) | 5.08 (7.69) | escalated | run_sql+repair > run_sql > compute:zscore_anomalies | 1: 0/0 |
| ha12 | statistics | A baseline | PASS | - | 2 (2) | 2.63 (2.63) | direct | run_sql | 0: 0/0 |
| ha12 | statistics | D full | PASS | - | 3 (2) | 2.13 (3.90) | escalated | run_sql > run_sql > compute:linear_trend_forecast | 0: 0/0 |
| ha13 | recovery | A baseline | PASS | - | 2 (2) | 1.81 (1.81) | direct | run_sql | 0: 0/0 |
| ha13 | recovery | D full | PASS | - | 3 (1) | 0.74 (2.54) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | A baseline | PASS | - | 2 (2) | 1.60 (1.60) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | D full | PASS | - | 3 (1) | 0.90 (2.49) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | A baseline | PASS | - | 2 (2) | 2.05 (2.05) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | D full | PASS | - | 3 (1) | 0.87 (2.90) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | A baseline | PASS | - | 2 (2) | 2.07 (2.07) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | D full | PASS | - | 3 (1) | 0.97 (3.02) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | A baseline | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 2 (2) | 1.92 (1.92) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | D full | PASS | - | 3 (1) | 0.68 (2.57) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | A baseline | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 2 (2) | 1.96 (1.96) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | D full | PASS | - | 3 (1) | 1.14 (3.07) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | A baseline | PASS | - | 2 (2) | 3.04 (3.04) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | D full | PASS | - | 4 (3) | 3.74 (5.76) | escalated | run_sql > run_sql > run_sql | 0: 0/0 |
| ha20 | segment_comparison | A baseline | PASS | - | 3 (3) | 4.76 (4.76) | direct | run_sql | 1: 1/0 |
| ha20 | segment_comparison | D full | PASS | - | 4 (1) | 0.92 (5.65) | escalated | run_sql+repair | 1: 1/0 |
| ha21 | segment_comparison | A baseline | PASS | - | 3 (3) | 7.35 (7.35) | direct | run_sql | 0: 0/0 |
| ha21 | segment_comparison | D full | PASS | - | 4 (1) | 0.87 (8.19) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| D full | 9 | 4 | 0 | 5 | 0 | ha03 no change, ha06 helped, ha07 helped, ha08 no change, ha09 helped, ha11 helped, ha12 no change, ha19 no change, ha20 no change |

## Missed escalations (questions A failed)

A failed 7 question(s). C escalated 0, did not escalate 0, ERROR 0, not run 7; D escalated 5, did not escalate 2, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| ha06 | not run | escalated (pass) |
| ha07 | not run | escalated (pass) |
| ha08 | not run | escalated (fail) |
| ha09 | not run | escalated (pass) |
| ha11 | not run | escalated (pass) |
| ha17 | not run | direct (pass) |
| ha18 | not run | direct (pass) |

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
