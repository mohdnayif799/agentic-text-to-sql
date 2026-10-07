# Run phase5-holdout-agent

Set `holdout_agent`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `b868f38`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 13/21 | 0.41-0.79 | 2.14 | 3.76 (3.76) | ambiguous 1/3, multi_hop 3/3, recovery 3/3, segment_comparison 2/3, statistics 2/3, term_matching 2/3, why 0/3 |
| C router | 15/21 | 0.50-0.86 | 3.86 | 2.75 (6.08) | ambiguous 1/3, multi_hop 3/3, recovery 3/3, segment_comparison 2/3, statistics 3/3, term_matching 3/3, why 0/3 |
| D full | 16/21 | 0.55-0.89 | 3.67 | 1.15 (5.74) | ambiguous 1/3, multi_hop 3/3, recovery 3/3, segment_comparison 2/3, statistics 3/3, term_matching 3/3, why 1/3 |

## Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C router | 21 | 13 | 6 | 0 | 2 | 0.500 | yes |
| D full | 21 | 13 | 5 | 0 | 3 | 0.250 | yes |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | multi_hop | A baseline | PASS | - | 2 (2) | 4.21 (4.21) | direct | run_sql | 0: 0/0 |
| ha01 | multi_hop | C router | PASS | - | 3 (1) | 0.94 (4.12) | direct | run_sql | 0: 0/0 |
| ha01 | multi_hop | D full | PASS | - | 3 (0) | 0.06 (4.13) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | A baseline | PASS | - | 2 (2) | 2.62 (2.62) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | C router | PASS | - | 3 (1) | 0.95 (3.56) | direct | run_sql | 0: 0/0 |
| ha02 | multi_hop | D full | PASS | - | 3 (0) | 0.02 (3.54) | direct | run_sql | 0: 0/0 |
| ha03 | multi_hop | A baseline | PASS | - | 2 (2) | 3.07 (3.07) | direct | run_sql | 0: 0/0 |
| ha03 | multi_hop | C router | PASS | - | 6 (5) | 5.58 (7.64) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| ha03 | multi_hop | D full | PASS | - | 4 (1) | 1.26 (5.12) | escalated | run_sql | 0: 0/0 |
| ha04 | term_matching | A baseline | PASS | - | 2 (2) | 2.52 (2.52) | direct | run_sql | 0: 0/0 |
| ha04 | term_matching | C router | PASS | - | 3 (1) | 2.20 (4.71) | direct | run_sql | 0: 0/0 |
| ha04 | term_matching | D full | PASS | - | 3 (0) | 0.03 (4.71) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | A baseline | PASS | - | 2 (2) | 3.04 (3.04) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | C router | PASS | - | 3 (1) | 0.95 (3.97) | direct | run_sql | 0: 0/0 |
| ha05 | term_matching | D full | PASS | - | 3 (0) | 0.04 (3.95) | direct | run_sql | 0: 0/0 |
| ha06 | term_matching | A baseline | FAIL | expected 1 row(s), got 0; answer missing ['text: Cobalt Nimbus', 'number: 913'] | 2 (2) | 2.27 (2.27) | direct | run_sql | 0: 0/0 |
| ha06 | term_matching | C router | PASS | - | 4 (3) | 3.62 (5.04) | escalated | run_sql+repair | 1: 1/0 |
| ha06 | term_matching | D full | PASS | - | 4 (1) | 1.12 (5.00) | escalated | run_sql+repair | 1: 1/0 |
| ha07 | why | A baseline | FAIL | driver not named: ["any_of: [{'pattern': 'fewer\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'order\\\\s+(volume/count/counts/numbers)'}, {'pattern': '(number/count/volume)\\\\s+of\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'orders\\\\s+(fell/dropped/declined/decreased/shrank)'}]"] | 2 (2) | 3.31 (3.31) | direct | run_sql | 0: 0/0 |
| ha07 | why | C router | FAIL | driver not named: ["any_of: [{'pattern': 'fewer\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'order\\\\s+(volume/count/counts/numbers)'}, {'pattern': '(number/count/volume)\\\\s+of\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'orders\\\\s+(fell/dropped/declined/decreased/shrank)'}]"] | 7 (6) | 8.63 (10.38) | escalated | run_sql+repair > run_sql > run_sql > assemble | 1: 0/0 |
| ha07 | why | D full | FAIL | driver named; judge fail | 6 (3) | 5.08 (9.64) | escalated | run_sql+repair > compute:decompose_change > compute:contribution_by_dimension > stack | 1: 0/0 |
| ha08 | why | A baseline | FAIL | driver named; judge fail | 2 (2) | 2.81 (2.81) | direct | run_sql | 0: 0/0 |
| ha08 | why | C router | FAIL | driver named; judge fail | 6 (5) | 9.26 (10.80) | escalated | run_sql > run_sql > run_sql > assemble | 0: 0/0 |
| ha08 | why | D full | FAIL | driver named; judge fail | 6 (4) | 5.13 (7.52) | escalated | run_sql > compute:decompose_change > run_sql > assemble | 0: 0/0 |
| ha09 | why | A baseline | FAIL | driver not named: ['pattern: \\b(units?/volume/quantit(y/ies))\\b/fewer\\s+(hardware\\s+)?(items/products/sales)'] | 2 (2) | 7.86 (7.86) | direct | run_sql | 0: 0/0 |
| ha09 | why | C router | FAIL | driver not named: ['pattern: \\b(units?/volume/quantit(y/ies))\\b/fewer\\s+(hardware\\s+)?(items/products/sales)'] | 6 (5) | 7.35 (13.89) | escalated | run_sql > run_sql > assemble | 0: 0/0 |
| ha09 | why | D full | PASS | - | 5 (3) | 5.20 (13.03) | escalated | run_sql > compute:decompose_change > compute:contribution_by_dimension > stack | 0: 0/0 |
| ha10 | statistics | A baseline | PASS | - | 3 (3) | 4.25 (4.25) | direct | run_sql | 1: 1/0 |
| ha10 | statistics | C router | PASS | - | 4 (1) | 1.15 (5.36) | escalated | run_sql+repair | 1: 1/0 |
| ha10 | statistics | D full | PASS | - | 4 (0) | 0.06 (5.35) | escalated | run_sql+repair | 1: 1/0 |
| ha11 | statistics | A baseline | FAIL | expected 3 row(s), got 0; answer missing ["any_of: [{'text': '2025-07'}, {'pattern': 'jul(y)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-08'}, {'pattern': 'aug(ust)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-09'}, {'pattern': 'sep(t/tember)?\\\\.?\\\\s+2025'}]"] | 2 (2) | 4.59 (4.59) | direct | run_sql | 0: 0/0 |
| ha11 | statistics | C router | PASS | - | 4 (3) | 5.14 (8.08) | escalated | run_sql+repair | 1: 1/0 |
| ha11 | statistics | D full | PASS | - | 4 (2) | 3.32 (9.01) | escalated | run_sql+repair > run_sql > compute:zscore_anomalies | 1: 0/0 |
| ha12 | statistics | A baseline | PASS | - | 2 (2) | 4.23 (4.23) | direct | run_sql | 0: 0/0 |
| ha12 | statistics | C router | PASS | - | 3 (1) | 1.40 (5.58) | escalated | run_sql | 0: 0/0 |
| ha12 | statistics | D full | PASS | - | 3 (2) | 2.53 (5.28) | escalated | run_sql > run_sql > compute:linear_trend_forecast | 0: 0/0 |
| ha13 | recovery | A baseline | PASS | - | 2 (2) | 2.24 (2.24) | direct | run_sql | 0: 0/0 |
| ha13 | recovery | C router | PASS | - | 3 (1) | 0.94 (3.17) | direct | run_sql | 0: 0/0 |
| ha13 | recovery | D full | PASS | - | 3 (0) | 0.02 (3.16) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | A baseline | PASS | - | 2 (2) | 1.97 (1.97) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | C router | PASS | - | 3 (1) | 1.11 (3.08) | direct | run_sql | 0: 0/0 |
| ha14 | recovery | D full | PASS | - | 3 (0) | 0.02 (3.06) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | A baseline | PASS | - | 2 (2) | 2.24 (2.24) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | C router | PASS | - | 3 (1) | 0.98 (3.19) | direct | run_sql | 0: 0/0 |
| ha15 | recovery | D full | PASS | - | 3 (0) | 0.05 (3.18) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | A baseline | PASS | - | 2 (2) | 2.82 (2.82) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | C router | PASS | - | 3 (1) | 1.28 (4.07) | direct | run_sql | 0: 0/0 |
| ha16 | ambiguous | D full | PASS | - | 3 (0) | 0.05 (4.05) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | A baseline | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 2 (2) | 2.43 (2.43) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | C router | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 3 (1) | 1.02 (3.42) | direct | run_sql | 0: 0/0 |
| ha17 | ambiguous | D full | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 3 (0) | 0.05 (3.41) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | A baseline | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 2 (2) | 2.24 (2.24) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | C router | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 3 (1) | 1.00 (3.21) | direct | run_sql | 0: 0/0 |
| ha18 | ambiguous | D full | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 3 (0) | 0.06 (3.20) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | A baseline | PASS | - | 3 (3) | 9.35 (9.35) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | C router | PASS | - | 4 (1) | 1.76 (11.04) | direct | run_sql | 0: 0/0 |
| ha19 | segment_comparison | D full | PASS | - | 4 (0) | 0.06 (11.00) | direct | run_sql | 0: 0/0 |
| ha20 | segment_comparison | A baseline | FAIL | expected 4 row(s), got 5 | 2 (2) | 4.29 (4.29) | direct | run_sql | 0: 0/0 |
| ha20 | segment_comparison | C router | FAIL | expected 4 row(s), got 5 | 3 (1) | 1.51 (5.75) | direct | run_sql | 0: 0/0 |
| ha20 | segment_comparison | D full | FAIL | expected 4 row(s), got 5 | 3 (0) | 0.06 (5.73) | direct | run_sql | 0: 0/0 |
| ha21 | segment_comparison | A baseline | PASS | - | 3 (3) | 6.64 (6.64) | direct | run_sql | 0: 0/0 |
| ha21 | segment_comparison | C router | PASS | - | 4 (1) | 1.00 (7.61) | direct | run_sql | 0: 0/0 |
| ha21 | segment_comparison | D full | PASS | - | 4 (0) | 0.04 (7.57) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

| arch | escalated | helped | harmed | no change | unknown | questions |
| --- | --- | --- | --- | --- | --- | --- |
| C router | 8 | 2 | 0 | 6 | 0 | ha03 no change, ha06 helped, ha07 no change, ha08 no change, ha09 no change, ha10 no change, ha11 helped, ha12 no change |
| D full | 8 | 3 | 0 | 5 | 0 | ha03 no change, ha06 helped, ha07 no change, ha08 no change, ha09 helped, ha10 no change, ha11 helped, ha12 no change |

## Missed escalations (questions A failed)

A failed 8 question(s). C escalated 5, did not escalate 3, ERROR 0, not run 0; D escalated 5, did not escalate 3, ERROR 0, not run 0.

| question | C | D |
| --- | --- | --- |
| ha06 | escalated (pass) | escalated (pass) |
| ha07 | escalated (fail) | escalated (fail) |
| ha08 | escalated (fail) | escalated (fail) |
| ha09 | escalated (fail) | escalated (pass) |
| ha11 | escalated (pass) | escalated (pass) |
| ha17 | direct (fail) | direct (fail) |
| ha18 | direct (fail) | direct (fail) |
| ha20 | direct (fail) | direct (fail) |

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
