# Handoff briefing — Hermes trading agent

**Read this if you are an AI assistant being brought in to discuss or advise
on this project.** It exists because the working split from here on is:
**you (this chat) handle discussion, strategy, and second opinions; Claude
Code handles execution** — reading/editing the actual code, running the
harness, touching Neon, committing. Don't propose direct code edits as if
you'll apply them yourself; propose the *decision*, and it gets relayed back
to Claude Code to implement and verify.

---

## What this project is

Hermes: an LLM-driven agent that trades US equities (long-only, paper
account) using a swing-trading horizon. It reasons over technicals and
news, is gated by a hard-coded risk checker before anything executes, and
runs in two modes: **backtest** (point-in-time historical simulation,
identity-masked so the model can't just recall what really happened) and
**live** (real Alpaca paper-trading API). Currently in the backtest/tuning
phase — no live trading yet.

Full plain-language architecture + every rule's reasoning:
**[The Hermes Ledger](https://claude.ai/code/artifact/57a0d9c6-8a25-4fa7-933c-de5327c6f9cb)**.
This file is the condensed, decision-focused version.

---

## Architecture, condensed

- One LLM agent (`gateway`, currently **GPT-5.4-mini via ChatGPT Codex,
  medium reasoning** — *never deliberately chosen, just the tool's
  shipped default, still an open question*) plus five isolated tool
  containers (technicals, news, live execution, backtest fill sim, trade
  journal), hub-and-spoke — no tool can reach another tool.
- **Backtest mode:** Hermes never calls a tool directly. A Python harness
  (`run_backtest.py`) fetches everything, masks tickers/dates, and hands
  Hermes one prompt per simulated day. One "cycle" = one simulated trading
  day, not one trade — most cycles produce zero trades by design.
- **The rules layer** (`RULES.md`) is enforced in code
  (`risk_checker.py`), independent of the model's own reasoning — a
  pure-function module shared by both live and backtest paths, fail-closed
  on any unknown value.
- **Neon Postgres** is the single source of truth (one DB, `mode` column
  distinguishes backtest/live). A public dashboard reads it read-only via
  Neon's Data API; Hermes writes through a separate full-credential path.

---

## Current state (as of 2026-08-21)

- Backtest cursor: Day 8 of the tuning window (2026-02-17 → 2026-08-14,
  ~123 real trading-day cycles total). 7 cycles run so far under the
  current (fixed) code, all no-trade.
- Weekly quota: 34/175 used. Budget raised today from an untested 32 to
  175 after confirming real ChatGPT Plus headroom — still a guess, just a
  less blind one. Real measured cost: **~21.5k tokens/cycle**, included in
  the subscription (not metered per-token).
- Reflection loop fired for real for the first time on Day 7 — logged a
  coherent, process-focused insight, correctly avoided naming a masked
  alias.
- Four real pre-training bugs found and fixed today: a masking leak (7/10
  watchlist company names were leaking through real headline text
  unmasked), a SELL-decision execution bug (never yet triggered live, but
  would have corrupted the portfolio the first time it did), unrealistic
  MARKET fills (could execute at a price that never traded that day), and
  a mis-derived time stop (10 calendar days ≈ 7 trading days, not 10).
  Neon was wiped and the run restarted clean so nothing currently in the
  database predates these fixes.

---

## Phase 0 backtest-integrity repair (2026-08-21, second pass)

A narrowly-scoped follow-up repair, separate from the four bugs fixed
earlier the same day (masking leak, SELL execution, MARKET fills, time
stop — see "Current state" above). This pass targeted three specific
structural gaps in the backtest harness itself, found by direct
inspection, not by a failure in the field. **No real backtest cycle was
run or advanced to do this work** — cursor is still Day 8, quota still
34/175, exactly as before.

### What was repaired

**1. Same-day lookahead removed.** Every discretionary BUY/SELL decision
now fills against the *next* real trading day's open, never the same day
its data came from.

**2. Cycle failure handling made fail-safe.** A failed cycle now halts the
run instead of silently skipping to the next day; portfolio mutations are
never published until a cycle fully succeeds; state-file writes are
atomic; retrying a failed day can no longer duplicate a journaled trade.

**3. Watchlist enforcement made fail-closed.** An empty/unavailable
watchlist now blocks every BUY instead of silently allowing everything
through.

### New day-T / day-T+1 execution semantics

Previously: Hermes reasoned on day T's completed data and got filled
*within that same day's range* — meaning the decision and the fill both
drew on information that only existed once day T was already over. That's
lookahead by construction, even though no individual line of code looked
wrong in isolation.

Now: any BUY/SELL Hermes decides on day T is **queued** (`QueuedOrder`),
never executed that day. On the *next* cycle (day T+1), before anything
else happens, each queued order is resolved against day T+1's **real
opening price** — never against `entry_price`, which Hermes proposed
before that price existed and is kept only for audit logging. The **same
risk checker** re-validates the order at that real price (stop distance,
reward:risk, exposure, cash, sizing) and **rejects outright** on any
breach — it never silently resizes a gapped order to make it fit. If a
queued BUY fills and the *rest* of that same day's range also touches the
fresh stop or target, that's resolved immediately (same-day), with the
stop taking priority over the target whenever both are technically
possible from the day's OHLC (conservative, matches the existing
mechanical-exit priority ordering elsewhere in the file). A queued SELL
whose position already closed via a mechanical exit earlier the same
cycle is treated as superseded, not as an error.

Mechanically: `resolve_queued_order()` is a **pure function** (no I/O,
explicit arguments) mirroring `risk_checker.py`'s own design philosophy —
built that way specifically so it's exhaustively unit-testable without
Docker or Neon.

### Fail-safe cycle handling, in detail

- `run_day()` now operates on a **deep-copied working portfolio** and
  only the caller adopts it after the cycle fully succeeds — a failure
  partway through never leaves a partially-mutated portfolio visible
  anywhere, in memory or on disk.
- A failed cycle now **halts the run** (`_run_cycle_or_halt`) rather than
  silently advancing to the next day — the old `except: cursor += 1;
  continue` behavior is gone.
- `backtest_state.json` writes are now **atomic**
  (temp-file-then-rename), mirroring `quota_pacer.py`'s existing pattern.
- Every journal write now carries a **deterministic `decision_id`**
  (`make_decision_id`, a `uuid5` derived from run/day/ticker/action/kind).
  `trade-journal-mcp`'s `_record_decision` was extended with `ON CONFLICT
  (decision_id) DO NOTHING` + a fallback read — retrying a failed day
  regenerates the *same* id for the *same* real event, so a retry is a
  safe no-op, never a duplicate row.
- The weekly quota is now counted the instant Hermes is **actually
  invoked** (inside `_call_hermes`, right after a successful subprocess
  return), not after the whole cycle succeeds — a downstream parsing or
  journaling failure no longer under-counts real usage against the
  budget.

### Tests run and results

**Pure (no Docker/Neon), all passing:**
- `risk-checker/test_risk_checker.py` — 2 new cases added (empty
  watchlist blocks a BUY; does not block an exit of an already-held
  position)
- `backtest-harness/test_anonymize.py` — unchanged, still green
- `backtest-harness/test_quota_pacer.py` — unchanged, still green
- `backtest-harness/test_execution_semantics.py` — **new file**, 8 tests:
  queued order fills at the real open and ignores the stale
  `entry_price`; a real gap that breaks §2b sizing is rejected outright,
  not resized; same-day stop/target resolution (stop-only, target-only,
  ambiguous-resolves-to-stop, neither); `make_decision_id` is
  deterministic and collision-free across every real dimension of
  difference; a simulated crash mid-write never corrupts the previous
  valid state file and leaves no stray temp file; the quota-invocation
  callback fires exactly once per cycle even across a JSON-retry, and
  survives a simulated downstream failure; the working-copy pattern
  isolates a failed cycle's mutations from the caller's portfolio;
  `_run_cycle_or_halt` halts correctly on failure and returns cleanly on
  success

**Integration (real containers — `maverick-mcp`, `trade-journal`,
`execution-sim` only; `gateway`/`alpaca-mcp` never started, no Hermes call
made, no live order touched):**
- `backtest-harness/test_execution_path.py` — updated for the new
  required `decision_id` param, **plus a new case**: calling
  `record_trade` twice with the identical deterministic `decision_id`
  journals exactly one row, proving the DB-level idempotency guarantee
  end to end (not just that the id-generation scheme is deterministic —
  the pure test above proves that half separately)
- `trade-journal-mcp/test_idempotency.py`, `test_decision_fields.py` —
  re-run unchanged against the modified `_record_decision`, both still
  green (the new `decision_id` param is optional and backward compatible)

One real deployment gap found and fixed along the way: **both
`execution-sim` and `trade-journal` bake their code into the Docker image
(`build:`, not a live mount)**, so editing `server.py` on the host does
nothing until the image is rebuilt (`docker compose up -d --build
<service>`). Both were rebuilt and re-verified against the live
container, not just re-read as source. `alpaca-mcp`'s `risk_checker.py`
is a live bind-mount, so its watchlist fix needed no rebuild.

All containers touched during testing were stopped again afterward,
returning the environment to exactly how it was found (nothing was
running at the start of this task either — see below).

### Remaining limitations of this repair

- Per-order rejection/supersession messages are informative but not yet
  surfaced anywhere structured for a dashboard view — they currently only
  reach `agent_activity`'s free-text narration.
- `agent_activity` and `portfolio_snapshots` rows are **not**
  idempotency-protected the way `trades` now is — a retried day could in
  principle duplicate narration/snapshot rows (cosmetic, not financial;
  deliberately out of scope — "do not build a generalized job system").
- A queued order whose scheduled ticker has genuinely no bar data on the
  fill day (a halt, not a holiday — holidays skip the whole cycle before
  reaching the queue) is **dropped**, not indefinitely retried. This
  matches "smallest stable identity, not a generalized job system" but is
  worth knowing if halts turn out to be common in the real watchlist.
- Postgres writes within one cycle are **not** wrapped in a single atomic
  transaction — each write commits independently. The working-copy
  pattern protects local/state-file consistency; the deterministic
  `decision_id` scheme protects Neon consistency on retry. Together
  these make a retry *safe*, not *atomic* — an honest distinction, not an
  oversight.
- **Docker containers were left stopped** at the end of this task
  (matching how they were found — see "Environment note" below). Nothing
  starts automatically; the stack needs `docker compose up -d` before the
  next real cycle.

### Environment note (found, not caused by this task)

All six containers were already stopped when this task began (exited
~20 hours prior, exit code 137 — most likely a host sleep/Docker Desktop
restart, not anything in the code). `maverick-mcp`, `trade-journal`, and
`execution-sim` were started **only** for the testing above and stopped
again afterward. `gateway` and `alpaca-mcp` were never started during this
task.

### Still-deferred audit findings (from the earlier full-project review)

Explicitly out of scope for this repair, still open:

- **Live bracket orders.** `alpaca-mcp`'s `place_order` submits one order
  with no OCO/bracket — nothing rests a real stop at the broker in live
  mode. If a live cron process died, an open position would sit
  unprotected.
- **Partial fills.** Neither the live nor backtest path currently models
  a partial fill (fewer shares than requested) — every fill is
  all-or-nothing.
- **Circuit-breaker latching.** RULES.md §4's daily/weekly breakers are
  re-evaluated fresh every cycle from the broker's live P&L figures, not
  latched — a breaker that trips and then the P&L figure recovers
  mid-day could theoretically un-trip without an explicit "halted for the
  rest of the day" state being held anywhere.
- **Run isolation.** Aliases are reseeded per run (`--seed`), but insights
  in `insight_log` persist across runs in Neon — a name-specific insight
  (which the reflection prompt explicitly instructs Hermes never to
  write) would still silently point at a different real company after a
  reseed, if that instruction were ever violated.
- **Open-position thesis inputs.** Technicals/news are only fetched for
  *candidate* (unheld) tickers each cycle, not for currently-held
  positions — meaning §6d's "has the signal that justified the entry
  reversed" thesis-invalidation check has no fresh data to actually
  reason from on any given cycle.
- **Rules inconsistencies.** RULES.md's own §10 still lists several
  numbers as explicitly unverified/worth-a-second-opinion (see "Open
  questions" above) — none of those were touched by this repair, per
  instruction ("do not change any risk number").

---

## Intraday chronology follow-up (2026-08-25)

Completes the no-lookahead repair. The day-T+1 execution from the earlier
Phase 0 pass still had three real problems: a queued BUY filled at the
**market open**, violating RULES.md §7's 15-minute entry floor outright; the
BUY's risk check marked existing positions at the **prior day's close**
(a full day of lookahead into the check itself, not just the fill); and
the whole day's stop/target movement was processed **before** a queued
SELL, so an afternoon stop could retroactively supersede a morning exit
that should already have happened. **No real backtest cycle was run or
advanced to do this work** — cursor is still Day 8, quota still 34/175.

### Data source verification (done before writing any code)

Real 15-minute regular-session bars from **Alpaca's market-data API**
(`https://data.alpaca.markets/v2/stocks/bars`) — the account's existing
credentials, no new provider, no new dependency (`zoneinfo` is stdlib,
used to convert UTC bar timestamps to real ET wall-clock time, correctly
handling the March DST transition inside the backtest window).

Checked directly against the live API for **every one of the 10 watchlist
tickers, across the full backtest window** (2026-02-17 → 2026-08-14)
before building anything on top of it:

| Check | Result |
|---|---|
| Trading days with bar data | **125/125**, every ticker |
| Days with an exact 09:30 ET bar (the open) | **125/125**, every ticker |
| Days with an exact 09:45 ET bar (the entry floor) | **125/125**, every ticker |
| Days with an exact 15:45 ET bar (last regular-session bar) | **125/125**, every ticker |

Full coverage confirmed — no fallback to daily OHLC was needed, no price
was ever fabricated.

### Corrected intraday timeline

For every trading day, in real chronological order:

1. **09:30 (open):** opening-gap stops/targets resolve first (using only
   that instant's open price — no bar range yet), then time-stops (decided
   on information already known before the session — elapsed trading days
   — so there's nothing to wait for), then any queued discretionary SELL
   (exempt from §7's entry floor — exits are always allowed — fills at the
   open **only if the position is still open** after the two prior checks).
2. **09:30–09:45:** the first bar's real low/high range, for positions
   still open after the opening instant.
3. **09:45 (the only entry point):** queued BUY orders fill here, never
   earlier. Re-validated by the risk checker using **only** marks known by
   09:45 — the 09:30 bar's close for anything still open, never today's
   close or any later bar — and the **real** clock context
   (`minutes_since_open=15`, `minutes_until_close=375`; never a
   placeholder like the old fixed `120`/`120`).
4. **09:45 through the close:** every remaining 15-minute bar, walked in
   order, for every open position — both carried-over holdings and
   anything that just entered at 09:45 (its own entry bar counts, since
   entry happened at that bar's start; earlier bars that day do not,
   since the position didn't exist yet).

**Ambiguity rule, stated once and applied everywhere:** when a single
bar's low/high range technically crosses both the stop and the target
(possible, if rare, at 15-minute granularity), OHLC alone cannot say
which came first — resolved conservatively, **always the stop, never the
target**. A real, accepted limitation of bar-level (not tick-level)
simulation, not silently assumed away.

Implementation: `simulate_trading_day` (pure, no I/O) decides the entire
sequence above and returns it as an ordered list of plain-dict events;
`run_day` replays that plan with real I/O (journal writes, slippage-aware
fills via the same `execution-sim` path as before — mechanical exits
still never go through slippage, matching the pre-existing distinction
between mechanical and discretionary fills). `evaluate_opening_gap` and
`evaluate_bar_stop_target` are the small pure primitives it's built from;
`require_intraday_coverage` is the fail-closed gate.

### A real bug caught before it shipped

While wiring the risk-checker context for the new 09:30/09:45 checks, the
daily/weekly P&L and drawdown figures were initially hardcoded to `None`
(no `equity_curve` access inside the new pure function). `risk_checker.py`
correctly treats `None` there as **fail-closed** — every single BUY in the
first test run was silently rejected with "P&L could not be determined."
Caught immediately by the new tests, not shipped: `simulate_trading_day`
now takes the real `daily_pl_pct`/`weekly_pl_pct`/`drawdown_from_peak_pct`
(computed once from `working.equity_curve`, which doesn't change until
the very end of the day, so these are stable "as of end of yesterday"
figures for the whole session) as explicit parameters instead of
guessing. A second, related bug caught the same way: the function never
received the portfolio's actual starting cash, so its internal running
balance began at $0 instead of the real figure — fixed by threading
`starting_cash` through explicitly.

### Tests run and results

**New pure tests (no Docker/Neon), `test_execution_semantics.py`, 20
total, all passing** — including every required scenario: a BUY cannot
fill before 09:45; a queued SELL fills at the open when nothing closed it
first; a later stop cannot supersede an already-completed opening SELL;
an opening-gap stop correctly supersedes a queued SELL; the 09:45 BUY
risk check is provably unaffected by an extreme later close (two separate
scenarios); stops/targets resolve at the *first* touching bar, never a
later one; entry-day resolution only considers bars at or after the
entry bar; missing intraday data (a ticker entirely absent, or missing
just the 09:45 boundary) fails closed with `RuntimeError`. Two existing
Phase 0 tests were updated (`resolve_queued_order`'s same-day-exit fields
moved to the new bar-walk, since a single aggregate daily bar was itself
a mild form of intraday lookahead) — everything else from Phase 0 passes
unchanged.

**Real end-to-end smoke test against live Alpaca data** (not part of the
permanent suite, run manually): `fetch_intraday_bars` → real 15-min OLN
bars → fed into `simulate_trading_day` with a hand-built position and a
hand-built queued BUY. The BUY was correctly **rejected** by the real
risk checker (stop distance 19.5%, outside RULES.md §6a's 5–10% band) —
confirms the full pipeline (Alpaca → `alpaca-mcp` → `run_backtest.py` →
`risk_checker.py`) is wired correctly end to end against real market
data, not just hand-built fixtures.

**Integration suites, all still green:** `test_execution_path.py`
(unaffected by this change), `test_risk_checker.py`,
`test_anonymize.py`, `test_quota_pacer.py` (all unchanged, unaffected).

One deployment step repeated from Phase 0: **`alpaca-mcp` also bakes code
into its image** (`build:`, not a live mount) — rebuilt and re-verified
against the live container before any of the above ran.

### Remaining limitations of this follow-up

- **Approximation, stated plainly:** `simulate_trading_day`'s own internal
  cash bookkeeping (used only to decide the 09:45 risk-check equity
  figure) uses **unslipped** bar-open reference prices for every leg,
  including a same-day SELL that precedes a BUY. Real execution-sim
  slippage (5bps) is applied by `run_day` only to the actual recorded
  fill/cash movement, never fed back into the planning function's own
  sequencing math. Bounded to at most a few dollars on a same-day
  SELL-then-BUY sequence — small enough that it would essentially never
  flip a real risk-check verdict, but a real, documented approximation
  rather than exact to the cent.
- **Two different data vendors now coexist for two different purposes:**
  `fetch_bars` (yfinance, via `maverick-mcp`) still supplies the
  holiday/no-trading-day check and `positions_view`'s informational
  display; `fetch_intraday_bars` (Alpaca) supplies every execution-timing
  decision. Minor cross-vendor close-price discrepancies between the two
  are possible in principle (different feeds can print slightly different
  official closes) — never a correctness problem for execution (Alpaca is
  the sole source there), but worth knowing if a displayed price and an
  executed price look slightly different on the same day.
- A queued order whose scheduled ticker has genuinely no intraday data
  now **halts the cycle** (fails closed) rather than being silently
  dropped, which is stricter than Phase 0's original behavior — a
  deliberate change per this follow-up's explicit requirement, not an
  oversight.
- Cross-ticker event ordering in the activity log is per-ticker, not
  strictly wall-clock-interleaved across multiple tickers on the same
  day (each ticker's own bars are still processed in correct chronological
  order, which is what actually matters for a single position's
  stop/target resolution) — cosmetic only.

### Bug fix: partial-SELL cash credit (2026-08-25)

`simulate_trading_day`'s queued-SELL stage credited `running_cash` using
the position's **full** `holding.quantity` even when the order only sold
part of it (`order.quantity < holding.quantity`) — inflating the 09:45
cash/equity figure a subsequent BUY got risk-checked against, and could
have caused a real concentration-cap or cash-floor violation to be missed.
Fixed to credit exactly the executed quantity
(`min(order.quantity, holding.quantity)`); the remaining-quantity
bookkeeping was already correct and untouched. New regression test
(`test_partial_sell_credits_only_the_sold_shares`) proves all three
required properties in one scenario — only the sold shares are credited,
the correct remainder (80 of 100) is what a later same-day exit reports,
and a boundary-cased BUY is correctly rejected (§2b) using the true,
non-inflated equity where it would have been wrongly accepted under the
old bug. Verified the test actually catches the bug by temporarily
reintroducing it and confirming the test fails, then restoring the fix.

---

## Open questions worth a second opinion — this is where you're useful

1. **Risk per trade: 1% (current) vs. 5% (a comparable real system Arjun
   referenced).** Deliberately left open, to be settled against real
   tuning-window results rather than argued from theory now. Worth
   revisiting once meaningful trade volume exists.
2. **Model choice.** GPT-5.4-mini/medium was never actually chosen — it's
   the default. A side-by-side against a stronger model/effort level on a
   handful of identical cycles (same day, same prompt) hasn't been run
   yet. Worth weighing in on whether that's worth doing before the full
   tuning run, or after, based on early results.
3. **Weekly circuit breaker (−6%, RULES.md §4).** Flagged in the rules
   file itself as the weakest-grounded number in the whole document — no
   external reference point, just sits between the daily (−3%) and total
   (−10%) limits.
4. **Time stop, just revised 10 → 15 trading days** (see Ledger for the
   derivation — it's tied to the 5%/10% stop-target geometry). Flagged for
   a real second opinion once results exist, not locked in as final.
5. **Total exposure cap (60%) and max open positions (8).** Judgment
   calls, not grounded in an external standard — see RULES.md §10 for the
   full list of what's flagged as worth checking.

---

## Ground rules for weighing in

- **RULES.md's hard limits are not up for negotiation via "better
  performance" arguments alone.** The file is explicit: even Hermes itself
  is barred from loosening a limit through its own reflection, no matter
  how compelling the backtest data looks after a win streak. Any proposed
  change to a hard number needs to go through Arjun explicitly — this
  isn't a rule you or Hermes can talk your way around.
- **Don't propose architecture changes lightly.** A few of the current
  design choices look over-engineered from a fresh read (isolated Docker
  networks per tool, masking, point-in-time gating) — they aren't. Most of
  these came from a real failure mode that was already hit once.
- **This project's own quant assumptions have known gaps**, already
  identified and not yet resolved: the watchlist was screened using data
  from inside the exact window being backtested (survivorship bias, P&L
  from the tuning run should be read as noise, not signal); no benchmark
  series (S&P MidCap 400) is wired in yet for comparison.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`. For the literal rules
Hermes operates under, read `RULES.md`/`SOUL.md`/`AGENTS.md` in the repo
root directly.*

---

**2026-08-27:** the fresh-start study guide was regenerated as
`PROJECT_FRESH_START.md` (repo root), fully re-audited against the current
code rather than carried forward from the prior version. The old private
study artifact (an earlier Claude Artifact, "The Hermes Ledger") was
retired — see that regeneration's own summary for details, not duplicated
here.
