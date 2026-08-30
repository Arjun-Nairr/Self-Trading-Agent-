# Handoff briefing — Hermes trading agent

**Read this if you are an AI assistant being brought in to discuss or advise
on this project.** The working split: **discussion/strategy/second opinions
happen in chat; Claude Code handles execution** — reading/editing code,
running the harness, touching Neon, committing. Don't propose direct code
edits as if you'll apply them yourself; propose the *decision*, and it gets
relayed to Claude Code to implement and verify.

**Context control:** read this compact handoff first, then only the files
directly relevant to the task you're assigned. Do not load the historical
archive (`docs/history/`), vendored third-party code (`maverick-mcp/`),
local backups, generated/state files, or unrelated test files unless the
task specifically requires them. Full dated repair narratives, test
transcripts, and superseded numbers live in
[`docs/history/HANDOFF_FULL_2026-08-28.md`](docs/history/HANDOFF_FULL_2026-08-28.md)
— link there instead of re-deriving or duplicating that detail here.

---

## What this project is

Hermes: an LLM-driven agent that trades US equities — **long only, paper
account, swing-trading horizon** (days to weeks) — reasoning over
technicals and news, gated by a hard-coded risk checker before anything
executes. Two modes: **backtest** (point-in-time historical simulation,
identity-masked so the model can't recall what really happened) and
**live** (real Alpaca paper-trading API). **Currently in the
backtest/tuning phase — no live trading yet, and the six-month training
run has not started.**

A separate personal study artifact, "The Hermes Ledger," exists as the
user's own optional deep-dive reading — it is **not** required context for
an assistant working on this project and was not consulted to write this
file. Do not treat it as authoritative; this file and the repo's own
source (`RULES.md`, `AGENTS.md`, `SOUL.md`, actual code) are.

---

## Locked model decision

**`openai-codex` / `gpt-5.4-mini` / medium reasoning — locked for this
training run.** Originally the tool's shipped default, not a deliberate
choice, but as of 2026-08-28 stated unambiguously: do not change it
without an explicit new decision.

**OpenCode Go + DeepSeek V4 Flash was investigated and abandoned.** Both
the provider and model are real and genuinely supported — but
`NousResearch/hermes-agent#61461` (open, unresolved) reports that exact
combination hanging on a real timeout. No config change was made;
`auth.json`/`config.yaml` still show only `openai-codex`/`gpt-5.4-mini`.
Do not reopen without new upstream evidence the timeout is fixed.

---

## Architecture and data flow

One LLM agent (`gateway`) plus five isolated tool containers, hub-and-
spoke — no tool container can reach another. Docker images are **pinned
by digest** (not `:latest`), rebuilt and confirmed behaviorally identical.

| Service | Responsibility |
|---|---|
| `gateway` | Runs Hermes (the model). Backtest: never calls a tool itself — see below. Live: calls tools directly. |
| `maverick-mcp` | Technicals (RSI/MACD/support-resistance/trend) + price history, point-in-time gated. |
| `google-news-rss-mcp` | Live-mode headlines only — real-time RSS, no interpretation. |
| `alpaca-mcp` | Live orders/account/positions (real Alpaca paper API) + backtest's historical news and intraday-bar data. |
| `trade-journal-mcp` | All Neon reads/writes — decisions, executions, activity narration, insights, portfolio snapshots. |
| `execution-sim-mcp` | Backtest-only fill simulator (slippage-aware MARKET/LIMIT fills against real historical ranges). |
| `dashboard` | Static frontend, served locally only (see below). |

**Backtest mode:** Hermes never calls a tool directly. `backtest-harness/
run_backtest.py` fetches everything (technicals, news — now including
**held** positions, not just unheld candidates), masks tickers/dates, and
hands Hermes one prompt per simulated day. One "cycle" = one simulated
trading day, not one trade.

**Live mode:** Hermes calls MCP tools directly against real (paper)
broker/news endpoints.

**Where each kind of data lives:**

| Data | Owner |
|---|---|
| Every decision, execution result, activity narration, insight, equity snapshot | Neon Postgres (`trades`, `agent_activity`, `insight_log`, `portfolio_snapshots`) — the single source of truth, one DB, `mode` column separates backtest/live |
| The in-progress backtest portfolio (cash, holdings, pending orders, cursor, `run_id`) | `backtest-harness/backtest_state.json`, a local file |
| This real calendar week's Hermes-invocation count | `backtest-harness/quota_pacer_state.json`, a local file, keyed by ISO week |
| Live broker positions/orders | Alpaca itself — authoritative for live, never the journal |
| Dashboard's own state | None — it's a stateless read against Neon's Data API, nothing cached server-side |

---

## Rules, risk enforcement, and lookahead prevention

- **`RULES.md` is the authoritative, locked rules source** — mounted
  read-only into `gateway` (`docker-compose.yml`), re-read by Hermes every
  cycle, never trusted from memory. Hermes may refine judgment through
  reflection but may **never** loosen a limit.
- **`risk_checker.py`** enforces every numeric rule in code, independent
  of the model's own reasoning — pure functions, zero I/O, **fail-closed**
  on any unknown/non-finite/non-positive value (NaN, zero/negative equity,
  missing P&L, unknown market-hours timing while the market is reported
  open all block rather than silently pass or crash).
- **Point-in-time masking + lookahead prevention:** masked ticker/company
  identities (reshuffled per run), `as_of`-gated data fetches, and
  next-day queued execution together guarantee a backtest decision never
  sees information that wouldn't have existed yet.
- **Decision-time vs. execution-time validation:** a proposed decision is
  validated twice. *Decision time* (`validate_timing=False`) checks every
  rule except market-hours timing, using today's known state as a
  good-faith proxy — advisory, not authoritative. *Execution time*
  (`resolve_queued_order`, the next real trading day, real price)
  re-validates everything including timing and is the **sole authority**
  on whether a trade actually happens. A proposal passing decision-time
  can still be correctly rejected at execution time against the real
  price — this is the intended design, proven in the last real smoke
  test (a queued BUY passed decision-time, then was rejected the next day
  against a real price gap).
- **One-lot model:** a discretionary SELL must close the entire position.
  Rejected outright at both decision and execution time. (Involuntary
  *broker* partial fills — the venue filling fewer shares than requested
  — remain a separate, unmodelled live-mode limitation, distinct from
  this.)

## Journaling, reconciliation, run isolation, quota

- **Queue-time journaling:** a decision is journaled the instant it's
  queued (status `'pending'`), not only once it fills. The *same* row is
  updated to `filled`/`rejected` on resolution — never a fresh row, never
  a fabricated fill. A `decision_id` collision with a mismatched payload
  fails loudly rather than silently returning stale data.
- **Reconciliation (live only):** on restart, check every `'pending'`
  journal row against real broker state before doing anything else — the
  broker is always the source of truth for live positions.
- **Run isolation:** every backtest run gets a fresh `run_id` (uuid4,
  persisted in `backtest_state.json`, reused across a resume, never
  reused across a `--reset`). Every table carries it. A deterministic
  `cycle_id` per (run, simulated day) has a real database-level
  uniqueness constraint — a retried cycle cannot duplicate a snapshot,
  activity narration, or reflection insight. Reflection retrieval and the
  dashboard are both scoped to the current run's `run_id`.
- **Quota:** `quota_pacer.py` counts real Hermes invocations against the
  real ISO calendar week, counted the instant Hermes is actually called
  (including a call that later fails) — the dashboard reads this real
  count directly, never a second estimate.

---

## Dashboard

Local-only: `docker compose up -d dashboard` → **`http://127.0.0.1:8842`**,
bound to loopback, never publicly deployed (GitHub Pages was never live
for this and isn't the goal). Reads Neon's Data API with a SELECT-only
anonymous role. Shows real quota/token usage, real per-run scoping,
paginated (not row-capped) trade history, and neutral risk-check
observations (never a suggestion to loosen a rule). **Documented,
unaddressed exposure:** the anonymous role has no auth beyond SELECT-only
— anyone with the endpoint URL can read the same journal rows.

---

## Current training window and clean baseline (verified 2026-08-28)

**Local state** (`backtest-harness/backtest_state.json`), verified through
the real `Portfolio.from_dict` code path, not just inspected:

- Cash / equity: **$100,000**, no holdings, no closed trades, no pending
  orders
- Cursor: **`2026-02-17`** (the configured backtest window start)
- Trading days elapsed: **0**, cycles since reflection: **0**
- Equity curve: 6 flat entries at $100,000 (matches `Portfolio`'s own
  dataclass default — needed so the risk checker doesn't fail-closed on
  weekly P&L for the first 5 cycles of a real run)
- `run_id`: **not stored** — the harness generates a fresh one the next
  time it actually runs

**Quota** (`backtest-harness/quota_pacer_state.json`, untouched by this or
any prior reset): `{"2026-W34": 34, "2026-W35": 6}` — real invocation
history, preserved exactly.

**Neon:** zero `mode='backtest'`/`execution_mode='backtest'` rows in
`trades`, `agent_activity`, `portfolio_snapshots`, `insight_log`. The
AAPL manual integration-test row (`execution_mode: 'alpaca'`,
`status: 'rejected'`) was removed on 2026-08-28 (backed up first — see
`~/.hermes-trading-agent/backups/aapl_manual_test_row_backup_2026-08-28.json`)
— `trades` is now genuinely empty project-wide, zero rows.

**Dashboard**, verified live in a browser without running a Hermes cycle:
Training and Live views both show zero cycles/trades/positions/snapshots.
Zero console errors.

---

## 2026-08-28 correctness pass (second pass) — repaired guarantees

Narrowly scoped repair, no training started, no architecture/feature
changes. Full narrative in
`docs/history/HANDOFF_FULL_2026-08-28.md` is NOT written yet for this
pass (nothing to archive from — this is new content); this section is
the complete record.

1. **Circuit breakers now see this morning's own losses before a 09:45
   BUY.** `simulate_trading_day` used to receive daily/weekly/drawdown
   percentages frozen as of yesterday's close; a same-morning opening-gap
   or 09:30–09:45 stop never touched a 09:45 entry's risk check. Fixed by
   passing the raw `equity_curve` through and recalculating breakers
   fresh, from a real intraday equity mark, at every 09:30 SELL and 09:45
   BUY decision point (see `_recalc_breakers`).
2. **Failed-cycle retries are now deterministic.** A validated Hermes
   response is durably checkpointed (`backtest-harness/cycle_checkpoint.json`,
   gitignored, atomic write) the instant it exists — before
   `record_snapshot_once`, decision journaling, or activity logging. A
   retry of the same `(run_id, day_index)` reuses the checkpointed
   response verbatim: no second Hermes call, no quota charge. Cleared only
   after the atomic local-state commit for that cycle succeeds (see
   `_load_checkpoint` / `_save_checkpoint` / `_clear_checkpoint`).
3. **`decision_id` collisions now compare every decision-defining field**
   (notional, stop/target, confidence, signals, reward:risk, invalidation,
   notes, timestamps, `run_id` — not just ticker/action/quantity/order_type),
   normalized sensibly (numeric tolerance, JSON structural equality). A
   genuine retry stays a no-op; anything materially different fails loudly,
   naming the field (`trade-journal-mcp/server.py`, `_decision_field_mismatches`).
4. **Maverick's historical OHLC now explicitly requests the same raw basis
   Alpaca execution uses** (`auto_adjust=False` on the yfinance fetch) —
   previously yfinance's own default (dividend/split-adjusted) could
   silently disagree with real fill prices around an ex-dividend date.
   `as_of` point-in-time gating is untouched.
5. **Queued BUY risk checks now use the slippage-adjusted fill price**,
   not the raw bar open — mirrors execution-sim-mcp's own `SLIPPAGE_BPS=5`
   MARKET-fill rule exactly (`_slipped_buy_price`). A trade that only
   breaches a hard cap after slippage is now rejected before it can ever
   reach execution. SELL is deliberately unaffected (slippage only makes
   an exit more conservative against a cap).
6. **Dashboard's "blocked by rule" summary count now uses the complete
   blocked-activity dataset**, not the 80-row recent-activity feed — it
   could previously undercount on any run with more than 80 recent
   activity rows.
7. **The masking seed is now persisted** in `backtest_state.json`. A
   resumed run reuses its stored seed; a conflicting `--seed` fails
   loudly (`SystemExit`, names both seeds) instead of silently remapping
   ticker↔alias assignments Hermes has already learned this run. A
   fresh/`--reset` run may establish a new seed. A legacy state file
   missing the field falls back to the supplied/default seed safely.

**New regression tests** (`backtest-harness/test_execution_semantics.py`
unless noted): `test_combined_morning_stop_losses_trip_breaker_before_945_buy`,
`test_checkpoint_survives_a_retry_without_a_second_hermes_call_or_duplicate_journal_row`,
`test_buy_risk_check_slippage_price_matches_execution_sim_rule` +
`test_buy_risk_check_uses_slippage_adjusted_price_not_raw_open`,
`test_resolve_seed_resume_and_mismatch`; extended
`trade-journal-mcp/test_idempotency.py` with changed-stop/target/notional/
metadata collision cases; new
`maverick-mcp/tests/market_data/test_fetchers.py::test_default_history_fn_requests_raw_unadjusted_basis`.

**Exact verification results (2026-08-28):** all first-party Python files
touched compile clean. `risk-checker/test_risk_checker.py`,
`backtest-harness/test_execution_semantics.py` (58 checks),
`test_anonymize.py`, `test_quota_pacer.py`,
`trade-journal-mcp/test_decision_fields.py`, `test_idempotency.py`
(extended), `test_run_isolation.py`, `test_dashboard_fields.py` (real
Neon, self-cleaning), `execution-sim-mcp/test_fill_logic.py`,
`maverick-mcp/tests/market_data/test_fetchers.py` (22 tests) — all PASS.
`docker compose config -q` valid. Dashboard smoke-checked live in both
Training and Live modes, zero console errors. Backtest tables confirmed
empty before and after (found 8 stray `cycle_failed` rows generated by
running `test_execution_semantics.py`'s pre-existing
`_run_cycle_or_halt` test locally — backed up, then deleted, root cause
noted: that one test's best-effort failure logging shells out to the
real trade-journal container even when run standalone).

**Remaining blocker: none.** Stack is training-ready pending human
approval to start the run.

---

## 2026-08-28 correctness pass (third pass) — repaired guarantees

**Locked architectural decision — essential vs. best-effort:** cycle data
now splits into two classes. **Essential** (must halt safely and retry
deterministically): the checkpointed Hermes response, portfolio state and
pending orders, trade decisions and execution results, reflections/
insights (they feed future prompts), and every risk-enforcement check --
all completed inside `run_day` before it ever returns. **Best-effort
observability** (must never halt or replay trading): portfolio snapshots,
activity narration, and the dashboard stats derived from them -- deferred
into a `do_observability` closure `run_day` returns, which the caller
(`main()`) invokes ONLY after the atomic local-state commit and
checkpoint clear for that cycle have already succeeded, inside a
try/except that reports a failure without halting, retrying, or
replaying. A crash after the commit but before observability may leave
missing dashboard information (explicitly accepted); it can never create
a duplicate trade or repeat a Hermes call. `record_snapshot_once`'s
`is_new` no longer gates decisions, reflections, or activity.

**Four repairs:**
1. **Fixed identical backtest retry collisions.** Neon returns UUID
   columns (`run_id`) as `uuid.UUID` objects; the harness supplies plain
   strings. `_decision_field_mismatches` compared them directly, so an
   identical retry with a real `run_id` always false-failed as a
   `run_id` mismatch. Fixed via `_uuid_equal` (None-safe string
   normalization) in `trade-journal-mcp/server.py`. Proof:
   `test_identical_retry_with_real_run_id_is_a_no_op` (real Neon) --
   identical retry is a no-op, a genuinely different `run_id` still
   fails loudly.
2. **Corrected sequential 09:45 BUY accounting.** After an accepted
   queued BUY, `simulate_trading_day`'s own internal sequencing used to
   track it via the UNSLIPPED open and mark it at its earlier 09:30
   close for a SECOND queued BUY's risk check -- both could inflate
   equity. `_slipped_buy_price` now mirrors execution-sim-mcp's real
   MARKET-fill formula exactly (5bps + bar-range clamp) for both the
   risk check AND the internal fill/cash bookkeeping; a freshly-entered
   position is marked at its own real fill price, never an unrelated
   09:30 close. Proof: `test_sequential_945_buys_see_the_first_fills_real_state`
   -- a second BUY that would incorrectly PASS under the old inflated
   state is correctly REJECTED (§2b) under the real one.
3. **Isolated unit tests from real Neon/Docker.** `_run_cycle_or_halt`
   now takes an injectable `fail_logger` (defaults to the real
   `log_cycle_failure` in production); the test suite injects a fake, so
   running it locally can no longer create real `cycle_failed` Neon rows
   -- prevented structurally, not cleaned up after. Proof:
   `test_run_cycle_or_halt_halts_without_touching_cursor_semantics`
   asserts the FAKE logger (not the production one) received the exact
   failure event.
4. **Added a real lifecycle regression test.** `test_real_lifecycle_checkpoint_commit_then_best_effort_observability`
   exercises the ACTUAL orchestration boundary (`run_day`,
   `_load_checkpoint`/`_save_checkpoint`/`_clear_checkpoint`,
   `_atomic_write_json`) with only the true I/O functions mocked --
   proves attempt 1 calls fake Hermes once and checkpoints; an essential
   journal failure halts before state advancement; a retry reuses the
   checkpoint with no second Hermes call and an idempotent (same
   `decision_id`) essential re-write; the atomic commit succeeds and the
   checkpoint clears only after; and a post-commit snapshot/activity
   failure is caught without halting, rolling back, or replaying.

**Explicitly deferred (documented, not implemented):** restricting live
Hermes's large Maverick tool surface; removing the separate
execution-simulator container; consolidating Google News RSS and Alpaca
news; removing snapshot/activity tables; reworking duplicated
daily-price fetching; SEC fundamentals, dynamic screening,
company-research states, or evaluator agents; any risk-rule or strategy
change.

**Exact verification results (2026-08-28):** all touched Python files
compile clean. `backtest-harness/test_execution_semantics.py` (100
checks, including the 4 new/rewritten regressions above), `test_anonymize.py`,
`test_quota_pacer.py`, `risk-checker/test_risk_checker.py`,
`trade-journal-mcp/test_decision_fields.py`, `test_idempotency.py`
(extended), `test_run_isolation.py` (real Neon, self-cleaning) -- all
PASS. Confirmed no test invoked a real Hermes call (item 3's fix,
verified with the trade-journal container both up and down). Training
state (`backtest_state.json`) did not advance -- still cursor
`2026-02-17`, zero trading days. No checkpoint file remains. All
backtest Neon tables confirmed empty before and after (found and
cleaned 1 stray `cycle_failed` row generated by a local test run made
BEFORE item 3's fix landed -- exactly the class of pollution item 3
prevents going forward). `git status` otherwise clean -- only the four
files this pass touched.

---

## Known limitations that genuinely remain

- **Live bracket orders** — `alpaca-mcp`'s `place_order` submits one
  order, no OCO/bracket; nothing rests a real stop at the broker live.
- **Broker partial fills** — an involuntary partial fill (fewer shares
  than requested by the venue) isn't modeled in either mode.
- **Circuit-breaker latching** — breakers re-evaluate fresh every cycle
  from live P&L, not latched for the rest of a halted period.
- **Survivorship bias** — the watchlist was screened using data from
  inside the exact backtest window; tuning-window P&L should be read as
  noise, not signal.
- **No benchmark series** — no S&P MidCap 400 comparison line wired in.
- **Neon's anonymous-role exposure** and the **pgbouncer statement-cache
  fix's narrow scope** (only `trade-journal-mcp` — any other future
  service talking to the same pooled Neon endpoint via `asyncpg` would
  need the same `statement_cache_size=0` fix) — both documented, open by
  design/scope.
- **Open questions worth a second opinion**, deliberately unresolved
  (settle against real tuning-window results, not theory): risk-per-trade
  (1% vs. a 5% comparable system), the weekly circuit breaker (−6%,
  RULES.md's own weakest-grounded number), the just-revised time stop
  (10→15 trading days), total exposure cap (60%) and max positions (8).
  See `RULES.md` §10 for the full list.
  **Early tuning-window evidence on §6b (2:1 minimum reward:risk)**,
  observed 2026-08-29, run `26f1d371-08db-4885-ab24-b42200792fb7`, 83
  real trading days elapsed (window day 120/179), **zero fills**: only 4
  proposals were made in total, all 4 rejected. Three of four missed the
  2.0 floor by a hair (ALGM 1.95, ATI 1.97, DINO 1.92 — ATI also
  oversized, §2b 15.70% vs. 10%); the fourth (SFM) passed decision-time
  review, was queued, and then failed at real next-day execution when
  its reward:risk collapsed to 0.91 against the actual open. No rule
  changed — flagged here as the evidence this open question already
  asked for, not a recommendation. Worth revisiting once more of the
  window has run; a single early stretch could still be this specific
  slice of the market, not necessarily the threshold.

**Ground rule for any of the above:** `RULES.md`'s hard limits are not up
for negotiation via "better performance" arguments alone — even Hermes
itself is barred from loosening one through its own reflection. Any
proposed change to a hard number needs to go through the user explicitly.

---

## File map

| File | What it is |
|---|---|
| `RULES.md` | The authoritative, locked rules — position sizing, caps, circuit breakers, prohibitions |
| `AGENTS.md` | Hermes' operating procedure (the cycle, step by step) |
| `SOUL.md` | Hermes' identity/temperament |
| `backtest-harness/run_backtest.py` | The backtest harness/orchestrator |
| `backtest-harness/backtest_state.json` | Local, gitignored — current in-progress backtest portfolio |
| `backtest-harness/quota_pacer_state.json` | Local, gitignored — real weekly invocation count |
| `risk-checker/risk_checker.py` | Deterministic rule enforcement, shared by live and backtest |
| `trade-journal-mcp/`, `alpaca-mcp/`, `maverick-mcp/`, `google-news-rss-mcp/`, `execution-sim-mcp/` | The five tool containers |
| `dashboard/index.html` | The local-only dashboard frontend |
| `docker-compose.yml` | Full stack topology, pinned image digests |
| `watchlist/watchlist.json` | The fixed 10-ticker eligible universe |
| `docs/history/HANDOFF_FULL_2026-08-28.md` | Full dated repair history, test transcripts, superseded numbers — consult only when a task needs that detail |
| `PROJECT_FRESH_START.md` | From-zero, code-verified architecture explainer |

---

## Exact next step

### 2026-08-29 plugin-guided pre-training audit

The TDD/diagnosis pass found and repaired two first-party boundary defects,
then a follow-up code review caught and fixed one over-broad condition in
the first:

- `google-news-rss-mcp` now fails closed when its RSS fetch/parser
  genuinely fails (a bad HTTP status, or a malformed feed that also came
  back with zero entries); an unavailable veto feed can no longer
  masquerade as "no adverse news." The initial fix over-triggered on
  feedparser's `bozo` flag alone, which real Google News RSS responses
  set routinely even when entries parse fine — narrowed to only fail
  closed when the feed ALSO returned nothing, or it would have silently
  vetoed almost every live entry. `AGENTS.md` explicitly tells live
  Hermes to skip an entry when the feed errors.
- Trade-journal mode isolation is enforced at write and read boundaries:
  live decisions/results are tagged `alpaca` immediately, backtest decisions
  are tagged `backtest` when queued, and live history/reflection calls cannot
  read backtest rows.

Regression coverage lives in `google-news-rss-mcp/test_news_fetch.py`
(three cases: success, genuine failure, and bozo-with-real-entries-is-not-
a-failure) and `trade-journal-mcp/test_mode_isolation.py` (two cases,
both pure/mocked, no Neon). Verified 2026-08-29: both files' own suites
pass, plus the existing `test_idempotency.py`, `test_run_isolation.py`,
`test_decision_fields.py`, and `test_dashboard_fields.py` (real,
self-cleaning Neon) still pass unchanged against the new `execution_mode`
field. All are plain `python test_*.py` scripts, not pytest. Both
containers rebuilt from the current source, restarted, and confirmed up
with clean startup logs; all backtest Neon tables confirmed empty before
and after.

### 2026-08-29 correctness pass #4

A pre-training audit found 10 verified backtest-path defects where the
harness/journal/dashboard could silently produce wrong state, or fail by
crash instead of a named error. No rule, architecture, or live-path
changes.

**Ten repairs (`backtest-harness/run_backtest.py` unless noted):**
1. `stopped_out_today` (§9.4 same-day re-entry) now resets unconditionally
   every real trading day, not only inside `if needed:` — a stop-out no
   longer blocks re-entry on days after the one it happened on. Also
   persisted in `backtest_state.json` (was omitted entirely).
2. `make_decision_id`'s `kind` now includes each decision's own position
   in the response's decisions list — two identical decisions in one
   Hermes reply (same ticker/action) no longer collide on one
   `decision_id`; a rejected-then-filled identical pair now journals both
   outcomes independently. Stable under a checkpointed replay (same list,
   same positions, same ids).
3. `if not bars:` now cross-checks Alpaca's real trading calendar
   (`alpaca-mcp`'s new `_fetch_calendar_day`, same container-call
   mechanism as every other tool call) before deciding holiday vs.
   outage: a real trading day with no bars now halts the cycle like any
   other failure; a confirmed holiday/weekend still skips; a calendar
   source itself unavailable fails closed with a named message.
4. `require_intraday_coverage` now requires the FULL 15-minute session
   grid (`_expected_session_bar_times`, derived from the real session's
   own open/close — including a genuine half day) for every needed
   ticker, not just the 09:30/09:45 boundaries — a feed silently missing
   afternoon bars now fails closed instead of letting the walk stage
   iterate a truncated feed and the close mark trust a stale last bar.
5. `--start`/`--end` are now persisted in `backtest_state.json`; a
   resumed run with a conflicting value fails loudly (`SystemExit`,
   names all four values) before any fetch or Hermes call, mirroring
   `_resolve_seed`'s style (`_resolve_window`).
6. `quota_pacer.py`'s `record_cycle` now raises a named `RuntimeError`
   ("quota state unreadable...") on a corrupt state file instead of
   crashing with a bare `AttributeError` — this fires AFTER a real Hermes
   call already happened, so the failure needs to be legible.
   `can_run_cycle` already correctly reported closed in this case.
7. `trade-journal-mcp/server.py`: `data_timestamps` empty-dict/None
   asymmetry fixed — the write path already collapsed `{}` to `NULL`;
   the compare path (`_json_equal`) now does too
   (`_normalize_data_timestamps`), so an identical retry carrying `{}`
   is a genuine no-op instead of a false payload-mismatch.
8. `_decision_field_mismatches`'s confidence comparator is now
   None-safe (`_int_equal`) like every other comparator there — a
   collision against a NULL-confidence row reports a named mismatch,
   not a crash.
9. `record_exit` now journals mechanical exits (stop/target/time-stop)
   with `confidence=None` instead of a fabricated `confidence=5` —
   `_validate_decision_fields` skips all report-card checks when
   `confidence is None`. Stops polluting the confidence-calibration
   dataset with non-decisions; the dashboard's calibration view already
   reads from the entry side, unaffected.
10. `dashboard/index.html`: backtest mode with zero snapshots (no
    current run to scope to) now renders an explicit "no run scoped"
    empty state and skips the data queries entirely, instead of running
    them unfiltered and silently mixing every past discarded run's rows
    together. Live mode's unfiltered fallback is unchanged (live has no
    run_id concept).

**Hygiene (same pass):** `execution-sim` now has its own bridge network
(`net-execution-sim`), no longer sharing `net-execution` with
`alpaca-mcp` — the hub-and-spoke claim is now actually true for those
two. `alpaca-mcp` now gets explicit `environment:` for only its two
`ALPACA_*` vars instead of `env_file: .env` (was also handed
`NEON_DATABASE_URL` it never uses). `google-news-rss-mcp` fetches with
an explicit ~10s timeout (`_fetch_feed_bytes`) instead of an unbounded
`feedparser.parse(url)`. `trade-journal-mcp/schema.sql`'s `CREATE
POLICY` statements are now guarded (`pg_policies` existence check) so
the file is safely re-runnable — **not re-applied to Neon**, the
policies already exist there. `dashboard/index.html`: the blocked-
activity pagination query now has an explicit `order=`; `esc()` now
also escapes quotes. `trade-journal-mcp/test_idempotency.py`'s inline
execution-result test is now wrapped in `try/finally` so a failed
assertion can't strand a row in Neon.

**Regression tests:** items 1-5 in `backtest-harness/
test_execution_semantics.py` (9 new tests: stop-out reset/persistence,
duplicate-decision-id disambiguation + behavioral proof, outage-vs-
holiday, session-grid + truncated-feed coverage, `--start`/`--end`
resume/mismatch); item 6 in `test_quota_pacer.py`; items 7-9 in the
journal tests (`test_idempotency.py`: empty-`data_timestamps` no-op,
NULL-confidence collision; `test_decision_fields.py`: confidence=None
skips validation); item 10 verified live in a browser (no test
framework exists for the dashboard) — see below.

**Exact verification results (2026-08-29):** all touched files compile
clean. `backtest-harness/test_execution_semantics.py` (125 checks,
including all 9 new item 1-5 regressions), `test_anonymize.py`,
`test_quota_pacer.py` (including the new item-6 test),
`risk-checker/test_risk_checker.py`, `execution-sim-mcp/test_fill_logic.py`,
`alpaca-mcp/test_account_math.py`, `test_status_mapping.py`,
`test_historical_news.py` (real Alpaca API), `maverick-mcp/tests/
market_data/test_fetchers.py` (22 tests), and the full trade-journal
suite (`test_decision_fields.py`, `test_idempotency.py` extended,
`test_run_isolation.py`, `test_dashboard_fields.py`, `test_mode_isolation.py`
— real, self-cleaning Neon) — all PASS. The new `alpaca-mcp` calendar
path (`_fetch_calendar_day`/`fetch_trading_calendar_day`) verified
end-to-end against the real Alpaca API: a real trading day, a weekend,
and a real holiday all resolved correctly. `docker compose config -q`
valid; `google-news-rss-mcp` and `alpaca-mcp`/`execution-sim` rebuilt/
recreated for the network and Dockerfile changes, all confirmed up with
clean startup logs. Baseline untouched: `backtest_state.json` still
cursor `2026-02-17`, zero trading days, no checkpoint file; quota state
unchanged (`{"2026-W34": 34, "2026-W35": 6}`); all backtest Neon tables
confirmed empty before and after. Dashboard checked live in a browser:
Training mode with zero snapshots shows the new "no run scoped" empty
state (status line: `BACKTEST · NO RUN SCOPED`); Live mode's normal
unfiltered fallback is unchanged (`LIVE · LIVE @ NEON · updated ...`);
zero console errors in either mode. `git status` shows only the 13
files this pass touched.

**Remaining blocker: none.**

### 2026-08-30 round-2 prep — judgment-layer fixes, geometry diagnostic, counterfactual engine

Run 1 (`26f1d371-08db-4885-ab24-b42200792fb7`, 125 cycles, zero fills — see
`backtest-harness/run1_evaluation_extract.md`) showed flawless infrastructure and
judgment-layer failures: every proposal aimed at the bare-minimum §6b R:R (1.92–2.00) and
died to rounding or a next-day gap; two proposals breached the §2b concentration cap by
skipping the smaller-of-two sizing check; 25 reflections on zero closed trades entrenched
a no-trade prior; no-action reasons cited "location" without a measured distance. No
RULES.md number changed — every fix below is judgment-layer (`AGENTS.md`) or offline
analysis.

**Part A — `AGENTS.md` edits** (five, all inside the existing entry/reflection procedure):
R:R headroom doctrine (aim ~2.5:1, a ratio that only clears by rounding is treated as a
fail — cites SFM's real 2.00→0.91 collapse); a gap stress-test before proposing; a
mandatory dual sizing check (state both the stop-based and concentration-cap sizes
explicitly — cites ATI's 15.70% and DINO's 19.80% real breaches); a reflection guard for
`sample_size=0` (process-only, no strategy conclusions from zero outcomes); quantified
location rejections (a measured % distance, not "pressed into resistance").

**Part B — `backtest-harness/geometry_scan.py`** (new, read-only, human-eval-only):
classifies every (day, ticker) pair in the real window by whether maverick's own real
support/resistance algorithm offered a 2:1-or-better setup with a valid 5–10% stop.
Real result: **79.6% GEOMETRY-VALID, 20.4% CLOSE, 0% INVALID** across all 1,250 pairs —
but see the report's methodology note: maverick's synthetic ±5%/±10% resistance/support
levels make "geometry exists" true almost by construction, verified/corrected against
SFM's real ground-truth trade (an initial naive "nearest resistance" definition classified
100% of the window as INVALID, which is provably wrong).

**Part C — `backtest-harness/counterfactual.py`** (new, read-only, human-eval-only):
mechanically replays real fill/exit rules (same functions `run_backtest.py` uses,
imported not reimplemented) against SFM's real proposal and all 995 GEOMETRY-VALID pairs.
Real result: SFM would have stopped out at **-1.00R**; the 995 geometry-valid population
resolved at a **24.5% win rate, average -0.11R** — well below 2:1's ~33% breakeven. Reading:
**Hermes' caution in run 1 was directionally justified** — the geometry maverick's simple
algorithm flags as "available" mostly does not hold up as real resistance. (ALGM/ATI/DINO's
exact stop/target were never journaled — decision-time rejections don't persist geometry —
reported honestly as unreplayable rather than fabricated.)

Full numbers, methodology, and the real bug caught mid-build (a cycle-date-mapping
off-by-one between `agent_activity` events and `trades` rows, since `cycle_start`
narration is written at the END of its own cycle, not the start) are in
`backtest-harness/run2_prep_analysis.md`. New tests: `test_geometry_scan.py` (28 checks,
including a ground-truth regression against SFM's real trade) and
`test_counterfactual.py` (32 checks, including a regression for the date-mapping fix) —
both plain-python, no network/Neon/Hermes/Docker, all PASS.

**Verification:** both new modules run against real data for the full window (1,250
geometry pairs, 995 counterfactual simulations, 0 fetch errors). `docker compose config
-q` valid. No RULES.md/`risk_checker.py`/`run_backtest.py` enforcement-path changes.
`backtest_state.json`/`quota_pacer_state.json` untouched — no run started, resumed, or
reset. Neon: SELECT-only, scoped to run 1's own `run_id`, to recover its 5 proposals' real
dates/stop/target.

**Remaining blocker: none.**

**Start the controlled training run only after explicit human approval.**
The stack is training-ready — every repair through 2026-08-29 passed its
own tests, the baseline above is verified clean, and no blocking issue is
currently known. Nothing in this file authorizes starting it.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
