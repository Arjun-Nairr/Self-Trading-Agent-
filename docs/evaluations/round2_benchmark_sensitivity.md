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

> **Correction notice (2026-09-06):** this report replaces the version
> committed in `1529438`. Review found six calculation defects in
> `benchmark_sensitivity.py` that made parts of that version unreliable —
> entry-day exits were never checked, the technical baseline's profit
> target was computed from the wrong price, the time-stop/stop-target
> chronology on carried positions was ordered wrong, the trade log
> conflated signal date with entry date, missing SMA-history bars were
> silently skipped instead of failing loudly, and portfolio drawdown/
> volatility omitted the `$100,000 → day-1` transition. The
> risk%×concentration-cap sensitivity grid was also overclaimed as "fully
> replayable" without modeling cash, overlap, or exit timing. All six are
> fixed in the code (see `git log` for the exact commit) and every number
> below is regenerated from the corrected implementation — nothing from
> the prior version is preserved on the strength of having been committed
> first.

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
    before any signal is computed, rather than silently shortening the
    SMA window (the corrected behaviour; see the correction notice above).
  - **Entry:** next trading day, 09:45, same fill/slippage as above.
  - **Stop:** a fixed 5% below the **signal day's close** (the midpoint of
    `RULES.md` §6a's 3–12% band) — set at signal time, not re-derived from
    real support/resistance (keeps this baseline computable from the same
    daily bars already fetched for buy-hold, and keeps it un-tuned).
  - **Target:** the **real, slippage-adjusted fill** + 1.5 × (fill − stop)
    — `RULES.md` §6b base tier, computed from the actual entry price, not
    the signal day's close (the corrected behaviour — a version that
    priced the target off the signal close instead of the fill was found
    to silently distort the realized reward:risk whenever price moved
    between signal and fill). **Before accepting the entry**, the
    resulting stop distance relative to the real fill is checked against
    the same 3–12% band (±0.10 tolerance) the baseline claims to follow —
    an overnight gap large enough to invalidate the preselected stop
    causes the entry to be **skipped and recorded as a rejection**
    (`technical_baseline_rejected_entries` in the raw data), never
    silently traded outside the declared rules. 2 of the run's rejections
    were exactly this case (SFM and DINO signals whose real fills landed
    the stop inside ~1–2%, below the 3% floor).
  - **Exit:** the same mechanical primitives Hermes' own trades use
    (`evaluate_opening_gap`, `evaluate_bar_stop_target`), with two
    corrected ordering rules:
    - A position **checks its own remaining entry-day bars immediately
      after filling** — it can close the same day it opens. The prior
      version deferred this check to a future iteration that never
      actually ran it, so an entry-day stop/target touch was silently
      missed entirely.
    - A **carried** (already-open) position is resolved in the real
      harness's own order: (1) opening-gap stop/target, (2) a time-stop
      exit **at the open** if the horizon has expired, (3) only if still
      open, the rest of that day's range. The prior version checked the
      full day's range before the time-stop, which could let a
      same-day target the price only reached *after* the time-stop
      would have fired override a trade that should already have closed.
    5-trading-day time stop (`RULES.md` §6c) throughout.
  - **Sizing:** `RULES.md` §2's own formula — 2% of **current account
    equity** risk per trade, capped at 20% of **current account equity**
    per position, never exceeding available cash — against **one pooled
    \$100,000 account** shared by all 10 tickers (not a pre-divided
    per-ticker slice). Multiple concurrent positions are possible. One
    open lot per ticker at a time (`RULES.md` §9.1).
  - **Tie-break:** same-day signals across tickers are processed in fixed
    alphabetical ticker order — deterministic, not a setup-quality
    ranking.
  - **Trade log fields:** `signal_date` (the real crossover day) and
    `entry_date` (the next trading day) are both recorded and are always
    different — the prior version stored the entry day under
    `signal_date`, silently discarding the actual signal date.
- **Hermes (actual)** — the real, already-verified 50-cycle equity curve
  (`docs/evaluations/round2_50cycle_evaluation_extract.md`), re-pulled here
  directly from `portfolio_snapshots` for a same-source comparison.

**Fail-loud data policy:** every curve raises `MissingDataError` immediately
if a bar is absent for a ticker currently held, weighted, or needed for the
technical baseline's own signal history — no held position's value is ever
silently dropped, and no signal-history gap is silently skipped (both
verified by `test_benchmark_sensitivity.py`). No such gap occurred in this
window; the watchlist and SPY both have full daily coverage across it.

