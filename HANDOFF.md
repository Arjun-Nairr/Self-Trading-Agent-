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

**A 3-cycle Round 2 pilot ran (`run_id 97d112e8-...`), found a real
one-share position-sizing defect, and it was fixed.** A follow-up
`--reset` attempt (`run_id c55f0fc2-...`) then hit a **second, different
bug** — a real crash on cycle 1 (`ask_hermes`'s usage-token accumulator
threw on an explicit-`null` field). **That bug was fixed** (null usage
fields coerce to 0; regression test added), and a third `--reset` smoke
test (`run_id 1bf9af3d-...`) completed all 3 cycles mechanically clean —
but its own persisted reasoning showed **a third, more subtle defect**:
Hermes was sizing every BUY off the *superseded* 1% §2a risk budget, not
the currently-active 2% (the prompt never actually stated the real
percentages or formulas, so Hermes filled the gap from its own general
notion of a "typical" risk rule). Fixed by rendering the real, currently-
active `RiskLimits` values and formulas directly into every prompt. **A
fourth `--reset` smoke test (`run_id 81db75e5-f9e9-48df-8bf1-0a2f581d3db8`)
confirmed the fix** — every persisted BUY reasoning now explicitly computes
`§2a size = floor(100000*0.02/...)`, the real 2%.

**The separate, artificial 175-call weekly quota GATE has also been
removed** (user-approved; real usage never approached it — see below).
Local invocation counting is preserved for observability only; the
dashboard now shows a truthful "Hermes invocations this week: N," never a
`/175` denominator.

**None of the four run_ids above should be resumed** — the first three
are defective/superseded, and the fourth is a completed 3-cycle smoke
test, not a resumable partial run; the next attempt starts its own fresh
`run_id` via `--reset`. **Full training is safe to begin.** See the dated
sections near the bottom of this file for the complete story of all four
attempts, and "Exact next step" for what a full run needs before it
starts.

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

### 2026-08-30 — Round 2 started: controlled 3-cycle tranche, then paused

Used the `hermes-handoff` skill. Preflight confirmed before starting:
branch `round2-prep-judgment-layer` at `9146728`, working tree clean and
synced with origin; gateway locked to `openai-codex`/`gpt-5.4-mini`/medium
(verified in the real `config.yaml`/`auth.json`, not assumed); all
required Docker services healthy (`execution-sim` was down, started
before the run); real Neon connectivity confirmed; local state confirmed
to be Round 1's own completed, empty-portfolio end state (matching
`run_id 26f1d371-...`, cursor past `--end`, zero holdings/pending orders).

**Ran, from `backtest-harness`:**
`python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3 --reset`

- **New `run_id`: `97d112e8-57be-4632-a503-0417993cdf51`.**
- Simulated dates completed: 2026-02-17, 2026-02-18, 2026-02-19 (3 real
  trading days). Next cursor: `2026-02-20`.
- **Exit status: 0.** No cycle failed; nothing halted; no checkpoint left
  behind.
- Hermes calls: 3 (one per cycle, no retries). Tokens: 74,751 total
  (24,917 avg/cycle).
- End state: cash $100,000.00, equity $100,000.00 (+0.00%), **zero
  holdings, zero completed (closed) trades**, **two pending orders**
  carried into the next cycle (ATI x1 @ stop 145.00/target 168.12, BRX x1
  @ stop 27.20/target 31.60 — both queued on day 3, awaiting day 4's real
  open).
- Active profile: `aggressive_v1_base`, min reward:risk **1.5**,
  `block_start_trading_day=0`, `block_completed_trades=0` (3 of the
  block's 20 trading days elapsed — far short of a boundary; no
  escalation).
- **Real activity in 3 days already exceeded Round 1's entire 125-day
  total:** 5 proposals queued (DINO, ALGM day-1, ALGM day-2, ATI, BRX),
  3 rejected at real next-day execution — all for genuine §6a stop-
  placement issues (a stop landing above the real entry price twice; one
  stop 2.38% below entry against the new 3.0% floor once) — 2 still
  pending. Zero fills yet (expected — the two pending orders resolve on
  day 4, outside this tranche).

**Verification, scoped strictly to the new `run_id` via read-only Neon
queries:**

