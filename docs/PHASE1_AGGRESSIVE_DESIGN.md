# Phase 1 aggressive paper-trading profile — implemented

**Status: implemented.** `RULES.md`, `risk-checker/risk_checker.py`,
`AGENTS.md`, and `backtest-harness/run_backtest.py` all now encode this
profile together, synchronized in one change (per `risk_checker.py`'s own
drift-risk warning — a number changed in one and not the other is silently
unenforced). `SOUL.md`'s Phase 2 temperament and this numeric profile are
now in sync — see `HANDOFF.md` for exact verification results and the
real next step (Round 2 itself still requires separate, explicit human
approval to start — implementing the profile is not that approval).

This is a **permanent paper-money sandbox**, not a path toward real capital.
The point of the aggressive profile is to generate more real trading
evidence per unit of time than Round 1's near-total inactivity did — not to
simulate what a live account should risk.

## The profile

| Parameter | Value | Replaces (Round 1 / RULES.md before) |
|---|---|---|
| Minimum reward:risk | **1.5:1 base, 1.25:1 escalated** (see below) | 2.0:1 (§6b) |
| Risk budget per trade | **2% of equity** | 1.0% (§2a) |
| Max single position | **20% of equity** | 10% (§2b) |
| Exposure limit | **available cash only; no margin, cash cannot go negative** | 60% of equity, 5% min cash (§3) |
| Position-count / daily-entry limit | **none** | 8 max positions (§3) |
| Same-ticker concurrency | **one open position per ticker** | (unchanged — already the rule) |
| Stop distance | **3–12% from entry** | 5–10% (§6a) |
| Time stop | **5 trading days maximum hold** | 15 trading days (§6c) |
| Circuit breaker | **only a catastrophic −30% drawdown pause** | −3% daily / −6% weekly / −10% drawdown (§4) |
| Losing-streak de-risking | **removed entirely** (former §4a) | cut position size 25% after 3 losses |
| Market-hours entry buffers | **removed entirely** — any minute the market is open permits an entry | no entries in the first 15min / last 30min (§7) |
| Activity target | **≥5 completed trades per 20-trading-day block** | none |
| Escalation | **automatic, deterministic, harness-controlled** — see below | n/a |
| Six-month target | **30–60 completed trades** | n/a (Round 1 produced zero fills) |

## The escalation mechanism, exactly as implemented

Approved by the user in advance — this is **not** Hermes loosening its own
rules (RULES.md §9's one deliberate exception to "never loosen a limit
through your own reasoning").

- A fresh or `--reset` run always starts at the base tier
  (`aggressive_v1_base`, min R:R 1.5).
- The run is divided into fixed blocks of **20 real trading days**
  (`ACTIVITY_BLOCK_TRADING_DAYS`), counted from the run's own start —
  weekends and holidays never count.
- A trade counts toward a block's activity only once **fully completed**
  — a filled entry later closed by a filled exit (mechanical stop/target/
  time-stop, or a discretionary SELL fill). A rejected or merely-queued
  proposal never counts.
- Evaluated exactly on a block's own 20th real trading day, after that
  day's exits are recorded (so a completion on the block's last day still
  counts). If the block completed **fewer than 5** trades, the
  *following* block's minimum reward:risk becomes 1.25:1
  (`aggressive_v1_escalated`) instead of 1.5:1.
- Escalation never reverses and never re-evaluates once triggered — the
  run stays at 1.25:1 for the rest of its life. Never changes mid-block.
- **The exact threshold and profile label used to decide, queue, and
  execute a given order are the same number, always** — a queued order
  stores the tier that was binding when it was decided
  (`QueuedOrder.strategy_profile`/`min_reward_risk_used`) and
  `resolve_queued_order` uses THAT stored value at execution, never
  whatever tier happens to be "current" on the day it resolves.
