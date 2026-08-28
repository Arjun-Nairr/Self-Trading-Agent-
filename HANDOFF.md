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
`trades`, `agent_activity`, `portfolio_snapshots`, `insight_log`. Exactly
one live row exists project-wide: the AAPL manual integration-test
decision (`execution_mode: 'alpaca'`, `status: 'rejected'`, resolved
against real Alpaca broker state on 2026-08-28) — unchanged, verified
present.

**Dashboard**, verified live in a browser without running a Hermes cycle:
Training view shows zero cycles/trades/positions/snapshots; Live view is
unaffected and still shows the real AAPL decision. Zero console errors.

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