---

## 3. Benchmark results — 🟢 Verified (corrected)

| Strategy | End equity | Total return | Max drawdown | Ann. volatility | Return/volatility | Avg. exposure |
|---|---|---|---|---|---|---|
| Cash | \$100,000.00 | 0.00% | 0.00% | 0.00% | — | 0% |
| SPY buy-and-hold | \$104,202.06 | **+4.20%** | −8.53% | 15.17% | 0.28 | 99.4% |
| Equal-weight watchlist buy-and-hold | \$97,337.07 | **−2.66%** | −8.09% | 16.58% | −0.16 | 99.7% |
| Technical baseline (SMA10, pooled) | \$93,733.42 | **−6.27%** | −7.51% | 15.45% | −0.41 | 34.4% |
| **Hermes (actual)** | **\$103,696.08** | **+3.70%** | **−2.46%** | **8.95%** | **0.41** | **20.0%** |

"Return/volatility" is a crude return-per-unit-of-risk ratio (no
risk-free-rate subtraction — not a formal Sharpe ratio; flagged as an
🟡 **Interpretation** aid, not a verified statistic on its own). Max
drawdown and volatility both now include the `$100,000 → day-1` opening
observation (the corrected behaviour — omitting it could hide a real
first-day loss; see §7).

**Trade count / turnover:** Hermes — 16 closed + 1 open (verified,
`docs/evaluations/round2_50cycle_evaluation_extract.md` §2). Technical
baseline — **28** closed-or-resolved trades across the window (was
incorrectly 30 under the uncorrected entry-day/time-stop logic), plus
**3 rejected entries** (2 for an out-of-band stop distance once priced off
the real fill, 1 for sizing rounding to zero shares); win rate 10/28 =
35.7%. Buy-and-hold benchmarks and cash have no turnover by construction.

**Reconciliation check:** Hermes' end equity here (\$103,696.08, +3.696%)
matches the compact evaluation brief's own figure exactly — same source
table, independently re-queried; this figure is unaffected by any of the
six corrections (all six were in the technical-baseline/metrics code,
never in the Hermes-curve fetch).

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
- **vs. the technical baseline:** yes, clearly — the corrected SMA10
  baseline lost 6.27% (was −7.35% before correction) with worse volatility
  and a worse drawdown than Hermes, despite similar-order exposure
  discipline (both well under 100%). This is the most directly comparable
  same-universe, same-execution-timing, same-day-one-disadvantage
  benchmark available, and Hermes outperforms it by exactly 10 points of
  return.

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
- SPY and equal-weight enter one trading day earlier than Hermes and the
  technical baseline can (§1) — a real, structural difference, not
  normalized away in the table above.

---

## 4. Sensitivity analysis

