# Round 2 — Benchmark & Sensitivity Analysis

**Scope:** post-hoc, read-only evaluation of the completed Round 2 run
(`run_id e68525f9-ce89-4588-a57e-866f28cd99fd`, 50 completed cycles) against
simple alternatives over the identical dates, plus a mechanical sensitivity
pass over `RULES.md`'s current numeric parameters. **This is not permission
to optimize values, change trading behaviour, or run more training** — see
§8.

**Reproduce:** `python backtest-harness/benchmark_sensitivity.py` (writes
`backtest-harness/.benchmark_sensitivity_data.json`, gitignored — raw
machine-readable output, not committed). Tests:
`python backtest-harness/test_benchmark_sensitivity.py`.

---

## 1. Configuration (exact, for reproduction)

| Parameter | Value |
|---|---|
| Evaluation window | `2026-02-17` → `2026-04-28` inclusive |
| Trading days in window | 50 (verified below — matches Hermes' 50 completed cycles exactly) |
| Starting baseline, every strategy | **\$100,000.00**, explicit constant, never inferred from a curve's first mark |
| Market data provider | yfinance (daily bars), Alpaca market-data API (15-minute intraday bars + trading calendar) — same providers `run_backtest.py` itself uses |
| Neon read | `portfolio_snapshots` (`run_id`, `mode='backtest'`), SELECT-only |
| Fill/slippage model | Identical to Hermes' own: `_slipped_buy_price` (5bps against direction, clamped to the fill bar's own [low, high]) at the first 09:45 ET bar after a signal — same function `run_backtest.py` and `counterfactual.py` use |

### Why the window ends 2026-04-28, not the `2026-04-29` cursor

`HANDOFF.md` states cursor `2026-04-29`. That is the **next, not-yet-run**
trading day, not the 50th evaluated one — verified mechanically by
converting every one of the 50 real `portfolio_snapshots.day_label` values
("Day N") to a real date (`2026-02-17 + (N−1)` calendar days) and confirming
all 50 land on real weekdays no later than `2026-04-28`. Using `2026-04-29`
here would have silently compared Hermes' 50 cycles against a 51-trading-day
benchmark window. `benchmark_sensitivity.py` hard-fails
(`len(trading_days) != 50`) if this ever drifts.

---

## 2. Benchmark definitions

All four alternatives share the same \$100,000 baseline, the same 50
trading days, and the same entry-fill/slippage convention as Hermes' own
first possible BUY (next-day 09:45, 5bps slippage). None use any
information not available at that point in time.

- **Cash** — \$100,000 flat throughout. Trivial, included as the zero
  bound.
- **SPY buy-and-hold** — enter 100% of \$100,000 in `SPY` at day 1's 09:45
  fill; mark to each day's real close; never rebalanced.
- **Equal-weight watchlist buy-and-hold** — the same, split 10 ways evenly
  across the real frozen watchlist (`watchlist/watchlist.json`), each
  entered at its own day-1 09:45 fill.
- **Technical baseline — SMA(10) close-crossover, one pooled \$100,000
  account** (fully defined below, before any result was computed):
  - **Signal**, per ticker, independent: today's close crosses above its
    own trailing 10-trading-day SMA of closes (`yesterday close ≤ SMA10
    yesterday` and `today close > SMA10 today`), using only closes up to
    and including "today" — no lookahead. A standard, un-tuned
    trend-following entry, not fit to this window.
  - **Entry:** next trading day, 09:45, same fill/slippage as above.
  - **Stop:** a fixed 5% below the signal day's close (the midpoint of
    `RULES.md` §6a's 3–12% band) — set at signal time, not re-derived from
    real support/resistance (keeps this baseline computable from the same
    daily bars already fetched for buy-hold, and keeps it un-tuned).
  - **Target:** entry + 1.5 × risk (`RULES.md` §6b base tier).
  - **Exit:** the same mechanical primitives Hermes' own trades use
    (`evaluate_opening_gap`, `evaluate_bar_stop_target`), walked day by
    day, with a 5-trading-day time stop (`RULES.md` §6c).
  - **Sizing:** `RULES.md` §2's own formula — 2% of **current account
    equity** risk per trade, capped at 20% of **current account equity**
    per position, never exceeding available cash — against **one pooled
    \$100,000 account** shared by all 10 tickers (not a pre-divided
    per-ticker slice; a prior draft of this script made that mistake —
    see §7). Multiple concurrent positions are possible, exactly as
    Hermes' own run allows. One open lot per ticker at a time
    (`RULES.md` §9.1).
  - **Tie-break:** same-day signals across tickers are processed in fixed
    alphabetical ticker order — deterministic, not a setup-quality
    ranking.
