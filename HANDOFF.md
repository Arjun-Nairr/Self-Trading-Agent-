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

---

## Dashboard correctness + pre-training baseline reset (2026-08-28)

**Locked model decision, reconfirmed, untouched:** `openai-codex` /
`gpt-5.4-mini` / medium reasoning. A separate research pass this same day
confirmed `opencode-go` + `deepseek-v4-flash` is real and documented, but
`NousResearch/hermes-agent#61461` (open) reports the exact combination
hanging on a real timeout, so the switch was **abandoned** before any
config change — Codex auth (`auth.json`) was never touched.

### 1. Re-audit findings (before any edit)

Traced every dashboard value back to its real source, per file:

- **Quota was wrong by construction.** The dashboard counted
  `portfolio_snapshots` rows in a rolling 7-day window as a stand-in for
  weekly Hermes usage — a second, independent counter that could silently
  disagree with `quota_pacer.py`'s own real ISO-week count (which counts a
  cycle the instant Hermes is *invoked*, including one that later fails or
  retries — see `run_backtest.py`'s `mark_invoked`).
- **Real per-cycle token usage was computed and printed to the console,
  never persisted anywhere.** `_call_hermes`'s `--usage-file` report
  (input/output/cache/reasoning tokens, api_calls) only ever reached
  `run_usage`'s final print line.
- **`insight_log` had no mode column at all** — a training reflection and a
  live one would have been indistinguishable the moment live cron exists.
- **Stop/target were never persisted.** `risk_checker.py` computes and
  enforces them on every BUY, but `trades` had no columns for them — an
  open position's real stop/target only ever lived in the harness's
  in-memory `Holding` (backtest) or the live call's own arguments, both
  gone once the process exits.
- **Failed cycles left no trace anywhere queryable.** `_run_cycle_or_halt`
  printed and halted but wrote nothing to Neon.
- **`trades` query capped at 200 rows** — provably too low: even a
  conservative upper bound (10 watchlist tickers × ~125 real trading days,
  one decision max per ticker per day) is 1,250 possible rows.
- **Dashboard language recommended loosening `RULES.md`** in three places
  (target-distance flag in the summary, "first candidate for tuning" on
  blocked-rule counts, a §6b-blaming note on exit reasons) — against
  `RULES.md`'s own "Hermes may never loosen a constraint."
- **The Live dashboard's one pending decision was real, not a display bug**
  — a manual AAPL integration-test order from 2026-08-14, `execution_mode:
  'alpaca'`, status stuck `'pending'` with a real `broker_order_id`.

### 2. Runtime verification (real checks, this project's isolated stack only)

- **Isolation:** every container on this machine (`docker ps -a`) belongs
  to this project; no other Hermes instance shares a mount, container, or
  Neon database.
- **Hermes:** `hermes status` confirmed `gpt-5.4-mini` / `OpenAI Codex`,
  logged in; a minimal `hermes -z` request round-tripped real JSON.
- **Neon writer:** real `SELECT 1` succeeded over `NEON_DATABASE_URL`.
- **Neon anonymous Data API:** all four tables (`trades`, `agent_activity`,
  `portfolio_snapshots`, `insight_log`) returned real rows for the
  anonymous role — the same path the dashboard's browser JS uses.
- **Alpaca paper account:** `ACTIVE`, `trading_blocked: false`,
  `account_blocked: false`, equity $100,000, no open positions, no orders
  placed during this check.
- **Maverick historical technicals:** real point-in-time OLN bar,
  2026-03-01, via the exact `yfinance` call path `run_backtest.py` uses.
- **Alpaca historical news:** real call path confirmed working (WMG/SFM
  returned real Benzinga articles for a 2026-06 window; OLN/PLNT returned
  zero for the same window — a real absence, not a broken call).
- **Google News RSS:** real live headlines returned for a test query.

### 3. AAPL stale test record — resolved against real broker state

Alpaca's own order lookup (`get_order_status`, source of truth) reported
that order as `canceled` on the broker side, never filled. Journaled the
real outcome via `record_execution_result` (status `rejected`,
`rejection_reason` naming the real broker status and this resolution's
date) — no fill was fabricated, no row was deleted.

