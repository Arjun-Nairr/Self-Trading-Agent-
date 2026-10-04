# Laya selector vs. controls: bounded comparison (2026-10-04)

## Verdict

**Functional: yes. Responsive: yes, but not in the direction of its own
criteria. Distinct from the deterministic control: yes. Distinct from
random selection: not shown on these three days. Useful: inconclusive, and
nothing here supports it.**

Laya stays in the project. This was not a removal gate or a profitability
test, and no returns, trades or judge model were used.

| Claim | Status | Evidence |
|---|---|---|
| **Functional** | Yes | All 3 days scored: 51/51 complete cards, one batch each. The rebuilt cards reproduce smoke run `5573597c`'s stored input digests. All 149 overlapping fresh scores equal the stored scores. Re-scoring a duplicate card gives an identical score. |
| **Responsive** | Yes, but misaligned | All 7 inputs move the score by ≥ 0.0005 (§5). Most moves go against the stated "notable" criteria, and the all-notable card scores *below* the ordinary card. |
| **Distinct from the deterministic control** | Yes | Laya ∩ biggest-mover overlap is 2/10 each day, which is chance level (expectation 1.96). |
| **Distinct from random** | Not shown | On overlap, turnover and notable-flag profile, Laya sits inside the 20-seed random range on every day. |
| **Useful** | Inconclusive / unsupported | There is no outcome data, by design. Laya's shortlists do not even prioritise cards matching its own criteria more than random draws do. |

## 1. Setup

Plan: `docs/evaluations/laya_vs_controls_plan_2026-10-04.md`.
- Frozen and committed before any scoring, in `58adc6c`.
- SHA-256 `4ab0c549436607415d559c8970c14e291f75f450bd2bc5047505df12756897c9`.
- One deviation from the first draft, also made before scoring: the random
  seed string includes the session date.

Runner: `backtest-harness/laya_controls.py`, `laya-vs-controls-v1`, SHA-256
`134ea45e…`, one run at 2026-10-04 13:33Z from HEAD `58adc6c`. The repo was
dirty only with the user's own files.

| | |
|---|---|
| Sessions | 2026-09-28, 2026-09-29, 2026-10-02 |
| Pool | `candidates60-v1-pool-2026-09-28-eec1e3f2203f`, 51 names. Re-verified: `content_sha256 7d1e5f73…8702`, `evidence_sha256 eec1e3f2…1af2`, universe hash `536a1eca…6fe6` |
| Exclusions | None. All 51 names are eligible each day, with no portfolio state. |
| Cards | Built by the existing builder (`fetch_card_history` → `build_day_cards`). Only bars ≤ the session date are used, and every card's latest bar is the session date. Seven numbers each, no identity. |
| Row IDs | Seed 42 over the pool hash, the same as the smoke run |
| Laya | `laya` 0.3.23, `convaiinnovations/laya` @ `55cf4c4e`, weights `891102d3…`. Image `hermes-laya-worker:2` (`sha256:d423bbf7…`); its three Laya modules are byte-identical to the repository. `noul-criteria-v2`, CPU, batch 32. Isolation (`--network none`, read-only) verified by inspect. |
| Random control | 20 seeds (0–19) per day: `Random(f"{session}\|{s}")`, sample 10 from the sorted IDs |
| Deterministic control | Top 10 by abs(five_day_return), with the selector's seeded tie-break |

**Input hashes (complete-card digests):**

| Session | Digest |
|---|---|
| 09-28 | `c20553e0e7e3…7c828`, identical to the smoke snapshot |
| 09-29 | `8eca206ab50d…e194ef` |
| 10-02 | `3b07ca78412c…5ebc` |

On 09-29 and 10-02 the 49-card subset the smoke run saw also reproduces
its stored digest.

**Private outputs** (git-ignored `backtest-harness/.laya_controls/20261004T133319Z/`):

| File | SHA-256 |
|---|---|
| `public_summary.json` (aggregates only) | `eaf10794…83b9` |
| `private_results.json` | `8d140f99…08fa` |
| `private_mapping.json` | `bc8f1d5c…7b27` |

## 2. Comparison (k = 10; n = 51 complete cards every day; 0 unavailable)

