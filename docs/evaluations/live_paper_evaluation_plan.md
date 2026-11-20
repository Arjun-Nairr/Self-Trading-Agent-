# First supervised paper evaluation: plan (fixed 2026-11-06, before any paper order)

This plan is fixed now so that results cannot reshape it afterwards. Writing it authorizes nothing: every paper
session needs its own explicit user authorization. `loop` (unattended) stays unauthorized. No artificial trades are
placed to exercise a branch. A path that the market does not trigger stays "unexercised". **"Insufficient
evidence" is an acceptable result.** A session verifies functionality; a profitable month would not show a
repeatable edge.

## 0. Fixed definitions

- **Universe:** the frozen ten-name watchlist (`--universe watchlist`, `universe_version live-watchlist-2026-08-17`:
  ALGM, ATI, BRKR, BRX, DINO, OLN, PLNT, RYAN, SFM, WMG). No usable weekly screen exists, and this is the path
  verified end to end (acceptance report §5). Moving to `--universe screened` is a separate later decision. It needs
  a weekend `trace_screen.py --write` and a `dry-run --hermes` on the 20-name path first.
- **Rules:** `RULES.md`, `live/LIVE_POLICY.md` and `risk_checker.py`, unchanged. No strategy, risk or prompt
  change during collection. A change would end the series and start a new one.
- **Evaluation start (t0):** the pilot session's date. The starting equity is the paper account's equity
  immediately before the first `cycle`. All benchmarks start from that equity.
- **Calendar:** Alpaca's trading calendar, not weekday guesses.

## 1. Operational acceptance

| path | status today | evidence source in a session |
|---|---|---|
| broker reads, calendar, quotes, news, bars; Laya selection; Hermes research; reply validation; read-only guard; tracing and exit spool | **verified for real** (dry runs, hosted trace) | — |
| IOC limit entry, partial fill, OCO (stop + target) acceptance sized to the filled shares | mock-tested only | `events.jsonl`, Alpaca orders, `broker.submit` spans, `engine.order_filled` |
| protection check ("protected" only when the broker reports the intended OCO) | mock-tested only | reconcile output `protected/unprotected`, broker orders |
| exit by stop / target / Hermes close, cancel, stop replace (cancel → confirm → resubmit) | mock-tested only | engine events, journal `exit` rows |
| journal rows (`trades`, `execution_mode='alpaca'`), idempotent client ids `lv-<plan>-…` | mock-tested only | Neon rows vs. broker orders |
| rule enforcement (market hours, §9 prohibitions, sizing, `check_trade`) | mock-tested; market-hours refusal seen in dry runs | `entry_validation` spans, rejected plans |
| recovery after restart with open orders | mock-tested only | only if it actually happens; never induced |

Pass for a session means: every action the session took is traced, journaled and reconciled; every open position
is protected at the session's end; and no unmanaged position or orphan order exists. **Any protection or
reconciliation failure stops collection** until it is diagnosed.

## 2. Agent efficiency (from the traces, per review)

For each review, count model calls and provider-reported usage exactly as returned. Count tool calls by name.
Count **duplicates**: the same tool with the same arguments in the same review. A repeat with a different ticker,
window or `as_of` is not a duplicate. Count failed calls: an error status, or a "success" carrying unusable data
such as `NaN` prices. Measure latency: `hermes_research`, process start-up, the model, and the tool phase (tool
calls currently run one after another). Mark each answer claim as supported, partly supported or not established
from the tool results, as in `docs/research/langfuse_agent_behaviour_2026-11-06.md`. Baseline from that report:
3 model calls, 11 tool calls, 0 duplicates, 1 unused lookup, 1 tool with `NaN` data, 16.6 s tool phase.

## 3. Trading outcomes against fixed benchmarks

Metrics for each curve: total return; maximum drawdown measured from t0 equity; average gross exposure; **turnover**
(traded notional ÷ average equity); trade count and win/loss counts. Annualized volatility is shown, but is not
meaningful for windows shorter than about 3 months.

| curve | rule | execution assumptions |
|---|---|---|
| **Hermes (paper)** | the real paper account | Alpaca paper fills (simulated by Alpaca, likely optimistic versus real markets); commission $0 |
| **Cash** | t0 equity held flat | none |
| **Same-universe buy-and-hold** | t0 equity split equally across the ten names, bought once, never rebalanced | entry at the first session's 09:45 ET price, with the backtest's buy-slippage model; marked at each daily close; commission $0 |
| **Technical rule** | 10-day SMA cross-up on the daily close → enter at the next session's 09:45 ET, with the same sizing, stop and target rules as the backtest baseline | the same slippage model; exits walked on real bars; commission $0 |

Reuse: `backtest-harness/benchmark_sensitivity.py` already implements `cash_curve`, `buy_hold_curve`,
`technical_baseline_curve` and `metrics` with these rules. **The small follow-up needed** before the first weekly
review:
1. Make its start date, end date and starting equity parameters (today they are fixed to the backtest run).
2. Read Hermes' equity from the paper account's daily portfolio history, not from Neon backtest snapshots.
3. Add turnover from the `execution_mode='alpaca'` journal rows.

This is read-only and keeps the existing rules. It is not a new framework. Costs, borrow and taxes are not modelled
beyond the slippage stated above.

## 4. Sequence

1. **Pilot: one supervised session** (needs the authorization in §5). The operator runs `cycle` by hand during
   regular hours while watching the Alpaca paper dashboard. If an entry fills, the position stays protected at the
   broker by its OCO, but managing it on later days needs further supervised cycles, each authorized. Opening a
   position therefore commits the operator to follow-up sessions, or to closing it by hand.
2. **Pilot review** (the same or next day): fill in §1 for every path the session actually exercised, plus §2 for
   each review. No outcome metrics from one session.
3. **Continued collection, only if the pilot passes §1:** further supervised sessions (or a separate, later decision
   to authorize `loop`). A **weekly review** each Friday after the close covers §1 incidents, §2 counts, and §3 from
   t0 to date against all three benchmarks. Before about 30 closed trades and at least 3 months of sessions, the
   honest summary is "insufficient evidence for an edge", whatever the return.

## 5. Pilot: exact command, prerequisites and authorization

```bash
python live/run_live.py cycle --universe watchlist
```

Prerequisites:
- Docker is running and all containers are up.
- The `live` profile is configured (`setup-profile`, done 2026-11-06 with the spool-enabled plugin).
- The Laya checkpoint is present.
- A regular session day, run between 10:00 and 15:30 ET (the controller enforces market hours itself).
- Before the run, record the paper account's equity, positions (expected 0) and open orders (expected 0).
- Afterwards, the run's last trace records stay in the local spool until the next invocation. A later read-only
  `python live/run_live.py dry-run --universe watchlist` sends the controller's part; the Hermes part is sent on the
  next Hermes call.

**Authorization needed from the user:** "authorize one supervised paper `cycle` on <date>". It may place real paper
orders if Hermes plans an entry that passes validation. It does not authorize `loop`, further cycles or any
change to rules or prompts.
