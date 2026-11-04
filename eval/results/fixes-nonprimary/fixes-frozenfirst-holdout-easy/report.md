# Run fixes-frozenfirst-holdout-easy

Set `holdout_easy`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `f9e34a9`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| D full | 12/12 | 0.76-1.00 | 3.0 | 0.01 (4.88) | easy 12/12 |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| he01 | easy | D full | PASS | - | 3 (0) | 0.03 (3.21) | direct | run_sql | 0: 0/0 |
| he02 | easy | D full | PASS | - | 3 (0) | 0.01 (3.12) | direct | run_sql | 0: 0/0 |
| he03 | easy | D full | PASS | - | 3 (0) | 0.00 (3.83) | direct | run_sql | 0: 0/0 |
| he04 | easy | D full | PASS | - | 3 (0) | 0.01 (3.21) | direct | run_sql | 0: 0/0 |
| he05 | easy | D full | PASS | - | 3 (0) | 0.01 (3.86) | direct | run_sql | 0: 0/0 |
| he06 | easy | D full | PASS | - | 3 (0) | 0.00 (3.35) | direct | run_sql | 0: 0/0 |
| he07 | easy | D full | PASS | - | 3 (0) | 0.01 (3.19) | direct | run_sql | 0: 0/0 |
| he08 | easy | D full | PASS | - | 3 (0) | 0.01 (3.99) | direct | run_sql | 0: 0/0 |
| he09 | easy | D full | PASS | - | 3 (0) | 0.01 (18.24) | direct | run_sql | 0: 0/0 |
| he10 | easy | D full | PASS | - | 3 (0) | 0.00 (3.73) | direct | run_sql | 0: 0/0 |
| he11 | easy | D full | PASS | - | 3 (0) | 0.01 (4.02) | direct | run_sql | 0: 0/0 |
| he12 | easy | D full | PASS | - | 3 (0) | 0.00 (4.78) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

No C or D run escalated.

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