| Table / grouping | Result |
|---|---|
| `trades` by (status, action) | `pending`/BUY: 2, `rejected`/BUY: 3 |
| `agent_activity` by event type | `cycle_start`: 3, `order_queued`: 5, `order_rejected`: 3, `reasoning`: 6 |
| `insight_log` | 0 (no reflection cadence reached in 3 cycles — expected) |
| `portfolio_snapshots` | 3 |
| Distinct `cycle_id` | 3 |
| Distinct non-null `strategy_profile` | `aggressive_v1_base` only |
| Distinct non-null `min_reward_risk_used` | `1.5` only |

**Round 1 confirmed readable and completely unchanged** (re-verified
against `run_id 26f1d371-...`): `trades` 1, `agent_activity` 401,
`insight_log` 25, `portfolio_snapshots` 125 — identical to every prior
count, including the SFM row's stop/target values intact. No Round 1 row
was touched, deleted, or modified. `quota_pacer_state.json` advanced only
by the harness's own normal per-invocation counting (3 real calls this
week) — never reset or hand-edited.

**Dashboard smoke-checked live in a browser** against the new run: shows
"3 cycles recorded · 0 positions taken · 3 decisions blocked," the two
pending BUYs (ATI, BRX) with their real reasoning/R:R in "Recent
Decisions," §6a as the sole blocked-rule bucket, correct token/quota
figures, `openai-codex`/`gpt-5.4-mini` in Model Usage. Zero console
errors.

**No anomaly.** No cycle failed, no checkpoint was stranded, no code/
rules/config/model/schema/dashboard/architecture was changed by this
step.

**Training is now PAUSED after this 3-cycle tranche for a full read-only
repository/database redundancy audit, per explicit instruction.** The
harness has not been invoked again since; `backtest_state.json` sits at
cursor `2026-02-20` with the two pending orders above. This section does
not authorize further cycles.

> **⚠ SUPERSEDED — do not resume this state.** The audit found that
> `ATI x1`/`BRX x1` above are a real training-integrity defect (one-share
> placeholders, not RULES.md-sized positions — see the next section and
> "Current state" at the top of this file), and this state's equity
> history is the fabricated six-point seed later removed. **This pilot
> state must never be resumed.** It stays on disk only as evidence for
> that section; the actual next step is a fresh `--reset` run, not a
> resume of this one.

### 2026-08-30 — Training-integrity fix: decision-time position sizing

Used the `implement` skill. Took over an **incomplete, uncommitted patch
the orchestrator had started directly** in the working tree — every line
treated as untrusted and reviewed against the existing architecture
before finishing, correcting, or keeping it.

**The defect this fixes, discovered during the 3-cycle pilot above:**
Hermes queued **ATI x1** and **BRX x1** — real, accepted, journaled
proposals — even though RULES.md §2a/§2b/§3's own sizing formula implied
roughly **x130** and **x696** respectively for those setups. The
deterministic risk checker only ever rejected *excessive* quantities
(breaching §2a/§2b/§3's caps); it never verified a quantity was large
*enough* to mean anything. A meaningless one-share placeholder passed
every existing check. This is a genuine training-integrity issue, not a
cosmetic one: the two pending orders sitting in `backtest_state.json`
right now are exactly this defect's real output.

**The correction — `backtest-harness/run_backtest.py`:**

1. **`required_entry_quantity`**: a new, pure decision-time function
   computing the exact whole-share size RULES.md §2a (stop-risk), §2b
   (concentration), and §3 (remaining cash) imply — `math.floor` of
   whichever is smallest. A BUY's proposed quantity must equal this exact
   figure or it is rejected before queue-time journaling, naming both
   numbers. **Execution-time (`resolve_queued_order`) is deliberately
   untouched** — it still validates only genuine cap/risk/cash violations
   against the real next-day fill price, never demanding a fresh "exact"
   recalculation after an overnight gap moves the theoretical optimum.
2. **Multi-decision coherence within one Hermes response**: accepted BUYs
   now reserve their estimated notional against a running `available_cash`
   pool, in response-list order, so a later BUY in the same reply is sized
   against what's actually left — not the whole day's starting cash as if
   every earlier proposal didn't exist. A duplicate ticker already
   *accepted* this cycle is rejected outright (§9.1) before it can reach
   `record_queued_decision`. A *rejected* earlier proposal reserves
   nothing and never blocks a later, corrected proposal for the same
   ticker. Chronological execution-time accounting (`simulate_trading_day`)
   is unchanged.
3. **Real `data_timestamps`**: newly queued decisions now journal a
   non-null object distinguishing the technical cutoff, the news cutoff,
   and the latest actually-included news article's real timestamp (or
   `None` when no article existed) — sourced from the real simulated date
   and the real fetched news payload, never fabricated. No migration
   needed (`trades.data_timestamps` already existed); the existing
   idempotency comparison (`_json_equal` in `trade-journal-mcp/server.py`)
   already handled this generically and needed no change.
