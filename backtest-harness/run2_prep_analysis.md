# Round-2 prep: judgment-layer fixes, geometry diagnostic, counterfactual engine

Prepared 2026-08-29/30, offline. Follows run 1 (`26f1d371-08db-4885-ab24-b42200792fb7`,
125 cycles, zero fills) and `run1_evaluation_extract.md`'s finding: infrastructure was
flawless, the failures were judgment-layer (razor-thin R:R attempts, two §2b sizing
breaches, 25 reflections on zero outcome data, unquantified location rejections).

No RULES.md numbers changed. No backtest run started, resumed, or reset. All of Part B/C
below is read-only human-evaluation output — never wired into `run_backtest.py`, never
written anywhere Hermes reads, never referenced in `AGENTS.md` or any prompt.

---

## Part A — AGENTS.md judgment-layer edits

Five edits to `AGENTS.md`, all inside the existing entry/reflection procedure, none
touching a RULES.md number:

1. **R:R headroom doctrine** (Step 4, item 3): a proposal should aim for ~2.5:1 at
   decision time, not graze the 2.0 floor — a ratio that only clears by rounding (≤2.05)
   is treated as a fail. Cites SFM's real collapse (2.00 → 0.91) as the reason.
2. **Gap stress-test** (Step 4, item 3): before proposing, check whether a ~3% adverse
   next-day gap would still clear every rule.
3. **Mandatory dual sizing check** (Step 4, item 5): compute and state BOTH the
   stop-based size and the concentration-cap size explicitly, not just size intuitively —
   cites ATI (15.70%) and DINO (19.80%) as the real §2b breaches this closes.
4. **Reflection guard** (Reflection section): when `sample_size` is 0, reflect on process
   only and say plainly that no outcome data exists — never restate a strategy conclusion
   as if learned from results with zero closed trades behind it.
5. **Quantified location rejections** (Step 5): a no-action reason citing resistance/
   extension must state the measured percentage distance, not prose like "pressed into
   resistance."

---

## Part B — Geometry diagnostic (`geometry_scan.py`)

**Question:** on how many (day, ticker) pairs did maverick's own real support/resistance
algorithm actually offer a 2:1-or-better setup with a valid 5–10% stop, using point-in-time
data only?

**Method:** for all 10 watchlist tickers × all 125 real trading days (2026-02-17 to
2026-08-14, 1,250 pairs total), fetched maverick's real `get_full_analysis(as_of=day)` —
the exact same call `run_backtest.py` makes for a live cycle. Classified each pair:

- **Stop**: the shallowest (closest-to-entry) support level whose distance below close
  falls in RULES.md §6a's 5–10% band (with `risk_checker.py`'s own 0.10pt tolerance) — the
  tightest defensible stop, minimizing risk for any target.
