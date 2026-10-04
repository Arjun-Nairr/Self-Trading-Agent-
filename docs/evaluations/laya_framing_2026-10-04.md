# Laya input-framing × answer-label experiment (2026-10-04)

## Verdict

**Neither clearer context nor neutral labels fixes Laya's weak behaviour.**
No variant met the pre-registered development rule, so none was chosen, and
nothing survives confirmation. The production selector stays unchanged.

**What changed and what did not:**
- Both factors *change* the outputs. Explained context shifts every score
  upward by about 0.3 and reshuffles the real-card ranking. Neutral labels
  shift scores by about 0.17.
- Neither makes the scores follow the review criteria.
- Answer labels show measurable bias: swapping which neutral letter carries
  which meaning moves the task metric by up to 0.29 AUROC.

| Variant | Dev AUROC (8 true vs 3 ordinary) | Paired agreement (of 11) | Swap mean \|ΔP\| (dev) | Confirmation AUROC | Rule outcome |
|---|---|---|---|---|---|
| V1 JSON + `false`/`true` (production) | 0.54 | 3 | — | 0.79 | baseline |
| V2 explained + `false`/`true` | **0.75** | 3 | — | 0.71 | fails: no paired gain |
| V3 JSON + neutral `B`/`A` | 0.46 | 5 | **0.024** | 0.63 | fails: AUROC, paired, swap |
| V4 explained + neutral `B`/`A` | 0.63 | 1 | 0.008 | 0.67 | fails: AUROC, paired |
| V3s (V3 with A↔B swapped) | 0.38 | 7 | | 0.33 | control |
| V4s (V4 with A↔B swapped) | 0.67 | 1 | | 0.71 | control |

**Confirmation:** read only after the code applied the rule to development
metrics. With no variant chosen, these figures are descriptive. Every
variant's confirmation AUROC is at or below production's 0.79.

## 1. Setup (frozen before scoring)

**Plan:** `docs/evaluations/laya_framing_plan_2026-10-04.md`.
- SHA-256 `5ebaf981fe0e1e887754fbd5d22e773f117d7db2bd56bc3607d8a4d965ab41e8`.
- Committed and pushed in `26c1a3b` before any model call.

**Code and run:**
- Runner `laya_framing.py` (SHA-256 `2f239f50…17fa`) and worker
  `laya_framing_worker.py` (`c6c91f0e…91a0`).
- One run, `20261004T151244Z`.
- No execution repairs were needed.

**Model and worker:**
- `laya` 0.3.23 and checkpoint `convaiinnovations/laya` @ `55cf4c4e`,
  confirmed in the worker's handshake.
- Unchanged image `hermes-laya-worker:2` (`sha256:d423bbf7…`), with the
  experiment script mounted read-only.
- Production isolation flags, checked by `docker inspect` on the live
  container.
- One model load (24.4 s), then six variants in sequence.

**Inputs:**
- The saved complete cards of the Laya-vs-controls run: 3 days × 51,
  private file SHA-256 `8d140f99…08fa`, digests re-verified.
- 45 synthetic cards: 30 diagnostic cases plus 15 paired.
- No market data was fetched.

**Held constant:** question, criteria, batch size 32, CPU, 4-decimal
values, and the top-10 ranking semantics.

**Private outputs** (git-ignored `.laya_framing/20261004T151244Z/`):
public summary `2f65e99b…aa`, private results `3e9e1128…b0`.

## 2. Pinned-package facts and the pre-existing truncation

From the 0.3.23 source inside the image:
- **State:** a dict is sent as `json.dumps`; a string is sent verbatim.
- **Budgets:** `max_len` 512 and question head ≤ 192 tokens. Each answer
  option is **capped at 48 tokens**.