- **Precisely when the following block's profile becomes active:** the
  block boundary is evaluated inside `run_day`, once per real trading day,
  AFTER that day's exits/fills are resolved but BEFORE that same evening's
  new decisions are considered. So on the exact day a block closes out
  below target, `working.profile_level` flips immediately, and every
  decision Hermes makes *later that same cycle* — the prompt it's shown,
  the decision-time risk check, the resulting `QueuedOrder`, and the
  journaled fields — already reflects the new tier. A `QueuedOrder`
  already pending FROM AN EARLIER DAY is unaffected by this same-day flip:
  it resolves earlier in that day's `run_day` call, against its own
  already-stored threshold, before the boundary check even runs.
- **Hermes never sees a trade count, a block-progress figure, or anything
  that reads as a quota.** Its prompt shows exactly one line: the
  currently-binding minimum reward:risk and the profile label
  (`aggressive_v1_base`/`aggressive_v1_escalated`) — nothing else. The
  entire mechanism lives in `backtest-harness/run_backtest.py`
  (`_maybe_advance_activity_block`, `_record_trade_completion`,
  `_limits_for_profile`); `risk_checker.py` itself has no concept of
  blocks or tiers, only whichever single `min_reward_risk` value it's
  handed for one check.
- State (`profile_level`, `block_start_trading_day`,
  `block_completed_trades`) is persisted in `backtest_state.json` and
  restored exactly on resume; a checkpoint replay cannot double-count a
  completion or escalate twice (see `run_day`'s essential/best-effort
  commit ordering, unchanged by this profile).
- **Accepted (queued) decisions** get a real `trades` row with structured
  `strategy_profile`/`min_reward_risk_used` fields — see the `trades`
  table's additive migration.
- **Decision-time rejections are NOT trade rows at all** — a proposal that
  fails the decision-time risk check is never journaled to `trades` (only
  decisions that pass the decision-time check get a row). A queued decision
  is inserted immediately as `pending`; the next-day execution check updates
  that same row to `filled` or `rejected`. A decision-time rejection is
  recorded only as an `agent_activity` narration line, with the profile/
  threshold appended in a fixed, consistently parseable form —
  `[profile=aggressive_v1_base min_rr=1.5]` at the end of the line — so
  later analysis can extract it without parsing Hermes' free-text
  reasoning.

## What stays mandatory, unconditionally

Every one of the following stayed exactly as it was before this profile —
it changed numeric thresholds and removed specific limit checks, not the
safety architecture around them:

- **Lookahead prevention and point-in-time masking** — `as_of`-gated data
  fetches, reshuffled ticker/company aliases, next-day queued execution.
- **Decision-time vs. execution-time validation** — a proposal passing
  decision-time can still be correctly rejected at real execution.
- **Execution mechanics** — real 15-minute intraday bars, the exact
  5bps `execution-sim-mcp` slippage formula, stop-before-target same-bar
  tie-breaking, the opening-gap rule.
- **Missing-data fail-closed behavior** — a genuine data gap on a real
  trading day halts the cycle; it is never silently skipped or fabricated.
- **Queue-time journaling and idempotency** — every decision journaled the
  instant it's queued, `decision_id` collision detection (now also
  comparing `strategy_profile`/`min_reward_risk_used`), no fabricated
  fills.
- **Anti-fabrication discipline generally** — no invented price, level,
  stop, or outcome, anywhere in the stack, under any profile.
- **Sentiment-as-veto-only (§5a)** — the prompt-injection defense described
  there did not change with this profile.
- **`RULES.md`'s hard prohibitions (§9)** — no averaging down, no widened
  stops, no off-watchlist trading, no same-day re-entry after a stop-out,
  one open position per ticker.

## Verification

See `HANDOFF.md`'s dated section for the exact test results, migration
confirmation, and `docker compose config -q` output. Summary: risk-checker,
harness (execution semantics, quota), and trade-journal (decision fields,
idempotency — including new profile-field collision cases, mode isolation,
run isolation, dashboard fields) suites all pass against the new numbers;
the additive Neon migration (`trades.strategy_profile`,
`trades.min_reward_risk_used`) applied cleanly, Round 1's own row remains
readable with both fields NULL; `backtest_state.json`/
`quota_pacer_state.json` untouched; no run started, resumed, or reset.
