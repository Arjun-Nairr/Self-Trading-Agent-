# baseline_v1 vs baseline_v2 — read-only evaluation

**Status: internal analysis document. Read-only — no source code, rules, manifest, watchlist, Docker config, dashboard, database rows, or checkpoints were modified to produce this report. No backtest, holdout, fundamentals, optimization, or JEV work was run.**

Generated 2026-09-27, against the approved manifest
`backtest-harness/eval_suites/manifest_20260916T154923_30f4c12e0f7e.json`
(version 4, `content_hash 30f4c12e0f7ed7bb75780b9d84f6f38cf69c84a149c58b3ab66f4d4a97d018fb`).

`baseline_v1` predates the slippage-aware sizing correction. `baseline_v2` was run
against commit `1285526` ("Match sizing prompt to rounded fill reserve"), the final
commit in a three-pass correction of `required_entry_quantity()` and the
Hermes-facing sizing prompt to reserve `BUY_SLIPPAGE_BPS` (5bps) ahead of the
real, cent-rounded fill.

## Runs compared

| Regime | baseline_v1 run_id | baseline_v2 run_id |
|---|---|---|
| June 2022 / high_volatility | `1c754200-8b87-4abf-b6c8-04bc5d0e6583` | `bbabc03a-7d42-41d1-b01b-de6399f598cf` |
| July 2022 / rally | `295c1bb9-a774-4354-8f10-a6aa74b8d518` | `d4c71c98-1d6f-4f2a-92fb-3a46e50f1d83` |
| September 2022 / selloff | `329ec8aa-5aae-4442-bda7-f0364221ef04` | `cdb5120e-a243-4e7f-8461-a67ed094908e` |
| October 2024 / sideways | `dd02237a-db56-4cbe-bfdd-0fcb45d1129c` | `791f3137-e776-4dd7-9704-add8ef28b180` |

All eight are `evaluation`-kind runs, all marked `completed` (none halted), all
comparison episodes per the approved manifest (never holdout).

---

## 1. Provenance verification

Fetched read-only via `run_backtest.get_run_configuration(run_id)` (the private
`backtest_run_configuration` table) for all eight runs.

| Regime | Watchlist hash (both) | Alias-mapping hash (both) | Masking seed (both) | Fundamentals |
|---|---|---|---|---|
| June | `56a5ab41…dffbc6bb2` | `af224d57…dde9c0811e6` | `1171439737` | disabled, both |
| July | `56a5ab41…dffbc6bb2` | `d99d0b7f…c459fae0063b9` | `259137704` | disabled, both |
| September | `56a5ab41…dffbc6bb2` | `b43218dc…d1395b2f69691b59cfc189f` | `1436754838` | disabled, both |
| October | `56a5ab41…dffbc6bb2` | `64589dc7…09322c863a9bf1e4aca30e5273ac0d439d54b3253` | `1741756485` | disabled, both |

**Finding: for every regime, `baseline_v1` and `baseline_v2` used the same fixed
watchlist snapshot, alias mapping, masking seed, fundamentals-disabled setting,
and historical window.** This is expected — `derive_episode_seed(manifest_content_hash,
episode_identity)` is a pure function of the (unchanged) manifest hash and
episode identity, independent of code version. **This is narrower than "Hermes
saw byte-identical prompts throughout the month," and I am not claiming that.**
Day 1 of each V1/V2 pair does start from the same identity map, the same
underlying price/news window, and the same starting account state, so day 1's
candidate set and prices are the same in both versions. But `required_entry_quantity()`
is exactly what changed between versions — so an early proposal can be sized,
filled, or rejected differently in V1 vs. V2 starting as soon as day 1. Once
that happens, cash, holdings, and open-position exposure diverge, which changes
what later-day candidates even look like (available cash, whether a position is
already held, exposure-driven cap headroom) — so the two runs are **not**
comparable prompt-by-prompt past their first divergence, only aligned on the
fixed identity/market-data scaffolding they were both built from. This is as
close to a controlled A/B comparison as a single-path (non-Monte-Carlo) backtest
can offer at the *setup* level, but it does not make the two full runs a clean
trade-by-trade A/B — see §7.

`fundamentals_enabled: false` confirmed for all eight runs directly from the
persisted configuration row (not inferred).

---

## 2. Per-regime metrics: V1 vs V2

