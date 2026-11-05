# Post-fix rerun of the 50 questions: tables

**Not a primary result.** The primary run stays on record unchanged.

## Totals and D vs A, post-fix

| Split | A (Wilson 95%) | D (Wilson 95%) | Both | Neither | Only A | Only D | Exact McNemar p |
| --- | --- | --- | --- | --- | --- | --- | --- |
| All 50 (primary) | 37/50 (60-84%) | 45/50 (79-96%) | 37 | 5 | 0 | 8 | 0.008 |
| Old 33 (seen during development) | 27/33 (66-91%) | 31/33 (80-98%) | 27 | 2 | 0 | 4 | 0.125 |
| New 17 (unseen) | 10/17 (36-78%) | 14/17 (59-94%) | 10 | 3 | 0 | 4 | 0.125 |
| Inside the 33: holdout_agent | 15/21 (50-86%) | 19/21 (71-97%) | 15 | 2 | 0 | 4 | 0.125 |
| Inside the 33: holdout_easy | 12/12 (76-100%) | 12/12 (76-100%) | 12 | 0 | 0 | 0 | 1.000 |

Not evaluated or errored: none.

## Against the primary run

| Arch | Split | Primary | Post-fix | Regressions | Gains | Exact McNemar p |
| --- | --- | --- | --- | --- | --- | --- |
| A | All 50 | 37/50 | 37/50 | ha21, hv08, hv14 | ha17, hv10, hv13 | 1.000 |
| A | Old 33 | 27/33 | 27/33 | ha21 | ha17 | 1.000 |
| A | New 17 | 10/17 | 10/17 | hv08, hv14 | hv10, hv13 | 1.000 |
| D | All 50 | 44/50 | 45/50 | ha07, ha08 | hv04, hv08, hv13 | 1.000 |
| D | Old 33 | 33/33 | 31/33 | ha07, ha08 | - | 0.500 |
| D | New 17 | 11/17 | 14/17 | - | hv04, hv08, hv13 | 0.250 |

## Per question

| Question | Split | Type | A primary | A post-fix | D primary | D post-fix | D path | D's first failing check |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ha01 | old 33 | multi_hop | pass | pass | pass | pass | direct | - |
| ha02 | old 33 | multi_hop | pass | pass | pass | pass | direct | - |
| ha03 | old 33 | multi_hop | pass | pass | pass | pass | escalated | - |
| ha04 | old 33 | term_matching | pass | pass | pass | pass | direct | - |
| ha05 | old 33 | term_matching | pass | pass | pass | pass | direct | - |
| ha06 | old 33 | term_matching | FAIL | FAIL | pass | pass | escalated | - |
| ha07 | old 33 | why | FAIL | FAIL | pass | FAIL | escalated | driver named; judge fail |
| ha08 | old 33 | why | FAIL | FAIL | pass | FAIL | escalated | driver named; judge fail |
| ha09 | old 33 | why | FAIL | FAIL | pass | pass | escalated | - |
| ha10 | old 33 | statistics | pass | pass | pass | pass | direct | - |
| ha11 | old 33 | statistics | pass | pass | pass | pass | escalated | - |
| ha12 | old 33 | statistics | pass | pass | pass | pass | escalated | - |
| ha13 | old 33 | recovery | pass | pass | pass | pass | direct | - |
| ha14 | old 33 | recovery | pass | pass | pass | pass | direct | - |
| ha15 | old 33 | recovery | pass | pass | pass | pass | direct | - |
| ha16 | old 33 | ambiguous | pass | pass | pass | pass | direct | - |
| ha17 | old 33 | ambiguous | FAIL | pass | pass | pass | direct | - |
| ha18 | old 33 | ambiguous | FAIL | FAIL | pass | pass | direct | - |
| ha19 | old 33 | segment_comparison | pass | pass | pass | pass | escalated | - |
| ha20 | old 33 | segment_comparison | pass | pass | pass | pass | escalated | - |
| ha21 | old 33 | segment_comparison | pass | FAIL | pass | pass | escalated | - |
| he01 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he02 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he03 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he04 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he05 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he06 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he07 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he08 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he09 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he10 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he11 | old 33 | easy | pass | pass | pass | pass | direct | - |
| he12 | old 33 | easy | pass | pass | pass | pass | direct | - |
| hv01 | new 17 | multi_hop | pass | pass | pass | pass | direct | - |
| hv02 | new 17 | multi_hop | pass | pass | pass | pass | direct | - |
| hv03 | new 17 | term_matching | pass | pass | pass | pass | direct | - |
| hv04 | new 17 | term_matching | FAIL | FAIL | FAIL | pass | escalated | - |
| hv05 | new 17 | why | FAIL | FAIL | FAIL | FAIL | direct | driver not named: ["any_of: [{'text': 'SMB'}, {'pattern': 'small[-\\\\s]+(and[-\\\\s]+medium[-\\\\s]+)?business'}]"] |
| hv06 | new 17 | why | FAIL | FAIL | FAIL | FAIL | escalated | driver named; judge fail |
| hv07 | new 17 | why | FAIL | FAIL | FAIL | FAIL | escalated | driver not named: ['pattern: \\b(buying\\s+customers/buyers/customers/customer\\s+base/accounts)\\b'] |
| hv08 | new 17 | statistics | pass | FAIL | FAIL | pass | escalated | - |
| hv09 | new 17 | statistics | pass | pass | pass | pass | escalated | - |
| hv10 | new 17 | statistics | FAIL | pass | pass | pass | direct | - |
| hv11 | new 17 | recovery | FAIL | FAIL | pass | pass | escalated | - |
| hv12 | new 17 | recovery | pass | pass | pass | pass | direct | - |
| hv13 | new 17 | ambiguous | FAIL | pass | FAIL | pass | direct | - |
| hv14 | new 17 | ambiguous | pass | FAIL | pass | pass | direct | - |
| hv15 | new 17 | segment_comparison | pass | pass | pass | pass | escalated | - |
| hv16 | new 17 | segment_comparison | pass | pass | pass | pass | escalated | - |
| hv17 | new 17 | segment_comparison | pass | pass | pass | pass | direct | - |

## Calls and latency, post-fix

- A: 2.02 calls per question, 2.55 s on average, slowest 5.8 s.
- D: 3.54 calls per question, 4.19 s on average, slowest 7.8 s.

## Tokens and cost

| Run | Live calls | Input tokens | Output tokens | Thinking tokens | Cost |
| --- | --- | --- | --- | --- | --- |
| Post-fix rerun, A and D on all 50 | 213 | 395,692 | 40,483 | 0 | $0.220 |
| hv08 and hv04 checks, run 1 | 10 | 24,309 | 4,092 | 0 | $0.018 |
| hv08 and hv04 checks, run 2 | 8 | 18,699 | 3,203 | 0 | $0.014 |
| Safety set on D | 18 | 30,154 | 1,657 | 0 | $0.013 |
| Dev sets, A and D | 52 | 94,067 | 9,965 | 0 | $0.053 |
| **Total** | | | | | **$0.317** |
