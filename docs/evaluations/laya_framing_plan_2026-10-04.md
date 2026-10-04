# Laya input-framing × answer-label experiment: frozen plan (2026-10-04)

This plan is fixed **before any model scoring**. The runner is
`backtest-harness/laya_framing.py` (`laya-framing-v1`), with the worker
script `backtest-harness/laya_framing_worker.py`. The runner records this
file's SHA-256. Any repair made during execution will be documented in the
report; no wording will change because of results.

**Question:** do unclear numeric context, answer-label bias, or both,
explain the weak behaviour seen in `laya_vs_controls_2026-10-04.md`?

**Out of scope:** fine-tuning, any change to the deployed selector, Hermes,
trades, new market data, and the dashboard.

## 1. Compatibility (verified from the pinned package source, `laya` 0.3.23 inside `hermes-laya-worker:2`)

Not taken from upstream docs.

- **State:** a dict state is serialised with `json.dumps`. A string state
  is passed through unchanged. Either is placed after the question head as
  `[CLS] noul question: <instructions> [SEP] [MASK] <opt0> [MASK] <opt1> [SEP] <state> [SEP]`.
- **Budgets:** `max_len` 512 and `head_max_len` 192, from the checkpoint
  config. The state is clamped to the room left after the head, and
  per-answer usage reports `state_tokens_dropped` / `truncated`.
- **Option cap:** each option is cut at **48 tokens**
  (`common.build_head`).
