# Romanized Hindi and Telugu (Phase 5, November 2026)

Can the agent answer NorthStar questions written in Hindi or Telugu typed in
English letters, the way people type in chat, with no translation step and
no prompt that mentions these languages?

## Result

| Set | Questions | A (single query) | D (full) |
| --- | --- | --- | --- |
| Romanized Hindi | 10 | 10/10 | 10/10 |
| Romanized Telugu | 10 | 10/10 | 10/10 |

Every one of the 40 runs returned the gold result table and passed its
answer checks. Wilson 95% interval for 10/10: 0.72 to 1.00.

**No prompt was changed.** The plan was to add one instruction only if
several questions failed; none did. So there is no before and after: this is
the only run, and the frozen 50's offline replay is still complete with no
live call (D 45/50).

## What this does and does not show

- It shows the first query is written correctly from romanized Hindi and
  Telugu for single-query questions: counts, sums, averages, a grouped
  count, joins, date ranges, a top-1 by region and a top-1 by month.
- It does not show the agent's escalation works in these languages. Every
  question was answerable with one query, and D answered all 20 directly
  (3 calls each: A's own first query, the follow-up decision, the answer), so
  D's repair, exploration and metric tools were never used.
- Ten questions per language is a small sample. The interval above is the
  honest size of the claim.
- The questions keep the data's own words in English (completed, refunded,
  Partner, Mid-Market, support tickets), as people do when they type. A
  question that translated those terms too would be harder.
- The Hindi and Telugu text was written by the developer of this project
  with an AI assistant. The Telugu in particular should be read by a native
  speaker before the number is quoted as more than a smoke test.
- The answers came back in the question's language, romanized ("2024 lo
  North America region lo customers andarikante ekkuva support tickets open
  chesaru, avi total ga 260."). Nothing asks for that; it is the model's
  choice and was not graded.

## Setup

- Sets: `eval/romanized_hi.yaml`, `eval/romanized_te.yaml`: the same ten
  questions against the same gold SQL. None is from the frozen 50 or any
  other set. `scripts/build_romanized_gold.py` requires each question's
  English gloss to be dissimilar (TF-IDF cosine < 0.5) to every question in
  every other set, the dev probes and the agent's example pool, and its gold
  to share no distinctive value with another set's gold. Three first drafts
  were rejected by that screening and replaced before anything ran.
- Gold: hand-written SQL, executed, each with an independent second SQL that
  must agree. Frozen in `eval/FREEZE_v4.sha256` before any model call.
- Run: `eval/run_config.yaml` as for the frozen 50 (gemini-3.5-flash-lite,
  temperature 0, today pinned to 2026-10-04, 3 steps, 3 attempts).

```bash
python scripts/run_eval_v2.py --set romanized_hi --arch baseline full --run NAME --cache-dir eval/runs/NAME-cache
python scripts/run_eval_v2.py --set romanized_te --arch baseline full --run NAME --cache-dir eval/runs/NAME-cache
```

- Live calls: 30 for Hindi and 30 for Telugu. A makes 2 calls per question.
  D makes 3, but its first query and its final answer are the same prompts
  as A's and came from the response cache, so only its follow-up decision
  was a new call.
- Code: commit `8c3e10f` plus the new set files (the results record
  `8c3e10f-dirty` for that reason). Results:
  `eval/results/romanized/romanized_hi.json`, `romanized_te.json`.

## Questions

A / D verdict per language. Gold values are rounded here for reading.

| # | English gloss (never shown to the model) | Hindi | Telugu | Gold | Hindi A / D | Telugu A / D |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | How many completed orders did each sales channel have in 2024? | 2024 mein har sales channel se kitne completed orders aaye? | 2024 lo prathi sales channel nundi enni completed orders vachayi? | Direct, 911; Online, 1457; Partner, 809 | pass / pass | pass / pass |
| 2 | How many orders were cancelled in 2024? | 2024 mein kitne orders cancel hue the? | 2024 lo enni orders cancel ayyayi? | 798 | pass / pass | pass / pass |
| 3 | How many units of products in the Services category were sold in completed orders in 2024? | 2024 mein completed orders mein Services category ke products ki kitni units biki? | 2024 lo completed orders lo Services category products enni units ammudupoyayi? | 4572 | pass / pass | pass / pass |
| 4 | How much completed revenue came through the Partner channel in the fourth quarter of 2025 (October to December)? | 2025 ki chauthi timahi (October se December) mein Partner channel se kitna completed revenue aaya? | 2025 naalugo quarter lo (October nundi December varaku) Partner channel dwara vachina completed revenue entha? | 980,124.74 | pass / pass | pass / pass |
| 5 | Customers of which region opened the most support tickets in 2024, and how many? | 2024 mein kis region ke customers ne sabse zyada support tickets khole, aur kitne? | 2024 lo ye region customers ekkuva support tickets open chesaru, mariyu enni? | North America, 260 | pass / pass | pass / pass |
| 6 | How much was spent on marketing in the EMEA region in 2024? | 2024 mein EMEA region mein marketing par kul kitna kharch hua? | 2024 lo EMEA region lo marketing meeda mottham entha kharchu ayyindi? | 603,986.96 | pass / pass | pass / pass |
| 7 | How many orders placed by Mid-Market customers in 2025 were refunded? | 2025 mein Mid-Market customers ke kitne orders refund hue? | 2025 lo Mid-Market customers orders lo enni refund ayyayi? | 430 | pass / pass | pass / pass |
| 8 | In which region did the most customers leave us in 2025, and how many left? | 2025 mein kis region ke sabse zyada customers humein chhod kar gaye, aur kitne? | 2025 lo ye region lo ekkuva mandi customers manalni vadili vellaru, mariyu entha mandi? | APAC, 56 | pass / pass | pass / pass |
| 9 | How many different customers placed at least one completed order in 2025? | 2025 mein kitne alag-alag customers ne kam se kam ek completed order diya? | 2025 lo kaneesam oka completed order ichina veru veru customers entha mandi? | 1244 | pass / pass | pass / pass |
| 10 | Which month of 2024 had the most refunded orders, and how many? Give the month as YYYY-MM. | 2024 ke kis mahine mein sabse zyada orders refund hue, aur kitne? Mahina YYYY-MM format mein batao. | 2024 lo ye nelalo ekkuva orders refund ayyayi, mariyu enni? Nelanu YYYY-MM format lo cheppandi. | 2024-12, 95 | pass / pass | pass / pass |