- **Target**: the *nearest* resistance candidate that clears the §6b 2.0 minimum when
  paired with that stop — not simply the nearest resistance level overall (see the
  methodology note below for why that naive definition doesn't work), and not the single
  farthest number available either.
- **GEOMETRY-VALID** if such a target exists (ratio ≥ 2.0). **CLOSE** if the best
  available pairing lands 1.5–2.0. **INVALID** otherwise (no valid stop, or best ratio
  &lt;1.5).

**Methodology note — why "nearest resistance" alone doesn't work:** maverick's
`support_resistance` (a deliberately simple algorithm, not real pivot detection — see
`maverick/technical/analysis.py`'s own docstring) always synthesizes `close×1.05` and
`close×1.10` as resistance candidates regardless of real chart structure. The literal
nearest resistance above close is therefore almost always the ~5%-out synthetic level —
paired with a stop also near the 5% floor (also almost always available, since
`close×0.95` is a synthetic support candidate too), that mechanically caps the ratio near
1.0 on nearly every day, independent of real setup quality. An initial pass using strict
nearest-resistance classified **100% of the real window as INVALID**, including SFM's own
real day — which is known-wrong ground truth, since SFM's real journaled trade (stop
77.02, target 89.18, ratio 2.00) is a fact from run 1's own `trades` table. The corrected
definition above (nearest resistance that *clears* the minimum, shallowest stop) was
verified to reproduce SFM's exact real numbers when run against 2026-03-17's real data —
see `test_geometry_scan.py`'s ground-truth regression.

### Real results (full window, real market data)

| Class | Count | % |
|---|---|---|
| GEOMETRY-VALID | 995 | 79.6% |
| CLOSE | 255 | 20.4% |
| INVALID | 0 | 0.0% |

**By ticker** (GEOMETRY-VALID / CLOSE, out of 125 days each):

| Ticker | Valid | Close | Ticker | Valid | Close |
|---|---|---|---|---|---|
| PLNT | 113 | 12 | SFM | 108 | 17 |
| RYAN | 113 | 12 | ALGM | 112 | 13 |
| OLN | 109 | 16 | BRKR | 105 | 20 |
| WMG | 100 | 25 | ATI | 85 | 40 |
| DINO | 79 | 46 | BRX | 71 | 54 |

**By month:** roughly stable 80/20 split throughout — Feb 77/13 (86%), Mar 185/35 (84%),
Apr 166/44 (79%), May 152/48 (76%), Jun 160/50 (76%), Jul 172/48 (78%), Aug 83/17 (83%).
No month collapses to near-zero geometry; the drought was not "no setups existed."

**Honest reading of this number:** because of the synthetic ±5%/±10% floor baked into
maverick's own algorithm, "geometry exists" is true on the large majority of days almost
by construction, not because real, distinctive technical structure showed up 80% of the
time. This diagnostic still correctly reflects what the real agent's own tool actually
returns — it just means the interesting question isn't "was there ever a shape to trade,"
it's "would trading that shape have actually worked" — which is what Part C tests.

Full per-pair detail (995 GEOMETRY-VALID day/ticker/stop/target/ratio rows) lives in the
now-deleted `.geometry_scan_data.json` working file — regenerate with
`python geometry_scan.py` if needed; not duplicated here for size.

---

## Part C — Counterfactual engine (`counterfactual.py`)

**Question:** if these setups had actually been taken, what would have happened?
Mechanically replays real fill/exit rules (`run_backtest.py`'s own functions, imported
verbatim — not reimplemented): entry at the next real trading day's 09:45 with
execution-sim's exact 5bps slippage, exit on stop/target touch (stop checked first on a
same-bar ambiguity, an opening-gap rule for a gap through either level), or a
15-trading-day time stop. Entry uses real 15-minute intraday bars; the multi-day holding
walk after entry uses real daily bars (see the file's own `ponytail:` note on that
granularity tradeoff — entry-instant precision is what RULES.md §7 and the real slippage
actually depend on, day-2-through-15 doesn't need bar-level resolution to grade setup
quality at this volume).

### (1) Run 1's own 5 proposals, replayed

Only **SFM** has a real journaled stop/target — it's the one proposal that passed
decision-time review and was queued (`record_queued_decision` only fires after that gate);
the other 4 were rejected at decision time, *before* journaling, so their exact stop/
target/entry_price were never persisted anywhere in Neon. Reported honestly rather than
fabricated:

| Ticker | Real signal day | Rejection reason | Replayable? |
|---|---|---|---|
| ALGM | 2026-02-17 | §6b reward:risk 1.95 (min 2.0) | No — no geometry persisted |
| ATI | 2026-03-03 | §2b position 15.70% (cap 10%); §6b 1.97 | No — no geometry persisted |
| DINO | 2026-03-05 | §6b reward:risk 1.92 (min 2.0) | No — no geometry persisted |
| DINO | 2026-06-26 | §2b position 19.80% (cap 10%) | No — no geometry persisted |
| SFM | 2026-03-17 | queued, rejected at execution (§2b 10.26%; §6b collapsed to 0.91) | **Yes** |

