# Run fixes-nonprimary-holdout-easy

Set `holdout_easy`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `f9e34a9`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 12/12 | 0.76-1.00 | 2.0 | 1.93 (1.93) | easy 12/12 |
| D full | 12/12 | 0.76-1.00 | 3.0 | 0.82 (2.68) | easy 12/12 |

## Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D full | 12 | 12 | 0 | 0 | 0 | 1.000 | yes |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| he01 | easy | A baseline | PASS | - | 2 (2) | 2.44 (2.44) | direct | run_sql | 0: 0/0 |
| he01 | easy | D full | PASS | - | 3 (1) | 0.88 (2.70) | direct | run_sql | 0: 0/0 |
| he02 | easy | A baseline | PASS | - | 2 (2) | 1.86 (1.86) | direct | run_sql | 0: 0/0 |
| he02 | easy | D full | PASS | - | 3 (1) | 0.72 (2.56) | direct | run_sql | 0: 0/0 |
| he03 | easy | A baseline | PASS | - | 2 (2) | 1.77 (1.77) | direct | run_sql | 0: 0/0 |
| he03 | easy | D full | PASS | - | 3 (1) | 0.80 (2.56) | direct | run_sql | 0: 0/0 |
| he04 | easy | A baseline | PASS | - | 2 (2) | 1.80 (1.80) | direct | run_sql | 0: 0/0 |
| he04 | easy | D full | PASS | - | 3 (1) | 0.69 (2.48) | direct | run_sql | 0: 0/0 |
| he05 | easy | A baseline | PASS | - | 2 (2) | 2.13 (2.13) | direct | run_sql | 0: 0/0 |
| he05 | easy | D full | PASS | - | 3 (1) | 1.08 (3.19) | direct | run_sql | 0: 0/0 |
| he06 | easy | A baseline | PASS | - | 2 (2) | 1.82 (1.82) | direct | run_sql | 0: 0/0 |
| he06 | easy | D full | PASS | - | 3 (1) | 0.83 (2.64) | direct | run_sql | 0: 0/0 |
| he07 | easy | A baseline | PASS | - | 2 (2) | 1.80 (1.80) | direct | run_sql | 0: 0/0 |
| he07 | easy | D full | PASS | - | 3 (1) | 0.72 (2.51) | direct | run_sql | 0: 0/0 |
| he08 | easy | A baseline | PASS | - | 2 (2) | 1.76 (1.76) | direct | run_sql | 0: 0/0 |
| he08 | easy | D full | PASS | - | 3 (1) | 0.72 (2.47) | direct | run_sql | 0: 0/0 |
| he09 | easy | A baseline | PASS | - | 2 (2) | 2.28 (2.28) | direct | run_sql | 0: 0/0 |
| he09 | easy | D full | PASS | - | 3 (1) | 0.77 (3.04) | direct | run_sql | 0: 0/0 |
| he10 | easy | A baseline | PASS | - | 2 (2) | 1.73 (1.73) | direct | run_sql | 0: 0/0 |
| he10 | easy | D full | PASS | - | 3 (1) | 0.76 (2.49) | direct | run_sql | 0: 0/0 |
| he11 | easy | A baseline | PASS | - | 2 (2) | 2.07 (2.07) | direct | run_sql | 0: 0/0 |
| he11 | easy | D full | PASS | - | 3 (1) | 1.14 (3.15) | direct | run_sql | 0: 0/0 |
| he12 | easy | A baseline | PASS | - | 2 (2) | 1.67 (1.67) | direct | run_sql | 0: 0/0 |
| he12 | easy | D full | PASS | - | 3 (1) | 0.69 (2.34) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

No C or D run escalated.

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
