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

**Approved aggressive Phase 1 profile — implemented and synchronized.**
Rather than stay conservative, `RULES.md`, `risk_checker.py`, `AGENTS.md`,
and `run_backtest.py` now all encode looser numeric limits together (1.5:1
base / 1.25:1 escalated min R:R, 2% risk/trade, 20% max position, cash-only
exposure with no margin, no position-count cap, 3–12% stops, 5-trading-day
time stop, only a −30% drawdown breaker, no losing-streak de-risking, no
market-hours entry buffers) plus a deterministic, harness-controlled
activity-escalation mechanism (≥5 completed trades per 20-trading-day
block, or the next block's floor drops to 1.25:1 — approved by the user in
advance, never a quota Hermes sees or chases). Full profile, mechanism, and
what stays mandatory regardless (lookahead prevention, execution/slippage
mechanics, journaling, anti-fabrication): **`docs/PHASE1_AGGRESSIVE_DESIGN.md`**.

**`SOUL.md` (Phase 2) and this numeric profile are now in sync.** The
prior conservative temperament ("comfortable doing nothing... most cycles
should end with no trade") was replaced with an active one: search every
eligible ticker each cycle, ordinary confidence is sufficient to act,
uncertainty alone is not a veto, a no-trade cycle is valid only when every
candidate has a concrete rejection reason. Honesty, evidence discipline (no
strategy lessons from a tiny/empty sample), and prompt-injection resistance
are unchanged. The prior version is preserved at
**`docs/history/SOUL_phase1_conservative_2026-08-30.md`**.

**No new training run is authorized.** Implementing the profile is not
approval to start Round 2 — see "Exact next step" below for what actually
remains.

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
  reflection but may **never** loosen a limit — the one deliberate,
  human-approved exception is the §6b activity-escalation mechanism (see
  `docs/PHASE1_AGGRESSIVE_DESIGN.md`), which is harness-controlled, not
  Hermes' own reasoning. **`RULES.md` now encodes the aggressive Phase 1
  profile, synchronized with `risk_checker.py`, `AGENTS.md`, and
  `run_backtest.py`** — see "Current state" above.
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
- **Round 1's own real, complete evidence on the (now-superseded)
  conservative §6b 2:1 minimum** — see "Current state" above and
  `docs/evaluations/run1_evaluation_extract.md` /
  `docs/history/round2-conservative-draft/run2_prep_analysis.md` for the
  full numbers. That analysis argued against loosening §6b in isolation;
  the implemented Phase 1 profile loosens it anyway (to 1.5:1 base /
  1.25:1 escalated), alongside several other numbers, as a deliberate bet
  that more real evidence is worth more than this one number staying
  conservative — see `docs/PHASE1_AGGRESSIVE_DESIGN.md`.
- **Open questions worth a second opinion once Phase 1's own real results
  exist** — the same `RULES.md` §10 questions from before (risk-per-trade,
  the old weekly circuit breaker's grounding), now reframed: Phase 1 chose
  a direction on several of them (removing the weekly/daily breakers
  entirely, raising risk-per-trade to 2%) as a deliberate experiment, not
  as a resolution — see `RULES.md` §10 for the full, honest accounting of
  what each change costs.

**Ground rule for any of the above:** `RULES.md`'s hard limits are not up
for negotiation via "better performance" arguments alone — even Hermes
itself is barred from loosening one through its own reflection. Any
proposed change to a hard number needs to go through the user explicitly.

---

## File map

| File | What it is |
|---|---|
| `RULES.md` | The authoritative, locked rules — position sizing, caps, circuit breaker, prohibitions. **Implements the aggressive Phase 1 profile**, synchronized with `risk_checker.py`/`AGENTS.md`/`run_backtest.py` |
| `AGENTS.md` | Hermes' operating procedure (the cycle, step by step). **Reconciled for Phase 1/Phase 2** — active-search language, no soft R:R target beyond RULES.md's own, quota-free framing of the strategy-profile prompt line |
| `SOUL.md` | Hermes' identity/temperament — Phase 2 (active/experimental), in sync with the numeric profile |
| `backtest-harness/run_backtest.py` | The backtest harness/orchestrator. Owns the entire activity-escalation mechanism (`_maybe_advance_activity_block`, `_record_trade_completion`, `_limits_for_profile`) |
| `backtest-harness/geometry_scan.py`, `test_geometry_scan.py` | Executable offline diagnostic tooling (not a draft) — geometry classification against real market data |
| `backtest-harness/counterfactual.py`, `test_counterfactual.py` | Executable offline diagnostic tooling (not a draft) — mechanical counterfactual trade replay |
| `backtest-harness/backtest_state.json` | Local, gitignored — current in-progress backtest portfolio, now also carrying `profile_level`/`block_start_trading_day`/`block_completed_trades` |
| `backtest-harness/quota_pacer_state.json` | Local, gitignored — real weekly invocation count |
| `risk-checker/risk_checker.py` | Deterministic rule enforcement, shared by live and backtest. **Implements the aggressive Phase 1 profile** — `RiskLimits`' defaults are the base tier; escalation is applied per-cycle by the harness |
| `trade-journal-mcp/`, `alpaca-mcp/`, `maverick-mcp/`, `google-news-rss-mcp/`, `execution-sim-mcp/` | The five tool containers. `trade-journal-mcp/schema.sql` carries the additive `trades.strategy_profile`/`trades.min_reward_risk_used` migration |
| `dashboard/index.html` | The local-only dashboard frontend. Shows the Phase 1 profile/threshold as an unobtrusive tooltip on open positions when present |
| `docker-compose.yml` | Full stack topology, pinned image digests |
| `watchlist/watchlist.json` | The fixed 10-ticker eligible universe |
| `docs/evaluations/run1_evaluation_extract.md` | Full read-only extraction of Round 1's real results (125 cycles, zero fills) |
| `docs/PHASE1_AGGRESSIVE_DESIGN.md` | The aggressive profile — numbers, the escalation mechanism, and what stays mandatory regardless. **Implemented**, not just a design |
| `docs/history/round2-conservative-draft/run2_prep_analysis.md` | Archived — the superseded conservative Round-2 direction (geometry/counterfactual numbers, AGENTS.md rationale) |
| `docs/history/SOUL_phase1_conservative_2026-08-30.md` | Archived — `SOUL.md` as it stood through Round 1, before the Phase 2 rewrite |
| `docs/history/HANDOFF_FULL_2026-08-28.md` | Full dated repair history, test transcripts, superseded numbers from the pre-Round-1 correctness passes — consult only when a task needs that detail |
| `PROJECT_FRESH_START.md` | From-zero, code-verified architecture explainer — updated for the Phase 1 numbers where it described specifics |

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

### 2026-08-30 — Phase 1 aggressive profile implemented, synchronized

Used the `implement` skill. Every active layer now agrees: `RULES.md`,
`risk_checker.py`'s `RiskLimits`, `AGENTS.md`'s procedure, and
`run_backtest.py` all encode the same numbers together, in one change —
not piecemeal.

**Corrected factual errors:** `SOUL.md` and `docs/PHASE1_AGGRESSIVE_DESIGN.md`
both previously said Round 1 produced one filled trade; corrected to the
verified real result — 125 cycles, zero filled trades (one entry, SFM,
reached the queue and was rejected at execution).

**Synchronized the profile** across the four files above: 1.5:1 base /
1.25:1 escalated min reward:risk, 2% risk budget, 20% max position,
cash-only exposure with no margin (position-count and daily-entry limits
removed outright), 3–12% stops, 5-trading-day time stop, only a −30%
drawdown breaker (daily/weekly breakers and losing-streak de-risking
removed), no market-hours entry buffers. One open position per ticker,
sentiment-as-veto-only, and every lookahead/masking/execution/slippage/
missing-data/journaling/anti-fabrication guarantee are all unchanged.

**Implemented the deterministic activity-escalation mechanism**
(`backtest-harness/run_backtest.py`): fixed 20-trading-day blocks, a
completed-trade count (filled entry → filled exit only — rejections and
queued-only proposals never count), automatic escalation to the 1.25:1
tier when a block completes below 5, never reversing, never mid-block.
State (`profile_level`, `block_start_trading_day`, `block_completed_trades`)
persisted in `backtest_state.json`, restored exactly on resume. A queued
order stores the exact tier/threshold binding when it was decided
(`QueuedOrder.strategy_profile`/`min_reward_risk_used`) and
`resolve_queued_order` uses that stored value at execution, never
whatever's "current" that day. Hermes' prompt shows only the currently
binding threshold and profile label — never a trade count or anything
that reads as a quota.

**Recorded the profile structurally**: additive `trades.strategy_profile`/
`trades.min_reward_risk_used` columns (`ALTER TABLE ... ADD COLUMN IF NOT
EXISTS`, applied to Neon), populated on every new Round-2 decision,
included in idempotency collision comparison, exposed unobtrusively on the
dashboard (a tooltip, not a new column) when present.

**Reconciled `AGENTS.md`**: removed the superseded conservative Round-2
soft 2.5:1 R:R target and the 3%-gap stress-test veto; kept explicit
dual-sizing calculations, quantified rejection reasons, and the reflection
guard for tiny/empty samples; added active-search framing (an ordinary
setup that clears the rules deserves action, confidence 3 is sufficient,
multiple entries per cycle are normal, a no-trade cycle needs a concrete
reason per candidate) and explained the strategy-profile prompt line as
quota-free and harness-controlled.

**Updated `docs/PHASE1_AGGRESSIVE_DESIGN.md`** from "approved design" to
implemented status, adding the previously-omitted removals (losing-streak
de-risking, market-hours buffers) and describing escalation as automatic,
deterministic, and approved in advance. Updated `PROJECT_FRESH_START.md`
only where its numeric/behavioral description had gone stale (circuit
breaker, stop band, reward:risk floor).

**Exact verification (2026-08-30):** all touched Python files compile
clean. `risk-checker/test_risk_checker.py` (rewritten for the new profile,
all PASS), `backtest-harness/test_execution_semantics.py` (existing suite
updated for the new numbers plus 9 new activity-escalation regressions,
all PASS), `test_quota_pacer.py`, `test_anonymize.py` (unaffected, PASS).
Trade-journal suite rebuilt and run inside the container:
`test_decision_fields.py`, `test_idempotency.py` (extended with a new
profile-field collision + NULL-backward-compat regression),
`test_mode_isolation.py`, `test_run_isolation.py`, `test_dashboard_fields.py`
— all PASS against real, self-cleaning Neon. `docker compose config -q`
valid. The additive Neon migration applied cleanly (verified via real
`information_schema` query); Round 1's own single trades row remains
readable with both new fields NULL. Dashboard smoke-checked live in a
browser (Training view, real Round 1 data) — zero console errors, tooltip
addition inert on pre-Phase-1 rows exactly as designed. `git diff --check`
clean. `backtest_state.json`/`quota_pacer_state.json` confirmed untouched
(unmodified timestamps, gitignored either way); no run started, resumed,
or reset.

**Follow-up fix, same day — day-20 escalation boundary and legacy resume.**
The implementation above shipped a real bug: `active_min_rr`/`active_limits`
were computed once at the TOP of `run_day`, before that day's block
transition could run — so on the exact trading day a block escalates,
`working.profile_level` flipped to `aggressive_v1_escalated` but that
evening's own new decisions were still enforced/journaled against the
stale base 1.5, a real profile/threshold mismatch. Root cause fixed by
re-deriving `limits_ctx`/`active_limits`/`active_min_rr` from
`working.profile_level` a second time, immediately after
`_maybe_advance_activity_block` runs — so the prompt, the decision-time
check, the queued order, and its journaled fields all see the new tier
the instant it takes effect, for every decision made later that same
cycle. A previously-queued order is unaffected (it already resolved
earlier in that same `run_day` call, against its own stored threshold,
before the boundary check runs at all).

Also fixed `Portfolio.from_dict`'s legacy-resume handling: a pending order
restored from a pre-Phase-1 state file now gets an explicit
`PROFILE_LEGACY`/2.0 label (it was really checked against the FORMER 2.0
minimum, not the new 1.5 base — reporting it as the base tier would have
misrepresented what it was actually validated against); and a legacy
portfolio that already has real `trading_days_elapsed` history but
predates block tracking now starts its first activity block AT that
stored elapsed-day value, not at day 0 (which would have counted
pre-Phase-1 history toward the very first block's 20-day window). A
genuinely present `block_start_trading_day` — including a real 0 from a
freshly-created Phase 1 `Portfolio()` — is always used as-is, never
overridden by this fallback.

Also corrected a misleading test docstring:
`test_checkpoint_replay_cannot_double_count_or_re_escalate` previously
implied `_record_trade_completion` was itself idempotent under double-
invocation. It is not — it is a plain `+= 1`, and the test's own body
proves two calls really do produce a count of 2. Checkpoint-replay safety
comes entirely from `run_day`'s working-copy-and-commit-only-on-success
architecture, not from this function deduplicating anything.
`_maybe_advance_activity_block` genuinely is safe to call twice for the
same day (guarded by its own boundary check) — the docstring now
distinguishes the two claims instead of conflating them.

Corrected `docs/PHASE1_AGGRESSIVE_DESIGN.md`: only accepted (queued)
decisions get a real `trades` row with `strategy_profile`/
`min_reward_risk_used`; a decision-time rejection is never a trade row at
all, only a tagged `agent_activity` line
(`[profile=... min_rr=...]`). Also documents precisely when the following
block's profile becomes active: inside `run_day`, after that day's exits/
fills resolve but before that same evening's new decisions are considered.

**Exact verification of the fix:** all touched Python files
(`backtest-harness/run_backtest.py`, `test_execution_semantics.py`)
compile clean. The full `test_execution_semantics.py` suite — 158 checks,
including one new real-orchestration-boundary regression that drives
`run_day()` itself (not the pure helpers in isolation) across the exact
day-20 transition, proving pre-boundary stays base/1.5 everywhere,
post-boundary is escalated/1.25 everywhere (prompt, decision-time check,
journaled queue call, and the resulting `QueuedOrder` object), and a
previously-queued order keeps its own stored 1.5 across that same
transition — plus 6 new legacy-resume regressions
(`test_reset_returns_to_base_resume_preserves_the_tier`, extended) — all
PASS. `git diff --check` clean; `git status` shows only the two touched
files (`run_backtest.py`, `test_execution_semantics.py`) plus this
handoff and the design-doc correction.
`backtest_state.json`/`quota_pacer_state.json` confirmed untouched. No
run started, resumed, or reset. `RULES.md`, `AGENTS.md`, `SOUL.md`,
`risk_checker.py`, the journal schema/server, and the dashboard were not
touched by this fix — out of scope, and untouched.

**Remaining blocker: none for the implementation itself.**

## Exact next step

**The synchronized implementation, including the day-20 boundary fix, is
done and verified. Round 2 itself still requires separate, explicit human
approval before it starts** — implementing the profile is not that
approval. Nothing in this file authorizes starting it. When approved: a
fresh backtest run (`--reset`, a new `run_id`) under the now-synchronized
`RULES.md`/`risk_checker.py`/`AGENTS.md`/`SOUL.md`, tracked against the
six-month target of 30–60 completed trades
(`docs/PHASE1_AGGRESSIVE_DESIGN.md`).

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