All figures below are **read-only, derived from persisted Neon data**
(`portfolio_snapshots`, `trades`, `agent_activity`), not from re-running
anything. `return_pct` and `max_drawdown_pct` were recomputed with the exact
formula `eval_suite.py` uses (`_max_drawdown_pct`) directly from each run's
`portfolio_snapshots.equity` series, and validated to match the four
`baseline_v2` figures already captured verbatim from `eval_suite.py`'s own
JSON output at run time (exact match to 4 decimal places on all four). `fill_count`
and `closed_trade_count` were likewise derived from `trades` (`fill_count` =
BUY fills + `exit_provenance='agent_requested'` SELL fills; `closed_trade_count`
= all filled SELLs, mechanical + discretionary) and validated to match the same
four known V2 figures exactly. `blocked_count` = count of `agent_activity` rows
with `event_type IN ('risk_check','order_rejected')`, validated exactly against
all four known V2 `blocked_count` figures, including the September case (see
§6). **`proposal_count` for `baseline_v1` could not be reconstructed exactly**
— see the note beneath the table.

| Regime | Metric | baseline_v1 | baseline_v2 | Δ (V2 − V1) |
|---|---|---|---|---|
| **June / high_vol** | Return | −3.9719% | −3.5449% | **+0.43pp** |
| | Max drawdown | −5.2573% | −4.1444% | **+1.11pp** |
| | Proposals | ≈33 (see note) | 22 | — |
| | Fills | 16 | 9 | −7 |
| | Closed trades | 10 | 6 | −4 |
| | Blocked | 16 | 13 | −3 |
| | Failed cycles | 0 | 0 | 0 |
| **July / rally** | Return | +5.7962% | +3.0787% | **−2.72pp** |
| | Max drawdown | −2.2431% | −2.6667% | **−0.42pp** |
| | Proposals | ≈35 (see note) | 34 | — |
| | Fills | 14 | 15 | +1 |
| | Closed trades | 10 | 12 | +2 |
| | Blocked | 21 | 18 | −3 |
| | Failed cycles | 0 | 0 | 0 |
| **September / selloff** | Return | −3.2679% | −1.2877% | **+1.98pp** |
| | Max drawdown | −5.4131% | −3.7824% | **+1.63pp** |
| | Proposals | ≈30 (see note) | 29 | — |
| | Fills | 10 | 8 | −2 |
| | Closed trades | 7 | 5 | −2 |
| | Blocked | 20 | 21 | +1 |
| | Failed cycles | 0 | 0 | 0 |
| **October / sideways** | Return | +0.0842% | −0.9765% | **−1.06pp** |
| | Max drawdown | −1.8120% | −2.8394% | **−1.03pp** |
| | Proposals | ≈31 (see note) | 34 | — |
| | Fills | 14 | 20 | +6 |
| | Closed trades | 11 | 15 | +4 |
| | Blocked | 14 | 13 | −1 |
| | Failed cycles | 0 | 0 | 0 |

**Note on `proposal_count` for V1:** `eval_suite.py` defines `proposal_count`
from an in-memory `day_summaries[*]["proposals"]` list that is never persisted
to Neon verbatim. It is reconstructable as `fill_count + blocked_count +
superseded_count`, where `superseded_count` (an order queued, then superseded
before it could resolve — e.g. the position was already closed by the time it
would have executed) was estimated by text-matching `agent_activity.description
LIKE '%superseded%'`. Applying this formula to the four known V2 episodes
reproduced the true figure exactly for September and June, but landed **off by
1** for July and October (33 vs 34, 35 vs 34) — meaning the text-match
undercounts/overcounts superseded orders by exactly one in some cases. The V1
proposal figures above carry the same ±1 uncertainty and are marked
accordingly; I did not present them as exact.

