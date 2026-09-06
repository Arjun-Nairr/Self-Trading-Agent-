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

> **Correction history:**
> - **2026-09-06 (first pass, commit `310f6fd`):** fixed six defects —
>   entry-day exits never checked, target priced off the wrong day, wrong
>   time-stop ordering, signal date conflated with entry date, missing
>   SMA-history bars silently skipped, and drawdown/volatility omitting
>   the `$100,000 → day-1` transition. Also downgraded the risk%×cap%
>   grid from an overclaimed "fully replayable" to a disclosed
>   approximation.
> - **2026-09-06 (this pass):** fixed four further defects found on
>   review of that correction: (1) `risk_cap_grid`'s same-day ledger
>   trade (masked Day 18: enters and exits the same day) had its exit
>   event sort *before* its own entry, leaving it permanently
>   `"pending"` — a trade exiting before it existed; (2) the technical
>   baseline could let a profitable same-day exit's cash proceeds
>   retroactively fund another ticker's earlier, otherwise-unaffordable
>   09:45 entry, because same-day entries and their own exits were
>   interleaved ticker-by-ticker instead of settled in two clean phases;
>   (3) the technical baseline's sizing valued a *carried* (already-open)
>   position at its entry cost rather than its real 09:45 mark, silently
>   understating account equity whenever a held position had genuinely
>   moved; (4) a missing 09:45 entry bar, or a missing 09:30 mark needed
>   to value a carried position at the 09:45 sizing instant, was silently
>   treated as "no trade" instead of failing loudly. All four are fixed
>   and covered by direct regression tests.
> - **2026-09-06 (this pass):** fixed two further defects. (1) A carried
>   position's exit was still resolved using the WHOLE day's OHLC range
>   before 09:45 entries were sized — an afternoon stop/target touch could
>   leak into the 09:45 equity mark and its proceeds could fund a same-day
>   entry that only had cash because of a move that hadn't happened yet by
>   09:45. Carried-position resolution is now split at 09:45 exactly like
>   new entries already were: opening-gap → time-stop-at-open → the
>   09:30-09:45 bar's OWN range (never the whole day) resolve BEFORE
>   entries are sized; only AFTER sizing does every still-open position
>   (carried survivors and new fills alike) walk from 09:45 through the
>   close. **This changed the technical baseline's numbers materially —
>   see §3.** (2) The test claiming to independently prove the sensitivity
>   grid's cash never goes negative only checked that no trade was left
>   `"pending"` — it never inspected a single cash value. `risk_cap_grid`
>   now exposes a minimal `cash_events` trace (day/kind/qty/price per
>   cash-affecting event) and the test independently replays cash from
>   `STARTING_EQUITY` using only those raw fields, asserting every
>   chronological prefix stays nonnegative and reconciles with the
>   reported `end_equity` — plus a dedicated cash-pressure regression
>   (three same-day entries whose naive combined cost would overspend)
>   proving the affordability check is what prevents it. Also corrected
>   §3's own reconciliation, which had paired Hermes' full-portfolio
>   return (+3.70%, includes the still-open position's unrealized P&L)
>   with the closed-ledger-only realized dollar figure (+\$3,443.73) as if
>   they were the same number. All fixes covered by direct regression
>   tests (see `test_benchmark_sensitivity.py`); every number below is
>   regenerated from the corrected implementation — nothing is preserved
>   from an earlier version on the strength of having been committed
>   first.
> - **2026-09-06 (this pass, final):** two closing fixes. (1) The
>   technical baseline required the 09:30 bar but could silently walk
>   whatever LATER bars happened to be present — a missing afternoon bar
>   could hide a stop/target touch. Every held position's 09:45-through-
>   close walk now validates the complete expected regular-session
>   15-minute bar sequence first (reusing `run_backtest.py`'s own
>   `_expected_session_bar_times`, respecting the real trading calendar
>   and shortened sessions — not a second, possibly-conflicting
>   definition of "complete"), raising `MissingDataError` naming the
>   ticker, date, and missing timestamp(s) rather than proceeding on a
>   partial session. **Re-running against the real market data below
>   found no such gap in this window — the technical baseline's numbers
>   are unchanged by this fix**, which is a guard against a future run
>   silently tolerating one, not a correction to this run's own result.
>   (2) §3a's reconciliation table is now generated by one pure function
>   (`reconciliation_table`), so every displayed percentage is always
>   derived from its own dollar figure by the same formula and can never
>   again be hand-paired with the wrong one — directly regression-tested.
>   Values are unchanged from the prior pass.

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

### Entry timing is NOT identical across strategies — disclosed, not reconciled away

- **Cash, SPY buy-and-hold, equal-weight watchlist buy-and-hold** enter on
  **trading day 1 itself** — a real buy-and-hold investor's only decision
  ("how much") is made before day 1 opens, so day 1's own 09:45 fill is
  the natural, earliest entry.
- **Hermes (actual) and the technical baseline** both require a prior
  signal or decision, executed the **next** trading day — their earliest
  possible entry is trading day 2, one full day later than the buy-and-hold
  benchmarks. This is a structural, real difference in what each strategy
  even *can* do on day 1, not a bug to fix or an oversight to reconcile —
  it is called out explicitly here so the benchmark table below is not
  misread as "identical entry timing for everyone."

---

## 2. Benchmark definitions

All four alternatives share the same \$100,000 baseline and the same 50
trading days; entry timing differs as disclosed above, and every entry
uses the same fill/slippage convention (`_slipped_buy_price`, 5bps,
09:45 ET) as Hermes' own first possible BUY. None use any information not
available at that point in time.

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
    trend-following entry, not fit to this window. **Every daily bar
    across the full warmup+evaluation window is required up front** — a
    missing one raises `MissingDataError` naming the ticker and date
    before any signal is computed.
  - **Entry:** next trading day, 09:45, same fill/slippage as above.
  - **Stop:** a fixed 5% below the **signal day's close** (the midpoint of
    `RULES.md` §6a's 3–12% band) — set at signal time, not re-derived from
    real support/resistance.
  - **Target:** the **real, slippage-adjusted fill** + 1.5 × (fill − stop)
    — `RULES.md` §6b base tier, computed from the actual entry price.
    **Before accepting the entry**, the resulting stop distance relative
    to the real fill is checked against the same 3–12% band (±0.10
    tolerance) — an overnight gap large enough to invalidate the
    preselected stop causes the entry to be **skipped and recorded as a
    rejection** (`technical_baseline_rejected_entries` in the raw data).
  - **Sizing at the 09:45 instant — corrected this pass:** `RULES.md` §2's
    formula (2% of current account equity risk per trade, capped at 20%
    of current account equity per position, never exceeding available
    cash) is applied against **one pooled \$100,000 account**, where
    "current account equity" at 09:45 is `cash + (each SURVIVING carried
    position's quantity × its own real 09:30-close mark, the last
    fully-elapsed mark before 09:45 — the same convention
    `run_backtest.py`'s own risk-check `marks` dict uses) + (each of
    today's already-filled entries at its own real fill price)`. A
    carried position is **never** valued at its entry cost once it has a
    real 09:45 mark available, and never at today's close or any later
    bar.
  - **Entry/exit ordering at 09:45 — corrected this pass:** when several
    tickers signal the same day, **every** valid entry is sized and
    filled first, in fixed alphabetical order, using only cash and marks
    known *before* any of today's entries are checked for a same-day
    exit. **Only after** every simultaneous entry is settled are the new
    positions' own entry-day bars walked for a stop/target touch. This
    stops a profitable 10:00+ exit on one ticker from retroactively
    funding another ticker's earlier, otherwise-unaffordable 09:45 entry
    — verified by a dedicated regression test.
  - **Exit:** the same mechanical primitives Hermes' own trades use
    (`evaluate_opening_gap`, `evaluate_bar_stop_target`). A carried
    position is resolved in the real harness's own order: (1)
    opening-gap stop/target, (2) a time-stop exit **at the open** if the
    horizon has expired, (3) only if still open, the rest of that day's
    range. 5-trading-day time stop (`RULES.md` §6c).
  - **One open lot per ticker at a time** (`RULES.md` §9.1); tie-break for
    simultaneous signals is fixed alphabetical ticker order.
  - **Trade log fields:** `signal_date` (the real crossover day) and
    `entry_date` (the next trading day) are both recorded and always
    differ.
- **Hermes (actual)** — the real, already-verified 50-cycle equity curve
  (`docs/evaluations/round2_50cycle_evaluation_extract.md`), re-pulled here
  directly from `portfolio_snapshots` for a same-source comparison.

**Fail-loud data policy:** every curve raises `MissingDataError` immediately
if a bar is absent for a ticker currently held, weighted, or needed for the
technical baseline's own signal history or 09:45 sizing marks — no held
position's value is ever silently dropped, no signal-history gap is
silently skipped, and no missing 09:45/09:30 mark is ever treated as "no
trade" (all verified by `test_benchmark_sensitivity.py`). No such gap
occurred in this window; the watchlist and SPY both have full daily
coverage across it.

---

## 3. Benchmark results — 🟢 Verified (corrected)

| Strategy | End equity | Total return | Max drawdown | Ann. volatility | Return/volatility | Avg. exposure |
|---|---|---|---|---|---|---|
| Cash | \$100,000.00 | 0.00% | 0.00% | 0.00% | — | 0% |
| SPY buy-and-hold | \$104,202.06 | **+4.20%** | −8.53% | 15.17% | 0.28 | 99.4% |
| Equal-weight watchlist buy-and-hold | \$97,337.07 | **−2.66%** | −8.09% | 16.58% | −0.16 | 99.7% |
| Technical baseline (SMA10, pooled) | \$100,388.80 | **+0.39%** | −5.58% | 16.80% | 0.02 | 35.0% |
| **Hermes (actual)** | **\$103,696.08** | **+3.70%** | **−2.46%** | **8.95%** | **0.41** | **20.0%** |

"Return/volatility" is a crude return-per-unit-of-risk ratio (no
risk-free-rate subtraction — not a formal Sharpe ratio; flagged as an
🟡 **Interpretation** aid, not a verified statistic on its own).

**Trade count / turnover:** Hermes — 16 closed + 1 open (verified,
`docs/evaluations/round2_50cycle_evaluation_extract.md` §2). Technical
baseline — **26** closed-or-resolved trades, plus **3 rejected entries**
(2 for an out-of-band stop distance once priced off the real fill, 1 for
sizing rounding to zero shares); win rate 12/26 = 46.2%. Buy-and-hold
benchmarks and cash have no turnover by construction.

### 3a. Reconciliation — five different numbers, none interchangeable

Five separate \$ figures appear across this report and the compact
evaluation extract. Pairing any two of them requires knowing they measure
the **same scope**; a prior version of this report paired two that don't
(see the correction history above). **This table is generated by
`benchmark_sensitivity.py`'s `reconciliation_table()` function** — every
row's Return % is derived from that SAME row's \$ P&L by one shared
formula (`pnl / STARTING_EQUITY * 100`), never typed independently in
report prose, and directly regression-tested
(`test_benchmark_sensitivity.py`):

| Figure | Scope | Return % | \$ P&L | Source |
|---|---|---|---|---|
| Hermes actual portfolio | Full account: 16 closed trades **+ the 1 still-open position's unrealized P&L** | **+3.696%** | **+\$3,696.08** | `portfolio_snapshots`, re-queried live |
| Hermes realized-only (closed ledger) | The 16 closed trades **only** — excludes the open position entirely | **+3.444%** | **+\$3,443.73** | `docs/evaluations/round2_50cycle_evaluation_extract.md` §2 (harness-authoritative; the one externally-sourced figure in this table — `HARNESS_REALIZED_PNL`) |
| Same 16 trades, recomputed from `LEDGER`'s own fill/exit fields | Identical scope to the row above | +3.445% | +\$3,445.04 | `min_rr_threshold_filter([1.5])` in this report — the \$1.31 gap vs. the row above is the same **pre-existing, already-documented** 2-decimal `fill_price`-precision artifact the compact evaluation extract itself flags (§2.2 there), not a new error |
| Technical baseline (this report's own strategy) | Its own 26 trades, unrelated universe of decisions | +0.389% | +\$388.80 | §3 above |
| Risk/cap grid @ 2%/20% (§4.1) | Replays the same 16 closed trades **only** — no open position | +3.502% | +\$3,502.44 | `risk_cap_grid([0.02],[0.20])` |

**The risk%×cap% grid (§4.1) replays only the 16 closed trades — it has
no open position at all.** Its own baseline comparison is therefore the
**realized-only** row (+3.444%/+\$3,443.73), never the full-portfolio
+3.696%/+\$3,696.08 figure.

**Reconciliation check:** Hermes' end equity here (\$103,696.08, +3.696%)
matches the compact evaluation brief's own figure exactly — same source
table, independently re-queried; unaffected by any correction in either
pass (none touched the Hermes-curve fetch).

### Did Hermes add measurable value?

- **vs. Cash:** yes — Hermes beat cash by 3.70 points of return while cash
  is definitionally riskless; not a meaningful comparison beyond the floor
  it sets.
- **vs. SPY buy-and-hold:** 🟡 **Interpretation, not decisive.** SPY's raw
  return (+4.20%) edges Hermes' (+3.70%) by 0.5 points, but Hermes did it
  at roughly a fifth of SPY's exposure (20.0% vs. 99.4%), and well under a
  third of its volatility (8.95% vs. 15.17%) and drawdown (−2.46% vs.
  −8.53%) — Hermes' return-per-unit-of-risk ratio (0.41) is the highest
  of any strategy compared, including SPY's (0.28). Whether that
  risk-adjusted edge is "real" or an artifact of only 16 trades and one
  favorable 50-day SPY stretch is exactly the kind of claim §6's small-n
  uncertainty caveats. **This comparison also has the disclosed entry-
  timing asymmetry** (§1): SPY got a one-day head start Hermes structurally
  could not have taken.
- **vs. equal-weight watchlist buy-and-hold:** yes, clearly — the raw
  watchlist would have **lost** 2.66% held passively over the identical
  dates; Hermes' selectivity and exits produced a positive return over
  the same universe.
- **vs. the technical baseline:** yes — the corrected SMA10 baseline
  (+0.39%) essentially broke even, well behind Hermes' +3.70%, with
  double Hermes' volatility (16.80% vs. 8.95%) and a worse drawdown
  (−5.58% vs. −2.46%). This is the most directly comparable same-
  universe, same-execution-timing, same-day-one-disadvantage benchmark
  available. **This margin is much narrower than an earlier, defective
  version of this report claimed (−6.34%, a ~10-point gap).** That
  version resolved carried positions using the whole day's OHLC range
  before 09:45 entries were sized, letting an afternoon stop/target
  touch (and, in concurrent-signal cases, its cash) leak into decisions
  that should only have seen data available by 09:45 — both mis-sizing
  entries and mis-timing exits. The corrected, lookahead-free baseline's
  real gap to Hermes is about 3.3 points, not ~10; this report does not
  claim to know in general which direction that class of defect biases a
  result, only that this specific run's number changed materially once
  it was fixed.

### Limitations on this comparison

- 50 trading days and 16 Hermes trades is a short window for any of the
  above to be a durable performance claim — see §6.
- The technical baseline's own parameters (SMA(10), 5% stop, 1.5:1
  target, 5-day time stop) were fixed by this report's author, not swept
  or fit to this window — but a *different* simple baseline could show a
  materially different result. One baseline is not a survey of baselines.
- SPY buy-and-hold has no downside protection at all (no stop); Hermes
  and the technical baseline both do — the volatility/drawdown comparison
  partly reflects that structural difference, not skill.
- SPY and equal-weight enter one trading day earlier than Hermes and the
  technical baseline can (§1) — a real, structural difference, not
  normalized away in the table above.

---

## 4. Sensitivity analysis

Evaluated **only** where the effect is mechanically recoverable from
stored evidence — no assumption that Hermes would have proposed identical
trades under different rules, and no filter-only replay presented as a
faithful alternative strategy run.

**Data source:** the real, hash-verified 16-trade closed ledger from
`docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md` §3.1
(fill/exit/qty/stop/target/expected-R:R/exit-reason **and the archive's
own masked "Entry day → Exit day" trading-day indices** for every trade),
hardcoded in `benchmark_sensitivity.py`'s `LEDGER` constant. The day
indices are masked trading-day labels from the run itself, used only to
order and detect overlap between trades — never a real calendar date or
ticker identity. **Effective sample size: n = 16 closed trades** — well
short of `RULES.md` §6b's own stated 30–60-trade six-month target; every
finding below is qualified accordingly.

### 4.1 Risk-per-trade % × concentration cap % — 🟡 Mechanically reproducible, with one stated approximation

**Classification: `chronological_cash_constrained_approximation`** — never
"fully replayable." What IS genuinely modeled and tested: each masked
trading day is walked in the real harness's own order — (1) close
positions *carried into* the day, (2) size and open new entries for the
day, (3) close positions that *both entered and exit the same day* — an
exit can never be processed before its own entry, cash is never allowed
to go negative, and every one of the 16 ledger rows ends the walk either
`"completed"` or an explicitly labeled skip, never stuck `"pending"`
(directly tested, including the real masked "Day 18" same-day round trip).
What is **not** modeled: an open position's contribution to "current
equity" during sizing is its **cost basis**, not a live mark-to-market
value — the real intra-hold daily price path for each masked trade is not
available to this report (recovering it would require resolving the
masked-alias-to-real-ticker mapping, which this pass does not do — see
§6).

| Risk % ＼ Cap % | 10% | 20% | 30% |
|---|---|---|---|
| **1%** | +1.77% | +3.46% | +4.15% |
| **2%** (RULES.md current) | +1.77% | **+3.50%** | +5.54% |
| **3%** | +1.77% | +3.50% | +5.54% |

No cell in this run skips a trade for insufficient cash (`still_open_at_end`
is empty everywhere) — the concurrent-entry cash-affordability finding an
earlier draft of this correction surfaced at cap=30% was itself an
artifact of that draft's own same-day ordering defect, not a real result;
it does not survive this pass's fix and is not repeated here.

(2%/20% ≈ +3.50%/+\$3,502.44 vs. the real recorded **closed-ledger-only**
result of +3.44%/+\$3,443.73 — see §3a for why the realized-only figure,
not Hermes' +3.70% full-portfolio return, is this grid's correct
comparison point. The ~\$59 gap between them is the cost-basis-equity-
proxy approximation described above, not a data error.)

**Strongest justified conclusion:** **the concentration cap remains the
more consequential lever than the risk-per-trade percentage at every cap
level this run's trade sizes actually needed** — at cap=10%, all three
risk% values produce the identical return (1.77%), and risk% only starts
to differentiate results once the cap is loosened.

### 4.2 Minimum reward:risk threshold — 🟡 One-sided, filter-only counterfactual

**Classification: one-sided/filter-only** (untouched by either correction
pass — this section uses only the real historical qty/fill/exit for
retained trades). A trade whose recorded Expected R:R falls below the new
threshold is removed entirely; this analysis has no row-level geometry for
proposals rejected before ever reaching the journal, so **a looser
threshold's potential upside is not estimated.**

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

9 of 16 closed trades (56%) exited via the time stop — a very large
share, so return is plainly **sensitive** to this parameter. **The sign
and size of that sensitivity cannot be computed honestly from stored
evidence**: extending or shortening the horizon requires each trade's
real intraday/daily price path beyond (or before) its actual exit date,
which requires resolving each trade's real ticker identity from its
masked alias — deliberately not attempted in this pass (see §6).

### 4.4 Stop-distance boundaries (`RULES.md` §6a, currently 3–12%) — 🔴 Not mechanically identifiable

Hermes' stop is its own technical-level judgment (real support/
resistance), not a formula — changing the 3–12% band would change *which
technical level qualifies*, answerable only by re-running Hermes' own
judgment against a new band. (The technical baseline's OWN entries are
checked against this same band using the real fill — §2 — but that is a
statement about the baseline's own rule compliance, not about Hermes.)

### 4.5 Time-stop × reward-threshold interaction — 🔴 Not mechanically identifiable

Inherits §4.3's limitation directly.

### 4.6 Ranking, with confidence labels

| Parameter | Effect observed | Classification | Confidence |
|---|---|---|---|
| Concentration cap % | Large, dominant at tight caps (§4.1) | Chronological, cash-constrained approximation | 🟡 Medium-high (day-ordering and cash genuinely modeled and tested; intra-hold equity is a cost-basis proxy) |
| Risk-per-trade % | Small, only matters when cap is loose | Chronological, cash-constrained approximation | 🟡 Medium-high (same caveat) |
| Min reward:risk threshold | Large — a stricter threshold flips the sign of realized return | One-sided filter-only | 🟡 Medium (real trades only; no rejected-proposal upside estimated) |
| Time-stop horizon | Plausibly large (56% of trades resolved by it) | Not mechanically identifiable | 🔴 Unknown |
| Stop-distance bounds | Unknown | Not mechanically identifiable | 🔴 Unknown |

---

## 5. Data-quality checks — 🟢 Verified

- **Exact date alignment:** all 50 Hermes `portfolio_snapshots` dates fall
  inside the fetched 50-trading-day benchmark window with zero
  mismatches — hard-checked in `main()`.
- **No future-data leakage:** every benchmark's signal/entry decision uses
  only bars dated on or before the decision day; SMA(10) warmup uses a
  dedicated 15-real-trading-day lookback window strictly before
  `PERIOD_START`, with full coverage validated up front; every entry
  executes at the *next* trading day's 09:45 bar, never the signal day
  itself; every carried position is resolved pre-09:45 using ONLY its
  opening-gap price and its own 09:30-09:45 bar (never the whole day's
  range), and 09:45 sizing marks it at that same 09:30 close — never
  today's close, never any bar after 09:45, and never another ticker's
  same-day exit proceeds (all directly tested).
- **Identical starting capital:** `STARTING_EQUITY = $100,000.00`, one
  shared constant, used directly in every `metrics()` calculation,
  including as the leading observation for drawdown/volatility.
- **Reproducible formulas/results:** `risk_cap_grid` and
  `min_rr_threshold_filter` are pure, deterministic functions over a
  hardcoded, cited ledger — re-running produces byte-identical output.
- **Current Hermes metrics reconcile** with the compact evaluation extract
  (§3, end equity \$103,696.08 / +3.696%, matching exactly).
- **No lookahead / no held-position value silently dropped / no
  signal-history bar silently skipped / no missing 09:45 or 09:30 mark
  silently treated as "no trade" / no incomplete intraday session
  silently walked:** all verified by `test_benchmark_sensitivity.py`'s
  `MissingDataError` tests (six distinct scenarios). Every held
  position's 09:45-through-close walk first validates the COMPLETE
  expected regular-session 15-minute bar sequence (reusing
  `run_backtest.py`'s own `_expected_session_bar_times` — the real
  harness's own definition of a complete session, respecting shortened
  sessions via the real trading calendar, not a second invented
  definition); a single missing afternoon bar raises `MissingDataError`
  naming the ticker, date, and missing timestamp(s) rather than walking
  whatever bars happen to be present. No such gap occurred in the real
  data this report is built from.
- **Cash never goes negative in the sensitivity grid — independently
  verified, not inferred from completeness.** `risk_cap_grid` exposes a
  minimal `cash_events` trace (day/kind/qty/price per cash-affecting
  event); the test replays cash from `STARTING_EQUITY` using only those
  raw fields (never trusting the function's own recorded running total),
  asserts every chronological prefix is nonnegative, and reconciles the
  replay's final balance against `end_equity`. A dedicated cash-pressure
  scenario (three same-day entries whose naive combined cost would
  overspend a $1,000 account) proves the affordability check is what
  prevents it, asserting the exact accepted/skipped trades and exact
  quantity.
- **Every ledger row in the sensitivity grid resolves to `"completed"`
  or an explicit `"skipped_..."` label, never left `"pending"`** —
  including the real masked Day-18 same-day round trip, directly tested.
- **No single position ever exceeds 20% of current account equity, while
  total pooled exposure genuinely can** — directly tested with a
  three-ticker concurrent-signal scenario.
- **Neither a same-day exit's proceeds NOR a carried position's later
  (post-09:45) exit proceeds can fund another ticker's earlier 09:45
  entry**, and **a carried position's sizing contribution uses its real
  09:30-close mark, not its cost basis** — all directly tested with
  scenarios whose expected quantities are hand-derived, independent
  arithmetic, not values recomputed the way the code itself computes
  them.

---

## 6. Unknowns

- Whether Hermes' small real-return edge in return-per-unit-of-risk over
  SPY (§3) reflects genuine skill or 50-day/16-trade noise (and the
  disclosed one-day entry-timing head start SPY had) — cannot be settled
  with this sample size.
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
  baseline was built, by design.
- The true intra-hold mark-to-market equity path for each of the 16 real
  ledger trades (§4.1) — approximated by cost basis instead, since
  recovering it needs the same masked-alias resolution the time-stop
  question (§4.3) also needs and this pass does not pursue.

---

## 7. Known simplifications (stated, not hidden)

- §4.1's risk%×cap% grid genuinely models day-ordering, same-day round
  trips, and cash availability (both correction passes), but still
  approximates intra-hold equity via cost basis rather than a live mark —
  stated plainly in §4.1 itself.
- The technical baseline's stop is a fixed 5%-below-signal-close rule,
  not Hermes' own real support/resistance selection — a deliberate
  simplification stated in §2.
- Three discarded earlier drafts of this benchmark are documented here
  only to be clear none ever produced a currently-reported number: one
  divided capital into fixed \$10,000 per-ticker slices with the 20% cap
  applied per slice; one's risk%×cap% grid sorted a same-day trade's exit
  before its own entry; one resolved carried positions using the whole
  day's OHLC range before 09:45 entries were sized, letting an afternoon
  touch (and its cash) leak into a 09:45-only decision. All three are
  fully superseded by the implementation this report describes.

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
would make both the time-stop sensitivity question (§4.3) and the
risk/cap grid's remaining equity-proxy approximation (§4.1) mechanically
answerable from future runs without needing real ticker identity at
report-writing time at all.
