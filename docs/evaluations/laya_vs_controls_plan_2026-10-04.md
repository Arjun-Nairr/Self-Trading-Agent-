# Laya selector vs. controls: frozen experiment plan (2026-10-04)

This plan is fixed **before any card is scored**. The model, criteria,
controls, dates, pool, measurements and thresholds below must not be changed
after results are seen. The runner is `backtest-harness/laya_controls.py`
(`EXPERIMENT_VERSION = "laya-vs-controls-v1"`). It records this file's
SHA-256 in every output.

**Purpose:** describe what Laya's selection contributes compared with
simple controls, and what can honestly be claimed. This is not a removal
gate, a profitability test or a trading evaluation. Laya stays in the
project whatever the result.

## 1. Sample

| | |
|---|---|
| Sessions | 2026-09-28, 2026-09-29, 2026-10-02 (fixed; scored in this order) |
| Pool | `watchlist/experimental_pools/candidates60-v1-pool-2026-09-28-eec1e3f2203f.json`. This is the frozen pool used by smoke run `5573597c-2f6c-454f-99d3-fdce9f25b62f`, re-verified with `laya_preflight.load_frozen_pool`. |
| Pool hashes | `content_sha256 7d1e5f7334dbae5847e348e9213eb678739e3208591a429ce05f4c78bf8f8702`; `evidence_sha256 eec1e3f2203fd4f0cd761910e91dc65845265aa19f864ffaba81f789d34b1af2`; universe hash `536a1ecac51b48224c769dc691d55c089ad7c035cab1edd194a09d7159016fe6`; 51 members |
| Portfolio exclusions | None. All 51 names are eligible every day; this is a standalone selector comparison. |

**Nature of the sample:** a small retrospective demonstration. It is not
an untouched holdout and not an unbiased historical universe. The pool was
chosen by news/price coverage over this same window, and the dates were
already seen in the smoke run.

## 2. Cards (identical input for every method)

**Builder:** the existing point-in-time card builder.
- `run_backtest.fetch_card_history` fetches daily OHLCV through maverick's
  price path.
- `laya_cards.build_day_cards` builds the cards.

**Data cutoff:** only bars dated on or before the session date are used;
later bars are dropped by `laya_cards._clean_bars`. A card whose latest bar
predates the session is stale and unavailable. History window: 120
calendar days, at least 40 bars.

**Retrospective caveat:** bars are fetched now from the provider's cache,
not as a point-in-time vintage.

**Card schema:** seven numbers per card, plus a data-complete flag:
- `five_day_return`;
- `sma20_distance`;
- `rsi14`;
- `macd_direction`;
- `relative_volume`;
- `realized_volatility`;
- `range_position_20d`.

There is no ticker, name, date or text.

**Row IDs:** `make_row_ids(pool symbols, seed=42, pool universe hash)`.
These are exactly the smoke run's IDs, so the stored smoke snapshots'
`inputs_digest` can verify the rebuilt cards.

**Fairness:**
- Each day's complete, current cards form one immutable set; its
  `inputs_digest` is recorded.
- Laya, random and deterministic all receive exactly this set.
- Incomplete or stale cards are excluded for every method alike, and the
  reason is recorded.

**Provenance check against smoke run `5573597c`:**
- For each date, the rebuilt cards restricted to that run's eligible rows
  must reproduce the stored snapshot `inputs_digest`.
- Fresh Laya scores for those rows are compared with the stored scores.
- A mismatch is reported, not repaired.

## 3. Methods (k = 10 each)

**1. Laya** (unchanged production selector behaviour):

| Setting | Value |
|---|---|
| Package | `laya` 0.3.23 |
| Checkpoint | `convaiinnovations/laya` @ `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` |
| Weights SHA-256 | `891102d3…8d86c` |
| Worker image | `hermes-laya-worker:2` (`--network none`, read-only, isolation verified by inspect) |
| Question format | `noul-criteria-v2`, CPU |
| Batch size | 32 |
| State decimals | 4 |
| `min_confidence` | none |

Selection uses `laya_selector.select_pool`: descending `review_probability`,
seeded (42) row-ID hash tie-break, top 10. `answer_confidence` is audit-only.
The model is loaded once, and the three sessions are scored sequentially,
one batch each.

**2. Random control:** for each day and each seed `s` in 0..19,
`random.Random(f"{session}|{s}").sample(sorted(complete row IDs), 10)`.
That gives 20 shortlists per day and no model calls.

The session is part of the seed string so that seed `s` draws
independently on each day. A bare `Random(s)` over the same 51 IDs would
repeat the identical shortlist every day and fake the turnover baseline.
This correction was made before any scoring.

