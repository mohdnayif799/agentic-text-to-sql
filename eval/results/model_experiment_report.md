# Stronger-model experiment (secondary, limited, closed)

**SECONDARY and not pre-registered. It does not measure how good these models are.** A setting in this
agent cut off a large share of their replies, and one run did not finish. **The live demo stays on
`gemini-3.5-flash-lite`.** On this evidence the stronger models are too slow for it: 22 to 26 s per
question on D, with slowest runs of 89 to 114 s, against 5.3 s and 10.3 s. No more model experiments
are planned.

A and D ran the frozen 50 at commit `7b13955`, with the same `agentcrew/` code as the primary run and the
judge left on flash-lite, so only the model under test changed. The fixes in
[postfix50_report.md](postfix50_report.md) are not included. Raw results are in
[model-experiment/](model-experiment/), generated tables in
[model-experiment/tables.md](model-experiment/tables.md).

## Why the scores do not compare

The agent allows 2,048 output tokens per call, and on these models thinking counts against that limit.
Replies were cut off mid-JSON, and the run failed before it could answer. Every one of those failed
runs had a call at the limit. Flash-lite reports no thinking and never reached it.

| Model | Runs that failed before answering | Live calls at the limit |
| --- | --- | --- |
| `gemini-3.5-flash-lite` (primary) | 0 of 100 | 0 of 199 |
| `gemini-3.5-flash` | 29 of 100 | 48 of 170 |
| `gemini-3.8-flash` | 24 of 91 | 36 of 147 |

**`gemini-3.8-flash` is also incomplete.** It stopped at hv13 when the Google project reached its monthly
spending cap, leaving 9 of 100 runs without a result. They count as not passing below, and were not
resumed because the result was already limited by the token cap.

## Scores

| Model | Split | A (Wilson 95%) | D (Wilson 95%) | Only A | Only D | D vs A, exact McNemar p |
| --- | --- | --- | --- | --- | --- | --- |
| `gemini-3.5-flash-lite` (primary) | All 50 | 37/50 (60-84%) | 44/50 (76-94%) | 1 | 8 | 0.039 |
| | Old 33 | 27/33 (66-91%) | 33/33 (90-100%) | 0 | 6 | 0.031 |
| | New 17 | 10/17 (36-78%) | 11/17 (41-83%) | 1 | 2 | 1.000 |
| `gemini-3.5-flash` | All 50 | 31/50 (48-74%) | 33/50 (52-78%) | 0 | 2 | 0.500 |
| | Old 33 | 25/33 (59-87%) | 26/33 (62-89%) | 0 | 1 | 1.000 |
| | New 17 | 6/17 (17-59%) | 7/17 (22-64%) | 0 | 1 | 1.000 |
| `gemini-3.8-flash` (incomplete) | All 50 | 31/50 (48-74%) | 31/50 (48-74%) | 1 | 1 | 1.000 |
| | Old 33 | 26/33 (62-89%) | 26/33 (62-89%) | 0 | 0 | 1.000 |
| | New 17 | 5/17 (13-53%) | 5/17 (13-53%) | 1 | 1 | 1.000 |

**Among runs that did not fail,** the stronger models passed 25 or 26 of the 26 old questions, and 5 to 7
of the 7 to 10 new ones. That is not a fair score either, because the failures fall on the longer queries.
It does show that these numbers are not evidence the models are worse. Comparing each with flash-lite on
the same architecture, in [the tables](model-experiment/tables.md), is limited in the same way.

**What the models did do better:** A passed ha06, ha17, ha18, hv10 and hv11 on both models, all of which A
failed on flash-lite. A single run cannot say how much of that is the model.

## Latency, errors and cost

| Model | Arch | Avg latency per question | Slowest | Avg calls |
| --- | --- | --- | --- | --- |
| `gemini-3.5-flash-lite` | A | 3.22 s | 6.1 s | 2.10 |
| | D | 5.29 s | 10.3 s | 3.66 |
| `gemini-3.5-flash` | A | 17.30 s | 42.8 s | 2.22 |
| | D | 22.46 s | 113.7 s | 3.36 |
| `gemini-3.8-flash` | A | 21.59 s | 38.1 s | 2.17 |
| | D | 26.00 s | 89.3 s | 3.13 |

Latency is as if every call were live, judge excluded. The failed runs still cost time, and a longer
token limit would add thinking and writing time, not remove it.

| Model | Live calls | Input tokens | Output tokens | Thinking tokens | 503s | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| `gemini-3.5-flash-lite` | 218 | 412,880 | 41,588 | 0 | not recorded (no resumes) | $0.228 |
| `gemini-3.5-flash` | 170 + 12 judge | 346,062 | 22,642 | 164,970 | 0 | $0.970 (estimate) |
| `gemini-3.8-flash` (incomplete) | 147 + 12 judge | 273,903 | 19,397 | 109,480 | 0 | $0.695 |

Token counts for each stronger model exclude its judge calls, which ran on flash-lite; the cost includes
them. Neither model reached the $2-per-run limit. `gemini-3.8-flash` had 6 retries, all from the spending cap.

**Prices:** [read from the pricing page](https://ai.google.dev/gemini-api/docs/pricing) on 2026-11-05.
`gemini-3.8-flash` is $0.75 input and $3.75 output through 2026-12-31, doubling on 2027-01-01. Thinking is
billed as output. `gemini-3.5-flash` can be called but is not on that page, so its cost is an estimate at
the same rate.

## Choosing the models

The key lists 47 models that can generate content. `gemini-3.5-flash` was your example, and is the same
generation as the demo's model. `gemini-3.8-flash` is the page's "most intelligent Flash model". The agent's
author chose them, and the 50 questions were no longer blind.
