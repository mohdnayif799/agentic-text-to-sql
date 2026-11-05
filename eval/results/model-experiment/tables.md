# Model experiment: tables (secondary)

Code commits: gemini-3.5-flash-lite 7cbf891; gemini-3.5-flash 7b13955; gemini-3.8-flash 7b13955.

## Scores and D vs A within each model

| Model | Split | A (Wilson 95%) | D (Wilson 95%) | Only A | Only D | D vs A, exact McNemar p |
| --- | --- | --- | --- | --- | --- | --- |
| gemini-3.5-flash-lite | All 50 | 37/50 (60-84%) | 44/50 (76-94%) | 1 | 8 | 0.039 |
| gemini-3.5-flash-lite | Old 33 | 27/33 (66-91%) | 33/33 (90-100%) | 0 | 6 | 0.031 |
| gemini-3.5-flash-lite | New 17 | 10/17 (36-78%) | 11/17 (41-83%) | 1 | 2 | 1.000 |
| gemini-3.5-flash | All 50 | 31/50 (48-74%) | 33/50 (52-78%) | 0 | 2 | 0.500 |
| gemini-3.5-flash | Old 33 | 25/33 (59-87%) | 26/33 (62-89%) | 0 | 1 | 1.000 |
| gemini-3.5-flash | New 17 | 6/17 (17-59%) | 7/17 (22-64%) | 0 | 1 | 1.000 |
| gemini-3.8-flash | All 50 | 31/50 (48-74%) | 31/50 (48-74%) | 1 | 1 | 1.000 |
| gemini-3.8-flash | Old 33 | 26/33 (62-89%) | 26/33 (62-89%) | 0 | 0 | 1.000 |
| gemini-3.8-flash | New 17 | 5/17 (13-53%) | 5/17 (13-53%) | 1 | 1 | 1.000 |

## Each model against flash-lite, same architecture

| Model | Arch | Split | Only flash-lite | Only this model | Exact McNemar p |
| --- | --- | --- | --- | --- | --- |
| gemini-3.5-flash | A | All 50 | ha11, ha12, ha19, ha20, ha21, hv01, hv08, hv09, hv15, hv16, hv17 | ha06, ha17, ha18, hv10, hv11 | 0.210 |
| gemini-3.5-flash | A | Old 33 | ha11, ha12, ha19, ha20, ha21 | ha06, ha17, ha18 | 0.727 |
| gemini-3.5-flash | A | New 17 | hv01, hv08, hv09, hv15, hv16, hv17 | hv10, hv11 | 0.289 |
| gemini-3.5-flash | D | All 50 | ha07, ha08, ha11, ha12, ha19, ha20, ha21, hv01, hv09, hv15, hv16, hv17 | hv06 | 0.003 |
| gemini-3.5-flash | D | Old 33 | ha07, ha08, ha11, ha12, ha19, ha20, ha21 | - | 0.016 |
| gemini-3.5-flash | D | New 17 | hv01, hv09, hv15, hv16, hv17 | hv06 | 0.219 |
| gemini-3.8-flash | A | All 50 | ha11, ha12, ha20, ha21, hv02, hv08, hv09, hv12, hv14, hv15, hv16, hv17 | ha06, ha17, ha18, hv10, hv11, hv13 | 0.238 |
| gemini-3.8-flash | A | Old 33 | ha11, ha12, ha20, ha21 | ha06, ha17, ha18 | 1.000 |
| gemini-3.8-flash | A | New 17 | hv02, hv08, hv09, hv12, hv14, hv15, hv16, hv17 | hv10, hv11, hv13 | 0.227 |
| gemini-3.8-flash | D | All 50 | ha07, ha08, ha09, ha11, ha12, ha20, ha21, hv02, hv09, hv12, hv14, hv15, hv16, hv17 | hv04 | 0.001 |
| gemini-3.8-flash | D | Old 33 | ha07, ha08, ha09, ha11, ha12, ha20, ha21 | - | 0.016 |
| gemini-3.8-flash | D | New 17 | hv02, hv09, hv12, hv14, hv15, hv16, hv17 | hv04 | 0.070 |

## Latency, calls and errors