- **Hermes (actual)** — the real, already-verified 50-cycle equity curve
  (`docs/evaluations/round2_50cycle_evaluation_extract.md`), re-pulled here
  directly from `portfolio_snapshots` for a same-source comparison.

**Fail-loud data policy:** every curve raises `MissingDataError` immediately
if a bar is absent for a ticker currently held or weighted — no held
position's value is ever silently dropped from an equity mark (verified by
`test_benchmark_sensitivity.py`). No such gap occurred in this window; the
watchlist and SPY both have full daily coverage across it.

---

## 3. Benchmark results — 🟢 Verified

| Strategy | End equity | Total return | Max drawdown | Ann. volatility | Return/volatility | Avg. exposure |
|---|---|---|---|---|---|---|
| Cash | \$100,000.00 | 0.00% | 0.00% | 0.00% | — | 0% |
| SPY buy-and-hold | \$104,202.06 | **+4.20%** | −8.53% | 15.30% | 0.28 | 99.4% |
| Equal-weight watchlist buy-and-hold | \$97,337.07 | **−2.66%** | −7.82% | 16.64% | −0.16 | 99.7% |
| Technical baseline (SMA10, pooled) | \$92,647.02 | **−7.35%** | −8.28% | 16.01% | −0.46 | 33.9% |
| **Hermes (actual)** | **\$103,696.08** | **+3.70%** | **−2.46%** | **9.04%** | **0.41** | **20.0%** |

"Return/volatility" is a crude return-per-unit-of-risk ratio (no
risk-free-rate subtraction — not a formal Sharpe ratio; flagged as an
🟡 **Interpretation** aid, not a verified statistic on its own).

**Trade count / turnover:** Hermes — 16 closed + 1 open (verified,
`docs/evaluations/round2_50cycle_evaluation_extract.md` §2). Technical
baseline — 30 closed-or-resolved trades across the window (mechanically
counted from its own trade log; win rate 11/30 = 36.7%). Buy-and-hold
benchmarks and cash have no turnover by construction.

**Reconciliation check:** Hermes' end equity here (\$103,696.08, +3.696%)
matches the compact evaluation brief's own figure exactly — same source
table, independently re-queried.

### Did Hermes add measurable value?

- **vs. Cash:** yes — Hermes beat cash by 3.70 points of return while cash
  is definitionally riskless; not a meaningful comparison beyond the floor
  it sets.
- **vs. SPY buy-and-hold:** 🟡 **Interpretation, not decisive.** SPY's raw
  return (+4.20%) edges Hermes' (+3.70%) by 0.5 points, but Hermes did it
  at roughly a fifth of SPY's exposure (20.0% vs. 99.4%), a third of its
  volatility (9.0% vs. 15.3%), and well under a third of its drawdown
  (−2.46% vs. −8.53%) — Hermes' return-per-unit-of-risk ratio (0.41) is
  the highest of any strategy compared, including SPY's (0.28). Whether
  that risk-adjusted edge is "real" or an artifact of only 16 trades and
  one favorable 50-day SPY stretch is exactly the kind of claim §6's
  small-n uncertainty caveats.
- **vs. equal-weight watchlist buy-and-hold:** yes, clearly — the raw
  watchlist would have **lost** 2.66% held passively over the identical
  dates; Hermes' selectivity and exits produced a positive return over
  the same universe.
