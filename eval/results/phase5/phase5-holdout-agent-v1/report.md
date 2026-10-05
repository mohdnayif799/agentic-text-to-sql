# Run phase5-holdout-agent-v1

Set `holdout_agent`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `adff209`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| B v1 | 9/21 | 0.24-0.64 | 5.9 | 11.02 (11.02) | ambiguous 0/3, multi_hop 1/3, recovery 3/3, segment_comparison 2/3, statistics 1/3, term_matching 2/3, why 0/3 |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | multi_hop | B v1 | PASS | - | 6 (6) | 9.36 (9.36) | v1 | run_sql > run_sql | 0: 0/0 |
| ha02 | multi_hop | B v1 | FAIL | no result column matches gold column(s) [0]; first gold row [748889.067]; answer missing ['number: 748889.067'] | 8 (8) | 13.53 (13.53) | v1 | run_sql > run_sql | 0: 0/0 |
| ha03 | multi_hop | B v1 | FAIL | no result column matches gold column(s) [2]; first gold row [25, 'Anchor Prism', 1075]; answer missing ['number: 1075'] | 8 (8) | 13.68 (13.68) | v1 | run_sql > run_sql | 0: 0/0 |
| ha04 | term_matching | B v1 | PASS | - | 4 (4) | 5.87 (5.87) | v1 | run_sql | 0: 0/0 |
| ha05 | term_matching | B v1 | PASS | - | 4 (4) | 5.22 (5.22) | v1 | run_sql | 0: 0/0 |
| ha06 | term_matching | B v1 | FAIL | expected 1 row(s), got 5; answer missing ['number: 913'] | 6 (6) | 58.33 (58.33) | v1 | run_sql | 0: 0/0 |
| ha07 | why | B v1 | FAIL | driver not named: ["any_of: [{'pattern': 'fewer\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'order\\\\s+(volume/count/counts/numbers)'}, {'pattern': '(number/count/volume)\\\\s+of\\\\s+(completed\\\\s+)?orders'}, {'pattern': 'orders\\\\s+(fell/dropped/declined/decreased/shrank)'}]"] | 8 (8) | 13.38 (13.38) | v1 | run_sql > run_sql > run_sql | 0: 0/0 |
| ha08 | why | B v1 | FAIL | driver named; judge fail | 10 (10) | 15.00 (15.00) | v1 | run_sql > run_sql > run_sql | 0: 0/0 |
| ha09 | why | B v1 | FAIL | driver named; judge fail | 8 (8) | 12.89 (12.89) | v1 | run_sql > run_sql > run_sql | 0: 0/0 |
| ha10 | statistics | B v1 | PASS | - | 5 (5) | 7.29 (7.29) | v1 | run_sql | 0: 0/0 |
| ha11 | statistics | B v1 | FAIL | expected 3 row(s), got 7; answer missing ["any_of: [{'text': '2025-07'}, {'pattern': 'jul(y)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-08'}, {'pattern': 'aug(ust)?\\\\.?\\\\s+2025'}]", "any_of: [{'text': '2025-09'}, {'pattern': 'sep(t/tember)?\\\\.?\\\\s+2025'}]"] | 8 (8) | 10.62 (10.62) | v1 | run_sql | 0: 0/0 |
| ha12 | statistics | B v1 | FAIL | no selected result; answer missing ['number: 1220726.8511123192'] | 5 (5) | 7.04 (7.04) | v1 | - | 0: 0/0 |
| ha13 | recovery | B v1 | PASS | - | 4 (4) | 4.26 (4.26) | v1 | run_sql | 0: 0/0 |
| ha14 | recovery | B v1 | PASS | - | 4 (4) | 4.66 (4.66) | v1 | run_sql | 0: 0/0 |
| ha15 | recovery | B v1 | PASS | - | 4 (4) | 4.82 (4.82) | v1 | run_sql | 0: 0/0 |
| ha16 | ambiguous | B v1 | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 4 (4) | 5.36 (5.36) | v1 | run_sql | 0: 0/0 |
| ha17 | ambiguous | B v1 | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 6 (6) | 8.49 (8.49) | v1 | run_sql | 0: 0/0 |
| ha18 | ambiguous | B v1 | FAIL | no interpretation matched (assumption stated: False; answer fits: none) | 4 (4) | 4.78 (4.78) | v1 | run_sql | 0: 0/0 |
| ha19 | segment_comparison | B v1 | PASS | - | 6 (6) | 9.87 (9.87) | v1 | run_sql > run_sql | 0: 0/0 |
| ha20 | segment_comparison | B v1 | PASS | - | 8 (8) | 12.22 (12.22) | v1 | run_sql > run_sql | 0: 0/0 |
| ha21 | segment_comparison | B v1 | FAIL | expected 12 row(s), got 2 | 4 (4) | 4.70 (4.70) | v1 | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

No C or D run escalated.

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