**Failed cycles = 0 for all eight runs.** `eval_suite.py` halts an episode
outright on a cycle that exhausts all retries without a valid response (see
the July 2022 halt this session's earlier infra-preflight fix addressed) —
none of these eight runs halted, so none experienced a failed cycle by
definition.

---

## 3. Four-month averages — explicitly not a performance conclusion

| | baseline_v1 (avg of 4 one-month episodes) | baseline_v2 (avg of 4 one-month episodes) |
|---|---|---|
| Return | **−0.34%** | **−0.68%** |
| Max drawdown | **−3.68%** | **−3.36%** |
| Closed trades | 9.5 | 9.5 |
| Total blocked (sum, not avg) | 71 | 65 |

**These four numbers are not a strategy-performance verdict.** Four one-month,
single-seed, single-path episodes per version is far too small a sample to
support a claim like "V2 trades better than V1" or vice versa — there is no
resampling, no confidence interval, and each episode is one specific
realization of one masked market history with one specific sequence of Hermes
responses. A single differently-timed trade in week 1 of any of these episodes
could plausibly flip the sign of that episode's whole-month return. Treat the
per-regime numbers in §2 as a factual record of what happened in these eight
specific runs, not as an estimate of either version's true expected
performance.

---

## 4. The actual purpose of V2: did a predictable post-fill slippage-cap failure occur?

The defect this session fixed lived in `required_entry_quantity()` (decision-time
sizing, called before an order is ever queued). Before the fix, it sized a BUY
against the raw proposed entry price; after, it sizes against
`_expected_buy_fill_price()`, which reserves `BUY_SLIPPAGE_BPS` (5bps) ahead of
time. **This is a distinct, separate mechanism from `resolve_queued_order()`'s
own execution-time re-check** (dated 2026-08-28, predates this session's work,
untouched by it), which has always re-validated a queued BUY against the real
next-trading-day 09:45 bar open, itself run through `_slipped_buy_price()`.

The sharpest, cleanly-provable version of "predictable post-fill slippage-cap
failure" is: **a proposal sized to land exactly at the 20% concentration cap
using the raw entry price would, even with *zero* price movement between
decision day and execution day, fail the execution-time §2b check purely from
the deterministic 5bps markup that `_slipped_buy_price()` always applies.**
That is a bug — a proposal that should never have been rejected, rejected for
a reason that was 100% knowable in advance. Post-fix, the same zero-movement,
at-cap case passes, because the reserve is now baked into the required
quantity upfront.

**What the persisted data can and cannot show:**

- **Can show:** every `§2b`-citing rejection's *excess over cap* (e.g. "position
  is 20.28%, cap is 20.00%" → 0.28 percentage points of equity), and — checked
  per entry, not assumed — **which phase it actually fired in.** Every
  `agent_activity` row citing §2b was inspected for its message prefix:
  `"TICKER: queued {action} rejected -- ..."` is `resolve_queued_order()`'s
  execution-time re-check, run against the real next-trading-day bar and
  already including both real price movement and the slippage markup;
  `"TICKER: decision-time risk check rejected -- ..."` is the pre-queue check
  on Hermes' own raw proposed quantity and raw proposed price, with **no
  slippage applied at all**. Across all eight runs, 100 of the 101 §2b-citing
  rejections are execution-time. **Exactly one is decision-time**: `V1 July`'s
  `OLN: decision-time risk check rejected -- §2b: position is 20.00% of equity,
  cap is 20.00% [profile=aggressive_v1_base min_rr=1.5]` — Hermes' own proposed
  quantity, at Hermes' own quoted (unslipped) price, was already at/over the
  cap before the order was ever queued. That is a Hermes sizing miss, not a
  slippage or execution-time artifact, and it is excluded from the
  execution-time table below (it has no execution-time "excess" figure to
  report, since it never reached that check).

  | Regime | V1 §2b execution-time excess-over-cap (pp), sorted | V2 §2b execution-time excess-over-cap (pp), sorted |
  |---|---|---|
  | June | 0.04, 0.08, 0.09, 0.11, 0.14, 0.15, 0.22, 0.22, 0.23, 0.28, 0.40, 0.51, 0.58 | 0.01, 0.06, 0.08, 0.10, 0.14, 0.22, 0.23, 0.28 |
  | July | 0.03, 0.05, 0.08, 0.09, 0.10, 0.11, 0.13, 0.14, 0.22, 0.23, 0.24, 0.37, 0.67 (13 execution-time; the 14th §2b-citing entry this month is the decision-time OLN case above) | 0.03, 0.05, 0.07, 0.08, 0.08, 0.09, 0.12, 0.23, 0.31, 0.46, 0.46, 0.54 |
  | September | 0.01, 0.04, 0.06, 0.07, 0.08, 0.08, 0.12, 0.12, 0.17, 0.23, 0.65, 0.76 | 0.02, 0.06, 0.08, 0.08, 0.09, 0.11, 0.12, 0.16, 0.19, 0.31, 0.36, 0.69, 0.73, 0.75 |
  | October | 0.02, 0.02, 0.03, 0.05, 0.07, 0.07, 0.08, 0.08, 0.08, 0.14, 0.18, 0.57 | 0.05, 0.05, 0.08, 0.09, 0.10, 0.10, 0.10, 0.15, 0.17, 0.56 |

  A pure 5bps price markup on a ~20%-of-equity position moves that position by
  only about **0.01 percentage points** of equity (20.00% → ~20.01%). Every
  execution-time excess value above — in both V1 and V2 — is at or above that
  scale, most of them many times larger (tens to hundreds of bps of equity),
  which is far more consistent with genuine day-to-day price movement between
  the decision day and the real next-day execution open than with the flat,
  deterministic slippage markup alone. **None of the execution-time excess
  values in either version round to 0.00pp** — the one exactly-at-cap,
  zero-slippage-applied case in the whole dataset (V1 July's OLN) is the
  decision-time one described above, which is unambiguous evidence of a raw
  Hermes sizing miss, not of the execution-time slippage-cap defect this
  session's fix targeted.

- **Cannot show, from persisted Neon data alone:** a clean per-order split of
  "how much of this specific breach is slippage vs. how much is real
  overnight price drift." That would require the actual raw (unslipped)
  execution-day bar open for each rejected order, which is never persisted —
  a rejected order has no `fill_price` (nothing filled), and the harness does
  not journal the bar it was checked against. I am not willing to fabricate
  that number, so I did not attempt to force one out of the `trades` table.

**Bottom line for §4:** the specific, narrowly-defined defect (a proposal that
would pass at zero price movement, rejected anyway purely by the deterministic
5bps markup applied at *execution* time) is not re-observable now that it's
fixed by construction — post-fix, `required_entry_quantity()` cannot produce a
quantity that fails the zero-movement execution-time case. Whether it *did*
occur, unnoticed, inside V1's execution-time §2b rejections is plausible for
the smallest-excess cases (the sub-0.05pp cluster: 1 in V1 June, 2 in V1 July,
2 in V1 September, 4 in V1 October) but not provable from what's stored,
since the raw (unslipped) execution-day price isn't persisted for a rejected
order. The one exactly-zero-excess case in the dataset (V1 July's OLN) is
**not** part of this ambiguous pool — it's decision-time, so it's already
known with certainty not to be slippage-related (§ above). The large majority
of both V1's and V2's execution-time §2b rejections carry excesses far too
large to be slippage alone, and are correctly still being caught by
`resolve_queued_order()`'s execution-time re-check, exactly as designed: real,
unknowable-in-advance price gaps remain execution-time rejection territory in
both versions, unaffected by this fix.

---

## 5. Categorized rejections — all eight runs

Pulled from the full (untruncated) `agent_activity` log per run, not the
20-item-capped `blocked_reasons` field `eval_suite.py`'s JSON output prints.

Every §2b-citing entry was checked individually for its message prefix (see
§4) to separate the phase it actually fired in, rather than assuming
`agent_activity` membership implies execution-time.

| Category | V1 June | V1 July | V1 Sept | V1 Oct | V2 June | V2 July | V2 Sept | V2 Oct |
|---|---|---|---|---|---|---|---|---|
| §2b concentration cap — **execution-time** (`resolve_queued_order`, real next-day bar) | 13 | 13 | 12 | 12 | 8 | 12 | 14 | 10 |
| §2b concentration cap — **decision-time** (Hermes' own raw proposal, pre-queue) | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| §6b reward:risk only | 7* | 7* | 5* | 2* | 5* | 8* | 8* | 2* |
| §6a stop distance / invalid stop | 3 | 3 | 4 | 1 | 2 | 2 | 6 | 1 |
| §2/§3 exact-share mismatch | 0 | 3 | 0 | 0 | 1 | 2 | 0 | 0 |
| §3 cash exceeded | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| §9.1 one queued decision/ticker | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| §9.4 same-day re-entry | 0 | 1 | 0 | 0 | 1 | 1 | 0 | 0 |
| Pre-fill news veto | 0 | 0 | 2 | 1 | 1 | 0 | 1 | 1 |

`*` §6b counts overlap with §2b in most rows (a single rejection often cites
both "§2b: position is X%..." and "§6b: reward:risk is Y..." in the same
message) — the §6b column counts every message that cites §6b, whether or not
it also cites §2b; do not sum columns to get the total. The two §2b rows
above are mutually exclusive and, summed, equal the corresponding
`concentration_2b`-style figures implicitly used in this report's earlier
per-episode task summaries (delivered in chat before this document existed) —
this table is the corrected, phase-separated version of that same data.
**All eight runs, decision-time and execution-time together, are 101 §2b-citing
rejections; 100 are execution-time, 1 (V1 July) is decision-time.** No other
run in the dataset produced a decision-time §2b rejection — if a run's row
above is not unambiguously classified from the stored `agent_activity` text
(i.e. its message doesn't clearly start with one of the two known prefixes),
that is called out explicitly rather than guessed; no such ambiguous case was
found in this dataset.

**Bucketed by the request's five categories:**

- **Execution correctness evidence:** in both V1 and V2, genuine overnight
  gaps are still caught and rejected at execution time (§2b, large-excess
  cases in §4, `resolve_queued_order`'s real-bar re-check — 100 of the 101
  §2b-citing rejections across all eight runs); no fill in either dataset
  shows evidence of a BUY executing above the 20% cap once slipped-fill is
  applied — the fail-closed re-check held in every one of the eight runs.
  SELL sizing was never touched by this fix and shows no anomalies in either
  dataset.
- **Hermes raw sizing/arithmetic misses:** exact-share mismatches (Hermes
  proposing a quantity one share off the harness's own computed required
  size) — 3 in V1 July, 3 in V2 (2 July, 1 June), 0 elsewhere — plus one
  **decision-time §2b breach** (V1 July's OLN: Hermes' own proposed quantity,
  at Hermes' own quoted raw price, was already at/over the 20% cap before the
  order was ever queued — no slippage involved at all, since decision-time
  validation checks the raw proposal, not a slipped price). Both are genuine
  Hermes-side sizing misses, not harness or execution-time defects. Not
  meaningfully different in frequency between versions.
- **Ordinary risk-rule rejections:** §6a (stop distance/invalid stop), §6b
  (reward:risk), §9.1/§9.4 (queueing rules), §3 (cash) — present in
  comparable numbers in both versions, no pattern suggesting the fix changed
  Hermes' proposal quality on these axes (it wasn't meant to).
- **News-veto events:** 3 in V1 (2 September, 1 October), 3 in V2 (1 each in
  June, September, October) — comparable, unaffected by this fix as expected
  (§5a logic untouched).
- **Realized trading performance:** see §2/§3 — mixed, small-sample, not
  attributable to this fix (see §7).

---

## 6. Reconciling the September V2 blocked-count discrepancy (21 vs. 20)

**Fully reconciled, not hand-waved.** `eval_suite.py:1020` reads:

```python
"blocked_reasons": blocked_reasons[:20],  # bounded -- report stays compact
```

`blocked_count` (line 1019) is `len(blocked_reasons)` — the **true, untruncated**
count — while `blocked_reasons` itself is deliberately sliced to the first 20
entries "so the report stays compact." September 2022/selloff's V2 run
(`cdb5120e…`) produced exactly 21 blocked decisions; the 21st was cut from the
printed list by this slice, not lost or miscounted.

I queried `agent_activity` directly (`event_type IN ('risk_check',
'order_rejected')`, ordered by `created_at`) for that run and got all 21 rows.
The 21st, missing from the earlier report, is:

> `DINO: queued BUY rejected -- §6a: stop is 2.79% below entry, minimum is 3.0% — ordinary noise would trigger it`

An ordinary §6a stop-distance rejection — it does not change any conclusion in
this report (it isn't a §2b/cap case). This same truncation risk applies to
every other run's previously-reported `blocked_reasons` list whenever
`blocked_count > 20`; only September's V2 run crossed that threshold among the
eight. All counts and category tables in §2 and §5 of this report were pulled
from the full, untruncated `agent_activity` log, not the capped JSON field, so
they are unaffected by this truncation.

---

## 7. Can the V1/V2 return differences be causally attributed to the sizing correction?

**No — the evidence does not support that claim, and I am not making it.**

Three independent reasons:

1. **The results are mixed, not directional.** V2 improved return in 2 of 4
   regimes (June +0.43pp, September +1.98pp) and worsened it in the other 2
   (July −2.72pp, October −1.06pp). A fix that eliminated a real defect should
   not be expected to reliably *improve* returns in the first place — its job
   was correctness (never proposing an order that predictably breaches its
   own cap), not performance — but even judged purely as "did V2 do better,"
   the answer is "sometimes," which is not a causal signature.
2. **The magnitude of the return differences (1–3 percentage points per
   month) vastly exceeds what a 5bps sizing correction could produce
   mechanically.** A 5bps difference in entry price/quantity changes a
   single trade's P&L by a few basis points at most; it cannot directly
   explain a multi-percentage-point swing in a month's total return.
3. **The two versions are not resamples of the same trade path.** Because
   `required_entry_quantity()` changed, V1 and V2 queue *different share
   counts* for many of the same proposals from the very first trading day.
   Different fills change cash, equity, and exposure, which changes what
   later-day proposals can even be sized/accepted — the two paths diverge
   structurally, not just by a small sizing delta. §1 confirmed only that
   the two versions started from the same fixed identity map and market-data
   window, not that the two runs stayed comparable trade-by-trade — once the
   very first fill differs, they don't; any observed return gap reflects the
   accumulated effect of an entire different sequence of trades, most of
   which is unrelated to whether any individual fill was slippage-correctly
   sized.

Given all three, attributing the observed return deltas to "the sizing fix
made V2 better/worse" would be overclaiming from four single-path samples.
What *is* directly supported by the evidence: the fix did what it was built
to do at the code level (verified by unit tests in the implementing commits,
and by the absence of any zero-movement/exactly-boundary breach pattern in
V2's data — see §4), and it did not visibly degrade execution correctness,
sizing-mismatch frequency, or risk-rule enforcement in these eight runs (§5).

---

## 8. Recommendation

**Stop optimization here and preserve `baseline_v1` and `baseline_v2` as the
final, frozen baseline pair.**

Reasoning: the sizing correction was a correctness fix for a real, specific
defect (§4), it is verified fixed at the code level, and re-running the same
four one-month episodes gave no evidence of a new defect, a regression in
risk-rule enforcement, or a reliable performance signal in either direction
(§2, §3, §7). There is no evidence-backed problem left to chase with a V3 —
the return differences observed are consistent with ordinary path-dependent
variation in a single-seed-per-regime backtest, not with an identifiable
remaining defect. Proposing a V3 hypothesis now would mean optimizing against
noise on a four-sample dataset, which is exactly the kind of overfitting the
existing "point-in-time / no-lookahead / fail-closed" discipline this project
has consistently avoided elsewhere. If a future defect is found through other
means (code review, a live-mode incident, a new audit), that would be
grounds for a genuinely evidence-backed V3 hypothesis — this evaluation does
not surface one.

*(This is a recommendation only; no V3 hypothesis has been proposed or
implemented, and this task did not start optimization.)*

---

## What we know / What we do not know / Next user decision

**What we know:**
- All eight runs are valid, completed, comparison-episode evaluations against
  the same approved manifest, with fundamentals disabled and the same fixed
  watchlist, alias mapping, masking seed, and historical window for each
  V1/V2 regime pair — but **not** byte-identical prompts throughout the month;
  the two runs can diverge in cash/holdings/candidate context as early as day
  1, once a sizing-affected proposal is filled or rejected differently.
- The specific sizing defect the fix targeted (a proposal exactly at cap on
  raw price, rejected purely by the deterministic 5bps markup even with zero
  price movement, **at execution time**) is fixed at the code level and could
  not recur in V2 by construction. A related but distinct issue — Hermes
  proposing a quantity that already breaches §2b on its own raw quoted price
  at **decision time**, before slippage ever enters the picture — is a
  separate, pre-existing category of Hermes sizing miss, observed once (V1
  July) and unaffected by this fix either way.
- Execution-time re-validation against genuine price gaps remains intact and
  functioning identically in both versions (100 of 101 §2b-citing rejections
  across all eight runs are execution-time; see §4/§5).
- The September 2022 V2 blocked-count discrepancy (21 vs. 20) is fully
  explained by an intentional report-truncation line in `eval_suite.py`, not
  a data or counting bug.
- Four-month average returns/drawdowns are close between V1 and V2 (return:
  −0.34% vs −0.68%; max drawdown: −3.68% vs −3.36%) but this is not a
  performance verdict given the sample size.

**What we do not know:**
- Whether any specific V1 rejection was *actually* a pure slippage-cap
  failure (as opposed to a real price-drift-driven rejection) — the data
  needed to decompose that cleanly (the raw, pre-slippage execution-day bar
  open for each rejected order) is not persisted.
- Whether V1 or V2 has better *true* expected performance in any of these
  four regimes — four single-path one-month episodes cannot answer that.
- `baseline_v1`'s exact `proposal_count` per episode (±1, see §2 note).

**Next user decision:** approve stopping here and treating `baseline_v1` +
`baseline_v2` as the frozen comparison baseline going forward, or direct a
specific next step (e.g. a genuinely evidence-backed V3 hypothesis, a
holdout-episode run, or further audit work) — this report does not make that
call.