| | 09-28 | 09-29 | 10-02 |
|---|---|---|---|
| Laya ∩ deterministic | 2 (Jaccard 0.11) | 2 | 2 |
| Laya ∩ random, mean [min–max] over 20 seeds | 2.25 [0–6] | 2.00 [1–4] | 2.30 [0–6] |
| Deterministic ∩ random | 1.80 [0–7] | 1.70 [0–5] | 2.60 [0–4] |
| Chance expectation 10·10/51 | 1.96 | 1.96 | 1.96 |
| Mean notable flags (0–6): all cards | 1.94 | 1.80 | 1.67 |
| — Laya shortlist | 1.80 | 1.30 | 1.80 |
| — Random, mean [min–max] | 1.91 [1.2–3.1] | 1.64 [0.8–2.4] | 1.80 [1.2–2.7] |
| — Deterministic | 3.90 | 3.40 | 3.40 |
| Share with ≥ 1 flag: Laya / random mean / deterministic | 0.90 / 0.90 / 1.00 | 0.50 / 0.74 / 1.00 | 0.80 / 0.79 / 1.00 |

P(random overlap ≥ 2) for a single random shortlist is 0.64.

**Turnover** (shortlist overlap between consecutive days):

| Pair | Laya | Deterministic | Random, same seed |
|---|---|---|---|
| 09-28 → 09-29 | 2 | 3 | 1.95 [0–5] |
| 09-29 → 10-02 | 1 | 3 | 1.85 [0–4] |

**Laya score distribution:**

| | 09-28 | 09-29 | 10-02 |
|---|---|---|---|
| min / median / max | 0.1701 / 0.1906 / 0.2318 | 0.1612 / 0.1908 / 0.2152 | 0.1592 / 0.1947 / 0.2463 |
| Range / IQR | 0.062 / 0.024 | 0.054 / 0.019 | 0.087 / 0.021 |
| Distinct values; rows sharing a score | 49; 4 | 47; 8 | 49; 4 |
| Rank-10 vs 11 gap; cards within 0.001 of rank 10 | 0.0001; 4 | 0.0014; 1 | 0.0039; 1 |

Every score is below 0.25, so the model answers "no" to every card and the
ranking is relative within a narrow band. `answer_confidence` equals
1 − probability throughout (0.75–0.84). It is audit-only, as designed.

**Selected-card medians vs. the full set (selected values in bold):**

| | 09-28 | 09-29 | 10-02 |
|---|---|---|---|
| range_position_20d: Laya / all / det | **0.45** / 0.18 / 0.16 | **0.31** / 0.17 / 0.10 | 0.28 / 0.26 / **0.71** |
| macd_direction: Laya / all / det | **+0.48** / −0.65 / −0.96 | **+0.33** / −0.53 / −0.75 | −0.63 / −0.43 / −0.53 |
| rsi14: Laya / all / det | 50.4 / 41.0 / 38.1 | 44.3 / 40.0 / 36.1 | 44.8 / 41.3 / 59.2 |
| realized_volatility: Laya / all | **0.40** / 0.32 | 0.28 / 0.31 | **0.36** / 0.30 |

Random seed medians track the full set closely; see the public summary.

**Spearman ρ, Laya score vs. card field** (51 cards per day). Raw level
first, then |field − ordinary anchor|:

| Field | 09-28 | 09-29 | 10-02 |
|---|---|---|---|
| range_position_20d | +0.29 / −0.20 | +0.31 / −0.31 | +0.26 / −0.06 |
| realized_volatility (raw only) | +0.36 | +0.13 | +0.41 |
| rsi14 | +0.18 / −0.16 | +0.28 / −0.28 | +0.27 / −0.10 |
| five_day_return | +0.09 / +0.27 | +0.18 / −0.04 | +0.32 / +0.12 |
| relative_volume | +0.04 / +0.06 | −0.20 / +0.11 | −0.06 / +0.25 |

## 3. Runtime and cost

| Step | Time | Who pays it |
|---|---|---|
| Card retrieval | 29.0 / 28.2 / 25.2 s per day | All methods equally (shared input) |
| Laya cold start (worker, model load, handshake, isolation check) | 32.3 s, once | Laya only |
| Laya warm scoring, 51 cards | 37.2 / 34.6 / 34.4 s (≈ 0.7 s per card, CPU) | Laya only |
| Synthetic batch, 17 cards | 10.6 s | Laya only |
| Random (20 shortlists) + deterministic | < 0.02 s per day | Controls |

So Laya adds about 35 s of CPU per 51-card day plus a one-off 32 s start.
The controls are effectively free.

## 4. Supported observations vs. inference

