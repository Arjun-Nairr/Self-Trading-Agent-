# Handoff briefing — Hermes trading agent

**Read this if you are an AI assistant being brought in to discuss or advise
on this project** (Claude, ChatGPT, OpenCode, or otherwise). The working
split: **discussion/strategy/second opinions happen in chat; Claude Code
handles execution** — reading/editing code, running the harness, touching
Neon, committing. Don't propose direct code edits as if you'll apply them
yourself; propose the *decision*, and it gets relayed to Claude Code to
implement and verify.

**Context control:** read this compact handoff first, then only the files
directly relevant to the task you're assigned. Do not load the historical
archive (`docs/history/`), vendored third-party code (`maverick-mcp/`),
local backups, generated/state files, or unrelated test files unless the
task specifically requires them.

---

## Current state — read this first

**Round 1 has completed: 125 cycles, zero filled trades.** A full six-month
backtest (`run_id 26f1d371-08db-4885-ab24-b42200792fb7`, window
2026-02-17 → 2026-08-14) ran to its natural end under the locked model and
the then-current (conservative) rules and temperament. Diagnosis:
infrastructure was flawless; the failures were judgment-layer — every real
proposal aimed at the bare-minimum §6b reward:risk (1.92–2.00) and died to
rounding or a next-day price gap, two proposals breached the §2b
concentration cap, 25 reflections against zero closed trades entrenched a
no-trade prior. Full read-only extraction:
**`docs/evaluations/run1_evaluation_extract.md`**.

**A first, conservative Round-2 response was drafted and is now archived.**
It kept every `RULES.md` number and fixed judgment purely through
`AGENTS.md` edits, plus an offline geometry/counterfactual diagnostic
(79.6% of setups had 2:1-or-better geometry by maverick's own algorithm,
but a mechanical replay of them won only 24.5% of the time, avg -0.11R —
reading: Round 1's caution was directionally justified, not simply
over-cautious). This direction was **superseded before Round 2 started** by
the decision below. Preserved for comparison, not as current guidance:
**`docs/history/round2-conservative-draft/run2_prep_analysis.md`**.

**Approved direction: an aggressive Phase 1 profile, implementation
pending.** Rather than stay conservative, the approved next step loosens
several numeric limits (1.5:1 min R:R, 2% risk/trade, 20% max position,
cash-only exposure limit, 5-trading-day time stop, only a −30% drawdown
breaker, an activity target of ≥5 trades/20 days) specifically to generate
more real trading evidence — Round 1's near-total inactivity produced too
little evidence to judge the rules by. Full profile, and what stays
mandatory regardless (lookahead prevention, execution/slippage mechanics,
journaling, anti-fabrication): **`docs/PHASE1_AGGRESSIVE_DESIGN.md`**.
**This is a design only — no enforcement file has been changed to match it
yet.**