| Model | Arch | Avg latency s per question | Slowest s | Avg calls | Not evaluated | ERROR |
| --- | --- | --- | --- | --- | --- | --- |
| gemini-3.5-flash-lite | A | 3.22 | 6.1 | 2.10 | 0 | 0 |
| gemini-3.5-flash-lite | D | 5.29 | 10.3 | 3.66 | 0 | 0 |
| gemini-3.5-flash | A | 17.30 | 42.8 | 2.22 | 0 | 0 |
| gemini-3.5-flash | D | 22.46 | 113.7 | 3.36 | 0 | 0 |
| gemini-3.8-flash | A | 21.59 | 38.1 | 2.17 | 4 | 0 |
| gemini-3.8-flash | D | 26.00 | 89.3 | 3.13 | 5 | 0 |

## Tokens, retries and cost

| Model | Live calls | Input tokens | Output tokens | Thinking tokens | Provider retries (503s) | Rate-limit retries | Judge calls | Cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gemini-3.5-flash-lite | 218 | 412,880 | 41,588 | 0 | not recorded | not recorded | in the calls | $0.228 |
| gemini-3.5-flash | 170 | 346,062 | 22,642 | 164,970 | 0 (0) | 0 | 12 | $0.970 (estimate) |
| gemini-3.8-flash | 147 | 273,903 | 19,397 | 109,480 | 0 (0) | 6 | 12 | $0.695 |

## Per question (easy questions shown only where a verdict differs)

| Question | gemini-3.5-flash-lite A | gemini-3.5-flash-lite D | gemini-3.5-flash A | gemini-3.5-flash D | gemini-3.8-flash A | gemini-3.8-flash D |
| --- | --- | --- | --- | --- | --- | --- |
| ha01 | pass | pass | pass | pass | pass | pass |
| ha02 | pass | pass | pass | pass | pass | pass |
| ha03 | pass | pass | pass | pass | pass | pass |
| ha04 | pass | pass | pass | pass | pass | pass |
| ha05 | pass | pass | pass | pass | pass | pass |
| ha06 | FAIL | pass | pass | pass | pass | pass |
| ha07 | FAIL | pass | FAIL | FAIL | FAIL | FAIL |
| ha08 | FAIL | pass | FAIL | FAIL | FAIL | FAIL |
| ha09 | FAIL | pass | FAIL | pass | FAIL | FAIL |
| ha10 | pass | pass | pass | pass | pass | pass |
| ha11 | pass | pass | FAIL | FAIL | FAIL | FAIL |
| ha12 | pass | pass | FAIL | FAIL | FAIL | FAIL |
| ha13 | pass | pass | pass | pass | pass | pass |
| ha14 | pass | pass | pass | pass | pass | pass |
| ha15 | pass | pass | pass | pass | pass | pass |
| ha16 | pass | pass | pass | pass | pass | pass |
| ha17 | FAIL | pass | pass | pass | pass | pass |
| ha18 | FAIL | pass | pass | pass | pass | pass |
| ha19 | pass | pass | FAIL | FAIL | pass | pass |
| ha20 | pass | pass | FAIL | FAIL | FAIL | FAIL |
| ha21 | pass | pass | FAIL | FAIL | FAIL | FAIL |
| hv01 | pass | pass | FAIL | FAIL | pass | pass |
| hv02 | pass | pass | pass | pass | FAIL | FAIL |
| hv03 | pass | pass | pass | pass | pass | pass |
| hv04 | FAIL | FAIL | FAIL | FAIL | FAIL | pass |
| hv05 | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| hv06 | FAIL | FAIL | FAIL | pass | FAIL | FAIL |
| hv07 | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| hv08 | pass | FAIL | FAIL | FAIL | FAIL | FAIL |
| hv09 | pass | pass | FAIL | FAIL | FAIL | FAIL |
| hv10 | FAIL | pass | pass | pass | pass | pass |
| hv11 | FAIL | pass | pass | pass | pass | pass |
| hv12 | pass | pass | pass | pass | FAIL | FAIL |
| hv13 | FAIL | FAIL | FAIL | FAIL | pass | not evaluated |
| hv14 | pass | pass | pass | pass | not evaluated | not evaluated |
| hv15 | pass | pass | FAIL | FAIL | not evaluated | not evaluated |
| hv16 | pass | pass | FAIL | FAIL | not evaluated | not evaluated |
| hv17 | pass | pass | FAIL | FAIL | not evaluated | not evaluated |