**Supported by the data:**
- Laya is deterministic and reproducible on identical inputs, and its
  shortlist was unchanged from the smoke run.
- Laya's shortlists overlap the biggest-mover control and random draws at
  chance level.
- Its day-to-day turnover is within the random range.
- Its shortlists carry no more criteria-matching cards than random
  shortlists. On 09-29 it carried fewer than the random mean: 1.30 flags
  vs 1.64, and 50% vs 74% with any flag.
- Its picks lean toward mid-range or upper-range price position, positive
  MACD, higher RSI and higher volatility, relative to the pool. Its score
  correlates negatively with RSI's and range position's distance from
  "ordinary".
- In the synthetic check, every field moves the score.

**Inference (plausible, not proven):**
- The model seems to respond to the level and direction of the inputs
  (higher range position, volatility, RSI, positive five-day return)
  rather than to "distance from ordinary", which is what the question's
  criteria describe.
- Its ranking therefore behaves more like an idiosyncratic attention
  ordering than like the stated review criterion.

**Caveat on alignment:** the notable-flag count operationalises Laya's own
question with thresholds chosen in the plan. Disagreement with those flags
is evidence that the model does not follow its own wording. It is **not**
evidence that its picks are worse (or better) for trading. Agreement would
not have been proof of usefulness either.

## 5. Synthetic paired-input sensitivity

**Setup:**
- Identity-free cards, one field changed at a time from an "ordinary"
  base: 0, 0, RSI 50, MACD 0, volume 1.0, volatility 0.25, range 0.5.
- Base score 0.2166. A duplicate base card scored identically.
- Threshold for "moves": |Δ| ≥ 0.0005.

| Field | Low → Δ | High → Δ | What the stated criteria would predict |
|---|---|---|---|
| five_day_return | −0.10 → −0.0009 | +0.10 → **+0.0290** | both ↑ (large move) |
| sma20_distance | −0.08 → −0.0107 | +0.08 → −0.0039 | both ↑ (stretched) |
| rsi14 | 25 → +0.0075 | 75 → −0.0112 | both ↑ (extreme) |
| macd_direction | −0.80 → −0.0146 | +0.80 → −0.0123 | both ↑ (strong swing) |
| relative_volume | 0.40 → −0.0205 | 3.00 → +0.0021 | high ↑ (unusual volume) |
| realized_volatility | 0.10 → −0.0065 | 0.60 → −0.0161 | not in the criteria |
| range_position_20d | 0.02 → −0.0180 | 0.98 → −0.0211 | both ↑ (stretched from range) |
| All-notable card | | → 0.1943 (**−0.0223**) | ↑ |

Every field moves the score, and the moves are similar in size to a real
day's whole score range (0.054–0.087). So Laya is responsive.

Only two of the eleven predicted rises occur: a +10% five-day move (the
largest effect) and high relative volume (barely). Most "notable" changes
*lower* the score, and the card that is notable on every field scores
below the ordinary one.

Synthetic responsiveness is not evidence of real-world utility.

## 6. Limitations

**Sample:**
- Three retrospective days, already seen in the smoke run.
- One coverage-selected pool, one model setting, k = 10.
- Bars were fetched now from the provider cache, not as a point-in-time
  vintage. They do reproduce the smoke run's inputs exactly.

**Controls:**
- The deterministic control is one arbitrary heuristic, not ground truth.
- The notable-flag thresholds are this plan's own operationalisation.
- Random ranges describe 20 draws, not a full null distribution.

**Not measured:**
- No outcome, return, Hermes or downstream review quality was measured.
- "Useful" cannot be decided from this design.

**Cost figures:** CPU timings on this machine only.

## 7. Scoped audit

| Area | Result |
|---|---|
| **Fairness** | Every method drew from the same 51-card complete set each day (also covered by `test_laya_controls.py`). No method saw extra fields. |
| **Cutoffs** | Every card's latest bar equals its session date. Later bars are dropped by the builder. The digests match the smoke run's point-in-time build. |
| **Controls** | Random and deterministic shortlists and Laya's top 10 recompute exactly from the private cards and scores. |
| **Privacy** | The public summary contains no row IDs or tickers. Cards, scores, ranks and the mapping stay in the git-ignored private folder. This report uses aggregates and identity-free synthetic cards only. |
| **Isolation** | No Hermes call, no trade, no backtest, evaluation or holdout, and no Neon write. Dashboard, rules, watchlists, pool and model settings are unchanged. The worker container was removed after the run. |
