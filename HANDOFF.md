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

**Start the controlled training run only after explicit human approval.**
The stack is training-ready — every repair through 2026-08-28 passed its
own tests, the baseline above is verified clean, and no blocking issue is
currently known. Nothing in this file authorizes starting it.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