- **Answer labels:** each option renders as `<label>: <criterion>`, and the
  noul result is the probability of the semantic `true` slot.
  - 0.3.23 supports `labels: {"false": ..., "true": ...}`, which changes
    only the model-facing text and still returns P(true).
  - Its README documents label bias on this English checkpoint (#156) and
    shows `{"false": "B", "true": "A"}` as the override.
- **Measured with the pinned tokenizer:**
  - The question head is 121 tokens with either label set, leaving 390
    state tokens.
  - JSON states use 73–83 tokens and explained states 236–239, over all
    198 cards used here; **no state is truncated in any variant**.
  - **Pre-existing limitation, constant across variants:** the production
    `true` option is 53 tokens, so it is already cut at 48. The model sees
    "… or unusual volume -- worth a" and loses "full technical and news
    review". Every criterion signal survives.
  - The `false` option (45 tokens) is intact.
  - Labels `A`/`B` cost the same single token as `true`/`false`, so they
    do not change this.

## 2. Inputs (no new data)

- **Real cards:** the saved complete cards of the Laya-vs-controls run,
  `backtest-harness/.laya_controls/20261004T133319Z/private_results.json`
  (SHA-256 `8d140f99…08fa`). That is 3 × 51 cards (2026-09-28, 09-29,
  10-02), re-verified against their recorded `inputs_digest`s.
- **Order:** the same order as the original batches.
- **Use:** these days are already explored; results on them are
  descriptive only.
- **Constant across all variants:**
  - checkpoint `convaiinnovations/laya` @ `55cf4c4e` (weights verified by
    SHA-256 at load);
  - question and criteria (`noul-criteria-v2`, unchanged);
  - batch size 32, CPU, 4-decimal values;
  - ranking semantics: descending P, seeded (42) row-ID hash tie-break,
    top 10.

## 3. Variants (2 × 2, plus a label-identity swap control)

| | Default labels `false`/`true` (production) | Neutral labels `{"false": "B", "true": "A"}` |
|---|---|---|
| **JSON state** (production) | V1 (baseline) | V3 |
| **Explained state** | V2 | V4 |

**Swap controls:**
- V3s = V3 and V4s = V4 with `{"false": "A", "true": "B"}`.
- Same meanings, label identities swapped.
- These measure sensitivity to *which* neutral letter carries which
  meaning.

**What each change does:**
- The state change and the label change are independent. V2 alters only
  the state; V3 alters only the labels.
- The criteria text still begins "yes:" / "no:" in every variant, because
  the criteria are held constant. Neutral labels therefore replace only
  the `false:`/`true:` prefix: a partial neutralisation, by construction.

**Explained rendering** (the same seven 4-decimal numbers; percent =
fraction × 100 to 2 decimals, which is exact):
- a fixed header stating it is one anonymous US-listed stock and that the
  task is review priority, not buying or profit;
- one line per field, with units and a definition;
- no identity, news, fundamentals, extra history, verdict words or answer
  labels.

These properties are checked by `test_laya_framing.py`, including an exact
round-trip back to the JSON numbers.

**Exact rendering of one identity-free synthetic card (`C_breakout`):**

```
V1/V3 state: {"five_day_return": 0.1, "sma20_distance": 0.08, "rsi14": 75.0, "macd_direction": 0.7, "relative_volume": 2.5, "realized_volatility": 0.4, "range_position_20d": 0.97}

V2/V4 state:
Daily technical snapshot of one anonymous US-listed stock at a session close. The task is only to decide whether these numbers deserve a further technical and news review; it is not a buy decision and not a profit forecast.
5-day return: +10.00% (today's close vs the close 5 sessions earlier)
Distance from 20-day average: +8.00% (close vs its 20-day simple moving average)
RSI(14): 75.0000 (scale 0 to 100 from 14 sessions of gains vs losses; 50 = balanced)
MACD direction: +0.7000 (normalized MACD(12,26,9) histogram, -1 to +1; sign = momentum direction, 0 = flat)
Relative volume: 2.5000x (today's volume vs the average of the prior 20 sessions; 1.0000x = average)
Realized volatility: 40.00% (annualized, from the last 20 daily returns)
Position in 20-day range: 97.00% (0% = at the 20-day low, 100% = at the 20-day high)

Question (all variants): noul "Is this identity-free numeric card worth spending the existing full technical/news review on today?" with the production true/false criteria; V3/V4 add labels {"false": "B", "true": "A"}; V3s/V4s add {"false": "A", "true": "B"}.
```

## 4. Synthetic diagnostic cases (fixed; `laya_framing.CASES`)

**This is a diagnostic rubric, not financial truth.** Every case starts
from the ordinary base (5d 0, SMA distance 0, RSI 50, MACD 0, relative
volume 1.0, volatility 0.25, range 0.5).

**Unambiguous review-worthy (expected true):** at least one field named in
the criterion is clearly notable:
- |5d| ≥ 7%;
- |SMA distance| ≥ 7%;
- RSI ≤ 25 or ≥ 75;
- |MACD| ≥ 0.7;
- relative volume ≥ 2.5;
- range ≤ 0.05 or ≥ 0.95.

**Unambiguous ordinary (expected false):** every criterion field is inside
the ordinary band:
- |5d| ≤ 0.5%;
- |SMA| ≤ 0.5%;
- RSI 48–52;
- |MACD| ≤ 0.05;
- relative volume 0.95–1.05;
- range 0.45–0.55.

**Ambiguous (no expected label):**
- borderline values;
- volatility only (not in the criterion);
- unusually low volume (it is unclear whether that is "unusual");
- internally opposing signals.

**Split, fixed** (each split: 3 ordinary, 6 single-criterion, 2
combinations, 2 borderline, 2 opposing/other):

| | Development (choose a format) | Confirmation (reserved) |
|---|---|---|
| Ordinary (false) | O1–O3 | O4–O6 |
| Single criterion (true) | 5d +12%, SMA −10%, RSI 82, MACD −0.9, volume 3.5×, range 0.02 | 5d −12%, SMA +10%, RSI 18, MACD +0.9, volume 4×, range 0.98 |
| Combinations (true) | breakout, oversold | sell-off, volume + move |
| Borderline (ambiguous) | 5d +4%, RSI 66 | volume 1.4×, range 0.88 |
| Opposing / other (ambiguous) | RSI 80 at range low with 5d −8%; volatility 70% only | volume 0.3× only; MACD +0.9 with 5d −10% |

**Paired one-field changes** from the ordinary base (the same 14 as the
controls experiment):
- 5d ±10%, SMA ±8%, RSI 25/75, MACD ±0.8, range 0.02/0.98, volume 3.0×:
  predicted ↑ (11 predictions);
- volume 0.4× and volatility 10%/60%: no prediction.

## 5. Measurements

1. **Task agreement:**
   - AUROC of P between expected-true and expected-false cases, per split.
     It is threshold-free; ties count ½.
   - Counts above 0.5 are reported descriptively only.
2. **Paired direction:**
   - the number of the 11 predicted ↑ changes whose Δ ≥ +0.0005;
   - every Δ, reported.
3. **Real cards, per day:**
   - min, median and max; range and IQR;
   - distinct values and rows sharing a score;
   - the rank-10 vs 11 gap;
   - top-10 overlap with V1;
   - Spearman ρ vs V1.
4. **Label sensitivity:** mean |ΔP| between V3 and V3s, and between V4 and
   V4s, on development, confirmation and real cards, plus the swapped
   variants' AUROC.
5. **Diagnostics:**
   - runtime: cold start and per variant;
   - input tokens and state tokens;
   - truncated answers, which must be 0;
   - malformed outputs, where any reply failing validation aborts the run.
6. **Baseline reproduction:** V1 on the saved cards must reproduce the
   saved production scores. A mismatch is reported and the variants are
   not treated as comparable to them.

## 6. Pre-registered decision rule (development split only)

A variant **qualifies as better than V1** only if all of these hold:
- dev AUROC ≥ V1 + 0.15;
- paired agreement ≥ V1 + 3 (of 11);
- no truncated or malformed outputs;
- for neutral-label variants, the swap control shows mean |ΔP| ≤ 0.02 on
  dev and the swapped AUROC within 0.10 of the unswapped.

If several qualify, choose the highest dev AUROC; ties go to the simpler
variant (V2 before V3 before V4).

Higher, more spread or above-0.5 scores never count as "better" by
themselves.

**Confirmation:** for the chosen variant, the finding survives if its
confirmation AUROC ≥ V1's + 0.15 and, if it uses neutral labels, the swap
conditions also hold on confirmation.

The confirmation results are computed in the same run, but the choice is
made by code from development metrics only. Nothing is re-tuned after
they are read.

**Factor attribution (descriptive):**
- clearer context helped if V2 qualifies, or V4 qualifies and V3 does not;
- labels mattered if V3 qualifies, or the swap control moves P materially
  (> 0.02).

## 7. Execution, privacy and boundaries

**Worker:**
- `laya_framing_worker.py` is mounted read-only into the unchanged
  production image.
- It uses the same `--network none`, read-only root, `cap-drop ALL`,
  non-root and offline flags, verified by `docker inspect` on the live
  container.
- The model is loaded once, and the variants run sequentially.
- The worker accepts only the production question and criteria; only
  `labels` may differ.
- It is removed after the run.

**Outputs:**
- Private, in git-ignored `backtest-harness/.laya_framing/`: per-card
  scores and top-10 row IDs.
- Committed: this plan, the runner/worker/tests, and an aggregate report.

**Boundaries:** no Hermes, no trades, no market-data fetch, no Neon write,
no dashboard change, no change to the production selector, worker or
settings.

**If no variant improves the diagnostics,** the report describes what
labelled data would be needed before considering fine-tuning.