**`SOUL.md` has been rewritten for this direction (Phase 2) and is
complete.** The prior conservative temperament ("comfortable doing
nothing... most cycles should end with no trade") is replaced with an
active one: search every eligible ticker each cycle, ordinary confidence is
sufficient to act, uncertainty alone is not a veto, a no-trade cycle is
valid only when every candidate has a concrete rejection reason. Honesty,
evidence discipline (no strategy lessons from a tiny/empty sample), and
prompt-injection resistance are unchanged. The prior version is preserved
at **`docs/history/SOUL_phase1_conservative_2026-08-30.md`**.

**`RULES.md`, `risk_checker.py`, and `AGENTS.md` still encode the
conservative numbers.** `SOUL.md`'s temperament and the enforcement layer
are currently out of sync — that reconciliation is the actual next step,
below. **No new training run is authorized.**

---

## What this project is

Hermes: an LLM-driven agent that trades US equities — **long only, paper
account, swing-trading horizon** (days to weeks) — reasoning over
technicals and news, gated by a hard-coded risk checker before anything
executes. Two modes: **backtest** (point-in-time historical simulation,
identity-masked so the model can't recall what really happened) and
**live** (real Alpaca paper-trading API). **Round 1 (backtest) has run and
completed — see above. Live trading has never run.**

A separate personal study artifact, "The Hermes Ledger," exists as the
user's own optional deep-dive reading — it is **not** required context for
an assistant working on this project. Do not treat it as authoritative;
this file and the repo's own source (`RULES.md`, `AGENTS.md`, `SOUL.md`,
actual code) are.

---

## Locked model decision

**`openai-codex` / `gpt-5.4-mini` / medium reasoning — locked, and this is
what Round 1 actually ran.** Originally the tool's shipped default, not a
deliberate choice, but as of 2026-08-28 stated unambiguously: do not change
it without an explicit new decision.

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
run_backtest.py` fetches everything (technicals, news, held positions),
masks tickers/dates, and hands Hermes one prompt per simulated day. One
"cycle" = one simulated trading day, not one trade. Essential cycle data
(checkpointed Hermes response, portfolio state, trade decisions, risk
checks) commits atomically before any best-effort observability (snapshots,
activity narration) is attempted — a crash after commit can lose dashboard
detail but never duplicate a trade or a Hermes call. **A consequence worth
knowing when reading Neon timestamps:** activity narration, including each
cycle's own `cycle_start` row, is written as one deferred batch at the
*end* of its cycle — so a `cycle_start` timestamp is not the earliest event
of its own cycle; a `trades`-table row journaled mid-cycle can have an
*earlier* timestamp than its own cycle's `cycle_start`.

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
  reflection but may **never** loosen a limit. **No number in `RULES.md`
  has changed yet** — the approved aggressive Phase 1 profile
  (`docs/PHASE1_AGGRESSIVE_DESIGN.md`) is pending synchronized
  implementation here and in `risk_checker.py`; see "Current state" above.
- **`risk_checker.py`** enforces every numeric rule in code, independent
  of the model's own reasoning — pure functions, zero I/O, **fail-closed**
  on any unknown/non-finite/non-positive value.
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
  price — Round 1's SFM proposal is the real, observed example: it passed
  decision-time (self-reported R:R 2.00), then failed execution-time when
  the real next-day price collapsed it to 0.91.
- **One-lot model:** a discretionary SELL must close the entire position.
  Rejected outright at both decision and execution time. (Involuntary
  *broker* partial fills — the venue filling fewer shares than requested
  — remain a separate, unmodelled live-mode limitation, distinct from
  this.)

## Journaling, reconciliation, run isolation, quota

- **Queue-time journaling:** a decision is journaled the instant it's
  queued (status `'pending'`), not only once it fills. The *same* row is
  updated to `filled`/`rejected` on resolution — never a fresh row, never
  a fabricated fill.
- **Reconciliation (live only):** on restart, check every `'pending'`
  journal row against real broker state before doing anything else — the
  broker is always the source of truth for live positions.
- **Run isolation:** every backtest run gets a fresh `run_id` (uuid4,
  persisted in `backtest_state.json`, reused across a resume, never
  reused across a `--reset`). Every table carries it. A deterministic
  `cycle_id` per (run, simulated day) has a real database-level
  uniqueness constraint. Reflection retrieval and the dashboard are both
  scoped to the current run's `run_id`.
- **Quota:** `quota_pacer.py` counts real Hermes invocations against the
  real ISO calendar week, counted the instant Hermes is actually called
  (including a call that later fails) — the dashboard reads this real
  count directly, never a second estimate.

---

## Dashboard

Local-only: `docker compose up -d dashboard` → **`http://127.0.0.1:8842`**,
bound to loopback, never publicly deployed. Reads Neon's Data API with a
SELECT-only anonymous role. Shows real quota/token usage, real per-run
scoping, paginated trade history, and neutral risk-check observations.
**Documented, unaddressed exposure:** the anonymous role has no auth beyond
SELECT-only — anyone with the endpoint URL can read the same journal rows.

---

## Known limitations that genuinely remain

- **Live bracket orders** — `alpaca-mcp`'s `place_order` submits one
  order, no OCO/bracket; nothing rests a real stop at the broker live.
- **Broker partial fills** — an involuntary partial fill (fewer shares
  than requested by the venue) isn't modeled in either mode.
- **Circuit-breaker latching** — breakers re-evaluate fresh every cycle
  from live P&L, not latched for the rest of a halted period.
- **Survivorship bias** — the watchlist was screened using data from
  inside the exact backtest window; Round 1's P&L (there wasn't any —
  zero fills) can't speak to this either way yet.