**3. Deterministic control (alternative attention heuristic, not ground
truth):**
- Rank by `abs(five_day_return)`, descending, and take the top 10. This is
  the largest absolute five-day move: a plain "biggest mover" screen.
- Ties break by the same seeded (42) row-ID hash as Laya.
- No scaling and no other fields.
- It is deliberately one field and does not encode Laya's multi-criteria
  wording.

## 4. Measurements (per day and across the three days)

1. **Counts:** complete and unavailable cards, with reasons.
2. **Overlap:**
   - |Laya ∩ deterministic|, with Jaccard.
   - Laya vs. random over the 20 seeds: mean, min and max overlap.
   - Deterministic vs. random, the same way.
   - The hypergeometric expectation 10·10/n, and the exact hypergeometric
     P(overlap ≥ observed) for one random shortlist. This is descriptive,
     not a significance claim.
3. **Laya scores:**
   - min, median, max, range and IQR;
   - distinct values;
   - rows sharing a score;
   - the rank-10 vs. rank-11 gap;
   - cards within 0.001 of the rank-10 score (boundary fragility).
4. **Turnover:** |S(09-28) ∩ S(09-29)| and |S(09-29) ∩ S(10-02)| for Laya
   and deterministic, against the random distribution of the same pairing
   per seed. Row IDs are stable across days.
5. **Profiles:**
   - Per field, the median of selected cards vs. the median of the full
     complete set, for Laya, deterministic and random (mean over seeds of
     each seed's median).
   - Spearman rank correlation, across each day's complete cards, of the
     Laya score against each field, against |field − ordinary anchor|, and
     against the deterministic score.
6. **Alignment with Laya's own stated criteria:**
   - **Notable flags per card**, using thresholds fixed here:
     - |five_day_return| ≥ 0.05;
     - |sma20_distance| ≥ 0.05;
     - range_position_20d ≤ 0.10 or ≥ 0.90;
     - rsi14 ≤ 30 or ≥ 70;
     - |macd_direction| ≥ 0.50;
     - relative_volume ≥ 1.5.
   - Report mean flags and the share with ≥ 1 flag, for each method's
     shortlist and for the full set.
   - **Disclosure:** this measures agreement with Laya's own question, as
     operationalised by these thresholds. It is not independent evidence
     of better selection.
7. **Runtime:**
   - cold worker start, up to and including the handshake and isolation
     check;
   - card retrieval per day;
   - warm Laya scoring per day;
   - control computation time.

## 5. Synthetic paired-input sensitivity (identity-free, fixed here)

**Base "ordinary" card:**

| Field | Value |
|---|---|
| five_day_return | 0.00 |
| sma20_distance | 0.00 |
| rsi14 | 50 |
| macd_direction | 0.00 |
| relative_volume | 1.00 |
| realized_volatility | 0.25 |
| range_position_20d | 0.50 |

**Variants**, one field changed at a time with every other field kept at
base:

| Field | Low | High |
|---|---|---|
| five_day_return | −0.10 | +0.10 |
| sma20_distance | −0.08 | +0.08 |
| rsi14 | 25 | 75 |
| macd_direction | −0.80 | +0.80 |
| relative_volume | 0.40 | 3.00 |
| realized_volatility | 0.10 | 0.60 |
| range_position_20d | 0.02 | 0.98 |

**Extra cards:**
- one "all notable" card: +0.10, +0.08, 75, +0.80, 3.00, 0.60, 0.98;
- a duplicate base card under a different row ID, as a determinism check.

All 17 cards are scored in one batch. Report each Δ = variant − base.

**Response threshold:** a field "moves" the score if |Δ| ≥ 0.0005, five
times the 4-decimal resolution. Context: the size of Δ relative to the
real days' score range. Synthetic responsiveness is not evidence of
real-world utility.

## 6. Interpretation limits (decided now)

- **Not used:** future returns, trading outcomes and LLM judges. There is
  no "winner".
- **Not claimed:** `review_probability` is not treated as a probability of
  profit.
- **Separate verdicts:** functional, responsive, distinct from controls,
  and useful. "Useful" is expected to stay inconclusive: this sample
  cannot measure downstream value.
- **Limits of the sample:**
  - three days, one pool, one model setting;
  - random-control results describe chance overlap for this n and k;
  - the deterministic control is one arbitrary heuristic.

## 7. Outputs and privacy

**Private** (git-ignored, under `backtest-harness/.laya_controls/<run>/`):
- cards keyed by row ID;
- Laya scores and ranks;
- shortlists;
- the row-ID → ticker mapping;
- the worker log.

**Committed:** only this plan, the runner and tests, and an
aggregate/identity-free report.

**Boundaries:** no Hermes, no trades, no backtest/evaluation run, no Neon
write, no dashboard change.
