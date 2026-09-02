# Round 2, Tranche 1 — Evaluation Extract

**Purpose:** a read-only evidence package for orchestrator evaluation of Hermes's
trading performance and reasoning, and of how complete/trustworthy the underlying
data is for that evaluation. This document extracts and validates; it does not
tune Hermes, change any code, or recommend rule changes.

**Identified run:** `run_id e68525f9-ce89-4588-a57e-866f28cd99fd`
**Cross-check:** `backtest-harness/backtest_state.json`'s `run_id` and the latest
Neon `portfolio_snapshots` row (`mode='backtest'`, ordered by `created_at desc`,
`limit 1`) both return `e68525f9-ce89-4588-a57e-866f28cd99fd` — **match confirmed,
no mismatch to report.**

**Masking note:** all ticker identifiers below (`OLN`, `ALGM`, `DINO`, `ATI`,
`BRX`, `SFM`, `PLNT`, `WMG`, `RYAN`, `BRKR`) are reproduced exactly as persisted
in `trades.ticker` — the same identifiers already used throughout this project's
own `HANDOFF.md` and dashboard. This report does not look up, state, or infer
what real company any of them are; it reports only what is already stored, at
the same level of identity the project's own tooling already exposes. The
separate Hermes-facing alias words (`INDIA`, `NOVEMBER`, `LIMA`, …) appear only
inside verbatim `agent_activity`/`strategy_notes` text where the harness itself
wrote them — never added or resolved by this report.

**Method:** every number below was produced by a read-only SQL query against
Neon, explicitly filtered `WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'`
(the full query text is in the appendix), or derived from those query results, or
read directly from the local `backtest_state.json`/`cycle_checkpoint.json` files.
No historical price, news, or fundamentals data was fetched to fill gaps — every
"not captured" item below is reported as unavailable, not backfilled.

---

## 1. Run-integrity extraction

| Field | Value | Source |
|---|---|---|
| `run_id` (full) | `e68525f9-ce89-4588-a57e-866f28cd99fd` | `backtest_state.json`, cross-checked against Neon |
| Simulated start date | `2026-02-17` | `backtest_state.json.start` |
| Simulated planned end date | `2026-08-14` | `backtest_state.json.end` |
| Last completed simulated date | `2026-03-23` (harness day-label "Day 35") | Latest `portfolio_snapshots` row |
| Local cursor (next day to run) | `2026-03-24` | `backtest_state.json.cursor` — one real trading day after the last completed cycle; consistent |
| Successful cycles | **25** | `count(*)` on `portfolio_snapshots` for this run_id |
| Failed cycles | **0** | `count(*)` on `agent_activity` where `event_type='cycle_failed'` for this run_id — zero rows |
| Snapshot count | 25 | direct count |
| Unique simulated-day count (`day_label`) | 25 of 25 — **no duplicates** | `Counter` over all 25 `day_label` values |
| Unique `cycle_id` count | 25 of 25 — **no duplicates** | `cycle_id` is `UNIQUE` at the DB level; independently re-verified |
| Local checkpoint vs Neon | **Agree.** `cycle_checkpoint.json` does not exist on disk (confirms no in-flight/uncommitted cycle); the local `backtest_state.json` cursor (`2026-03-24`) is exactly one real trading day past Neon's latest completed cycle (`2026-03-23`, "Day 35") | filesystem check + Neon query |
| Current strategy profile | `aggressive_v1_base` throughout — **no escalation occurred this tranche** | `trades.strategy_profile`/`min_reward_risk_used` on all 29 BUY-side rows read `aggressive_v1_base`/`1.5`; `backtest_state.json.block_start_trading_day=20`, `block_completed_trades=0` — the current 20-trading-day activity block started at trading-day-index 20 and has completed only 5 trading days into it as of cursor, too early to have triggered or avoided escalation |
| Pending/unresolved | 1 open position (`DINO`, 324 sh, entered "Day 35"/2026-03-23); `pending_orders: []` in `backtest_state.json` — **no order is currently queued awaiting next-day resolution** | `backtest_state.json.portfolio.holdings`/`.pending_orders` |
| Run status | **Ended normally**, exactly at its requested `--days 25` limit — not paused by an error, not incomplete. `EXIT=0` in the harness's own console output for this run. | run log (`backtest-harness/round2_training_tranche.log`, not a Neon table — cited for context, not queried) |

**Reconciliation: every cycle and state count reconciles.** No missing days,
no duplicate `cycle_id`s, local state and Neon agree on both the run identity
and the exact stopping point.

---

## 2. Account performance

All values from the final (25th) `portfolio_snapshots` row unless noted, cross-
checked against `backtest_state.json`'s own equity curve and closed-P&L array
(the harness's own authoritative, full-float-precision bookkeeping — Neon's
`NUMERIC(14,2)` columns round to the cent, which is the source of the one
discrepancy documented below).

| Metric | Value | Type |
|---|---|---|
| Starting cash | $100,000.00 | Directly stored (implicit — `equity_curve[0]` / `STARTING_EQUITY`, no explicit "Day 0" snapshot row exists) |
| Ending cash | $78,389.79 (Neon) / $78,389.795 (state file, full precision) | Directly stored |
| Starting equity | $100,000.00 | Directly stored |
| Ending equity | $97,590.04 | Directly stored (latest snapshot) |
| **Total return, dollars** | **−$2,409.96** | Derived: ending equity − starting equity |
| **Total return, percent** | **−2.41%** | Directly stored (`drawdown_pct` on the latest snapshot equals total return here, since equity never exceeded the $100,000 starting peak this tranche) |
| Realized P&L (harness-authoritative) | **−$2,351.65** | Directly stored — `sum(backtest_state.json.portfolio.closed_pnl)` (7 values, full float precision) |
| Realized P&L (recomputed from Neon `trades.fill_price`) | −$2,350.33 | Derived — see discrepancy note below |
| Unrealized P&L | −$58.32 | Directly stored — latest snapshot's `open_positions_detail[0].unrealized_pl` (the one open `DINO` position) |
| Gross profit (2 winners) | $2,098.02 | Derived from `closed_pnl` positive entries: $1,321.58 (SFM) + $776.44 (OLN #1) |
| Gross loss (5 losers) | −$4,449.66 | Derived from `closed_pnl` negative entries |
| Profit factor | **0.47** | Derived: gross profit ÷ \|gross loss\| = 2098.02 / 4449.66 |
| Current exposure | 19.67% | Directly stored |
| Equity peak | $100,000.00 (never exceeded — see equity series below) | Derived from the full 25-point equity curve |
| Current drawdown | −2.41% | Directly stored |
| Maximum drawdown | **−2.46%** (precisely −2.4553%), occurring at "Day 8" (index 6 of the equity curve, right after `PLNT`'s opening-gap stop and `WMG`'s stop both hit) | Derived from the full equity curve — the per-snapshot `drawdown_pct` column only ever reports *current* drawdown at that moment, not a running maximum, so this is a derived figure, not a stored one |
| API calls | 35 | Directly stored — `sum(portfolio_snapshots.api_calls)` |
| Input tokens | 209,344 | Directly stored — sum |
| Output tokens | 200,439 | Directly stored — sum |
| Reasoning tokens | 185,180 | Directly stored — sum |
| Cache-read tokens | 628,736 | Directly stored — sum |
| Total tokens | 1,038,519 | Directly stored — sum |
| Failed Hermes invocations / cycles | **0** | Directly stored — zero `cycle_failed` rows |

### Daily/cycle equity series (25 points, harness-authoritative)

| Day | Equity | Day | Equity | Day | Equity |
|---|---:|---|---:|---|---:|
| 1 | $100,000.00 | 11 | $99,520.49 | 24 | $99,612.51 |
| 2 | $100,000.00 | 14 | $99,650.98 | 25 | $99,335.31 |
| 3 | $100,000.00 | 15 | $99,642.72 | 28 | $99,506.92 |
| 4 | $99,888.31 | 16 | $99,335.31 | 29 | $99,830.31 |
| 7 | $99,433.52 | 17 | $99,506.92 | 30 | $99,612.51 |
| 8 | $97,544.71 | 18 | $99,830.31 | 31 | $98,120.92 |
| 9 | $97,705.14 | 21 | $99,612.51 | 32 | $97,648.36 |
| 10 | $98,558.54 | 22 | $97,648.36 | 35 | $97,590.04 |
|   |            | 23 | $97,648.36 |   |           |

*(Table row grouping is a display artifact of a 3-column layout, not a data
grouping — read left-to-right, top-to-bottom in day order: 1,2,3,4,7,8,9,10,11,
14,15,16,17,18,21,22,23,24,25,28,29,30,31,32,35.)*

### Reconciliation: `ending equity ≈ starting equity + realized P&L + unrealized P&L`

**Using the harness's own authoritative numbers** (full float precision, from
`backtest_state.json`):
```
100,000.00 + (−2,351.65) + (−58.32) = 97,590.03
Actual ending equity (Neon, latest snapshot): 97,590.04
Difference: $0.01 — a single cent of floating-point rounding. Effectively exact.
```

**Using Neon's `trades.fill_price`-recomputed realized P&L instead:**
```
100,000.00 + (−2,350.33) + (−58.32) = 97,591.35
Actual ending equity: 97,590.04
Difference: −$1.31
```