4. **Fabricated equity history removed**: a fresh `Portfolio` now begins
   with exactly one real $100,000 baseline point, not the six-point flat
   seed invented to satisfy the daily/weekly circuit breakers Phase 1
   already removed. Peak drawdown is unaffected (the six seeded points
   were always identical, so `max(equity_curve)` never depended on the
   fabrication). The prompt Hermes reads no longer shows `daily`/`weekly`
   P&L figures for breakers that no longer exist.

**Cleanup portion of the pre-existing diff, reviewed and kept** (all
verified correct, none touched further): `.gitignore` additions
(`.pytest_cache/`, `*.log`); stale-comment corrections in
`alpaca-mcp/server.py` (tool count, the §4a reference — losing-streak
de-risking is gone, only §9.4 remains a live-mode gap),
`docker-compose.yml` (execution-sim's real role), and
`docs/PHASE1_AGGRESSIVE_DESIGN.md`/`trade-journal-mcp/schema.sql`
(generic "active minimum" wording, queue-then-resolve accuracy) — all
comment/doc-only, no behavior change. **`backtest-harness/
integration_check.py`'s deletion was reviewed and kept**: a manual,
non-automated diagnostic script hardcoded to a stale watchlist (AAPL/
MSFT — not the real 10-ticker watchlist), requiring hand-produced JSON
inputs from undocumented external steps, referenced nowhere else in the
repo, and functionally superseded by `test_anonymize.py`'s own real-
watchlist regression. Not restored.

**New regression tests** (`backtest-harness/test_execution_semantics.py`):
`test_required_entry_quantity_matches_rules_md_sizing_formula`,
`test_resolve_queued_order_does_not_recalculate_exact_quantity_after_a_gap`,
`test_fresh_portfolio_has_one_baseline_equity_point`,
`test_prompt_no_longer_exposes_obsolete_daily_weekly_breaker_figures`,
`test_decision_time_sizing_rejects_placeholder_accepts_exact_and_journals_timestamps`,
`test_decision_time_multi_decision_response_coherence` — the last of
these drives one realistic 4-decision Hermes response through the real
`run_day()` orchestration boundary, proving in one coherent scenario that
a wrong-size placeholder is rejected, the exact size is accepted, a
duplicate of an already-accepted ticker is blocked before journaling, and
a later BUY's accepted size correctly reflects the cash an earlier BUY in
the same response already reserved.

**Exact verification (2026-08-30):** `alpaca-mcp/server.py`,
`backtest-harness/run_backtest.py`, `backtest-harness/
test_execution_semantics.py` all compile clean. Full
`test_execution_semantics.py` suite — **178 checks, all PASS** (was 158;
+20 from the 6 new test functions above). `test_anonymize.py`,
`test_quota_pacer.py`, `risk-checker/test_risk_checker.py` — unaffected,
all PASS (risk_checker.py itself was not touched by this fix).
Trade-journal suite run in the container (real, self-cleaning Neon):
`test_decision_fields.py`, `test_idempotency.py`, `test_mode_isolation.py`,
`test_run_isolation.py`, `test_dashboard_fields.py` — all PASS unchanged
(no server.py/schema changes required this fix). `docker compose config
-q` valid. **No Hermes call, no training cycle, no `--reset`, no Neon
mutation of any kind occurred during this fix** — every verification step
was either a pure-Python test or a real-but-mocked `run_day()` call with
all I/O (Docker, Neon, Hermes) patched out.

**The 3-cycle Round 2 pilot state (`run_id 97d112e8-...`) was, at the time
this fix was written, still on disk unchanged.** *(Superseded a few hours
later the same day — see the next dated section: a `--reset` run has
since deleted `backtest_state.json` entirely. The pilot's own Neon rows
under `run_id 97d112e8-...` are untouched and still readable — only the
local state file describing it as "current" is gone.)*

**Remaining blocker: none for this fix itself.** A **separately
authorized fresh `--reset` run** was required before any further training
— see the next section for what happened when one was actually attempted.

### 2026-08-30 — First post-fix `--reset` attempt: real crash on cycle 1, training NOT yet safe to start

Used the `implement` skill (docs) plus a directly authorized harness run
(explicit user instruction, not a skill). **Task 1 of this pass: cleaned
the ambiguity above** — the prior "ready to resume" framing on the pilot
section and the (now-stale) "still sits at cursor 2026-02-20" line both
corrected in place rather than left to contradict this section.

**Exact command run, from `backtest-harness`:**
```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3 --reset
```

**Result: the run did NOT complete three cycles. It failed on cycle 1
with a real, reproducible crash — not a risk-checker rejection, an actual
uncaught exception:**

```
starting fresh run (run_id=c55f0fc2-5073-4cf8-bc5c-17800e139bfc)
Day 1 — 2026-02-17
  CYCLE FAILED — simulated date NOT advanced, state NOT written:
  TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
cycles this run: 0 | quota used this week: 135/175
```

**Root cause (diagnosed, not yet fixed — per this task's own "stop and
document" instruction):** `ask_hermes`'s `_accumulate()` helper
(`backtest-harness/run_backtest.py`) does
`usage_totals[key] = usage_totals.get(key, 0) + usage.get(key, 0)` for
each token-usage field. `dict.get(key, 0)` only substitutes the default
when `key` is *absent* — if the real Hermes CLI's usage payload includes
the key with an explicit `null` value (plausible for e.g.
`cache_read_tokens` when prompt caching didn't apply on a fresh, cold
context — this was the very first cycle of a brand-new run), `.get()`
returns `None`, and `int + None` throws exactly the observed `TypeError`.
This is a **pre-existing latent defect**, not something introduced by the
position-sizing fix earlier today — it simply never fired before because
no prior real cycle happened to get a null usage field back. Not fixed in
this pass; flagged for a dedicated, tested correction.

**Real consequences, verified, not assumed:**

- **Local state:** `--reset` deleted the old (defective) `backtest_state.json`
  as designed, and the crash means the new run's state was correctly
  never written either (fail-closed, exactly as documented) — no
  `backtest_state.json` currently exists at all. No `cycle_checkpoint.json`
  either (the crash happens before the response is ever checkpointed).
- **Quota:** one real Hermes call *was* consumed before the crash
  (`quota_pacer_state.json`: `135` this week, up from `134`) — the quota
  pacer's "count the instant Hermes is actually called, even on a later
  failure" contract worked exactly as documented; this was not a wasted
  slot silently unaccounted for.
- **Neon, scoped to the new `run_id c55f0fc2-...`** (read-only, verified):
  `trades` 0, `insight_log` 0, `portfolio_snapshots` 0, `agent_activity`
  **1** — a single honest `cycle_failed` narration row
  (`"Day 1: TypeError: unsupported operand type(s) for +: 'int' and
  'NoneType'"`), written by the real fail-closed logging path. Nothing
  fabricated, nothing silently dropped.
- **Round 1 and the Round 2 pilot's own Neon rows are untouched** — this
  run never got far enough to touch anyone else's data, and no manual
  cleanup was performed or needed.
- **Dashboard — a real, separate finding worth flagging on its own:**
  loaded live in a browser (Training view), the dashboard is **still
  showing the OLD, superseded pilot run** (`97d112e8-...`: "3 cycles
  recorded," the two one-share `ATI`/`BRX` placeholder BUYs, the old
  6-point-seeded equity curve) **as if it were current** — because the
  dashboard's "which run is current" logic keys off the most recent
  `portfolio_snapshots` row, and the new run never produced one before
  crashing. This is a direct, observable consequence of the harness
  crash above, not a separate dashboard defect to fix independently — but
  it means **the requirement "dashboard defaults to the fresh run, no
  stale entries presented as current progress" currently fails**, through
  no fault of the dashboard's own code.

**No broad fix was attempted**, per this task's explicit instruction to
stop and document a meaningful defect rather than improvise one. `RULES.md`,
`AGENTS.md`, `SOUL.md`, `risk_checker.py`, the dashboard, and Docker/Neon
runtime state were not touched. The position-sizing fix from earlier today
is unaffected and still correct — this is a genuinely different, pre-
existing bug the fresh run's first real cycle happened to surface.

**Verification performed:** real Neon SELECTs scoped to the new `run_id`
(above); real local filesystem check (`backtest_state.json`/
`cycle_checkpoint.json` absence confirmed); real `quota_pacer_state.json`
read; dashboard loaded live in a browser, zero console errors (the crash
is invisible to the dashboard's own JS — it simply has nothing new to
show), page text captured and confirmed showing the stale pilot data.

**Full command output preserved** at `backtest-harness/round2b_smoketest.log`
(gitignored — matches the `*.log` pattern added in the prior cleanup
pass — not committed, kept locally as evidence).

**Whether full training is safe to begin: NO.** A fresh `--reset` run
cannot currently complete even its first cycle. The exact next action is
a targeted, tested fix to `_accumulate`'s `usage.get(key, 0)` pattern
(treat an explicit `null` the same as a missing key — e.g. `usage.get(key)
or 0` per field, verified against a hand-built usage payload containing
an explicit `None`), followed by a fresh `--reset` 3-cycle smoke test
repeating this same verification (RULES-based sizing enforcement, cash
reservation, duplicate-ticker prevention, journal/dashboard correctness,
no stale data mixed in) before any larger training batch is considered.

**Remaining blocker at the time: yes — the `_accumulate` None-handling
bug above. See the next section — it is now fixed and re-verified.**

### 2026-08-31 — `_accumulate` null-usage bug fixed; fresh --reset smoke test completes all 3 cycles clean

**Fix.** `ask_hermes`'s `_accumulate()` helper
(`backtest-harness/run_backtest.py`) changed from
`usage_totals.get(key, 0) + usage.get(key, 0)` to
`(usage_totals.get(key) or 0) + (usage.get(key) or 0)` for every usage
field (`input_tokens`, `output_tokens`, `cache_read_tokens`,
`reasoning_tokens`, `total_tokens`, `api_calls`, `estimated_cost_usd`).
`.get(key, 0)` only substitutes the default for a *missing* key; `or 0`
also covers a key present with an explicit `None`, which is what the real
crash needed. Minimal, scoped to usage aggregation only — nothing else in
`ask_hermes` or the surrounding cycle logic was touched.

**Regression test added** —
`test_ask_hermes_treats_an_explicit_null_usage_field_as_zero` in
`backtest-harness/test_execution_semantics.py`: patches
`run_backtest._call_hermes` to return a clean decision reply plus a real
usage dict with `cache_read_tokens` and `estimated_cost_usd` both explicit
`None`, calls the real `ask_hermes` (not a reimplementation), and asserts
the null fields become `0` while every other real field (`input_tokens`,
`output_tokens`, `reasoning_tokens`, `total_tokens`, `api_calls`)
accumulates its true value untouched.

**`AGENTS.md` strengthened (no numeric rule changed).** Step 4.2's stop-
placement guidance now explicitly tells Hermes to stress-test a proposed
stop against a plausible ~3% adverse next-open gap before proposing the
entry — if that gap would land the fill at or below the stop, the stop
sits too close to the §6a floor to survive real execution, and Hermes
must find a genuinely valid deeper technical level within §6a's range or
skip the trade; never stretch a level merely to survive the stress test.
This directly targets what actually happened in the smoke test below
(two queued BUYs whose stop ended up at/above the next day's real entry
and were rejected at execution) without touching `RULES.md` or
`risk_checker.py`.

**Verification — full local suite, real commands, real output:**

```
cd backtest-harness
python test_execution_semantics.py   # includes the new test — PASS, ~100 checks
python test_anonymize.py             # PASS
python test_counterfactual.py        # OK — 32 checks
python test_geometry_scan.py         # OK — 28 checks
python test_quota_pacer.py           # PASS
python test_execution_path.py        # PASS (required starting the
                                      # execution-sim container first —
                                      # `docker compose up -d execution-sim`;
                                      # it ships with restart:"no" and was
                                      # stopped, unrelated to this fix)
python -m py_compile run_backtest.py test_execution_semantics.py   # clean
```

**Smoke test — exact command, from `backtest-harness`:**
```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3 --reset
```

**Result: all 3 cycles completed. No crash, no unexpected error.**
New `run_id 1bf9af3d-8375-45b2-8cdb-313fd56832d5`.

- **Day 1** — BUY ALGM x479 queued, BUY DINO x353 queued (both accepted in
  the SAME cycle — cash reservation across same-response BUYs held: ALGM's
  own recorded reasoning notes "chose 479, so concentration is the
  limiter and cash still remains ample" immediately before DINO was also
  accepted). OLN rejected at decision time: proposed quantity 847 ≠
  required whole-share size 850 (§2/§3 sizing enforcement).
- **Day 2** — Both Day-1 queued orders (ALGM, DINO) rejected at
  execution-time re-validation: §6a, stop no longer below the real
  next-day entry price. OLN and ALGM both rejected again at decision time
  for quantity mismatches (407≠814, 259≠518).
- **Day 3** — BUY ATI x130 queued (accepted; still `pending` — the window
  ends before its execution day). BRX rejected at decision time for a
  near-miss quantity mismatch (694 ≠ 696 required) — confirms the sizing
  check is exact, not tolerant of rounding.
- **Trades this run:** 3 (`1` pending, `2` rejected-at-execution, `0`
  filled — expected for a 3-day window with T+1 fills). **No ticker was
  proposed as a duplicate BUY within the run** (verified by direct query,
  see below). **Zero closed trades, equity unchanged at $100,000.00 across
  all 3 real snapshots** — one clean baseline, exactly as required.

**Neon, scoped to the new `run_id` (read-only, verified via a scratch
script through the `trade-journal` container, not assumed):**
- `trades`: 3 rows (ALGM/DINO rejected with real `§6a` rejection_reason
  text, ATI pending) — real `intended_quantity`, real timestamps.
- `portfolio_snapshots`: 3 rows, `day_label` Day 1/2/3, cash=equity=
  $100,000.00 on every row, `open_positions` 0 on every row.
- `agent_activity`: 18 rows — `cycle_start`×3, `reasoning`×6, `risk_check`×4,
  `order_queued`×3, `order_rejected`×2. Real narration text for every
  rejection, naming the specific rule and number (matches AGENTS.md
  Step 5's requirement).
- `insight_log`: 0 (expected — no reflection cadence reached in 3 cycles).
- Duplicate-ticker query (`GROUP BY ticker HAVING count(*) > 1` on BUYs for
  this run_id): empty — no ticker was proposed twice.
- **Old runs confirmed untouched:** `97d112e8-...` still shows 5
  trades/17 activity rows, `c55f0fc2-...` still shows 0 trades/1 activity
  row — identical to their pre-fix counts. No manual cleanup was performed
  or needed.
- **Quota:** `quota_pacer_state.json` now `{"2026-W34": 34, "2026-W35":
  135, "2026-W36": 3}` — a real new ISO week (today, 2026-08-31, is a
  Monday) started its own counter at 3, matching the 3 real Hermes calls
  this smoke test made. Not a reset bug; the weekly key rolled over
  correctly.

**Dashboard verification (loaded live, `http://127.0.0.1:8842`, zero
console errors):** shows the new run as current — "3 cycles recorded,"
Day 3, $100,000.00 cash/equity, the real ALGM/DINO/ATI decisions with
their real §2a/§2b sizing arithmetic spelled out in each decision's own
reasoning text, the real §6a/§2/§3 rejection counts (§2: 4, §3: 4, §6a: 2)
in the "blocked by rule" panel, and the real activity feed in chronological
order. **No trace of the old `97d112e8-...` pilot data anywhere on the
page** — the crash-caused staleness from the previous attempt is gone now
that a real snapshot exists for the dashboard to key off.

**No other defect appeared at the time.** Nothing beyond the fixed bug
needed stopping-and-documenting in that pass.

> **⚠ SUPERSEDED.** This section's "training is safe to begin: YES" was
> wrong — not because the `_accumulate` fix was bad (it wasn't), but
> because this same smoke test's own persisted reasoning contained a
> THIRD defect nobody had checked for yet: Hermes was sizing every BUY
> off a stale 1% §2a risk budget, not the real, active 2%. See the next
> section for the full story, the fix, and the actually-current answer.

### 2026-08-31 — third defect found: Hermes was sizing off a stale 1% §2a budget, not the real 2%; fixed, and the 175/week quota gate removed

**How this was found.** Nothing crashed and nothing was rejected for
being wrong-sized in the previous smoke test (`1bf9af3d-...`) — it looked
completely clean by cycle-count alone. Only reading the actual persisted
reasoning on the ATI decision showed the tell: `§2a stop-based size =
floor(100000*0.01/(152.84-145.20))` — **0.01**, i.e. 1%, when RULES.md's
Phase 1 profile has run at **2%** since before that pilot even started.
Day 2's two rejections corroborated it independently: Hermes proposed
407 and 259 shares, exactly HALF the real required 814 and 518 — the
signature of computing against a 1% budget instead of 2%. ATI's own
BUY only happened to pass earlier because the 20% concentration cap
(unaffected by this bug) coincidentally produced the same final quantity
as the correct 2% calculation would have, masking the defect in that one
case.

**Root cause.** `build_prompt` (`backtest-harness/run_backtest.py`) told
Hermes to compute "§2a stop-based size" but never actually stated the
real percentage or the formula — no number, no arithmetic, nothing to
anchor against. Hermes filled that gap from its own general training-data
notion of a "typical" risk-per-trade rule (1% is the common textbook
default), not from anything this project ever told it. This was latent
since Phase 1 shipped 2% — RULES.md/`risk_checker.py`/`AGENTS.md` all
correctly say 2%, but the ONE place Hermes actually reads live numbers
from (the prompt) never passed that number through.

**Fix.** `build_prompt` now takes the same `RiskLimits` instance
`run_day`'s own decision-time checker (`required_entry_quantity`/
`check_trade`) enforces — never an independent copy of the numbers — and
renders a real **POSITION SIZING** block into every prompt: the actual
`risk_budget_pct`/`max_position_pct`/cash-only rule, plus all three
formulas spelled out (`stop-based size = floor(equity * (X/100) /
(entry-stop))`, `concentration size = floor(equity * (Y/100) / entry)`,
`cash size = floor(remaining_cash / entry)`, `required quantity = min(...)`).
Because it reads from the live `RiskLimits` object rather than literals,
this stays correct automatically if a future approved `RULES.md` change
alters the active profile — no `RULES.md`, `risk_checker.py`, or strategy
number was touched to fix this.

**Regression test added** —
`test_build_prompt_sizing_block_reads_the_active_limits_not_hardcoded_defaults`
in `test_execution_semantics.py`: builds a prompt with a deliberately
non-default `RiskLimits(risk_budget_pct=7.5, max_position_pct=33.0)` and
asserts those exact values and formulas appear (and the old 2.0/20.0
defaults do NOT leak in) — proving the block reads the supplied instance,
not a second hard-coded copy.

**Separately, the 175/week quota GATE was removed** (user-approved): it
was always a guessed local safety brake, never a real OpenAI limit, and
real usage (125 cycles, run 1) stayed at roughly 1–3% of the visible
weekly ChatGPT Plus allowance. `quota_pacer.py` was simplified to a pure
ISO-week invocation counter — `can_run_cycle`/`budget`/
`remaining_this_week`/`next_week_starts` removed entirely, nothing reads
a threshold to decide whether to keep running. Every real invocation
(including a JSON/semantic retry — previously deduped to "1 quota slot
per cycle," now counted separately since there's no slot concept left to
dedupe against) still increments the counter, and the counter is now
explicitly best-effort: a corrupt/unreadable local state file is caught
and swallowed at the call site (`run_day`'s `mark_invoked`) rather than
raised, so a broken counter can never block a cycle whose real Hermes
call already succeeded. The dashboard's "Weekly quota — used/175 cycles"
progress bar is now "Hermes invocations this week: N," with no
denominator and no claim of being an official OpenAI figure. The
historical `weekly_budget` Neon column is left in place for compatibility
(no destructive schema change) but is no longer populated with a
meaningful number (`NULL` going forward).

**Verification — full local suite, real commands, real output, from
`backtest-harness`:**

```
python -m py_compile run_backtest.py test_execution_semantics.py quota_pacer.py test_quota_pacer.py   # clean
python test_execution_semantics.py   # PASS — 184 checks, incl. the new sizing-block test and 2 new mark_invoked tests
python test_quota_pacer.py           # PASS — 7 checks (rewritten: pure counting + ISO-week rollover + fail-closed-on-corrupt, no blocking-threshold assertions)
python test_anonymize.py             # PASS
python test_counterfactual.py        # OK — 32 checks
python test_geometry_scan.py         # OK — 28 checks
python test_execution_path.py        # PASS
```

**Smoke test — exact command, from `backtest-harness`:**
```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days 3 --reset
```

**Result: all 3 cycles completed. No crash, no unexpected error, no
quota-gate message anywhere in the output.** New
`run_id 81db75e5-f9e9-48df-8bf1-0a2f581d3db8`.

- **Day 1** — BUY ALGM x479, BUY DINO x353 queued (both accepted).
- **Day 2** — BUY ALGM x518, BUY DINO x396 queued — **the exact correct
  2%-based quantities this time**, not the stale 1%-based 259/407 from
  the prior run. Both later rejected at execution under real §6a (stop no
  longer below the real next-day entry).
- **Day 3** — 4 new BUYs queued (ALGM x551, OLN x836, WMG x688, PLNT
  x219); ALGM separately rejected for a real §6a min-stop-distance
  breach (2.08% vs the 3.0% floor) and DINO for a real §2b concentration
  breach (20.34% vs the 20.00% cap) — both genuine, deterministic
  risk-checker rejections, not fabricated.
- **Every single persisted BUY's reasoning this run explicitly shows
  `§2a ... size = floor(100000*0.02/...)` or the equivalent
  `floor(2000/...)` (2% of $100,000 = $2,000) — real, verified via a
  direct Neon query of `trades.strategy_notes`, not sampled or assumed.**
  The stale 1% figure never appears anywhere in this run's reasoning.
- **Cash reservation across same-response BUYs verified directly in the
  reasoning text**, not just inferred: Day 3's OLN cites "cash size ...
  after the INDIA buy," WMG cites "... after the INDIA and LIMA buys,"
  PLNT cites "... after the INDIA, LIMA, and DELTA buys" — the running
  cash figure compounds correctly across all four same-cycle BUYs.
- **No duplicate-ticker BUY within any single cycle** — verified from
  real per-decision timestamps (each day's distinct tickers land 4–15
  seconds apart; no ticker repeats within one day's cluster).
- **Old run_ids confirmed untouched** in Neon (unchanged from the prior
  verification pass).
- **Quota, real and observable, no gate anywhere:** printed as `Hermes
  invocations this week: 6` (3 from the earlier `1bf9af3d-...` smoke test
  + 3 from this one, same real ISO week) — no "QUOTA PACER" message, no
  halt, at any point.

**Dashboard verification (loaded live, `http://127.0.0.1:8842`, zero
console errors after a hard reload — an earlier same-session read had a
stale browser-cached pre-edit copy of `index.html`, corrected by
reloading, not a real defect):** shows the new run as current — "3 cycles
recorded," Day 3, $100,000.00 cash/equity, all four Day-3 BUYs with their
real, truthful §2a/§2b arithmetic in each one's own reasoning text, real
§6a/§2b rejection counts (§6a: 3, §2b: 1) in the "blocked by rule" panel,
and **"HERMES INVOCATIONS THIS WEEK: 6" with no `/175` or any other
denominator** in the run-progress panel — the old quota bar and its
"MODEL USAGE & QUOTA" panel's implicit ceiling are gone.

**No other defect appeared.** Nothing beyond the two fixed issues (1%
sizing, quota gate) needed stopping-and-documenting this pass.

**Whether full training is safe to begin: YES — now genuinely verified,**
including the specific thing the PRIOR "YES" missed (real-percentage
sizing correctness, not just cycle-count success).

## Exact next step

**Full training is unblocked.** The next action is to start the real
training batch with a fresh `--reset` (do not resume any of
`97d112e8-...`, `c55f0fc2-...`, `1bf9af3d-...`, or
`81db75e5-f9e9-48df-8bf1-0a2f581d3db8` — all four are superseded/
completed smoke tests, not the start of the real batch) and let it run
the full `--start 2026-02-17 --end 2026-08-14` window rather than
stopping at `--days 3`. Watch early cycles for the §6a stop-margin
pattern seen across both the third and fourth smoke tests (queued BUYs
rejected at execution because the stop sat too close to or above the
real next-day entry) — the `AGENTS.md` stress-test guidance added in the
prior pass targets exactly that, but it has not yet visibly changed
Hermes's stop placement across a real multi-day run; watch whether it
does. Tracked against the six-month target of 30–60 completed trades
(`docs/PHASE1_AGGRESSIVE_DESIGN.md`).

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