Evaluated **only** where the effect is mechanically recoverable from
stored evidence — no assumption that Hermes would have proposed identical
trades under different rules, and no filter-only replay presented as a
faithful alternative strategy run (per this task's own instructions).

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

**Classification: `chronological_cash_constrained_approximation`** — a
correction from the prior version's overclaimed "fully replayable" label.
What IS genuinely modeled and tested: trades are replayed in the archive's
own entry/exit day order, an exit's cash is credited **before** same-day
entries are sized, pooled cash is never allowed to go negative (a trade
that cannot be afforded is skipped and counted, not silently allowed), and
concurrent overlapping positions are tracked explicitly. What is **not**
modeled: an open position's contribution to "current equity" during
sizing is its **cost basis**, not a live mark-to-market value — the real
intra-hold daily price path for each masked trade is not available to
this report (recovering it would require resolving the masked-alias-to-
real-ticker mapping, which this pass does not do — see §6). This is why
the label stops short of "fully replayable": every input that could
realistically be recovered from stored evidence *is* modeled; the one
input that cannot be (true intra-hold equity) is disclosed, not hidden.

| Risk % ＼ Cap % | 10% | 20% | 30% |
|---|---|---|---|
| **1%** | +1.78% | +3.49% | +4.29% |
| **2%** (RULES.md current) | +1.78% | **+3.54%** | +3.79% † |
| **3%** | +1.78% | +3.54% | +3.79% † |

† At cap=30%/risk≥2%, one trade is skipped outright (insufficient pooled
cash when three positions are sized concurrently on the archive's Day 45)
— a genuine finding this corrected, cash-aware replay can surface that the
prior, cash-blind version could not: **loosening the cap without loosening
the risk budget can make one of the run's own concurrent-entry days
literally unaffordable**, not merely larger.

(2%/20% ≈ +3.54% vs. the real recorded +3.70%/+\$3,443.73 — the small gap
is the cost-basis-equity-proxy approximation described above, not a data
error.)

**Strongest justified conclusion, still supported after correction:** **the
concentration cap remains the more consequential lever than the risk-per-
trade percentage at every cap level this run's trade sizes actually
needed** — at cap=10%, all three risk% values produce the identical return
(1.78%), and risk% only starts to differentiate results once the cap is
loosened. The correction changes the exact numbers and adds the cap=30%
affordability finding above, but does not overturn this ranking.

### 4.2 Minimum reward:risk threshold — 🟡 One-sided, filter-only counterfactual

**Classification: one-sided/filter-only** (unchanged by this correction —
this section used only the real historical qty/fill/exit for retained
trades, which none of the six defects touched). A trade whose recorded
Expected R:R falls below the new threshold is removed entirely; this
analysis has no row-level geometry for the 45 decision-time and 31
execution-time BUY proposals rejected before ever reaching the journal, so
**a looser threshold's potential upside is not estimated.**

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
a fabricated ranking. (The technical baseline's OWN entries are checked
against this same band using the real fill — §2 — but that is a
statement about the baseline's own rule compliance, not about what
Hermes' judgment would do under a different band.)

### 4.5 Time-stop × reward-threshold interaction — 🔴 Not mechanically identifiable

Inherits §4.3's limitation directly: an interaction cannot be measured
when one of its two axes is not itself measurable from stored evidence.

### 4.6 Ranking, with confidence labels

| Parameter | Effect observed | Classification | Confidence |
|---|---|---|---|
| Concentration cap % | Large, dominant at tight caps; can make a concurrent-entry day unaffordable at loose caps (§4.1) | Chronological, cash-constrained approximation | 🟡 Medium-high (cash/overlap/exit-timing modeled; intra-hold equity is a cost-basis proxy) |
| Risk-per-trade % | Small, only matters when cap is loose | Chronological, cash-constrained approximation | 🟡 Medium-high (same caveat) |
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
  `PERIOD_START`, with full coverage validated up front (§2); every entry
  executes at the *next* trading day's 09:45 bar, never the signal day
  itself.
- **Identical starting capital:** `STARTING_EQUITY = $100,000.00`, one
  shared constant, used directly in every `metrics()` calculation,
  including as the leading observation for drawdown/volatility (the
  corrected behaviour) — regression-tested in
  `test_benchmark_sensitivity.py`.
- **Reproducible formulas/results:** `risk_cap_grid` and
  `min_rr_threshold_filter` are pure, deterministic functions over a
  hardcoded, cited ledger — re-running produces byte-identical output
  (tested).
- **Current Hermes metrics reconcile** with the compact evaluation extract
  (§3, end equity \$103,696.08 / +3.696%, matching exactly).
- **No lookahead / no held-position value silently dropped / no
  signal-history bar silently skipped:** verified by
  `test_benchmark_sensitivity.py`'s `MissingDataError` tests.
- **Cash never goes negative** in either the technical baseline's own
  pooled-account walk or the risk/cap sensitivity grid — asserted
  internally and independently re-verified by a test that replays the
  public trade log without trusting that internal assertion.
- **No single position ever exceeds 20% of current account equity, while
  total pooled exposure genuinely can** — directly tested with a
  three-ticker concurrent-signal scenario.

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
  baseline was built, by design (the task calls for one).
- The true intra-hold mark-to-market equity path for each of the 16
  real ledger trades (§4.1) — approximated by cost basis instead, since
  recovering it needs the same masked-alias resolution the time-stop
  question (§4.3) also needs and this pass does not pursue.

---

## 7. Known simplifications (stated, not hidden)

- §4.1's risk%×cap% grid now genuinely models chronological cash and
  overlap (the correction), but still approximates intra-hold equity via
  cost basis rather than a live mark — stated plainly in §4.1 itself, not
  buried here.
- The technical baseline's stop is a fixed 5%-below-signal-close rule,
  not Hermes' own real support/resistance selection — a deliberate
  simplification stated in §2, not an attempt to mirror Hermes' judgment.
- A prior, discarded draft of this benchmark (before either the initial
  merge or this correction pass) made a real sizing error — dividing
  capital into fixed \$10,000 per-ticker slices and applying the 20% cap
  to each slice, which silently limited total exposure to ~20% instead of
  matching `RULES.md`'s account-level cap. That version was never used
  for any reported number.

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