### 4. Schema migration (additive only, applied live to Neon)

```sql
ALTER TABLE trades ADD COLUMN stop_price NUMERIC(14,2);
ALTER TABLE trades ADD COLUMN target_price NUMERIC(14,2);
ALTER TABLE insight_log ADD COLUMN mode TEXT CHECK (mode IN ('backtest','live'));
-- backfilled: every pre-migration insight_log row is 'backtest' (no live
-- cron has ever run — verified, not assumed)
ALTER TABLE portfolio_snapshots ADD COLUMN input_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN output_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN reasoning_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN total_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN api_calls INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN hermes_calls_used INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN weekly_budget INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN open_positions_detail JSONB;
```

No table dropped, no existing column changed, no grant/RLS touched (new
columns on an already-granted table need no new grant). `schema.sql`
carries this as an idempotent migration block for the next fresh database.

**Real bug found and fixed as a direct result of this migration:**
`NEON_DATABASE_URL` is a Neon **pooled** (PgBouncer transaction-mode)
endpoint. asyncpg's default prepared-statement cache is documented as
incompatible with transaction pooling — any DDL (including this one)
invalidates a cached plan on a query that never itself changed, surfacing
as `asyncpg.exceptions.InvalidCachedStatementError` on the *next* call,
persistently, not just once. Fixed by setting `statement_cache_size=0` on
`trade-journal-mcp/server.py`'s pool (`get_pool`) — the documented fix
(see asyncpg#750), not a retry loop. This was a **latent bug that predates
this task** and would have struck on any future migration; found here,
fixed here, verified via `test_idempotency.py` before and after.

### 5. Code changes

- **`trade-journal-mcp/server.py`:** `stop_price`/`target_price` on
  `record_decision`; `mode` required on `_log_insight` (`log_insight` tool
  auto-tags `'live'`, matching `log_activity`'s existing pattern);
  token/quota/`open_positions_detail` fields on `record_portfolio_snapshot`;
  the `statement_cache_size=0` fix above.
- **`backtest-harness/run_backtest.py`:** `record_trade` now persists real
  stop/target; `record_insight` tags `mode="backtest"`; `record_snapshot`
  now also writes real per-cycle usage (from `ask_hermes`'s totals), the
  real pacer count (`pacer.used_this_week()`/`pacer.budget` at write time —
  not re-derived), and a real per-position detail list (ticker, entry,
  current mark, stop, target, days held, unrealized P&L) computed from the
  same intraday marks used for that cycle's own equity figure. The
  snapshot write itself moved to right after `ask_hermes` (same values,
  computed at the original point) purely so the real usage numbers land in
  the same row instead of a second write. `_run_cycle_or_halt` now
  best-effort logs a `cycle_failed` activity row on halt (day label +
  exception), wrapped so a logging failure can never mask the real one.
- **`dashboard/index.html`:** quota now reads the latest snapshot's real
  `hermes_calls_used`/`weekly_budget` (no independent counting); new
  "Model Usage & Quota" section (input/output/reasoning/cache-read/total
  tokens, avg tokens/cycle, retry count via `api_calls > 1`, failed-cycle
  count via an exact-count PostgREST query); `trades` query raised
  200 → 2,000 with the provable-bound derivation in a comment; `insight_log`
  now filtered by `mode`; open positions now show current mark, value,
  unrealized P&L $/%, stop, target, days held (joined from the latest
  snapshot's `open_positions_detail`, falling back to the trade row's own
  `stop_price`/`target_price` where available); added Realized P&L,
  Unrealized P&L, Max Drawdown, Last Successful Cycle, Last Failed Attempt;
  removed all three rule-loosening suggestions, replaced with neutral
  observation-only language and explicit sample sizes.
- **`docker-compose.yml`:** new `dashboard` service — the existing static
  frontend, unchanged, served via `python:3.12-slim`'s stdlib
  `http.server`, bound to **`127.0.0.1:8842` only** (not `0.0.0.0`), no
  build, no framework, `restart: unless-stopped`.

### 6. Tests

- All pre-existing pure suites still pass, unchanged behavior:
  `risk-checker/test_risk_checker.py` (22 assertions),
  `backtest-harness/test_execution_semantics.py` +
  `test_anonymize.py` + `test_quota_pacer.py` (37 pytest cases),
  `trade-journal-mcp/test_decision_fields.py`,
  `trade-journal-mcp/test_idempotency.py` (real Neon, real idempotency
  proof), `alpaca-mcp/test_status_mapping.py` + `test_account_math.py`,
  `execution-sim-mcp/test_fill_logic.py`,
  `backtest-harness/test_execution_path.py` (real containers, real Neon
  round-trip).
- **New:** `trade-journal-mcp/test_dashboard_fields.py` — proves
  stop_price/target_price round-trip on a decision, `mode` is required and
  enforced on an insight, and usage/quota/`open_positions_detail` round-trip
  on a snapshot. Self-cleans its own rows.
- **Real browser verification** (local Docker dashboard, not a mockup):
  Training view, Live view, mode switching, auto-refresh, empty states,
  populated states, mobile-width layout — all with zero console errors.
  The Live view correctly now shows the resolved AAPL decision as
  `REJECTED`.

### 7. Local dashboard access

```bash
docker compose up -d dashboard
```
`http://127.0.0.1:8842` — bound to loopback only, confirmed via
`docker port hermes-trading-agent-dashboard` (`127.0.0.1:8842`, not
`0.0.0.0`). Not publicly deployed; GitHub Pages 404 is unaddressed by
design (private learning project). **Documented, unaddressed exposure:**
the anonymous Neon Data API role is SELECT-only but has no per-request
auth beyond that — anyone who has the endpoint URL (visible in this file's
own page source) can read the same journal rows this dashboard reads.
Out of scope for this task per its own instructions; flagged, not fixed.

### 8. Pre-training baseline reset

**Backup (private, outside git — confirmed via `git check-ignore`) at**
`C:\Users\<user>\.hermes-trading-agent\backups\`:
- `pretraining_backup_2026-08-28.json` — full row export before deletion
- `backtest_state_pre_reset_2026-08-28.json` — the harness's local state
  before reset
- `quota_pacer_state_2026-08-28.json` — the pacer's local state (untouched
  by the reset — copied only as an extra rollback point)

**Row counts deleted** (all backtest-tagged, verified before deletion that
the filter cannot touch the live/Alpaca row):

| Table | Filter | Rows deleted |
|---|---|---|
| `agent_activity` | `mode = 'backtest'` | 22 |
| `portfolio_snapshots` | `mode = 'backtest'` | 7 |
| `insight_log` | `mode = 'backtest'` | 1 |
| `trades` | `execution_mode = 'backtest'` | 0 (no trade ever filled in the discarded run) |

**Confirmed preserved, unmodified:** the live/Alpaca AAPL row (still
present, `execution_mode: 'alpaca'`), schema, `config.yaml`, `.env`,
Hermes/Codex auth, every other Hermes project on this machine, real Alpaca
broker history, and `quota_pacer_state.json`'s real count — **not** reset
to zero (`{"2026-W34": 34}` untouched; the harness naturally started a new
`"2026-W35"` key for this real calendar week, which is correct ISO-week
behavior, not a reset).

**`backtest_state.json` reset to:** cash $100,000, equity $100,000, no
holdings, no closed trades, no pending orders, `equity_curve` reseeded flat
at 6× $100,000 (matching `Portfolio`'s own dataclass default — needed so
the risk checker doesn't fail-closed on weekly P&L for the first 5 cycles
of the real run, exactly as the existing code comment explains), cursor
reset to `2026-02-17` (the existing configured window start, unchanged).
Verified by loading it through the real `Portfolio.from_dict` path before
using it, not just inspected by eye.

**Also cleaned (found, not part of the requested scope):** 3 leftover
`execution_mode: 'simulated'`, ticker `TEST.NS` rows I created myself while
debugging the pgbouncer statement-cache bug above, and one accidental real
`cycle_failed` activity row written when `test_execution_semantics.py`'s
own failure-simulation test unexpectedly reached the live `docker compose
exec` path (Docker happened to be running during that pytest run). Neither
belonged to the discarded backtest run; both deleted directly, by decision
id / description match, once identified.

### 9. Controlled smoke test (≤ 3 cycles, from the clean baseline)

```
$ python run_backtest.py --days 3
```

| Cycle | Real trades | Cursor after | Snapshot written | Failed |
|---|---|---|---|---|
| Day 1 (2026-02-17) | BUY ALGM x250 queued | 2026-02-18 | ✅ (21,062–23,669 real tokens, see below) | no |
| Day 2 (2026-02-18) | Day 1's queue rejected on fill (§6a: stop above entry); new BUY ALGM x500 queued | 2026-02-19 | ✅ | no |
| Day 3 (2026-02-19) | Day 2's queue rejected on fill (§2b: 18.71% > 10% cap; §6a: stop 2.03% < 5% floor) | 2026-02-20 | ✅ | no |

Every check the task required, verified against real Neon after every
cycle (not just the console output):

- Cursor advanced **exactly once per cycle** (`2026-02-17 → 2026-02-20`,
  3 real trading days, no skip/double-advance).
- Portfolio state and Neon agree: 3 `portfolio_snapshots` rows
  (`mode='backtest'`), each `equity`/`cash` = $100,000 (no trade ever
  filled — both queued BUYs were correctly rejected by the risk checker on
  resolution, a real and legitimate outcome, not a bug).
- `agent_activity`: 12 real rows (3 `cycle_start`, 5 `reasoning`,
  2 `order_queued`, 2 `order_rejected`) — matches the console output
  exactly.
- Real quota usage incremented correctly: `hermes_calls_used` 1 → 2 → 3 on
  the snapshot rows, matching `quota_pacer_state.json`'s real
  `{"2026-W35": 3}` (a genuinely new ISO week from the prior `"2026-W34":
  34`, not a reset).
- Token usage persisted **and matches the console line exactly**: 22,798 /
  23,669 / 21,062 tokens; 22,510 avg/cycle shown identically in both the
  console and the dashboard.
- No failed cycles (0 `cycle_failed` rows) — nothing to verify there this
  run, but the mechanism itself is proven real (see §8's note above).
- Dashboard reconciled against the source data directly (not just visually
  read) — Training view showed the exact same numbers, activity narration,
  and quota/token figures Neon holds.

**No inconsistency found. Did not continue into the full six-month run —
stopped at 3 cycles per instruction, reporting back here.**

### 10. Rollback

- **Dashboard/schema/harness code:** `git diff` / `git checkout --` the
  5 modified files listed in "Files changed" below; the migration is
  additive-only and safe to leave in place even if the code changes are
  reverted (unused columns, not a breaking change).
- **Neon data:** restore from
  `C:\Users\<user>\.hermes-trading-agent\backups\pretraining_backup_2026-08-28.json`
  (full row content for the 30 deleted rows) by re-inserting.
- **Local state:** restore `backtest_state.json` from
  `backtest_state_pre_reset_2026-08-28.json` in the same backups directory
  to resume the discarded Day-1–9 run instead of the fresh baseline.
- **Quota:** untouched either way — no rollback needed.

### Remaining limitations / compatibility risks

- `open_positions_detail`'s "current price" is the last **regular-session
  15-minute bar's close** at end of day (same mark `equity` itself uses),
  not a true intraday tick — consistent with how the rest of the harness
  already marks positions, not a new approximation.
- Dashboard's `TRADES_LIMIT = 2000` is a provable upper bound, not a
  measured one — revisit if the real watchlist or window size changes.
- Neon's anonymous-role exposure (§7) remains open by design per this
  task's own scope.
- The pgbouncer statement-cache fix (§4) only covers `trade-journal-mcp`;
  any other service that later talks to the same pooled endpoint through
  `asyncpg` (none currently do) would need the same fix.