- **vs. the technical baseline:** yes, clearly — the un-tuned SMA10
  baseline lost 7.35% with worse volatility and a worse drawdown than
  Hermes, despite similar-order exposure discipline (both well under
  100%). This is the most direct same-universe, same-execution-timing
  comparison available, and Hermes outperforms it by 11 points of return.

### Limitations on this comparison

- 50 trading days and 16 Hermes trades is a short window for any of the
  above to be a durable performance claim — see §6.
- The technical baseline's own parameters (SMA(10), 5% stop, 1.5:1
  target, 5-day time stop) were fixed by this report's author, not swept
  or fit to this window — but a *different* simple baseline (a different
  SMA length, a different indicator entirely) could show a materially
  different result. One baseline is not a survey of baselines.
- SPY buy-and-hold has no downside protection at all (no stop); Hermes
  and the technical baseline both do — the volatility/drawdown comparison
  partly reflects that structural difference, not skill.

---

## 4. Sensitivity analysis

Evaluated **only** where the effect is mechanically recoverable from
stored evidence — no assumption that Hermes would have proposed identical
trades under different rules, and no filter-only replay presented as a
faithful alternative strategy run (per this task's own instructions).

**Data source:** the real, hash-verified 16-trade closed ledger from
`docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md` §3.1
(fill/exit/qty/stop/target/expected-R:R/exit-reason for every trade),
hardcoded in `benchmark_sensitivity.py`'s `LEDGER` constant and cross-checked
against it. **Effective sample size: n = 16 closed trades** — well short of
`RULES.md` §6b's own stated 30–60-trade six-month target; every finding
below is qualified accordingly.

### 4.1 Risk-per-trade % × concentration cap % — 🟢 Mechanical counterfactual, fully replayable

**Classification: fully replayable.** Each historical trade's real
entry/stop/exit *prices* are unchanged (mechanical and discretionary exits
were price- or time-triggered, never size-triggered) — only quantity, and
therefore \$ P&L, differs. **This parameter pair scales P&L; it does not
change which trades win or lose, or how many complete.**

| Risk % ＼ Cap % | 10% | 20% | 30% |
|---|---|---|---|
| **1%** | +1.79% | +3.51% | +4.22% |
| **2%** (RULES.md current) | +1.79% | **+3.55%** | +5.31% |
| **3%** | +1.79% | +3.55% | +5.31% |

(2%/20% ≈ +3.55% vs. the real recorded +3.70%/+\$3,443.73 — the small gap
is the sequential-equity-compounding approximation described in §7, not a
data error.)

**Strongest justified conclusion:** **the concentration cap, not the
risk-per-trade percentage, is the binding constraint at every cap value
this run's trade sizes actually needed.** At cap=10%, all three risk%
values produce the identical return (1.79%) — the cap ceiling is reached
before risk% ever matters. Risk% only starts to differentiate results once
the cap is loosened to 30% AND risk% is dropped to 1% (4.22% vs. 5.31%
at 2–3%). This is a real, mechanically verified interaction, not a
guess: **loosening the cap has a larger effect on this ledger's return
than loosening the risk budget does**, at every point checked in this
grid.

### 4.2 Minimum reward:risk threshold — 🟡 One-sided, filter-only counterfactual

**Classification: one-sided/filter-only.** A trade whose recorded Expected
R:R falls below the new threshold is removed entirely (never fills under
that rule); this analysis has no row-level geometry for the 45 decision-time
and 31 execution-time BUY proposals that were rejected before ever reaching
the journal (see the compact evaluation's §4 funnel), so **a looser
threshold's potential upside — trades that might have newly qualified — is
not estimated.** This is a real limitation, not an oversight; the
counterfactual can only ever look "worse," never "better," because of what
data exists.

| Min R:R threshold | Trades retained | Total return | Win rate |
|---|---|---|---|
| ≥1.25 (escalated tier) | 16/16 | +3.45% | 50.0% |
| ≥1.5 (**current base tier**) | 16/16 | +3.45% | 50.0% |
| ≥1.75 | 14/16 | +2.21% | 50.0% |
| ≥2.0 (Round 1's old threshold) | 5/16 | **−0.70%** | 40.0% |

**Classification: outcome-changing** (unlike §4.1) — trade count and win
rate both move, not just \$ magnitude.

**Strongest justified conclusion:** raising the minimum reward:risk back
toward Round 1's old 2.0 threshold would have **removed 11 of the 16
trades that actually happened** and turned the realized result negative —
but this says nothing about whether a 2.0-only Hermes would have proposed
*different, better* trades instead of simply fewer of the same ones. 🟡
Interpretation, not proof either way.

### 4.3 Time-stop horizon (`RULES.md` §6c, currently 5 trading days) — 🔴 Not mechanically identifiable

9 of 16 closed trades (56%) exited via the time stop (2 mechanical + 7
discretionary-at-time-stop) — a very large share, so return is plainly
**sensitive** to this parameter. **The sign and size of that sensitivity
cannot be computed honestly from stored evidence**: extending or
shortening the horizon requires each trade's real intraday/daily price
path beyond (or before) its actual exit date, which requires resolving
each trade's real ticker identity from its masked alias — deliberately not
attempted in this pass (see §6). Labeled `not_mechanically_identifiable`,
not defaulted to "no effect."

### 4.4 Stop-distance boundaries (`RULES.md` §6a, currently 3–12%) — 🔴 Not mechanically identifiable

Hermes' stop is its own technical-level judgment (real support/resistance),
not a formula — changing the 3–12% band would change *which technical
level qualifies*, which only re-running Hermes' own reasoning against a new
band can answer. This report does not simulate Hermes' judgment, so this
parameter is labeled `not_mechanically_identifiable` rather than assigned
a fabricated ranking.

### 4.5 Time-stop × reward-threshold interaction — 🔴 Not mechanically identifiable

Inherits §4.3's limitation directly: an interaction cannot be measured
when one of its two axes is not itself measurable from stored evidence.

### 4.6 Ranking, with confidence labels

| Parameter | Effect observed | Classification | Confidence |
|---|---|---|---|
| Concentration cap % | Large, dominant at tight caps (§4.1) | Fully replayable | 🟢 High (mechanical, n=16) |
| Risk-per-trade % | Small, only matters when cap is loose | Fully replayable | 🟢 High (mechanical, n=16) |
| Min reward:risk threshold | Large — a stricter threshold flips the sign of realized return | One-sided filter-only | 🟡 Medium (real trades only; no rejected-proposal upside estimated) |
| Time-stop horizon | Plausibly large (56% of trades resolved by it) | Not mechanically identifiable | 🔴 Unknown (sign/size unrecoverable without real price paths) |
| Stop-distance bounds | Unknown | Not mechanically identifiable | 🔴 Unknown (requires re-running Hermes' judgment) |

---

## 5. Data-quality checks — 🟢 Verified

- **Exact date alignment:** all 50 Hermes `portfolio_snapshots` dates
  (derived from `day_label`) fall inside the fetched 50-trading-day
  benchmark window with zero mismatches — hard-checked in
  `benchmark_sensitivity.py`'s `main()` (`SystemExit` on any mismatch).
- **No future-data leakage:** every benchmark's signal/entry decision uses
  only bars dated on or before the decision day; SMA(10) warmup uses a
  dedicated 15-real-trading-day lookback window strictly before
  `PERIOD_START`; every entry executes at the *next* trading day's 09:45
  bar, never the signal day itself.
- **Identical starting capital:** `STARTING_EQUITY = $100,000.00`, one
  shared constant, used directly (not inferred from any curve's first
  point) in every `metrics()` calculation — regression-tested in
  `test_benchmark_sensitivity.py`.
- **Reproducible formulas/results:** `risk_cap_grid` and
  `min_rr_threshold_filter` are pure functions over a hardcoded, cited
  ledger — re-running produces byte-identical output (tested).
- **Current Hermes metrics reconcile** with the compact evaluation extract
  (§3, end equity \$103,696.08 / +3.696%, matching exactly).
- **No lookahead / no held-position value silently dropped:** verified by
  `test_benchmark_sensitivity.py`'s `MissingDataError` tests; not
  triggered in the actual run (full data coverage).

---

## 6. Unknowns

- Whether Hermes' small real-return edge in return-per-unit-of-risk over
  SPY (§3) reflects genuine skill or 50-day/16-trade noise — cannot be
  settled with this sample size.
- The sign and magnitude of the time-stop horizon's effect (§4.3) —
  requires real per-ticker price paths this pass deliberately did not
  fetch, to keep the masked-alias mapping unreconstructed (an earlier
  attempt in this same investigation to derive that mapping for internal
  use only was itself declined by this session's own tooling guardrails —
  consistent with, not a workaround of, the masking-secrecy invariant).
- Whether a looser reward:risk threshold would have surfaced better
  trades Hermes never got to propose (§4.2) — no rejected-proposal
  geometry exists to check.
- Whether Hermes' own technical-level stop selection would land inside a
  different 3–12% band if that band changed (§4.4) — requires re-running
  Hermes' judgment, out of scope here.
- Whether a different simple technical baseline (different SMA length,
  RSI-based, MACD-based) would beat or lag the one chosen here — only one
  baseline was built, by design (the task calls for one).

---

## 7. Known simplifications (stated, not hidden)

- The risk%/cap% grid (§4.1) compounds equity sequentially in **entry
  order** using each trade's own realized \$ P&L — it does not model the
  exact cash effect of same-day overlapping entries or intra-hold
  unrealized equity swings the real harness tracks. The 2%/20% cell's
  close match to the real recorded result (+3.55% vs. +3.70%) suggests
  this approximation is good enough for a ranking, not that it is exact.
  A prior, discarded draft of this benchmark also made a real sizing
  error here — dividing capital into fixed \$10,000 per-ticker slices and
  applying the 20% cap to each slice, which silently limited total
  exposure to ~20% instead of matching `RULES.md`'s account-level cap.
  That version was never used for any reported number and has been fully
  replaced by the pooled-account model described in §2.
- The technical baseline's stop is a fixed 5%-below-close rule, not
  Hermes' own real support/resistance selection — a deliberate
  simplification stated in §2, not an attempt to mirror Hermes' judgment.

---

## 8. Recommendation

**Improve measurement before either optimizing or running another
unchanged tranche.** Two of the five ranked parameters above (§4.3, §4.4)
are flatly not identifiable from what this run stored, and the one with
the largest observed effect on realized return (the reward:risk threshold,
§4.2) can only be checked in the pessimistic direction. Before Bayesian
optimization or a decision to run another unchanged tranche:

1. **This is post-hoc, development-period analysis** — a description of
   what the recorded 16-trade ledger implies under alternate sizing rules
   and thresholds, **not proof of an edge**, not a backtest guarantee of
   future performance, and **not permission to change `RULES.md`,
   `AGENTS.md`, `SOUL.md`, the watchlist, or model/provider settings.**
   Any such change requires an explicit separate user decision.
2. Existing evidence is **not** sufficient to begin Bayesian optimization
   — the two parameters most likely to matter most (time-stop horizon,
   stop-distance bounds) are unmeasurable from current instrumentation,
   and n=16 is far short of `RULES.md` §6b's own 30–60-trade target.
3. Running another unchanged tranche would help (roughly doubling n), but
   would **not** by itself fix the time-stop/stop-distance
   unmeasurability — that requires either richer per-trade price-path
   instrumentation captured *during* a run, or accepting that those two
   parameters can only ever be evaluated by literally re-running Hermes
   under the changed rule.

**Next concrete experiment, if pursued:** instrument `run_backtest.py` (a
separate, explicit decision, not made here) to persist each closed
position's own daily mark-to-market path (not just entry/exit), which
would make the time-stop sensitivity question in §4.3 mechanically
answerable from future runs without needing real ticker identity at
report-writing time at all.