**Explanation of the $1.31–$1.32 discrepancy (documented, not silently
corrected):** it traces to exactly one closed trade — `SFM`'s exit. The
`agent_activity` "exit" narration and `backtest_state.json`'s `closed_pnl`
array both report `SFM`'s exit P&L as **+$1,321.58** (consistent with an
internal exit price of ≈$78.125/share), while `trades.fill_price` for that
same `SFM` SELL is stored as **$78.13** (Neon's `NUMERIC(14,2)` column,
recomputing to +$1,322.89 against the same $73.10 entry). $78.125 rounds to
$78.13 under standard rounding but the harness's own float-formatted display
and equity-curve arithmetic evidently resolved the same underlying float
differently (a `78.12`-vs-`78.13` boundary case — likely a binary
floating-point representation of `78.125` landing fractionally under the exact
half, so Python's own `:.2f` formatting rounded it down while the value stored
to the `NUMERIC` column rounded up). **This is a display/storage rounding
inconsistency on one trade, not a financial-logic defect** — every other one
of the 7 closed trades reconciles between `agent_activity`/`closed_pnl` and
`trades.fill_price` to the exact cent. It means: trust `backtest_state.json`'s
`closed_pnl` (or `agent_activity`'s exit narration) as the authoritative P&L
figure; a recompute from `trades.fill_price` alone can be off by roughly a
dollar on an affected trade.

---

## 3. Complete trade ledger

All 35 `trades` rows for this `run_id`, in chronological order. "Cycle/Day" is
derived (not a stored `trades` column) by joining each row's `decision_id` →
the `agent_activity` row that carries both that `decision_id` and a `cycle_id`
→ that `cycle_id`'s `portfolio_snapshots.day_label`. This join resolved for
**all 35 of 35 rows** (0 unmapped).

"Proposed Price (derived)" is **not** a directly stored column — `trades` has
no `entry_price`/`proposed_price` field at all (see the data-adequacy audit,
§5, item 5). It is reconstructed as `intended_notional / intended_quantity`,
which assumes that notional was computed as `quantity × Hermes's stated entry
price` — a reasonable assumption given the reasoning text, but not itself
verifiable from stored data. Treat it as an estimate, not ground truth.

| # | Cycle/Day | Ticker | Action | Status | Proposed Qty | Filled Qty | Proposed Price (derived) | Fill Price | Stop | Target | Conf | Signal/Basis | Exp R:R | Profile / Min R:R |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---|
| 1 | Day 2 | ALGM | BUY | rejected | 479 | — | 41.69 | — | 39.61 | 45.86 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 2 | Day 2 | DINO | BUY | rejected | 353 | — | 56.53 | — | 53.7 | 62.18 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 3 | Day 3 | OLN | BUY | rejected | 814 | — | 24.55 | — | 23.32 | 27.0 | 3 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 4 | Day 3 | ALGM | BUY | rejected | 518 | — | 38.59 | — | 36.66 | 42.45 | 3 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 5 | Day 4 | PLNT | BUY | **filled** | 219 | 219 | 91.13 | 91.16 | 86.57 | 100.24 | 3 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 6 | Day 7 | ALGM | BUY | rejected | 513 | — | 38.93 | — | 36.97 | 43.86 | 3 | technical/multiple | 2.52 | aggressive_v1_base / 1.5 |
| 7 | Day 7 | WMG | BUY | **filled** | 671 | 671 | 29.73 | 29.23 | 28.23 | 32.7 | 3 | technical/multiple | 1.98 | aggressive_v1_base / 1.5 |
| 8 | Day 7 | OLN | BUY | rejected | 833 | — | 23.98 | — | 22.77 | 26.94 | 3 | technical/multiple | 2.45 | aggressive_v1_base / 1.5 |
| 9 | Day 8 | WMG | **SELL** | **filled** | 671 | 671 | 29.23 | 28.27 | 28.23 | 32.7 | — | (discretionary) | — | — |
| 10 | Day 8 | PLNT | **SELL** | **filled** | 219 | 219 | 82.89 | 82.89 | — | — | — | (mechanical) | — | — |
| 11 | Day 9 | ALGM | BUY | rejected | 499 | — | 39.04 | — | 37.08 | 42.94 | 4 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 12 | Day 9 | SFM | BUY | **filled** | 263 | 263 | 73.95 | 73.10 | 70.24 | 81.35 | 3 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 13 | Day 9 | OLN | BUY | rejected | 815 | — | 23.93 | — | 22.72 | 26.32 | 3 | technical/multiple | 1.98 | aggressive_v1_base / 1.5 |
| 14 | Day 10 | ALGM | BUY | rejected | 512 | — | 38.12 | — | 36.21 | 41.93 | 3 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 15 | Day 10 | OLN | BUY | **filled** | 826 | 826 | 23.65 | 23.52 | 22.47 | 26.02 | 3 | technical/multiple | 2.01 | aggressive_v1_base / 1.5 |
| 16 | Day 16 | ATI | BUY | rejected | 126 | — | 156.96 | — | 149.11 | 172.66 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 17 | Day 16 | SFM | **SELL** | **filled** | 263 | 263 | 78.125 | 78.13 | — | — | — | (mechanical) | — | — |
| 18 | Day 17 | OLN | **SELL** | **filled** | 826 | 826 | 24.46 | 24.46 | — | — | — | (mechanical) | — | — |
| 19 | Day 18 | DINO | BUY | **filled** | 353 | 353 | 56.43 | 55.31 | 53.6 | 62.07 | 4 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 20 | Day 18 | SFM | BUY | rejected | 259 | — | 76.91 | — | 73.06 | 84.6 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 21 | Day 18 | ATI | BUY | rejected | 129 | — | 154.18 | — | 146.47 | 168.14 | 3 | technical/multiple | 1.81 | aggressive_v1_base / 1.5 |
| 22 | Day 18 | OLN | BUY | **filled** | 815 | 815 | 24.45 | 24.00 | 23.22 | 26.89 | 3 | technical/multiple | 1.98 | aggressive_v1_base / 1.5 |
| 23 | Day 18 | BRX | BUY | rejected | 673 | — | 29.59 | — | 28.11 | 32.55 | 3 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 24 | Day 18 | OLN | **SELL** | **filled** | 815 | 815 | 23.22 | 23.22 | — | — | — | (mechanical) | — | — |
| 25 | Day 21 | ATI | BUY | **filled** | 132 | 132 | 150.09 | 148.31 | 142.59 | 165.1 | 3 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 26 | Day 21 | DINO | **SELL** | **filled** | 353 | 353 | 53.6 | 53.60 | — | — | — | (mechanical) | — | — |
| 27 | Day 24 | DINO | BUY | rejected | 357 | — | 55.66 | — | 52.87 | 61.22 | 4 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 28 | Day 24 | BRX | BUY | rejected | 679 | — | 29.30 | — | 27.83 | 32.23 | 3 | technical/multiple | 1.99 | aggressive_v1_base / 1.5 |
| 29 | Day 25 | ATI | **SELL** | **filled** | 132 | 132 | 142.59 | 142.59 | — | — | — | (mechanical) | — | — |
| 30 | Day 28 | DINO | BUY | rejected | 355 | — | 55.00 | — | 52.25 | 60.5 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 31 | Day 28 | OLN | BUY | rejected | 795 | — | 24.56 | — | 23.33 | 26.94 | 3 | technical/multiple | 1.94 | aggressive_v1_base / 1.5 |
| 32 | Day 31 | RYAN | BUY | rejected | 561 | — | 34.77 | — | 33.7 | 36.51 | 3 | technical/multiple | 1.63 | aggressive_v1_base / 1.5 |
| 33 | Day 32 | RYAN | BUY | rejected | 562 | — | 34.75 | — | 33.7 | 36.49 | 3 | technical/multiple | 1.66 | aggressive_v1_base / 1.5 |
| 34 | Day 35 | DINO | BUY | **filled** | 324 | 324 | 60.22 | 59.44 | 57.21 | 66.24 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |
| 35 | Day 35 | SFM | BUY | rejected | 238 | — | 81.80 | — | 77.71 | 89.98 | 4 | technical/multiple | 2.0 | aggressive_v1_base / 1.5 |

*("Signal/Basis" and confidence are blank/`—` for SELL rows by design — AGENTS.md
does not require them on exits; `primary_signal='sentiment'` etc. only applies
to entry decisions. `expected_reward_risk`/`strategy_profile` are similarly `NULL`
on SELL rows in the schema — nothing was omitted here.)*

### Full detail per row (decision_id, invalidation condition, complete verbatim reasoning, rejection/exit reason, data timestamps, real timestamps)

**Row 1 — Day 2 — ALGM BUY (rejected)**
- `decision_id`: `4e231117-ac90-5c17-ac45-54b4e4f45db3`
- Invalidation condition: If it closes back below 39.61, the support-hold continuation thesis is broken.
- Reasoning (verbatim): Bullish RSI, MACD, trend, and outlook are aligned, and the trade has room to the 45.86 resistance band. §2a stop-based size = floor(100000 * 0.02 / (41.69 - 39.61)) = 961; §2b concentration size = floor(100000 * 0.20 / 41.69) = 479; cash size = floor(100000 / 41.69) = 2398. Required quantity is 479 shares.
- Rejection reason: §6a: stop must sit below the entry price for a long
- `data_timestamps`: {"news_as_of": "2026-02-17", "technical_as_of": "2026-02-17", "latest_news_created_at": "2026-02-13T11:24:40Z"}
- `created_at`: 2026-08-31T16:03:11Z | `updated_at`: 2026-08-31T16:03:45Z

**Row 2 — Day 2 — DINO BUY (rejected)**
- `decision_id`: `6000bf8f-10d9-5e9d-ba0b-34ca5b4b5ca0`
- Invalidation condition: If it closes back below 53.70, the support-hold continuation thesis is broken.
- Reasoning (verbatim): Bullish RSI, MACD, trend, and outlook are aligned, and the next resistance target at 62.18 gives adequate reward:risk. §2a stop-based size = floor(100000 * 0.02 / (56.53 - 53.70)) = 706; §2b concentration size = floor(100000 * 0.20 / 56.53) = 353; cash size after the earlier INDIA buy = floor(80030.49 / 56.53) = 1416. Required quantity is 353 shares.
- Rejection reason: §6a: stop must sit below the entry price for a long
- `data_timestamps`: {"news_as_of": "2026-02-17", "technical_as_of": "2026-02-17", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:03:15Z | `updated_at`: 2026-08-31T16:03:49Z

**Row 3 — Day 3 — OLN BUY (rejected)**
- `decision_id`: `79a55c67-0646-56d7-922c-9d519b52f159`
- Invalidation condition: If it closes below 23.32, the support it is using, the long thesis is broken.
- Reasoning (verbatim): Bullish RSI/MACD/trend with price holding above support and room to the 27.00 resistance. §2a stop-based size = floor(100000*0.02/(24.55-23.32)) = 1626; §2b concentration size = floor(100000*0.20/24.55) = 814; cash size = floor(100000/24.55) = 4072; required quantity = 814. No adverse news veto.
- Rejection reason: §2b: position is 20.31% of equity, cap is 20.00%; §6b: reward:risk is 1.26, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-02-18", "technical_as_of": "2026-02-18", "latest_news_created_at": "2026-02-17T12:26:51Z"}
- `created_at`: 2026-08-31T16:05:33Z | `updated_at`: 2026-08-31T16:06:07Z

**Row 4 — Day 3 — ALGM BUY (rejected)**
- `decision_id`: `1d55f697-bf1a-5036-94b0-aedac75a5353`
- Invalidation condition: If it closes below 36.66, the support that defines this setup, the thesis is invalid.
- Reasoning (verbatim): Bullish trend and RSI with price holding above support, and the second resistance at 42.45 gives a 2.0R target despite bearish MACD. After the LIMA buy, remaining cash is 80016.30. §2a stop-based size = floor(100000*0.02/(38.59-36.66)) = 1036; §2b concentration size = floor(100000*0.20/38.59) = 518; cash size = floor(80016.30/38.59) = 2073; required quantity = 518. No adverse news veto.
- Rejection reason: §6a: stop is 2.08% below entry, minimum is 3.0% — ordinary noise would trigger it
- `data_timestamps`: {"news_as_of": "2026-02-18", "technical_as_of": "2026-02-18", "latest_news_created_at": "2026-02-13T11:24:40Z"}
- `created_at`: 2026-08-31T16:05:37Z | `updated_at`: 2026-08-31T16:06:11Z

**Row 5 — Day 4 — PLNT BUY (filled)**
- `decision_id`: `073e43cd-146b-5c34-b5b5-717de20773d4`
- Invalidation condition: If it closes below 86.57, the support that justified the entry has failed.
- Reasoning (verbatim): PLNT is the clearest long setup in the list: price is sitting between support at 87.44/86.57 and resistance at 95.69/100.24, MACD is bullish, and the move to 100.24 gives about 2.0:1 reward:risk versus the 86.57 stop. §2a stop-based size = floor(100000*0.02/(91.13-86.57)) = 438; §2b concentration size = floor(100000*0.20/91.13) = 219; cash size = 1096; required quantity = 219.
- `data_timestamps`: {"news_as_of": "2026-02-19", "technical_as_of": "2026-02-19", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:08:26Z | `updated_at`: 2026-08-31T16:08:57Z
- **Exit** (Row 10, same position): mechanical, "stop hit (opening gap)" @ $82.89, P&L −$1,811.13.

**Row 6 — Day 7 — ALGM BUY (rejected)**
- `decision_id`: `876e4355-7123-532a-a68c-b1a7454c2e1b`
- Invalidation condition: If it closes back below 36.97, the support failed and the setup is invalid.
- Reasoning (verbatim): Bullish RSI and bullish trend are the main supports, with price holding above 36.98 support and room to 43.86 resistance. §2a stop-based size = floor(99,888.31*2%/(38.93-36.97)) = 1019; §2b concentration size = floor(99,888.31*20%/38.93) = 513; cash size on $80,035.96 = 2055; required quantity = 513. Reward:risk = (43.86-38.93)/(38.93-36.97) = 2.52. News was not adverse and did not veto the trade.
- Rejection reason: §2b: position is 20.27% of equity, cap is 20.00%
- `data_timestamps`: {"news_as_of": "2026-02-20", "technical_as_of": "2026-02-20", "latest_news_created_at": "2026-02-19T12:08:02Z"}
- `created_at`: 2026-08-31T16:11:35Z | `updated_at`: 2026-08-31T16:12:14Z

**Row 7 — Day 7 — WMG BUY (filled)**
- `decision_id`: `61cdfb9b-10af-510c-bd3e-6a35977b76b4`
- Invalidation condition: If it closes back below 28.23, the support failed and the setup is invalid.
- Reasoning (verbatim): Bullish RSI plus bullish MACD gives a workable long even though trend is only neutral; price is holding above 28.24 support with room to 32.70 resistance. §2a stop-based size = floor(99,888.31*2%/(29.73-28.23)) = 1331; §2b concentration size = floor(99,888.31*20%/29.73) = 671; cash size after the INDIA buy on remaining $60,064.87 = 2020; required quantity = 671. Reward:risk = (32.70-29.73)/(29.73-28.23) = 1.98. Headlines were not adverse and did not veto the trade.
- `data_timestamps`: {"news_as_of": "2026-02-20", "technical_as_of": "2026-02-20", "latest_news_created_at": "2026-02-13T13:51:21Z"}
- `created_at`: 2026-08-31T16:11:39Z | `updated_at`: 2026-08-31T16:12:21Z
- **Exit** (Row 9, same position): **discretionary**. Reasoning (verbatim): "The stop is already broken: current price 28.13 is below the 28.23 stop, so the long should be closed before considering anything new." Filled @ $28.27, P&L −$644.16.

**Row 8 — Day 7 — OLN BUY (rejected)**
- `decision_id`: `12a79ffd-2f91-58e8-9b6b-c525c651327a`
- Invalidation condition: If it closes back below 22.77, the support failed and the setup is invalid.
- Reasoning (verbatim): Bullish RSI and bullish trend keep the long valid even with one bearish MACD read; price has support at 22.78 and room to 26.94 resistance. §2a stop-based size = floor(99,888.31*2%/(23.98-22.77)) = 1651; §2b concentration size = floor(99,888.31*20%/23.98) = 833; cash size after the INDIA and DELTA buys on remaining $40,116.04 = 1672; required quantity = 833. Reward:risk = (26.94-23.98)/(23.98-22.77) = 2.45. News was not adverse and did not veto the trade.
- Rejection reason: §2b: position is 20.44% of equity, cap is 20.00%; §6b: reward:risk is 1.40, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-02-20", "technical_as_of": "2026-02-20", "latest_news_created_at": "2026-02-17T12:26:51Z"}
- `created_at`: 2026-08-31T16:11:43Z | `updated_at`: 2026-08-31T16:12:25Z

**Row 11 — Day 9 — ALGM BUY (rejected)**
- `decision_id`: `d8c7955b-54bf-5662-93ba-e703202c6421`
- Invalidation condition: If it closes back below 37.08 after entry, the support bounce is broken.
- Reasoning (verbatim): Bullish trend with strong bullish outlook and price holding above support. Entry 39.04, stop 37.08 risks 1.96/share and target 42.94 offers 3.90/share, for 1.99:1. §2a size = 995, §2b size = 499, cash size = 2498; take 499 shares. News was positive and not adverse.
- Rejection reason: §2b: position is 20.01% of equity, cap is 20.00%
- `data_timestamps`: {"news_as_of": "2026-02-24", "technical_as_of": "2026-02-24", "latest_news_created_at": "2026-02-19T12:08:02Z"}
- `created_at`: 2026-08-31T16:19:41Z | `updated_at`: 2026-08-31T16:20:19Z

**Row 12 — Day 9 — SFM BUY (filled)**
- `decision_id`: `c07f73b9-23b2-58dd-8270-1a5ce87d4d82`
- Invalidation condition: If it closes back below 70.24 after entry, the support bounce failed.
- Reasoning (verbatim): MACD is bullish and RSI is healthy, with price above support and room to the next resistance level. Entry 73.95, stop 70.24 risks 3.71/share and target 81.35 offers 7.40/share, for 1.99:1. After the INDIA buy, cash size = 1055; §2a size = 525 and §2b size = 263, so take 263 shares. News was not adverse.
- `data_timestamps`: {"news_as_of": "2026-02-24", "technical_as_of": "2026-02-24", "latest_news_created_at": "2026-02-23T18:39:33Z"}
- `created_at`: 2026-08-31T16:19:44Z | `updated_at`: 2026-08-31T16:20:25Z
- **Exit** (Row 17, same position): mechanical, "time stop (5 trading days)" @ $78.13, P&L +$1,321.58 (see §2's rounding note — the recomputed-from-fill-price figure is +$1,322.89).

**Row 13 — Day 9 — OLN BUY (rejected)**
- `decision_id`: `4e14aeba-7b34-5e17-a6e4-a7d7caedc0d9`
- Invalidation condition: If it closes back below 22.72 after entry, the support level failed.
- Reasoning (verbatim): Bullish trend and moderately bullish outlook with price above the 22.73 support. Entry 23.93, stop 22.72 risks 1.21/share and target 26.32 offers 2.39/share, for 1.98:1. After the prior buys, cash size = 2449; §2a size = 1612 and §2b size = 815, so take 815 shares. News was neutral/positive, not adverse.
- Rejection reason: §2b: position is 20.19% of equity, cap is 20.00%
- `data_timestamps`: {"news_as_of": "2026-02-24", "technical_as_of": "2026-02-24", "latest_news_created_at": "2026-02-20T17:35:23Z"}
- `created_at`: 2026-08-31T16:19:48Z | `updated_at`: 2026-08-31T16:20:29Z

**Row 14 — Day 10 — ALGM BUY (rejected)**
- `decision_id`: `62bffc09-47a9-542a-93e6-08678886b7df`
- Invalidation condition: If it closes back below 36.21 support, the bullish setup failed.
- Reasoning (verbatim): Bullish trend and strongly bullish outlook with price sitting between support and resistance; RSI is bullish. The stop at 36.21 is a real technical level, about 5.0% below entry, so it has room beyond the overnight-gap stress test. Reward to 41.93 is 3.81 vs 1.91 risk, about 1.99:1, above the 1.5:1 floor. Size: stop-based floor(97705.14*0.02/1.91)=1023, concentration floor(97705.14*0.20/38.12)=512, cash floor(78319.41/38.12)=2054; required quantity is 512 shares.
- Rejection reason: §6a: stop is 2.69% below entry, minimum is 3.0% — ordinary noise would trigger it
- `data_timestamps`: {"news_as_of": "2026-02-25", "technical_as_of": "2026-02-25", "latest_news_created_at": "2026-02-19T12:08:02Z"}
- `created_at`: 2026-08-31T16:22:40Z | `updated_at`: 2026-08-31T16:23:14Z

**Row 15 — Day 10 — OLN BUY (filled)**
- `decision_id`: `f6fc84f4-cfc4-57a0-9824-409d6eb30178`
- Invalidation condition: If it closes back below 22.47 support, the pullback-bounce thesis is wrong.
- Reasoning (verbatim): Bullish trend with RSI near neutral-bullish and price above support, giving a tradable pullback setup even though MACD is bearish. The stop at 22.47 is a real support level, about 5.0% below entry, so it is comfortably outside the gap-stress concern. Reward to 26.02 is 2.37 vs 1.18 risk, about 2.01:1, above the 1.5:1 floor. Size: stop-based floor(97705.14*0.02/1.18)=1655, concentration floor(97705.14*0.20/23.65)=826, cash floor(78319.41/23.65)=3312; required quantity is 826 shares.
- `data_timestamps`: {"news_as_of": "2026-02-25", "technical_as_of": "2026-02-25", "latest_news_created_at": "2026-02-20T17:35:23Z"}
- `created_at`: 2026-08-31T16:22:44Z | `updated_at`: 2026-08-31T16:23:21Z
- **Exit** (Row 18, same position): mechanical, "time stop (5 trading days)" @ $24.46, P&L +$776.44.

**Row 16 — Day 16 — ATI BUY (rejected)**
- `decision_id`: `b2d0e41a-81a2-509c-8720-63fac01f9171`
- Invalidation condition: If it closes back below 149.11, the higher-low thesis is broken.
- Reasoning (verbatim): Bullish RSI/MACD/trend alignment with room to the 172.66 resistance. Risk per share is 7.85; §2a size = floor(1990.4098/7.85)=253, §2b size = floor(19904.098/156.96)=126, cash size = floor(58891.89/156.96)=375, so required quantity is 126.
- Rejection reason: §2b: position is 20.21% of equity, cap is 20.00%; §6b: reward:risk is 1.35, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-03", "technical_as_of": "2026-03-03", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:39:06Z | `updated_at`: 2026-08-31T16:39:39Z

**Row 19 — Day 18 — DINO BUY (filled)**
- `decision_id`: `73149e69-2e85-5ec6-a2aa-e202e7ccaf0a`
- Invalidation condition: If it closes back below 53.60 support, the bullish thesis is wrong.
- Reasoning (verbatim): Bullish RSI, bullish MACD, bullish trend, and strongly bullish outlook. Entry at 56.43 with stop at 53.60 gives risk/share 2.83. §2a stop-based size = floor(1992.85 / 2.83) = 704; §2b concentration size = floor(19928.54 / 56.43) = 353; cash size on remaining cash = 1765, so required quantity is 353 shares. Target at 62.07 gives reward/risk = (62.07 - 56.43) / (56.43 - 53.60) = 1.99.
- `data_timestamps`: {"news_as_of": "2026-03-05", "technical_as_of": "2026-03-05", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:46:32Z | `updated_at`: 2026-08-31T16:47:24Z
- **Exit** (Row 26, same position): mechanical, "stop hit" @ $53.60, P&L −$603.63.

**Row 20 — Day 18 — SFM BUY (rejected)**
- `decision_id`: `ae4a81a2-c3d9-55e5-af38-6e33eab99834`
- Invalidation condition: If it closes back below 73.06 support, the setup is invalid.
- Reasoning (verbatim): Bullish RSI, bullish MACD, bullish trend, and strongly bullish outlook. Entry at 76.91 with stop at 73.06 gives risk/share 3.85. §2a stop-based size = floor(1992.85 / 3.85) = 517; §2b concentration size = floor(19928.54 / 76.91) = 259; cash size on remaining cash after earlier buys = 1036, so required quantity is 259 shares. Target at 84.60 gives reward/risk = (84.60 - 76.91) / (76.91 - 73.06) = 2.00.
- Rejection reason: §2b: position is 20.23% of equity, cap is 20.00%; §6b: reward:risk is 1.42, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-05", "technical_as_of": "2026-03-05", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:46:36Z | `updated_at`: 2026-08-31T16:47:29Z

**Row 21 — Day 18 — ATI BUY (rejected)**
- `decision_id`: `3a690947-6dbb-5387-8804-5bdd9e15745f`
- Invalidation condition: If it closes back below 146.47 support, the bullish case is broken.
- Reasoning (verbatim): Bullish RSI and bullish trend with moderately bullish outlook, though MACD is bearish. Entry at 154.18 with stop at 146.47 gives risk/share 7.71. §2a stop-based size = floor(1992.85 / 7.71) = 258; §2b concentration size = floor(19928.54 / 154.18) = 129; cash size on remaining cash after earlier buys = 387, so required quantity is 129 shares. Target at 168.14 gives reward/risk = (168.14 - 154.18) / (154.18 - 146.47) = 1.81.
- Rejection reason: §6a: stop is 2.09% below entry, minimum is 3.0% — ordinary noise would trigger it
- `data_timestamps`: {"news_as_of": "2026-03-05", "technical_as_of": "2026-03-05", "latest_news_created_at": "2026-03-03T17:35:27Z"}
- `created_at`: 2026-08-31T16:46:40Z | `updated_at`: 2026-08-31T16:47:33Z

**Row 22 — Day 18 — OLN BUY (filled)**
- `decision_id`: `2158c4a3-96fe-585f-a587-a8482d2972dd`
- Invalidation condition: If it closes back below 23.22 support, the trade is wrong.
- Reasoning (verbatim): Bullish trend and moderately bullish outlook, with RSI still above neutral even though MACD is bearish. Entry at 24.45 with stop at 23.22 gives risk/share 1.23. §2a stop-based size = floor(1992.85 / 1.23) = 1620; §2b concentration size = floor(19928.54 / 24.45) = 815; cash size on remaining cash after earlier buys = 1632, so required quantity is 815 shares. Target at 26.89 gives reward/risk = (26.89 - 24.45) / (24.45 - 23.22) = 1.98.
- `data_timestamps`: {"news_as_of": "2026-03-05", "technical_as_of": "2026-03-05", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:46:45Z | `updated_at`: 2026-08-31T16:47:39Z
- **Exit** (Row 24, same position, same-day): mechanical, "stop hit" @ $23.22, P&L −$635.70. **Entered and stopped out the same simulated trading day** (Day 18) — a real, verified 0-trading-day holding period, not a data artifact (confirmed via the `agent_activity`→`portfolio_snapshots` day-label join).

**Row 23 — Day 18 — BRX BUY (rejected)**
- `decision_id`: `6e6bf72b-cbb3-5c9e-8d9f-8ec829c19c5e`
- Invalidation condition: If it closes back below 28.11 support, the bullish thesis failed.
- Reasoning (verbatim): Bullish trend with RSI still constructive, despite bearish MACD. Entry at 29.59 with stop at 28.11 gives risk/share 1.48. §2a stop-based size = floor(1992.85 / 1.48) = 1346; §2b concentration size = floor(19928.54 / 29.59) = 673; cash size on remaining cash after earlier buys = 675, so required quantity is 673 shares. Target at 32.55 gives reward/risk = (32.55 - 29.59) / (29.59 - 28.11) = 2.00.
- Rejection reason: §2b: position is 20.15% of equity, cap is 20.00%
- `data_timestamps`: {"news_as_of": "2026-03-05", "technical_as_of": "2026-03-05", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:46:49Z | `updated_at`: 2026-08-31T16:47:43Z

**Row 25 — Day 21 — ATI BUY (filled)**
- `decision_id`: `511d7dc6-6b23-5456-907d-49e607e7d6ef`
- Invalidation condition: If it closes back below 142.59, the bullish structure and support that justified the entry have failed.
- Reasoning (verbatim): Bullish RSI and trend with the price still below the 157.59 resistance and room to the 165.10 level. I used stop 142.59, so §2a size = floor(99335.31*0.02/7.50) = 264, §2b size = floor(99335.31*0.20/150.09) = 132, cash size = floor(79482.59/150.09) = 529; required size is 132 shares. Reward:risk = (165.10-150.09)/(150.09-142.59) = 2.0:1.
- `data_timestamps`: {"news_as_of": "2026-03-06", "technical_as_of": "2026-03-06", "latest_news_created_at": "2026-03-03T17:35:27Z"}
- `created_at`: 2026-08-31T16:49:52Z | `updated_at`: 2026-08-31T16:50:24Z
- **Exit** (Row 29, same position): mechanical, "stop hit" @ $142.59, P&L −$755.04.

**Row 27 — Day 24 — DINO BUY (rejected)**
- `decision_id`: `599518ed-b83b-5c2b-8d58-70cb2cadce7b`
- Invalidation condition: Close back below 52.87, which would break the support used for the stop.
- Reasoning (verbatim): Bullish RSI/MACD/trend with strongly bullish outlook and no adverse news. §2a stop-based size = floor(99,612.51*0.02 / (55.66-52.87)) = 714; §2b concentration size = floor(99,612.51*0.20 / 55.66) = 357; cash size = floor(78,826.47 / 55.66) = 1416; required quantity = 357. Entry 55.66, stop 52.87, target 61.22 gives reward:risk = 5.56 / 2.79 = 1.99.
- Rejection reason: §2b: position is 20.70% of equity, cap is 20.00%; §6b: reward:risk is 0.89, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-11", "technical_as_of": "2026-03-11", "latest_news_created_at": null}
- `created_at`: 2026-08-31T16:59:01Z | `updated_at`: 2026-08-31T16:59:33Z

**Row 28 — Day 24 — BRX BUY (rejected)**
- `decision_id`: `7f40448c-d796-5ed9-b9c2-216f4fba5b4d`
- Invalidation condition: Close back below 27.83, which would negate the support break that justified the entry.
- Reasoning (verbatim): Bullish RSI and trend with a constructive setup despite bearish MACD; positive news was not the reason to enter, only not a veto. After the NOVEMBER buy, remaining cash is 58,955.85. §2a stop-based size = floor(99,612.51*0.02 / (29.30-27.83)) = 1355; §2b concentration size = floor(99,612.51*0.20 / 29.30) = 679; cash size = floor(58,955.85 / 29.30) = 2012; required quantity = 679. Entry 29.30, stop 27.83, target 32.23 gives reward:risk = 2.93 / 1.47 = 1.99.
- Rejection reason: §2b: position is 20.40% of equity, cap is 20.00%; §6b: reward:risk is 1.39, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-11", "technical_as_of": "2026-03-11", "latest_news_created_at": "2026-03-05T15:32:50Z"}
- `created_at`: 2026-08-31T16:59:05Z | `updated_at`: 2026-08-31T16:59:37Z

**Row 30 — Day 28 — DINO BUY (rejected)**
- `decision_id`: `a8c64ac1-20c6-5a74-b568-25117bdef4d4`
- Invalidation condition: Exit if it closes back below 52.25, the support level used for the stop.
- Reasoning (verbatim): Bullish trend, bullish MACD, bullish RSI, and strongly bullish outlook are aligned. Entry 55.00 with stop 52.25 risks 2.75; target 60.50 offers 5.50 reward for a 2.00:1 reward:risk. The ~3% adverse-gap check still opens above the stop. §2a size is 710 shares, §2b size is 355 shares, and cash size is 1775 shares, so the required order is 355 shares.
- Rejection reason: §2b: position is 20.48% of equity, cap is 20.00%; §6b: reward:risk is 1.02, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-13", "technical_as_of": "2026-03-13", "latest_news_created_at": "2026-03-12T18:54:54Z"}
- `created_at`: 2026-08-31T17:04:33Z | `updated_at`: 2026-08-31T17:05:04Z
- (Note: this row explicitly names the "~3% adverse-gap check" — direct textual evidence Hermes is applying the `AGENTS.md` stop-margin stress-test guidance during reasoning, though the trade was rejected on §2b/§6b, not §6a.)

**Row 31 — Day 28 — OLN BUY (rejected)**
- `decision_id`: `7ab12b26-1051-50fa-9e0a-699fb0051e93`
- Invalidation condition: Exit if it closes back below 23.33, the support level used for the stop.
- Reasoning (verbatim): Bullish trend and bullish RSI are acceptable, and the overhead room to 26.94 gives the trade enough reward to clear the 1.5:1 floor. Entry 24.56 with stop 23.33 risks 1.23; target 26.94 offers 2.38 reward for a 1.94:1 reward:risk. The ~3% adverse-gap check still opens above the stop. After the earlier buy, remaining cash still leaves the cash cap far above size, so the binding numbers are §2a 1587 shares, §2b 795 shares, and cash size 3180 shares; required order is 795 shares.
- Rejection reason: §2b: position is 20.35% of equity, cap is 20.00%; §6b: reward:risk is 1.16, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-13", "technical_as_of": "2026-03-13", "latest_news_created_at": "2026-03-12T15:44:04Z"}
- `created_at`: 2026-08-31T17:04:36Z | `updated_at`: 2026-08-31T17:05:07Z

**Row 32 — Day 31 — RYAN BUY (rejected)**
- `decision_id`: `0c26a29d-fd12-554a-bebf-0c57903412e0`
- Invalidation condition: If it closes back below 33.70, the support bounce failed.
- Reasoning (verbatim): Oversold RSI with bullish MACD gives a workable long setup against the bearish trend, and price is holding above measurable support at 33.70 with the next resistance at 36.51. Reward is 1.74 versus 1.07 risk for 1.63:1. Position sizing: §2a stop-based size = floor(97,648.35*0.02/1.07) = 1,825; §2b concentration size = floor(97,648.35*0.20/34.77) = 561; cash size = floor(97,648.35/34.77) = 2,807; required quantity = 561.
- Rejection reason: §2b: position is 20.37% of equity, cap is 20.00%; §6b: reward:risk is 0.61, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-18", "technical_as_of": "2026-03-18", "latest_news_created_at": "2026-03-17T14:39:22Z"}
- `created_at`: 2026-08-31T17:11:10Z | `updated_at`: 2026-08-31T17:11:36Z

**Row 33 — Day 32 — RYAN BUY (rejected)**
- `decision_id`: `470d19a2-c826-50a3-971b-5baf8184690b`
- Invalidation condition: If it closes back below 33.70, the support failed and the bounce thesis is wrong.
- Reasoning (verbatim): Oversold RSI with bullish MACD gives a technical bounce setup at measurable support. Stop at 33.70 risks 1.05/share, target at 36.49 offers 1.74/share for RR 1.66. §2a size = floor(97,648.35*0.02/1.05)=1,859 shares; §2b size = floor(97,648.35*0.20/34.75)=562 shares; cash size = 2,809 shares; required quantity = 562 shares.
- Rejection reason: §2b: position is 20.13% of equity, cap is 20.00%; §6b: reward:risk is 1.20, minimum is 1.5
- `data_timestamps`: {"news_as_of": "2026-03-19", "technical_as_of": "2026-03-19", "latest_news_created_at": "2026-03-17T14:39:22Z"}
- `created_at`: 2026-08-31T17:13:34Z | `updated_at`: 2026-08-31T17:14:01Z

**Row 34 — Day 35 — DINO BUY (filled, open at end of tranche)**
- `decision_id`: `e4f52c86-b0e6-5208-9c1c-2faf10144e81`
- Invalidation condition: If it closes back below 57.21, the support level used for the stop.
- Reasoning (verbatim): Strong bullish alignment: RSI 65.9, MACD bullish, trend bullish, and outlook strongly bullish. Stop is anchored to measurable support at 57.21, target is the next higher resistance at 66.24, giving reward:risk 6.02/3.01 = 2.0. §2a stop-based size = floor(97,648.35*0.02 / (60.22-57.21)) = 648 shares; §2b concentration size = floor(97,648.35*0.20 / 60.22) = 324 shares; cash size is larger, so the required quantity is 324 shares.
- `data_timestamps`: {"news_as_of": "2026-03-20", "technical_as_of": "2026-03-20", "latest_news_created_at": "2026-03-17T17:48:01Z"}
- `created_at`: 2026-08-31T17:16:22Z | `updated_at`: 2026-08-31T17:16:56Z
- **Still open** as of the run's last snapshot: current price $59.26, unrealized P&L −$58.32 (−0.30%).

**Row 35 — Day 35 — SFM BUY (rejected)**
- `decision_id`: `29e6472c-6f8f-583b-9bdf-2fc8f54ff076`
- Invalidation condition: If it closes back below 77.71, the support level used for the stop.
- Reasoning (verbatim): Clean bullish structure: RSI 60.1, MACD bullish, trend bullish, and outlook strongly bullish. Stop is anchored to measurable support at 77.71, target is the next higher resistance at 89.98, giving reward:risk 8.18/4.09 = 2.0. §2a stop-based size = floor(97,648.35*0.02 / (81.80-77.71)) = 477 shares; §2b concentration size = floor(97,648.35*0.20 / 81.80) = 238 shares; cash size after earlier buys remains above this, so the required quantity is 238 shares.
- Rejection reason: §6a: stop is 2.35% below entry, minimum is 3.0% — ordinary noise would trigger it
- `data_timestamps`: {"news_as_of": "2026-03-20", "technical_as_of": "2026-03-20", "latest_news_created_at": null}
- `created_at`: 2026-08-31T17:16:25Z | `updated_at`: 2026-08-31T17:17:00Z

### Entry/exit pairing verification

Positions were paired by strict sequential BUY→SELL matching per ticker
(matches the dashboard's own `pairTrades()` logic in `dashboard/index.html`) —
verified, not assumed, by walking every ticker's row history in `created_at`
order.

| Ticker | Entry (Row) | Exit (Row) | Result |
|---|---|---|---|
| PLNT | Row 5, Day 4 | Row 10, Day 8 | Closed, −$1,811.13 |
| WMG | Row 7, Day 7 | Row 9, Day 8 | Closed, −$644.16 |
| SFM | Row 12, Day 9 | Row 17, Day 16 | Closed, +$1,321.58 (see rounding note) |
| OLN #1 | Row 15, Day 10 | Row 18, Day 17 | Closed, +$776.44 |
| DINO #1 | Row 19, Day 18 | Row 26, Day 21 | Closed, −$603.63 |
| OLN #2 | Row 22, Day 18 | Row 24, Day 18 | Closed, −$635.70 (same-day) |
| ATI | Row 25, Day 21 | Row 29, Day 25 | Closed, −$755.04 |
| DINO #2 | Row 34, Day 35 | *(open)* | **Open** at tranche end, unrealized −$58.32 |

**7 filled BUYs paired to exactly 7 exits (7 closed positions) with no
orphaned exit and no double-counted entry; the 8th filled BUY (`DINO #2`) has
no exit row, matching the 1 open position in the final snapshot exactly.**
This reconciles cleanly with `open_positions=1` / `closed` implied count of 7
on every relevant snapshot.

---

## 4. Performance statistics

All figures show sample size explicitly. **7 closed trades is a small sample —
every ratio/rate below is flagged as statistically weak; none should be read
as a reliable estimate of true strategy performance.**

| Statistic | Value | n |
|---|---|---|
| BUY proposals | 28 | 28 |
| SELL proposals | 7 | 7 |
| Decision-time rejections (`risk_check`) | 18 | 18 |
| Execution-time rejections (`order_rejected`) | 20 | 20 |
| Filled entries (BUY) | 8 | 8 |
| Filled exits (SELL) | 7 | 7 |
| Open positions (end of tranche) | 1 | 1 |
| Closed positions | 7 | 7 |
| Winners | 2 (SFM, OLN #1) | 7 |
| Losers | 5 (PLNT, WMG, OLN #2, DINO #1, ATI) | 7 |
| Breakeven | 0 | 7 |
| **Win rate** | **28.6% (2/7)** | **n=7 — statistically weak** |
| Average $ P&L (closed) | −$335.95 | 7 |
| Median $ P&L (closed) | −$635.70 | 7 |
| Average % return (closed) | −1.67% | 7 |
| Median % return (closed) | −3.25% | 7 |
| Average win | +$1,049.01 ($2,098.02 / 2) | 2 |
| Average loss | −$889.93 (−$4,449.66 / 5) | 5 |
| Gross profit | $2,098.02 | 2 |
| Gross loss | −$4,449.66 | 5 |
| **Profit factor** | **0.47** | 7 |
| **Expectancy per closed trade** | **−$335.95** | 7 |
| Average holding period | 3.71 trading days | 7 |
| Median holding period | 4 trading days | 7 |
| Holding periods (individual) | DINO#1: 3, OLN#1: 7, OLN#2: 0, PLNT: 4, WMG: 1, SFM: 7, ATI: 4 | 7 |

*(Holding periods are derived by joining each entry's and exit's `decision_id`
→ `agent_activity.cycle_id` → `portfolio_snapshots.day_label`, then subtracting
the parsed day numbers — not from any directly-stored "holding days" field on
`trades` itself, though `portfolio_snapshots.open_positions_detail.days_held`
independently confirms the same numbers day-by-day while a position was open;
see §5 item 6.)*

### Exit-reason distribution (from `agent_activity` "exit" event descriptions, 7 of 7 closed positions — every exit has an explicit reason, none inferred)

| Reason | Count |
|---|---|
| Stop hit (intraday) | 3 (OLN #2, DINO #1, ATI) |
| Time stop (5 trading days) | 2 (SFM, OLN #1) |
| Stop hit via opening gap | 1 (PLNT) |
| Discretionary (thesis-invalidation) | 1 (WMG) |

### Results grouped by confidence (closed trades only, n=7)

| Confidence | Closed trades | Winners | Win rate |
|---|---|---|---|
| 3 | 6 (PLNT, WMG, SFM, OLN#1, OLN#2, ATI) | 2 (SFM, OLN#1) | 33% — **n=6, statistically weak** |
| 4 | 1 (DINO#1) | 0 | 0% — **n=1, not meaningful** |

*(No confidence-1, -2, or -5 BUY decisions occurred this tranche — the full
distribution across all 28 BUY proposals, not just closed ones, is
confidence-3: 18, confidence-4: 10, nothing else.)*

### Results grouped by primary signal / technical basis

**Not computable as a real breakdown** — all 28 BUY decisions this tranche used
`primary_signal='technical'` and `technical_basis='multiple'` with zero
exceptions. There is no variation to group by; reporting a "100% technical,
100% multiple" table would not carry any comparative information.

### Planned reward vs. realized R-multiple (closed positions, initial risk reconstructed from `stop_price` at entry × filled quantity)

| Ticker | Entry | Stop | Planned risk ($) | Realized P&L | R-multiple |
|---|---:|---:|---:|---:|---:|
| DINO #1 | 55.31 | 53.60 | $603.63 | −$603.63 | **−1.00R** (exact stop) |
| OLN #1 | 23.52 | 22.47 | $867.30 | +$776.44 | **+0.90R** (time-stopped before reaching full target) |
| OLN #2 | 24.00 | 23.22 | $635.70 | −$635.70 | **−1.00R** (exact stop) |
| PLNT | 91.16 | 86.57 | $1,005.21 | −$1,811.13 | **−1.80R** (lost MORE than planned — opening-gap slippage through the stop) |
| WMG | 29.23 | 28.23 | $671.00 | −$644.16 | **−0.96R** (discretionary exit fractionally inside planned risk) |
| SFM | 73.10 | 70.24 | $752.18 | +$1,322.89 (Neon fill_price) | **+1.76R** (time-stopped well past 1R, short of the full target) |
| ATI | 148.31 | 142.59 | $755.04 | −$755.04 | **−1.00R** (exact stop) |

**Real, verified finding: PLNT lost 1.80R against a planned 1.00R** — a genuine
gap-through-stop event (mechanical exit reason: "stop hit (opening gap)"),
exactly the risk `AGENTS.md`'s stop-margin stress-test guidance targets. This
is real evidence the guidance is necessary, not evidence it is sufficient —
this exact position was entered on Day 4 (2026-02-20), **before** that guidance
existed in `AGENTS.md` (added 2026-08-31, per `HANDOFF.md`), so it does not by
itself show the guidance failing to prevent a gap loss on a *later* decision.

### Proposed vs. fill price (decision-to-fill price movement), filled BUYs only (n=8)

**Correction (see §5, item 5): the earlier version of this table labeled the
gap between the decision-day proposed price and the next-day fill "slippage"
and called it adverse. That was wrong on two counts.** First, "proposed"
here is a *decision-time* price (2026-08-14T close-of-data, the day Hermes
reasoned from); "fill" is the *next real trading day's* 09:45 execution --
the difference is real overnight/next-morning price movement between two
different points in time, not execution slippage against a single moment
(`run_backtest.py`'s actual 5bps slippage model is a separate, much smaller
effect applied AT the 09:45 fill itself, not measured by this table at all).
Second, and more importantly: **a lower BUY fill is price-favourable by
itself** -- paying less for the same long is a better fill, not a worse
one -- though a fill consistently below the prior day's proposed price can
also be a signal of real overnight weakness in the name, worth noting
separately from whether the fill itself was "good execution." The table
below is therefore renamed and reframed; every underlying number is
unchanged from the original calculation.

| Ticker | Decision-day proposed (derived) | Next-day fill | Price movement | Movement % |
|---|---:|---:|---:|---:|
| PLNT | 91.13 | 91.16 | +0.03 | +0.03% |
| WMG | 29.73 | 29.23 | −0.50 | −1.68% |
| SFM | 73.95 | 73.10 | −0.85 | −1.15% |
| OLN #1 | 23.65 | 23.52 | −0.13 | −0.55% |
| DINO #1 | 56.43 | 55.31 | −1.12 | −1.99% |
| OLN #2 | 24.45 | 24.00 | −0.45 | −1.84% |
| ATI | 150.09 | 148.31 | −1.78 | −1.19% |
| DINO #2 | 60.22 | 59.44 | −0.78 | −1.30% |

7 of 8 filled BUYs' next-day fill landed below the decision-day proposed
price, and **price-favourable for the entry itself** (a long that fills
cheaper than it was proposed at is a better fill, not a worse one). **Eight
observations are too few to distinguish a repeatable pattern from noise** --
this dataset does not support a claim about *why* fills skew lower, only
that they did seven of eight times in this tranche. The finding here is
that this metric is worth tracking across future tranches, not an
explanation of its cause now. (See §5, item 5's caveat that "proposed"
here is a derived, not directly stored, figure -- a gap this instrumentation
task's own Implementation Decision 4 closes going forward by persisting
`proposed_entry_price` directly.)

### Stop-distance and target-distance distributions (all 28 BUY proposals)

| | n | Average | Median | Min | Max |
|---|---:|---:|---:|---:|---:|
| Stop distance (% below entry) | 28 | 4.54% | 5.00% | 3.02% | 5.06% |
| Target distance (% above entry) | 28 | 9.77% | 10.00% | 5.00% | 12.66% |

Stop distances cluster tightly around two values: most near the harness's
common **5%** technical-level default, and a smaller cluster near **3.0–3.9%**
— close to RULES.md §6a's 3.0% floor, where the `AGENTS.md` stress-test
guidance is most relevant. **6 of 28 BUY proposals (21%) had a stop under 4%
of entry**, and of those, most were rejected at execution time (§6a) — see §5
below.

### How often a trade reached its target, stop, or time limit (closed positions, n=7)

| Outcome | Count |
|---|---|
| Reached the intraday stop | 3 |
| Reached the intraday stop via an opening gap | 1 |
| Hit the 5-trading-day time stop before reaching either stop or target | 2 |
| Exited early on discretionary thesis-invalidation (before stop/target/time-stop) | 1 |
| **Reached the actual `target_price`** | **0 of 7** |

**No closed position in this tranche ever reached its stated target price.**
This is a real, notable, verified fact — not inferred from a summary field,
directly cross-checked against every exit's `fill_price` vs. its entry's
`target_price` in the ledger above.

---

## 5. Rejections and no-trade behaviour

From the complete `agent_activity` dataset for this run (162 rows total).

### Rejections grouped by RULES.md section

| Section | Decision-time (`risk_check`) | Execution-time (`order_rejected`) | Total |
|---|---:|---:|---:|
| §2b (concentration cap) | 15 | 14 | 29 |
| §6b (reward:risk floor) | 0 | 10 | 10 |
| §6a (stop-distance floor) | 0 | 6 | **6** |
| §2 (general sizing) | 3 | 0 | 3 |
| §3 (cash) | 3 | 0 | 3 |

*(§2 and §3 counts above are the SAME 3 events — each of those 3 rows cites
both sections together in one combined "quantity does not equal required
whole-share size" message; they are not 6 independent occurrences.)*

**Real, verified pattern: §6a and §6b rejections occurred ONLY at execution
time, never at decision time — 0 of 18 decision-time (`risk_check`) rows cite
either section.** This is consistent with the project's own documented design
(decision-time context is advisory against same-day marks; §6a/§6b's real
gate is execution-time, against the actual next-day fill) — not a defect, but
a real, confirmed behavior worth recording precisely rather than assuming.

### Decision-time vs. execution-time rejection counts

- Decision-time (`risk_check`): **18** — none of these produce a `trades` row
  at all (Hermes's proposal is blocked before ever being queued/journaled).
- Execution-time (`order_rejected`): **20** — these correspond **exactly**
  1:1 with the 20 `trades` rows carrying `status='rejected'` (a queued order
  that failed real next-day re-validation). Verified: `count(trades WHERE
  status='rejected') = 20 = count(agent_activity WHERE event_type=
  'order_rejected')`.

### Rejections by masked ticker (all 38 rejection events, both types)

| Ticker | Count |
|---|---:|
| ALGM | 10 |
| DINO | 8 |
| OLN | 5 |
| SFM | 5 |
| ATI | 3 |
| BRX | 3 |
| BRKR | 2 |
| RYAN | 2 |

### Rejections by simulated day

| Day | Count | Day | Count |
|---|---:|---|---:|
| 2 | 2 | 16 | 4 |
| 3 | 2 | 18 | 4 |
| 7 | 4 | 24 | 2 |
| 9 | 2 | 28 | 2 |
| 10 | 4 | 31 | 1 |
| 11 | 3 | 32 | 1 |
| 14 | 2 | 35 | 3 |
| 15 | 2 | | |

### Gap-through-stop / reward failures

- **Gap-through-stop:** 1 confirmed instance — `PLNT`'s exit, "stop hit
  (opening gap)" — see the R-multiple table in §4 (−1.80R, worse than the
  planned −1.00R).
- **Reward-floor (§6b) failures:** 10, all execution-time, all with an exact
  numeric ratio in the stored reason (e.g. "reward:risk is 1.02, minimum is
  1.5") — none are vague.

### Quantity/sizing failures

3 decision-time `risk_check` rows, all citing §2/§3 together, all with an
exact numeric mismatch stated (e.g. "quantity 393 does not equal required
whole-share size 395"). All 3 occurred on `DINO` (×2) and `ALGM` (×1).

### News-vetoed entries

**Not captured as a formal, countable event.** Direct query of every
`rejection_reason` on all 20 `status='rejected'` trades and every §-numbered
`risk_check`/`order_rejected` description found **zero** formal news-veto
rejections — RULES.md §5a news vetoes never produced a `trades` row or a
§-numbered activity row this tranche. However, free-text `reasoning` activity
rows **do** mention news vetoes narratively at least 5 separate times (e.g.
"skipped LIMA because the headline was an adverse liability-type news item,"
"NOVEMBER had adverse CEO-leave news"). Per this task's own instruction not
to treat general narration as a confirmed rejection unless the persisted
event type/description supports that classification, **this report does not
count those as formal rejections** — it flags that news-veto counting is
possible only by free-text search, not by a reliable, structured query. See
the data-adequacy audit, item 3.

### Missing-data / provider failures

**Zero.** No `cycle_failed` rows exist for this run_id, and no `agent_activity`
row of any type mentions a data-fetch error, timeout, or provider failure.

### No-trade cycles

**10 of 25 cycles (40%) produced no `order_queued` event at all.** Derived by
joining every `cycle_start` row's `cycle_id` against the set of `cycle_id`s
that have at least one `order_queued` row — a `cycle_start` with none present
had genuinely nothing queued that cycle (this matches the console log's own
"no trade — …" lines exactly).

### Watchlist coverage per cycle

**All 25 cycles logged an explicit "Evaluating N candidates: …" line naming
every candidate by masked alias.** N ranged from 8 to 10 (10 cycles at N=10,
9 cycles at N=9, 5 cycles at N=8, real watchlist size is 10) — the shortfall
from 10 in every case matches exactly the number of tickers already held as
an open position that cycle (excluded per `AGENTS.md` Step 4's own
instruction to evaluate every name *not already held*), not an incomplete
scan. Confirmed by cross-referencing the named candidate list against the
same cycle's open-position count; not merely assumed from the count alone.

### Reflection/insight entries (all 5, chronological, verbatim)

| # | Type | Sample size | Timestamp (UTC) | Text |
|---|---|---:|---|---|
| 1 | `weekly_reflection` | 0 | 2026-08-31T16:15:41 | "No closed trades exist yet, so there is no outcome sample to learn from. The useful process check is whether entries are tied to measurable support/resistance and whether I keep rejecting setups when they do not clear the reward:risk floor. Confidence should stay ordinary unless the signals truly line up; otherwise the scale stops meaning anything." |
| 2 | `weekly_reflection` | 2 | 2026-08-31T16:35:09 | "With zero closed trades, there is still no outcome sample to learn from, so the useful check is whether entries are anchored to measurable support and invalidation is explicit. The pattern that cleared was bullish or at least not-bearish structure with enough room to the next resistance; mixed-bearish names with crowded overhead room stayed below the required reward:risk floor." |
| 3 | `weekly_reflection` | 6 | 2026-08-31T16:53:18 | "The useful check is still whether a setup is anchored to measurable support with explicit invalidation and enough room to the next resistance. This cycle, the long candidates that looked structurally acceptable did not offer enough reward relative to the nearest resistance, while the one name that did clear the ratio was still structurally bearish. With no closed-trade sample large enough to infer outcome patterns, I kept the focus on rule compliance rather than trying to force activity." |
| 4 | `weekly_reflection` | 7 | 2026-08-31T17:06:31 | "The useful process check remains whether the stop is anchored to measurable support and the target has enough room to the nearest resistance; that is a real filter, not a hindsight excuse. The recent sample is still too small to infer outcome patterns, so I stayed with rule compliance instead of trying to manufacture a trade from an ordinary setup." |
| 5 | `weekly_reflection` | 7 | 2026-08-31T17:20:18 | "The recent closes still point to the same process lesson: the named support level really is the right invalidation point, because the losses failed when price broke that level or opened through the stop. I should keep filtering for longs where the stop sits under measurable support with enough cushion for a normal gap and where the next clear resistance still leaves at least 1.5R. I do not have enough outcome data yet to claim more than that process check." |

**Every `sample_size` value above was independently re-verified against the
real closed-trade count at that exact timestamp** (counting how many of the 7
exits in the ledger above had already occurred before each reflection's
`created_at`) **and matches exactly in all 5 cases, with no discrepancy** —
the structured `sample_size` field itself is trustworthy, not merely
self-reported.

**Real evidence-reading defect, reflection #2: the structured `sample_size`
field (2) is correct, but Hermes's own free-text opens with "With zero
closed trades..." — directly contradicting the very number in the same row.**
Two real closed trades (PLNT, WMG) existed by 2026-08-31T16:35:09, the
timestamp of this exact reflection. This is not a data-adequacy gap in what
was persisted — both the true count and Hermes's stated claim are captured
verbatim above, which is exactly how this contradiction is provable — it is
a real reasoning failure to correctly read its own evidence at that moment.
Reflections 1, 3, 4, and 5 do not show this problem (reflection 1's "no
closed trades exist yet" was genuinely true at that exact moment, seconds
before WMG's own exit filled — see §3's ledger — not a contradiction).

Reflections 3 through 5 (sample_size 6 and 7) correctly follow
RULES.md/`AGENTS.md`'s reflection discipline (explicitly declining to draw a
strategy conclusion from a small sample, repeatedly, in their own words) —
a real, positive, directly-observed behavior, not an inference, and
unaffected by reflection #2's separate defect above.

---

## 6. Data-adequacy audit — coverage matrix

| # | Question | Classification |
|---|---|---|
| 1 | Reconstruct why Hermes entered each trade? | **Reliably captured** |
| 2 | See the exact indicators/numeric evidence Hermes saw? | **Partially captured** |
| 3 | Verify the news available at decision time? | **Partially captured** |
| 4 | Verify data freshness / point-in-time correctness? | **Partially captured** |
| 5 | Reconstruct proposed vs. actual execution? | **Captured but potentially misleading** |
| 6 | Reconstruct daily performance for every open position? | **Reliably captured** |
| 7 | Calculate MFE/MAE? | **Partially captured** |
| 8 | Measure distance/progress toward stop and target each day? | **Reliably captured** |
| 9 | Determine precisely why every position exited? | **Reliably captured** |
| 10 | Evaluate rejected opportunities after the fact, lookahead-safe? | **Not captured** |
| 11 | Compare confidence against outcomes? | **Partially captured** |
| 12 | Compare strategy profiles? | **Not captured** |
| 13 | Determine whether Hermes consistently evaluated the full watchlist? | **Reliably captured** |
| 14 | Measure decision/provider latency and cycle duration? | **Partially captured** |
| 15 | Distinguish model refusal, malformed output, provider failure, risk rejection? | **Partially captured** |
| 16 | Calculate token/API usage per cycle and per completed trade? | **Partially captured** |
| 17 | Compare performance against a benchmark? | **Not captured** |
| 18 | Detect survivorship bias from stored evidence? | **Partially captured** |
| 19 | Can the dashboard accurately reconstruct each trade's lifecycle? | **Captured but potentially misleading** |

### Detail for every partial/missing/misleading item

**2 — exact indicators/numeric evidence.** *Available:* Hermes's own prose
summary of indicators inside `strategy_notes` (e.g. "RSI 65.9, MACD bullish").
*Absent:* the actual per-cycle technical payload the harness computed and
sent to Hermes (RSI/MACD/support/resistance numbers, support/resistance
levels for every candidate) is never persisted anywhere — only Hermes's
paraphrase of it survives. *Blocks:* verifying Hermes read the real numbers
correctly (a check this data cannot answer at all) — does **not** block
trade-outcome analysis, since fill prices/P&L are independently real.
*Smallest addition:* persist the per-cycle prompt's candidate technical dict
(or a hash of it) alongside `data_timestamps`.

**3 — news available at decision time.** *Available:* `data_timestamps`
gives a real `news_as_of` cutoff and a real `latest_news_created_at` on every
BUY row. *Absent:* the actual headline text shown to Hermes that cycle —
only Hermes's own paraphrase survives (e.g. "adverse CEO-leave news"). *Blocks:*
verifying a specific headline's real content or severity — weakens confidence
in news-veto correctness, does not block P&L analysis. *Smallest addition:*
persist the actual news payload (or a hash) alongside `data_timestamps`.

**4 — data freshness/point-in-time correctness.** *Available:* the cutoff
metadata itself (`news_as_of`/`technical_as_of`) is present and internally
consistent on every row checked. *Absent:* the underlying raw data those
cutoffs describe (see #2/#3) — so freshness of the *label* is verifiable,
freshness of the *content* is not. *Blocks:* nothing about this run's already-
computed outcomes; weakens future point-in-time audits. *Smallest addition:*
same as #2/#3.

**5 — proposed vs. actual execution.** *Available:* `fill_price` is the real
executed price, `intended_notional`/`intended_quantity` exist. *Absent:*
there is no `entry_price` (or `proposed_price`) column on `trades` at all —
the "Proposed Price" in this report's ledger is **derived** by dividing
notional by quantity, which assumes (not verifies) that notional was computed
as `quantity × Hermes's own stated entry price`. *Why "potentially
misleading":* a reader could mistake that derived column for a directly-
stored value; it is a reconstruction with one unverified assumption baked in.
*Blocks:* nothing about `fill_price`-based P&L (that part is exact) — only
weakens confidence in the specific slippage-percentage figures in §4.
*Smallest addition:* persist Hermes's raw `entry_price` field directly as its
own column.

**7 — MFE/MAE.** *Available:* `portfolio_snapshots.open_positions_detail`
gives one `current_price`/`unrealized_pl` mark **per real trading day** a
position was held (verified directly for `OLN` across 6 days and `ATI` across
4 days — every intermediate day present, no gaps). This lets MFE/MAE be
computed at **daily (end-of-day) granularity**. *Absent:* true intraday
high/low — only one mark per day is captured, so the real intraday MFE/MAE
(e.g. a stop that was nearly hit intraday and recovered before the daily mark)
would understate the true adverse excursion. *Blocks:* a precise MFE/MAE
figure; does not block a daily-granularity approximation, which this data
does support. *Smallest addition:* persist the real intraday bar range
(already fetched by the harness for stop/target resolution) alongside the
daily snapshot.

**11 — confidence vs. outcomes.** *Available:* `confidence` is stored on
every BUY, cross-referenceable against closed-trade P&L. *Absent:* nothing —
this is mechanically complete. *Why only "partially captured":* the
**sample** is what's inadequate, not the field — 6 closed trades at
confidence 3, 1 at confidence 4, nothing else. A real comparison exists but
is statistically meaningless at n=1/n=6. *Blocks:* a trustworthy conclusion,
not the calculation itself. *Smallest addition:* more completed cycles —
not a data-model change.

**14 — decision/provider latency.** *Available:* `trades.created_at` vs.
`updated_at` gives a real wall-clock span per decision (e.g. 34 seconds for
Row 1) — but that span includes DB round-trips and risk-check evaluation, not
pure model inference time. *Absent:* no request-sent/response-received pair
around the actual Hermes API call is persisted anywhere. *Blocks:* isolating
true provider latency specifically; does not block cycle-level analysis (the
harness's own console/log timestamps give a coarser real proxy, not queried
here since it's not a Neon table). *Smallest addition:* record the real
`_call_hermes` request/response timestamps into `agent_activity` or
`portfolio_snapshots`.

**15 — distinguish refusal / malformed output / provider failure / risk
rejection.** *Available:* risk rejections are cleanly distinguished (§-numbered
`risk_check`/`order_rejected` rows). `api_calls > 1` on a snapshot (10 such
cycles this tranche) proves a JSON-parse-or-validation retry happened.
*Absent:* the specific reason the FIRST attempt failed (malformed JSON vs. a
semantic validation error vs. something else) is never journaled to Neon —
only the final, successful response's own numbers are kept. *Also:* 0
genuine provider failures (`cycle_failed`) occurred this tranche, so that
specific failure mode was never exercised or observable here at all. *Blocks:*
distinguishing retry causes after the fact; does not block outcome analysis.
*Smallest addition:* persist the first attempt's raw error string when a
retry occurs.

**16 — token/API usage per completed trade.** *Available:* exact, real
per-cycle usage (all 4 token categories + `api_calls`) on all 25 snapshots.
*Absent:* a cycle can propose/journal multiple trades (BUY+SELL, or several
BUYs) in one Hermes call — there is no way to split that cycle's token spend
across its individual trade rows. *Blocks:* a precise per-trade cost figure;
does not block per-cycle cost, which is exact. *Smallest addition:* none
practical — this is an inherent one-call-many-decisions structure, not a
missing field.

**18 — survivorship bias.** *Available:* within this run, the same 10-ticker
watchlist was named consistently in every "Evaluating N candidates" line —
no ticker was added or dropped mid-run (directly verified, not assumed).
*Absent:* whether the watchlist's original one-time screening (predates this
run, done by `watchlist/screen_watchlist.py` per `PROJECT_FRESH_START.md`)
was itself survivorship-biased is a fact about that screening step, entirely
outside this run's own Neon evidence — this report's scope (this run_id's
persisted rows) cannot speak to it either way. *Blocks:* nothing about this
run's own internal consistency; leaves the upstream screening question
genuinely open. *Smallest addition:* out of scope for this run's schema —
would require auditing the screener's own historical output separately.

**19 — dashboard trade-lifecycle reconstruction.** *Available:* the
dashboard's sequential per-ticker BUY→SELL pairing (`pairTrades()`) produces
the exact same 7 closed / 1 open result this report independently derived —
correct for this run's real data (verified above, §3). *Why "potentially
misleading":* that pairing is a **heuristic assumption enforced only by
application logic** (RULES.md §9.1's one-open-lot-per-ticker rule), not a
database-level link — there is no `closes_decision_id` column tying a SELL
row to the specific BUY it closed. Nothing in this run's data violates that
assumption, but nothing in the schema itself prevents a future violation
(e.g. hypothetical overlapping same-ticker lots) from silently mispairing.
*Blocks:* nothing for this run; is a forward-looking reliability caveat, not
a defect found here. *Smallest addition:* an explicit `closes_decision_id`
foreign key on SELL rows.

---

## 7. Required reconciliations

| Check | Result |
|---|---|
| Trade-status counts equal complete trade-row count | ✅ 20 rejected + 8 filled + 7 filled(SELL) = 35 = `count(trades)` |
| Closed + open positions reconcile with entry/exit rows | ✅ 7 closed (7 BUY + 7 SELL matched) + 1 open (1 unmatched BUY) = 8 filled BUYs, 7 filled SELLs — exact |
| Final open positions reconcile with final cash/exposure/equity | ✅ 1 open `DINO` position, snapshot `open_positions=1`, `exposure_pct=19.67%` = (324 × $59.26) / $97,590.04 × 100 = 19.66% (rounds to the stored 19.67% within a cent of position value) |
| Realized P&L recomputed from closed positions matches account figure | ⚠️ **Documented discrepancy, not silently corrected** — recomputing from `trades.fill_price` gives −$2,350.33 vs. the harness-authoritative −$2,351.65 (backtest_state.json `closed_pnl` sum); traced to one trade (`SFM`) with a $1.31 fill-price rounding inconsistency — see §2 |
| Unrealized P&L recomputed from final snapshot matches account figure | ✅ Exact — both are the same stored `open_positions_detail[0].unrealized_pl` value (−$58.32); no independent recomputation path exists (would require an external market-data fetch, out of scope per lookahead hygiene) |
| Snapshot count reconciles with successful cycles | ✅ 25 = 25 |
| Failure counts reconcile between activity rows and local state | ✅ 0 `cycle_failed` activity rows; no `cycle_checkpoint.json` on disk (nothing was ever mid-failure); harness console reported `EXIT=0` |
| All SQL queries scoped to the exact `run_id` | ✅ every query in the appendix below carries `WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'` explicitly (or filters an already-scoped result set derived from one) |

---

## 8. SQL and reproducibility appendix

All queries run read-only via the `trade-journal` Docker container's real
(non-anonymous) database credentials, scoped to
`run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'`. No secrets are reproduced
below — `NEON_DATABASE_URL` was read from the container's own environment,
never printed or logged.

```sql
-- Cross-check: latest snapshot's run_id vs. backtest_state.json
SELECT run_id, day_label, created_at
FROM portfolio_snapshots
WHERE mode = 'backtest'
ORDER BY created_at DESC LIMIT 1;
-- returned: e68525f9-ce89-4588-a57e-866f28cd99fd | Day 35 | 2026-08-31 17:20:22+00
-- (1 row)

-- Full trade ledger for this run
SELECT * FROM trades WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'
ORDER BY created_at;
-- 35 rows

-- Full snapshot history for this run
SELECT * FROM portfolio_snapshots WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'
ORDER BY created_at;
-- 25 rows

-- Full activity narration for this run
SELECT * FROM agent_activity WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'
ORDER BY created_at;
-- 162 rows

-- Full reflection/insight log for this run
SELECT * FROM insight_log WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'
ORDER BY created_at;
-- 5 rows

-- Row counts (verification)
SELECT
  (SELECT count(*) FROM trades WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd') AS trades,
  (SELECT count(*) FROM portfolio_snapshots WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd') AS snapshots,
  (SELECT count(*) FROM agent_activity WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd') AS activity,
  (SELECT count(*) FROM insight_log WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd') AS insights;
-- {"trades": 35, "snapshots": 25, "activity": 162, "insights": 5}

-- Rejection breakdown by RULES.md section (regex over description text)
SELECT unnest(regexp_matches(description, '§[0-9]+[a-z]?', 'g')) AS section, count(*)
FROM agent_activity
WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'
  AND event_type IN ('risk_check', 'order_rejected')
GROUP BY section ORDER BY count(*) DESC;

-- Duplicate-ticker-in-one-cycle check (none found)
SELECT date_trunc('minute', created_at) AS ts_minute, ticker, count(*)
FROM trades
WHERE run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd' AND action = 'BUY'
GROUP BY date_trunc('minute', created_at), ticker HAVING count(*) > 1;
-- 0 rows

-- Prior runs' row counts, unchanged (contamination check)
SELECT count(*) FROM trades WHERE run_id::text LIKE '97d112e8%';  -- 5
SELECT count(*) FROM trades WHERE run_id::text LIKE 'c55f0fc2%';  -- 0
SELECT count(*) FROM trades WHERE run_id::text LIKE '1bf9af3d%';  -- 3
SELECT count(*) FROM trades WHERE run_id::text LIKE '81db75e5%';  -- 8
```

### Derived formulas used in this report

- **Total return ($)** = ending equity − starting equity
- **Total return (%)** = (ending equity / starting equity − 1) × 100
- **Max drawdown (%)** = min over the equity curve of `(equity[i] / running_peak[i] − 1) × 100`, where `running_peak[i] = max(equity[0..i])` — a derived running-maximum, since the stored per-snapshot `drawdown_pct` is only ever the *current* drawdown at that moment
- **Gross profit / gross loss** = sum of positive / negative values in `backtest_state.json`'s `closed_pnl` array
- **Profit factor** = gross profit ÷ |gross loss|
- **Expectancy per closed trade** = mean(`closed_pnl`)
- **R-multiple** = realized P&L ÷ (planned entry-to-stop risk per share × filled quantity), using the entry's own stored `stop_price`
- **"Proposed price" (derived, not stored)** = `intended_notional` ÷ `intended_quantity` — see §5 item 5 for the assumption this relies on
- **Stop/target distance (%)** = `|entry − stop_or_target| / entry × 100`, using `fill_price` where filled, else the derived proposed price
- **Holding period (trading days)** = (exit's simulated day number) − (entry's simulated day number), where the day number comes from joining `trades.decision_id` → `agent_activity.cycle_id` (any activity row sharing that `decision_id`) → `portfolio_snapshots.day_label` for that `cycle_id`
- **Entry/exit pairing** = strict sequential per-ticker BUY→SELL matching in `created_at` order (identical logic to `dashboard/index.html`'s `pairTrades()`) — see §6 item 19 for why this is an enforced-by-convention, not schema-enforced, assumption

### Assumptions required to pair/classify rows

1. Sequential per-ticker BUY→SELL pairing correctly identifies each closed
   position (verified true for this run — no overlapping same-ticker lots
   were found — but not a schema-enforced guarantee; see §6 item 19).
2. "Proposed price" = `intended_notional / intended_quantity` correctly
   reconstructs Hermes's stated entry price (plausible, consistent with every
   reasoning row's own stated entry figure where checked, but not itself a
   stored/verifiable field; see §6 item 5).
3. A `decision_id`'s simulated day is taken from whichever `agent_activity`
   row shares both that `decision_id` and a `cycle_id` — resolved for all 35
   trade rows in this run with zero ambiguity (each `decision_id` maps to
   exactly one `cycle_id` in the data checked).

---

## Verification checklist

- [x] No credentials, connection strings, or API keys appear anywhere in this
      report (`NEON_DATABASE_URL` was read from the container's environment
      only, never echoed).
- [x] No real company names are stated or looked up anywhere — only the same
      ticker/alias identifiers already stored in Neon and already used
      elsewhere in this project's own `HANDOFF.md`/dashboard.
- [x] Every SQL query above is scoped to the single identified
      `run_id = 'e68525f9-ce89-4588-a57e-866f28cd99fd'` (or filters a result
      set already scoped to it).
- [x] All required counts and financial totals reconciled in §7; the one
      real discrepancy found ($1.31–$1.32, one trade's fill-price rounding)
      is documented with its root cause, not silently corrected.
- [x] No historical price, news, or fundamentals data was fetched during this
      audit — every "not captured" item in §6 is reported as genuinely
      absent from the persisted evidence, not filled in after the fact.
- [x] No application code, schema, `RULES.md`/`SOUL.md`/`AGENTS.md`,
      dashboard, `HANDOFF.md`, or `PROJECT_BACKLOG.md` was modified.
- [x] No rule-number change is recommended anywhere in this document.
