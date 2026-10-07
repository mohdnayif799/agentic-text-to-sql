# Run phase5-holdout-easy-v1

Set `holdout_easy`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `adff209`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| B v1 | 12/12 | 0.76-1.00 | 4.0 | 5.12 (5.12) | easy 12/12 |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| he01 | easy | B v1 | PASS | - | 4 (4) | 6.72 (6.72) | v1 | run_sql | 0: 0/0 |
| he02 | easy | B v1 | PASS | - | 4 (4) | 4.75 (4.75) | v1 | run_sql | 0: 0/0 |
| he03 | easy | B v1 | PASS | - | 4 (4) | 4.58 (4.58) | v1 | run_sql | 0: 0/0 |
| he04 | easy | B v1 | PASS | - | 4 (4) | 4.48 (4.48) | v1 | run_sql | 0: 0/0 |
| he05 | easy | B v1 | PASS | - | 4 (4) | 5.83 (5.83) | v1 | run_sql | 0: 0/0 |
| he06 | easy | B v1 | PASS | - | 4 (4) | 4.60 (4.60) | v1 | run_sql | 0: 0/0 |
| he07 | easy | B v1 | PASS | - | 4 (4) | 4.91 (4.91) | v1 | run_sql | 0: 0/0 |
| he08 | easy | B v1 | PASS | - | 4 (4) | 4.91 (4.91) | v1 | run_sql | 0: 0/0 |
| he09 | easy | B v1 | PASS | - | 4 (4) | 6.00 (6.00) | v1 | run_sql | 0: 0/0 |
| he10 | easy | B v1 | PASS | - | 4 (4) | 4.58 (4.58) | v1 | run_sql | 0: 0/0 |
| he11 | easy | B v1 | PASS | - | 4 (4) | 5.55 (5.55) | v1 | run_sql | 0: 0/0 |
| he12 | easy | B v1 | PASS | - | 4 (4) | 4.53 (4.53) | v1 | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

No C or D run escalated.

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