- **Answer:** a noul answer is P(semantic `true` slot).
- **Labels:** `labels` overrides only the model-facing words, and the
  package README itself warns about label bias on this checkpoint (#156).

**Measured (pinned tokenizer and live usage):**
- The question head is 121 tokens with either label set.
- JSON states use 73–83 tokens and explained states 236–239, against 390
  available.
- **0 truncated answers** in 1,188 scored states.

**Pre-existing production limitation, the same in every variant:** the
`true` option is 53 tokens, so the model sees it cut at "… or unusual
volume -- worth a". Every criterion signal survives; "full technical and
news review" is lost.

The verified option mapping (rendered, after the cap):

| Labels | Rendered options |
|---|---|
| Default | `false: no: …` / `true: yes: … worth a` |
| Neutral | `B: no: …` / `A: yes: … worth a` |
| Swapped | `A: no: …` / `B: yes: … worth a` |

The returned value is P(the `yes` option) in all three.

## 3. Exact example (identity-free synthetic card `C_breakout`, expected review-worthy)

**V1/V3 state (production JSON):**
`{"five_day_return": 0.1, "sma20_distance": 0.08, "rsi14": 75.0, "macd_direction": 0.7, "relative_volume": 2.5, "realized_volatility": 0.4, "range_position_20d": 0.97}`

**V2/V4 state (explained):**

```
Daily technical snapshot of one anonymous US-listed stock at a session close. The task is only to decide whether these numbers deserve a further technical and news review; it is not a buy decision and not a profit forecast.
5-day return: +10.00% (today's close vs the close 5 sessions earlier)
Distance from 20-day average: +8.00% (close vs its 20-day simple moving average)
RSI(14): 75.0000 (scale 0 to 100 from 14 sessions of gains vs losses; 50 = balanced)
MACD direction: +0.7000 (normalized MACD(12,26,9) histogram, -1 to +1; sign = momentum direction, 0 = flat)
Relative volume: 2.5000x (today's volume vs the average of the prior 20 sessions; 1.0000x = average)
Realized volatility: 40.00% (annualized, from the last 20 daily returns)
Position in 20-day range: 97.00% (0% = at the 20-day low, 100% = at the 20-day high)
```

**Question (all variants):**
- noul, "Is this identity-free numeric card worth spending the existing
  full technical/news review on today?";
- the production criteria;
- V3/V4 add `"labels": {"false": "B", "true": "A"}`, and the swap controls
  use `{"false": "A", "true": "B"}`.

**Output P(review-worthy) for this card,** with the all-ordinary base card
for comparison. Both cards are synthetic and identity-free.

| Variant | Input tokens (state) | `C_breakout` P | Ordinary base P | Truncated |
|---|---|---|---|---|
| V1 | 195 (73) | 0.2003 | 0.2166 | no |
| V2 | 359 (237) | 0.5233 | 0.6058 | no |
| V3 | 195 (73) | 0.3831 | 0.3750 | no |
| V4 | 359 (237) | 0.4681 | 0.5283 | no |
| V3s | 195 (73) | 0.3723 | 0.3333 | no |
| V4s | 359 (237) | 0.4703 | 0.5180 | no |

The breakout card is clearly review-worthy under the criterion, yet it
scores *below* the ordinary card in V1, V2 and V4. It scores barely above
in V3, and that edge depends on the label identity (V3s).

Per-variant means on the development cases:

| | Mean P, expected-true (dev) | Mean P, expected-ordinary (dev) | Ordinary base |
|---|---|---|---|
| V1 | 0.211 | 0.206 | 0.217 |
| V2 | 0.592 | 0.549 | 0.606 |
| V3 | 0.383 | 0.383 | 0.375 |
| V4 | 0.511 | 0.483 | 0.528 |

Every V1/V3 score is below 0.5. Under V2, every case, including all three
ordinary dev cases, is above 0.5. That is a level shift, not
discrimination.

## 4. Findings

**Baseline reproduction:** V1 on the saved cards reproduced every saved
production score exactly (153/153, max |Δ| 0), so the comparisons are
valid.

**Clearer context (V1 → V2, labels fixed):**
- The development AUROC rose from 0.54 to 0.75.
- Paired agreement stayed at 3/11, though with different misses. Under V2
  the +10% five-day move now *lowers* the score (−0.017), and both SMA
  directions raise it.
- On confirmation, V2's AUROC (0.71) is below V1's (0.79).
- On real cards it produced a different ordering: top-10 overlap with
  production 0, 3 and 1; Spearman ρ −0.34, +0.05, −0.24.
- **Conclusion:** the effect is real but not task-aligned, and it does not
  replicate.

**Answer labels (V1 → V3, state fixed):**
- Neutral labels lifted the level (0.21 → 0.38) and mostly kept the
  production ordering: top-10 overlap 7, 5, 8; ρ 0.81, 0.66, 0.79.
- They did not separate ordinary from notable cases (dev AUROC 0.46,
  means 0.383 vs 0.383).
- **Label identity matters.** Swapping A↔B changed:
  - dev AUROC 0.46 → 0.38 and confirmation 0.63 → 0.33;
  - paired agreement 5 → 7;
  - mean |ΔP| 0.024 on dev and 0.029 on confirmation, both above the 0.02
    limit;
  - mean |ΔP| on real cards 0.007–0.009.
- With explained context (V4 vs V4s), the swap effect is smaller (0.008
  on dev; 0.016 on real cards), but V4 itself agrees with only 1 of 11
  predicted directions.
- **Conclusion:** answer-label bias exists, but removing `false`/`true` does
  not uncover a criteria-following signal underneath.

**Both together (V4):** worse than either alone on paired direction (1/11);
dev AUROC 0.63.

**Ambiguous cases:** no variant separates them in any interpretable way.
Within each variant they sit in the same band as the labelled cases.

**Real-card spread:**
- The range is 0.05–0.13 in every variant.
- A few tied rows remain in every variant.
- The rank-10/11 gaps stay tiny (0.0001–0.0039).
- No variant produces a decisive cut-off.

**Runtime:**
- One-off model load: 24.4 s.
- JSON variants: 131–136 s for 198 states each, about 0.67 s per card.
- Explained variants: 254–261 s, about 1.3 s per card, because the state
  is about 3× the tokens.

## 5. Scoped audit

| Check | Result |
|---|---|
| Identical data | Every variant scored the same hash-verified cards. The explained text round-trips exactly to the JSON numbers (test). The worker accepted only the production question and criteria, with `labels` the sole difference. |
| Units, rounding, mapping | Percentages are fraction × 100 to 2 decimals (exact), RSI/MACD/volume to 4 decimals, as in production. The label mapping is shown in §2, and P is the `true` slot per the pinned source. |
| No truncation or injected labels | 0 truncated answers. States contain no yes/no/true/false, A:/B:, verdict words or identities (test). |
| Reproducibility and validation | V1 reproduces production exactly. Every reply was validated (id, count, P a float in [0,1]); there were 0 malformed replies. |
| Cleanup | 0 Laya worker containers remain. |
| Production untouched | No diff in the selector, adapter, worker, cards, runner or image; the image ID is unchanged. No Neon, Hermes, trade, market-data or dashboard activity. |

**Correction made during this work:** the Laya-vs-controls report said 2
of 11 predicted rises occurred. The correct count is 3 (RSI 25 → +0.0075),
and that report is now corrected. Its other conclusions are unchanged.

## 6. Limitations

**Sample:**
- Synthetic cases are a diagnostic rubric, not financial truth.
- Each split has only 8 expected-true and 3 expected-ordinary cases, so
  the AUROC is coarse: production's own AUROC differs between dev (0.54)
  and confirmation (0.79).
- The real days were already explored, so their results are descriptive.

**Design:**
- One question, one criteria text (already truncated), one checkpoint,
  one neutral label pair.
- Neutral labels replace only the `false:`/`true:` prefix. The criteria
  still begin "no:"/"yes:", held constant by design.

**Not measured:** no outcome or usefulness measurement; none was
intended.

## 7. What labelled data would be needed before considering fine-tuning

No variant improves the diagnostics, so prompt changes are not a fix. A
fine-tuning decision would first need labelled, identity-free cards:

- **Source of labels that is not just the rubric.** If labels come from a
  deterministic rule, that rule *is* the selector, and teaching Laya to
  imitate it adds cost without capability.
- **Who labels:** independent human reviewers judging "worth a full
  review?" from the card alone.
- **Agreement:** measured (for example, Cohen's κ), with an explicit
  "ambiguous" class rather than forced labels.
- **Size and spread:** a few hundred to low thousands of cards across many
  dates and market regimes. That needs point-in-time card building for
  dates beyond the three explored here.
- **Splits:** by date, with an untouched test period.
- **Benchmarks:** compare against random selection and the simple
  deterministic rule before any trading use.
- **Outcome-based labels** (future returns) would change the question
  into prediction and are outside this project's stated use of Laya.

## 8. Smallest justified next action

No further framing or label work is justified. Keep the production
selector unchanged.

The one concrete defect found, the production `true` criterion being cut
at 48 tokens, is noted for any future question-version change. Fixing it
would be a new question version needing its own frozen check, not a tweak
to this one.
