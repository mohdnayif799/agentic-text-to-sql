# Run phase5-holdout-easy

Set `holdout_easy`; model `gemini-3.5-flash-lite`, temperature 0.0, today 2026-10-04, max steps 3, max attempts 3; code `b868f38`; complete: True.

## Summary

| arch | primary | Wilson 95% | avg LLM calls | avg latency s (as if live) | by type |
| --- | --- | --- | --- | --- | --- |
| A baseline | 12/12 | 0.76-1.00 | 2.0 | 3.85 (3.85) | easy 12/12 |
| C router | 12/12 | 0.76-1.00 | 3.0 | 1.16 (4.91) | easy 12/12 |
| D full | 12/12 | 0.76-1.00 | 3.0 | 0.04 (4.91) | easy 12/12 |

## Paired comparison against A

| arch | questions | both pass | neither | only A | only this arch | exact McNemar p | not worse than A |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C router | 12 | 12 | 0 | 0 | 0 | 1.000 | yes |
| D full | 12 | 12 | 0 | 0 | 0 | 1.000 | yes |

## Per question

| question | type | arch | result | first failing check | LLM calls (live) | latency s (as if live) | path | tools by step | repairs fix/break |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| he01 | easy | A baseline | PASS | - | 2 (2) | 3.30 (3.30) | direct | run_sql | 0: 0/0 |
| he01 | easy | C router | PASS | - | 3 (1) | 0.97 (3.23) | direct | run_sql | 0: 0/0 |
| he01 | easy | D full | PASS | - | 3 (0) | 0.06 (3.24) | direct | run_sql | 0: 0/0 |
| he02 | easy | A baseline | PASS | - | 2 (2) | 2.15 (2.15) | direct | run_sql | 0: 0/0 |
| he02 | easy | C router | PASS | - | 3 (1) | 1.04 (3.17) | direct | run_sql | 0: 0/0 |
| he02 | easy | D full | PASS | - | 3 (0) | 0.03 (3.14) | direct | run_sql | 0: 0/0 |
| he03 | easy | A baseline | PASS | - | 2 (2) | 2.63 (2.63) | direct | run_sql | 0: 0/0 |
| he03 | easy | C router | PASS | - | 3 (1) | 1.25 (3.87) | direct | run_sql | 0: 0/0 |
| he03 | easy | D full | PASS | - | 3 (0) | 0.03 (3.86) | direct | run_sql | 0: 0/0 |
| he04 | easy | A baseline | PASS | - | 2 (2) | 2.36 (2.36) | direct | run_sql | 0: 0/0 |
| he04 | easy | C router | PASS | - | 3 (1) | 0.89 (3.24) | direct | run_sql | 0: 0/0 |
| he04 | easy | D full | PASS | - | 3 (0) | 0.03 (3.24) | direct | run_sql | 0: 0/0 |
| he05 | easy | A baseline | PASS | - | 2 (2) | 2.90 (2.90) | direct | run_sql | 0: 0/0 |
| he05 | easy | C router | PASS | - | 3 (1) | 1.03 (3.90) | direct | run_sql | 0: 0/0 |
| he05 | easy | D full | PASS | - | 3 (0) | 0.05 (3.89) | direct | run_sql | 0: 0/0 |
| he06 | easy | A baseline | PASS | - | 2 (2) | 2.48 (2.48) | direct | run_sql | 0: 0/0 |
| he06 | easy | C router | PASS | - | 3 (1) | 0.91 (3.38) | direct | run_sql | 0: 0/0 |
| he06 | easy | D full | PASS | - | 3 (0) | 0.03 (3.37) | direct | run_sql | 0: 0/0 |
| he07 | easy | A baseline | PASS | - | 2 (2) | 2.29 (2.29) | direct | run_sql | 0: 0/0 |
| he07 | easy | C router | PASS | - | 3 (1) | 0.95 (3.23) | direct | run_sql | 0: 0/0 |
| he07 | easy | D full | PASS | - | 3 (0) | 0.03 (3.22) | direct | run_sql | 0: 0/0 |
| he08 | easy | A baseline | PASS | - | 2 (2) | 3.10 (3.10) | direct | run_sql | 0: 0/0 |
| he08 | easy | C router | PASS | - | 3 (1) | 0.94 (4.03) | direct | run_sql | 0: 0/0 |
| he08 | easy | D full | PASS | - | 3 (0) | 0.03 (4.01) | direct | run_sql | 0: 0/0 |
| he09 | easy | A baseline | PASS | - | 2 (2) | 16.95 (16.95) | direct | run_sql | 0: 0/0 |
| he09 | easy | C router | PASS | - | 3 (1) | 1.34 (18.27) | direct | run_sql | 0: 0/0 |
| he09 | easy | D full | PASS | - | 3 (0) | 0.13 (18.36) | direct | run_sql | 0: 0/0 |
| he10 | easy | A baseline | PASS | - | 2 (2) | 2.66 (2.66) | direct | run_sql | 0: 0/0 |
| he10 | easy | C router | PASS | - | 3 (1) | 1.12 (3.77) | direct | run_sql | 0: 0/0 |
| he10 | easy | D full | PASS | - | 3 (0) | 0.02 (3.75) | direct | run_sql | 0: 0/0 |
| he11 | easy | A baseline | PASS | - | 2 (2) | 2.83 (2.83) | direct | run_sql | 0: 0/0 |
| he11 | easy | C router | PASS | - | 3 (1) | 1.27 (4.07) | direct | run_sql | 0: 0/0 |
| he11 | easy | D full | PASS | - | 3 (0) | 0.03 (4.04) | direct | run_sql | 0: 0/0 |
| he12 | easy | A baseline | PASS | - | 2 (2) | 2.59 (2.59) | direct | run_sql | 0: 0/0 |
| he12 | easy | C router | PASS | - | 3 (1) | 2.23 (4.81) | direct | run_sql | 0: 0/0 |
| he12 | easy | D full | PASS | - | 3 (0) | 0.03 (4.80) | direct | run_sql | 0: 0/0 |

## Escalation against A (help / harm)

No C or D run escalated.

## Missed escalations (questions A failed)

A failed no question.

## Provider errors and resumes (pre-run amendment 3)

No question hit a persistent provider error.
