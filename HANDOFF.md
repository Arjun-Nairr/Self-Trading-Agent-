# Handoff briefing — Hermes trading agent

**Read this if you are an AI assistant being brought in to discuss or advise
on this project.** It exists because the working split from here on is:
**you (this chat) handle discussion, strategy, and second opinions; Claude
Code handles execution** — reading/editing the actual code, running the
harness, touching Neon, committing. Don't propose direct code edits as if
you'll apply them yourself; propose the *decision*, and it gets relayed back
to Claude Code to implement and verify. If you want to see current file
contents to reason about something precisely, ask Arjun to paste them in —
you don't have live repo access.

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
**[The Hermes Ledger](https://claude.ai/code/artifact/57a0d9c6-8a25-4fa7-933c-de5327c6f9cb)**
— read that first if you want real depth before opining on anything below.
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
  networks per tool, masking, point-in-time gating) — they aren't. Read
  the Ledger's reasoning before suggesting a simplification; most of these
  came from a real failure mode that was already hit once.
- **This project's own quant assumptions have known gaps**, already
  identified and not yet resolved: the watchlist was screened using data
  from inside the exact window being backtested (survivorship bias, P&L
  from the tuning run should be read as noise, not signal); no benchmark
  series (S&P MidCap 400) is wired in yet for comparison.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`. For the literal rules
Hermes operates under, read `RULES.md`/`SOUL.md`/`AGENTS.md` in the repo
root directly.*