**SFM replayed:** entered 2026-03-18 (the real next trading day) at $83.38 (real
slippage-adjusted 09:45 fill — notably even farther from Hermes' own $81.07 decision-time
estimate than the real execution-time price that got it rejected, underscoring how
unstable this specific setup's entry price was). Stopped out 2026-03-24, 4 trading days
later, **-1.00R**. Maximum adverse excursion 1.22R (price kept falling past the stop before
the mechanical exit fires — expected: `evaluate_bar_stop_target` fills at the stop level,
not the bar's actual low), maximum favorable excursion only 0.35R. **Real-world verdict:
the risk checker's rejection of SFM was correct — this trade would have lost, not the
"good trade the rules blocked" story a first read of the R:R collapse might suggest.**

### (2) All 995 GEOMETRY-VALID pairs, replayed

| Metric | Value |
|---|---|
| Simulated | 995 (0 fetch errors) |
| Resolved (target/stop/time_stop) | 913 |
| Unresolved (window_end/no_data) | 82 |
| **Win rate** (target / resolved) | **24.5%** |
| **Average R** (resolved) | **-0.11** |
| Avg days held (resolved) | 5.6 |

**Outcome breakdown:** target 224 (22.5%), stop 571 (57.4%), time_stop 118 (11.9%),
window_end 54 (5.4%, ran out of the data window before resolving), no_data 28 (2.8%,
a required bar was missing).

**Reading this against Part B:** the mechanical setups maverick's own algorithm classified
as "2:1 geometry available" would have lost money as a population — a 24.5% win rate at
a nominal 2:1 payoff is well below the ~33% breakeven a true 2:1 system needs, and the
realized average R is negative. This is the strongest evidence in this whole analysis that
**Hermes' caution in run 1 was directionally justified, not simply over-conservative**: the
geometry that "existed" by maverick's simplistic synthetic-level algorithm mostly did not
hold up as real support/resistance. The zero-trade outcome traded a real cost (no signal
on the handful of setups that would have worked) for avoiding a much larger one (taking
the ~57% that would have stopped out). The judgment-layer fixes in Part A (real R:R
headroom, gap stress-testing, quantified rejections) target making that filtering sharper
and more legible — not looser.

Full per-trade detail (995 rows: outcome, R-multiple, MFE/MAE, days held) lives in the
now-deleted `.counterfactual_data.json` working file — regenerate with
`python counterfactual.py` if needed.

---

## Testing

Both new modules are pure-core / thin-I/O-wrapper split, so the decision logic is fully
unit-testable without network/Neon/Hermes/Docker:

- `test_geometry_scan.py` — 28 checks: `nearest_resistance`/`valid_stop` boundary and
  tolerance cases, `classify`'s ratio-threshold behavior, and a **ground-truth regression**
  against SFM's real 2026-03-17 journaled trade (stop 77.02, target 89.18, ratio 2.00,
  reproduced exactly from real maverick output for that day).
- `test_counterfactual.py` — 32 checks: entry-day stop/target resolution, the
  same-bar-crosses-both conservative tie-break, later-day target/gap/time-stop exits,
  missing-data fail-closed behavior, non-positive-risk fail-closed behavior,
  `_slipped_buy_price` mirrors execution-sim's real formula, `aggregate`'s stats, and a
  **regression for the cycle-date-mapping bug** caught mid-build (see below).

Both suites: `python test_geometry_scan.py`, `python test_counterfactual.py` — all PASS,
no pytest, no fixtures, no network.

### A real bug caught and fixed during this build

`counterfactual.py`'s Neon recovery of run 1's proposal dates initially matched every
event to its *nearest preceding* `cycle_start` row. That's correct for `agent_activity`
rows (`risk_check`/`order_queued`/`order_rejected`) — they're written in the same
end-of-cycle batch as `cycle_start` itself (`run_backtest.py`'s locked
essential-vs-best-effort split defers all activity narration to `do_observability()`,
called once per cycle, with `cycle_start` first in that batch). But `trades` table rows
are written *during* essential Step D, **before** that same cycle's own
`do_observability()` batch runs — so a trade row's `created_at` is earlier than its own
cycle's `cycle_start` timestamp, and "nearest preceding" silently attributed it to the
*previous* day. Caught by cross-checking SFM's recovered signal date against the real
maverick technicals for that day (a wrong date would have failed to reproduce the real
journaled stop/target) — fixed by using nearest-*following* `cycle_start` for `trades` rows
specifically, nearest-preceding for `agent_activity` rows. Both directions are now covered
by `test_counterfactual.py`'s regression.

---

## Verification

- `python test_geometry_scan.py` — 28/28 PASS.
- `python test_counterfactual.py` — 32/32 PASS.
- `python geometry_scan.py` run against real data, full window: 1,250/1,250 pairs
  classified, 0 fetch errors.
- `python counterfactual.py` run against real data: 995/995 geometry-valid pairs
  simulated (0 fetch errors after a Docker Desktop restart mid-run — see below), 1/1
  replayable run-1 proposal simulated.
- `docker compose config -q` — valid.
- No RULES.md, `risk_checker.py`, or `run_backtest.py` enforcement-path changes.
- `backtest_state.json`, `quota_pacer_state.json` — untouched; no backtest run started,
  resumed, or reset.
- Neon: only SELECTs, scoped to run 1's own `run_id`, to recover its 5 proposals'
  real dates/stop/target for Part C item (1). No writes.
- **Operational note:** the full 995-pair counterfactual run exceeded the background
  process's own time limit partway through and was resumed twice — once via
  `counterfactual.py --resume`'s own checkpoint (written every 25 pairs), once after Docker
  Desktop itself went down mid-run and was restarted. Neither interruption lost data or
  required re-simulating already-completed pairs.