- **No benchmark series** — no S&P MidCap 400 comparison line wired in.
- **Neon's anonymous-role exposure** and the **pgbouncer statement-cache
  fix's narrow scope** (only `trade-journal-mcp`) — both documented, open
  by design/scope.
- **Round 1's own real, complete evidence on §6b (2:1 minimum
  reward:risk)** — see "Current state" above and
  `docs/evaluations/run1_evaluation_extract.md` /
  `docs/history/round2-conservative-draft/run2_prep_analysis.md` for the
  full numbers. That analysis argued against loosening §6b in isolation;
  the approved Phase 1 profile loosens it anyway (to 1.5:1), alongside
  several other numbers, as a deliberate bet that more real evidence is
  worth more than this one number staying conservative — see
  `docs/PHASE1_AGGRESSIVE_DESIGN.md`.
- **Open questions worth a second opinion**, deliberately unresolved
  under `RULES.md`'s current (still-conservative) numbers: risk-per-trade,
  the weekly circuit breaker (`RULES.md`'s own weakest-grounded number),
  total exposure cap and max positions. See `RULES.md` §10 for the full
  list — several of these are exactly what the Phase 1 profile proposes
  to change, once implemented.

**Ground rule for any of the above:** `RULES.md`'s hard limits are not up
for negotiation via "better performance" arguments alone — even Hermes
itself is barred from loosening one through its own reflection. Any
proposed change to a hard number needs to go through the user explicitly.

---

## File map

| File | What it is |
|---|---|
| `RULES.md` | The authoritative, locked rules — position sizing, caps, circuit breakers, prohibitions. **Still the conservative Round-1 numbers** — Phase 1 implementation pending |
| `AGENTS.md` | Hermes' operating procedure (the cycle, step by step). Carries the conservative Round-2-draft judgment edits; not yet reconciled with the aggressive profile or Phase 2 `SOUL.md` |
| `SOUL.md` | Hermes' identity/temperament — **rewritten for Phase 2** (active/experimental), see "Current state" |
| `backtest-harness/run_backtest.py` | The backtest harness/orchestrator |
| `backtest-harness/geometry_scan.py`, `test_geometry_scan.py` | Executable offline diagnostic tooling (not a draft) — geometry classification against real market data |
| `backtest-harness/counterfactual.py`, `test_counterfactual.py` | Executable offline diagnostic tooling (not a draft) — mechanical counterfactual trade replay |
| `backtest-harness/backtest_state.json` | Local, gitignored — current in-progress backtest portfolio |
| `backtest-harness/quota_pacer_state.json` | Local, gitignored — real weekly invocation count |
| `risk-checker/risk_checker.py` | Deterministic rule enforcement, shared by live and backtest. **Still the conservative Round-1 numbers** — Phase 1 implementation pending |
| `trade-journal-mcp/`, `alpaca-mcp/`, `maverick-mcp/`, `google-news-rss-mcp/`, `execution-sim-mcp/` | The five tool containers |
| `dashboard/index.html` | The local-only dashboard frontend |
| `docker-compose.yml` | Full stack topology, pinned image digests |
| `watchlist/watchlist.json` | The fixed 10-ticker eligible universe |
| `docs/evaluations/run1_evaluation_extract.md` | Full read-only extraction of Round 1's real results (125 cycles, zero fills) |
| `docs/PHASE1_AGGRESSIVE_DESIGN.md` | The approved aggressive profile — numbers, and what stays mandatory regardless. Design only, not yet implemented |
| `docs/history/round2-conservative-draft/run2_prep_analysis.md` | Archived — the superseded conservative Round-2 direction (geometry/counterfactual numbers, AGENTS.md rationale) |
| `docs/history/SOUL_phase1_conservative_2026-08-30.md` | Archived — `SOUL.md` as it stood through Round 1, before the Phase 2 rewrite |
| `docs/history/HANDOFF_FULL_2026-08-28.md` | Full dated repair history, test transcripts, superseded numbers from the pre-Round-1 correctness passes — consult only when a task needs that detail |
| `PROJECT_FRESH_START.md` | From-zero, code-verified architecture explainer |

---

## Repair history (chronological, historical — not current state)

Pre-Round-1 correctness work. Kept here for provenance; none of it is a
pending action. Full narrative and exact test transcripts for the two
2026-08-28 passes: `docs/history/HANDOFF_FULL_2026-08-28.md`.

- **2026-08-28, second pass:** circuit breakers now recalculate fresh from
  real intraday equity at every entry point (previously frozen as of
  yesterday's close); failed-cycle retries made deterministic via a
  durable checkpoint; `decision_id` collisions now compare every
  decision-defining field; maverick's historical OHLC now matches
  Alpaca's raw (unadjusted) basis; queued-BUY risk checks now use the
  slippage-adjusted fill price; the masking seed is now persisted and
  resume-checked. 58+ new/extended regression checks, all PASS.
- **2026-08-28, third pass:** locked the **essential vs. best-effort**
  architectural split described under "Architecture" above; fixed a
  UUID-string comparison bug that false-failed identical retries; fixed
  sequential-same-cycle BUY accounting to use real (not stale) marks;
  isolated unit tests from real Neon/Docker via an injectable fail logger;
  added a full lifecycle regression test. 100 checks, all PASS.
- **2026-08-29, plugin-guided pre-training audit:** `google-news-rss-mcp`
  now fails closed on a genuine RSS fetch/parse failure (narrowed from an
  over-broad initial fix that would have vetoed most live entries); trade-
  journal mode isolation (live vs. backtest) enforced at write and read
  boundaries. New regression suites, all PASS.
- **2026-08-29, correctness pass #4:** ten verified backtest-path defects
  fixed — same-day re-entry blocking now resets correctly, duplicate-
  decision-id disambiguation within one Hermes reply, outage-vs-holiday
  detection via the real trading calendar, full-session intraday coverage
  checks, `--start`/`--end` persistence and resume-mismatch failure,
  quota-state corruption now fails loudly and legibly, `data_timestamps`
  empty/None comparison symmetry, None-safe confidence comparison,
  mechanical exits no longer journal a fabricated confidence score,
  dashboard empty-state handling for a zero-snapshot run. 125 checks plus
  the existing suites, all PASS. This was the last correctness pass before
  Round 1 was run.

**This section describes work completed before Round 1 ran.** It is not
evidence about the current state of the stack — see "Current state" at the
top of this file for that.

---

## Exact next step

**Phase 2's `SOUL.md` is done. The Phase 1 aggressive profile
(`docs/PHASE1_AGGRESSIVE_DESIGN.md`) is approved but not implemented
anywhere numeric.** The real next step is a synchronized implementation
pass, together, not piecemeal (a number changed in one place and not
another is silently unenforced — the same drift risk `risk_checker.py`
already warns about for the current numbers):

1. **`RULES.md`** — replace the conservative numbers (§2a/§2b/§3/§4/§6a/
   §6b/§6c) with the Phase 1 profile's values.
2. **`risk_checker.py`**'s `RiskLimits` — the same values, mirrored exactly
   as today, plus removing the position-count/daily-entry checks the
   profile drops and confirming the −30%-only breaker and the cash-only
   exposure check.
3. **`AGENTS.md`** — reconcile its still-conservative Round-2-draft
   judgment edits (R:R headroom aimed at the old 2.0 floor, sizing
   language keyed to the old caps) against both the new numbers and
   Phase 2's active `SOUL.md` temperament.
4. **The harness and its tests** — anything hardcoding the old numbers
   (`TIME_STOP_TRADING_DAYS`, breaker thresholds, etc.) plus new coverage
   for the activity-target tracking and escalation proposal mechanic.

**No new training run is authorized until that synchronized pass is done
and verified** — this document does not authorize starting Round 2 or any
backtest run.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
