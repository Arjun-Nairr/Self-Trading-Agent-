# Handoff briefing — trading agent built with the Hermes agentic harness

*Compacted 2026-09-01. Full pre-compaction history:
`docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md` — read only for
the exact chronological repair narrative behind a claim below; not more
current than this file.*

---

## 1. How assistants use this file

**Read this file first, completely, before touching anything else.**
Repository state (git log, current file contents, `backtest_state.json`,
Neon) is authoritative over any compacted conversation history you may
carry in. **Open only the specific source files directly relevant to your
assigned task** — do not reread the full repository, `docs/history/`,
vendored third-party code, or unrelated tests unless verification
genuinely requires it; this file exists so that scan is unnecessary.

The working split: **discussion/strategy/second opinions happen in chat;
Claude Code (or Codex/OpenCode) handles execution.** Don't propose direct
code edits as if you'll apply them yourself; propose the *decision*.
"The Hermes Ledger" (a separate personal study artifact) is the user's
own optional reading — **not** required context and not authoritative.
This file and the repo's own source (`RULES.md`, `AGENTS.md`, `SOUL.md`,
actual code) are.

---

## 2. Project purpose

**Canonical terminology:** this project is an unnamed LLM-driven trading
agent. It uses **Hermes**, an existing agentic harness; Hermes is neither the
project being built nor the name of the trading agent. Use **the trading
agent** for the project/decision-maker and **the Hermes harness** only when
referring to the external agentic runtime, unless the user explicitly chooses
a separate name for the trading agent later.

The trading agent trades US equities — **long only, paper account,
swing-trading horizon** (days to weeks) — reasoning over technicals and news
and gated by a hard-coded risk checker before anything executes. It runs using
the Hermes agentic harness. The trading system has two modes: **backtest**
(point-in-time historical simulation, identity-masked so the model can't
recall what really happened) and **live** (real Alpaca paper-trading API,
never yet run). This is a **permanent paper-money sandbox** — the goal is
honest evidence about whether the trading agent's judgment is any good, not
protecting a balance or preparing for real capital.

Two rounds so far:
- **Round 1** (conservative rules): 125 cycles, zero filled trades. Full
  extract: `docs/evaluations/run1_evaluation_extract.md`. This result is
  *why* Round 2's aggressive profile exists — see §5.
- **Round 2** (aggressive Phase 1 profile): one continuous 50-cycle run,
  now complete. This is the current, active dataset — see §3.

---

## 3. Current verified state

**Active run:** `run_id e68525f9-ce89-4588-a57e-866f28cd99fd`, window
2026-02-17 → 2026-08-14, seed 42.

- **50 completed trading cycles** (`trading_days_elapsed` = 50). Cursor
  now `2026-04-29`.
- **Equity: $103,696.08 — total return +3.70%.** Cash $83,111.53.
- **16 closed trades total** across the run — full evaluation (all 50
  cycles, tranche comparison, data-adequacy verdict) is in
  `docs/evaluations/round2_50cycle_evaluation_extract.md`, the active
  compact evaluation brief — see §10.
- **1 open position: WMG**, 721 shares @ $28.20, stop $27.00 / target
  $31.54, entered trading-day 50.
- **0 pending orders.**
- **No `cycle_checkpoint.json` present** — every cycle through 50 is
  durably committed to `backtest_state.json` and Neon.
- **Activity-escalation profile: still `aggressive_v1_base`** (min
  reward:risk 1.5:1) — never escalated. Current block started trading-day
  40 with 4 completions so far (boundary at day 60, not yet reached).
- **Locked model: `openai-codex` / `gpt-5.4-mini` / medium reasoning** —
  unchanged since Round 1; no commit since has touched gateway model
  config. Do not change without an explicit new decision.

**Verified via real, run-scoped Neon queries:** no duplicate `cycle_id` or
`decision_id` anywhere in this run; exactly one `cycle_failed` activity
row exists (the Day 50 incident below — preserved intentionally, not
deleted); `backtest_cycle_evidence` holds exactly 25 rows (cycles 26–50
only — see §7); all 35 tranche-1 `trades` rows are byte-for-byte
unchanged from their original values.

**Git:** branch `round2-prep-judgment-layer`, merged into `main` and
pushed (§7's benchmark/sensitivity work is the most recent addition).
`docs/PROJECT_BACKLOG.md` and `docs/research/` are separate,
still-iterating untracked documents — leave untouched unless the user
says otherwise. Full commit history: `git log`, not restated here.

**The Day 50 incident, resolved:** a real `expected_reward_risk`
BUY/SELL contract mismatch halted cycle 9 of a resumed attempt (real date
2026-04-07); fixed in `7c4ac06` (made the contract action-aware: BUY
requires finite `>0`, SELL/other permits finite `>=0`), verified against
the real checkpointed payload, and the run completed cleanly to 50 cycles
with zero further failures. Full narrative:
`docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md`. The historical
`cycle_failed` Neon row is preserved as an honest record, not deleted.

---

## 4. Current architecture and data flow

One LLM agent (`gateway`) plus five isolated tool containers, hub-and-
spoke — no tool container can reach another. Docker images pinned by
digest.

| Service | Responsibility |
|---|---|
| `gateway` | Runs the trading agent through the Hermes harness. Backtest: the agent never calls a tool itself. Live: it calls tools directly. |
| `maverick-mcp` | Technicals (RSI/MACD/support-resistance/trend) + price history, point-in-time gated. |
| `google-news-rss-mcp` | Live-mode headlines only. |
| `alpaca-mcp` | Live orders/account/positions + backtest's historical news and intraday-bar data. |
| `trade-journal-mcp` | All Neon reads/writes — decisions, executions, activity, insights, snapshots, cycle evidence. |
| `execution-sim-mcp` | Backtest-only fill simulator (slippage-aware, real historical ranges). |
| `dashboard` | Static frontend, local-only (`http://127.0.0.1:8842`, loopback-bound). |

**Backtest mode:** `backtest-harness/run_backtest.py` fetches everything,
masks tickers/dates, hands the trading agent one prompt per simulated trading day.
One "cycle" = one simulated trading day. A discretionary decision made on
day T is queued and resolves no earlier than day T+1's real open — no
same-day lookahead. Essential cycle data commits atomically before
best-effort observability is attempted — a crash after commit can lose
dashboard detail but never duplicate a trade or a trading-agent call.

**Live mode:** the trading agent calls MCP tools directly against real (paper)
broker/news endpoints. Never yet run.

**Where each kind of data lives:**

| Data | Owner |
|---|---|
| Decisions, execution results, activity, insights, snapshots, cycle evidence | Neon Postgres — single source of truth; `mode`/`run_id` columns separate backtest/live and run-from-run |
| In-progress backtest portfolio (cash, holdings, pending orders, cursor, `run_id`, profile state) | `backtest-harness/backtest_state.json`, local, gitignored |
| Live broker positions/orders | Alpaca itself — authoritative for live |
| Dashboard state | None — stateless read against Neon's Data API |

---

## 5. Locked decisions and invariants

These do not change without an explicit new user decision. Full numbers
and rationale live in `RULES.md`; this is the invariant list, not a
restatement of every threshold.

- **Model:** `openai-codex` / `gpt-5.4-mini` / medium reasoning (§3).
- **`RULES.md` is the authoritative, locked rules source** — mounted
  read-only into `gateway`, re-read every cycle. The trading agent may refine
  judgment through reflection but may **never** loosen a limit — the one
  approved exception is the deterministic §6b activity-escalation
  mechanism, which is harness-controlled, never the trading agent's own choice.
- **`risk_checker.py` enforces every numeric rule in code**, independent
  of the model's own reasoning — pure functions, fail-closed on any
  unknown/non-finite/non-positive value.
- **Point-in-time masking + lookahead prevention**: masked ticker/company
  identities (reshuffled per run), `as_of`-gated fetches, next-day queued
  execution together guarantee a backtest decision never sees information
  that wouldn't have existed yet.
- **Decision-time vs. execution-time validation**: a proposal is validated
  twice — decision-time is advisory (today's known state as a proxy);
  execution-time (real next-day price) is the sole authority on whether a
  trade actually happens.
- **One-lot model**: a discretionary SELL must close the entire position.
- **Sentiment is a veto only, never a trigger** (§5a) — also the deliberate
  prompt-injection defense.
- **Hard prohibitions (§9)**: never average down; never widen/remove a
  stop; never trade off-watchlist; never hold two open positions in the
  same ticker; never re-enter same-day after a stop-out; never exceed any
  cap; never loosen a limit through the trading agent's own reasoning
  (escalation mechanism excepted, and that is harness-controlled, not the
  trading agent deciding).
- **Aggressive Phase 1 profile is the active rules regime** (not the
  conservative Round-1 profile) — 2% risk/trade, 20% max position,
  cash-only exposure with no cap on position count, 1.5:1 base / 1.25:1
  escalated min reward:risk, 3–12% stop band, 5-trading-day time stop,
  single −30%-drawdown circuit breaker only. Full table and the honest
  cost of each loosened number: `docs/PHASE1_AGGRESSIVE_DESIGN.md` and
  `RULES.md` §10.
- **`SOUL.md` (Phase 2, active temperament)** is in sync with this
  profile: search every eligible ticker each cycle, ordinary confidence
  (3) is sufficient to act, a no-trade cycle is valid only when every
  candidate has a concrete rejection reason. Honesty, evidence discipline
  (no strategy lessons from a tiny/empty sample), and prompt-injection
  resistance are unchanged from Round 1's temperament.
- **`expected_reward_risk` is action-aware** (fixed 2026-09-01, `7c4ac06`):
  BUY requires finite `> 0`; SELL (or any non-BUY action) permits finite
  `>= 0` — 0.0 is the honest value for a closing decision, never
  fabricated. Enforced identically in the backtest response validator,
  `trade-journal-mcp`'s Python validation, and the Neon `CHECK` constraint.

---

## 6. What is implemented

- **Full backtest harness**: chronological intraday simulation (opening
  gap → time-stops → queued SELLs at 09:30 → first-bar stop/target →
  queued BUYs at 09:45, re-validated against 09:45-known marks only →
  bar-by-bar walk for the rest of the session), real 15-minute Alpaca
  bars, real 5bps `execution-sim-mcp` slippage, deterministic
  `decision_id`/`cycle_id` (safe retry, no double-journaling), a durable
  per-cycle checkpoint, essential-vs-best-effort write ordering.
- **Deterministic activity-escalation mechanism** (`run_backtest.py`):
  20-trading-day blocks, ≥5 completed trades keeps the base tier,
  otherwise the next block escalates to 1.25:1 — permanent once
  triggered, harness-controlled, never a trade count shown to the trading agent.
- **Risk checker** (`risk-checker/risk_checker.py`): pure, deterministic,
  fail-closed enforcement of every `RULES.md` numeric rule, shared by live
  and backtest.
- **Point-in-time masking** (`backtest-harness/anonymize.py`): reshuffled
  ticker/company aliases per run, verified leak-free against real
  headline text (`test_anonymize.py`).
- **Trade journal** (`trade-journal-mcp/`): idempotent decision/execution
  recording with full field-mismatch detection on any `decision_id`
  collision; run/mode isolation enforced at both write and read boundaries.
- **Per-cycle evidence instrumentation** (`backtest_cycle_evidence` table,
  added `bfb16fe`, hardened `af0921d`) — see §7.
- **Dashboard**: local-only, read-only against Neon's Data API, real
  token/retry/rejection counts, per-run scoping, mobile-responsive.
- **Offline diagnostics** (not drafts, executable, human-only, never
  wired into the live path): `geometry_scan.py` / `counterfactual.py`
  (justified the Round-1→Round-2 rule changes) and
  `benchmark_sensitivity.py` (§7's benchmark/sensitivity report).

---

## 7. Evidence and evaluation coverage

Two different levels of detail exist across the 50 completed cycles —
**do not conflate them**:

- **Cycles 1–25 (tranche 1): complete trade/outcome data**, but **no
  exact per-cycle prompt/input/response evidence** — `backtest_cycle_evidence`
  didn't exist yet, deliberately never backfilled.
- **Cycles 26–50: exact masked-prompt/input/response evidence for every
  one** — `backtest_cycle_evidence` holds the verbatim rendered prompt,
  exact masked inputs, final validated response, every candidate's
  proposed/no_proposal status, and rules-file hashes per cycle. Makes it
  possible to audit whether the trading agent actually read what it was shown —
  something tranche 1 cannot support.
- Real runtime `model`/`provider`/`reasoning_effort` fields on evidence
  rows are `NULL` for every row so far (the real trading-agent usage report has
  never included those keys) — not a bug, will populate automatically if
  a future trading-agent runtime reports them.

**Evaluation status:** the full 50-cycle run has been evaluated on two
levels. Read first: `docs/evaluations/round2_50cycle_evaluation_extract.md`
— the active, compact brief (headline facts, labeled verified/
interpretation/unknown). For row-level detail consult the archived raw
reports it links to: `docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md`
(cycles 1–50) and `..._tranche1_evaluation_extract.md` (cycles 1–25,
narrower) — open only if the task genuinely needs that detail.

**Benchmark and sensitivity pass (2026-09-05, corrected across four
2026-09-06 passes — status current as of the fourth):**
`docs/evaluations/round2_benchmark_sensitivity.md` — the trading system
vs. cash/SPY/equal-weight-watchlist buy-and-hold/a defined SMA(10)
technical baseline over 50 identical dates, plus sensitivity to
`RULES.md`'s risk%/cap%/reward-threshold parameters (every stop/target
walk validates the complete intraday session before use, `MissingDataError`
on any gap — none in real data; every %/$ pair comes from one
shared-formula function, `reconciliation_table`, never hand-typed — full
defect list in the report's own correction-history note). Headline: the
trading system (+3.696% full portfolio, −2.46% drawdown, 20.0% exposure)
beat the watchlist (−2.66%) and technical baseline (+0.39%, flat, double
the volatility) on return-per-volatility, though SPY (+4.20%) edged it on
raw return at far higher exposure and a one-day timing head start. The
concentration cap remains the more consequential lever
(`chronological_cash_constrained_approximation`, never "fully replayable").
**Verdict unchanged: improve measurement first** — not sufficient for Bayesian optimization, still unapproved.

**Per-trade journey report** (offline, read-only, human-evaluation-only,
`backtest-harness/trade_journey_report.py`): reconstructs each closed
trade's daily price/P&L/stop-target path from data the harness already
writes (no new capture). Round 2: 14/16 closed trades have a complete
daily journey; the other 2 are genuine same-session stop-outs with no
intermediate day. Explains actual five-day time-stop outcomes only —
**not** a longer-hold or stop-distance-band counterfactual (needs re-fetched intraday bars, not just a re-read). `docs/evaluations/round2_trade_journey.md`.

---

## 8. Known limitations and deferred work

- **Live bracket orders** — `alpaca-mcp`'s `place_order` submits one
  order, no OCO/bracket; nothing rests a real stop at the broker live.
- **Broker partial fills** — not modeled in either mode.
- **Circuit-breaker latching** — breakers re-evaluate fresh every cycle
  from live P&L, not latched for the rest of a halted period.
- **Survivorship bias** — the watchlist was screened using data from
  inside the exact backtest window.
- **Asyncpg statement-cache fix's narrow scope** — `statement_cache_size=0`
  is only applied in `trade-journal-mcp/server.py`.
- **No benchmark series wired into the dashboard itself** — a real,
  offline comparison now exists (§7), but no live index-comparison line
  in the dashboard UI.
- **Neon's anonymous-role exposure** — the dashboard's read role has no
  auth beyond SELECT-only; `backtest_cycle_evidence` is excluded from
  this grant (verified live).
- **Per-trade (vs. per-cycle) token attribution** remains only
  approximate.
- **Cycles 1–25 have no exact evidence record** (see §7) — a real,
  permanent, accepted gap, not an oversight to fix later.
- **Longer-hold/stop-distance-band counterfactuals still not
  mechanically identifiable** — §7's journey report explains actual
  time-stop outcomes only; needs re-fetched intraday bars, not a re-read.

**Ground rule:** `RULES.md`'s hard limits are not up for negotiation via
"better performance" arguments alone. Any proposed change to a hard
number needs to go through the user explicitly.

---

## 9. Superseded decisions — brief pointers only

- **Round 1's conservative rules profile** — superseded by the aggressive
  Phase 1 profile. `docs/evaluations/run1_evaluation_extract.md`.
- **A first, conservative Round-2 draft** and **`SOUL.md`'s Phase 1
  temperament** — both superseded before/at Round 2's start. Archived:
  `docs/history/round2-conservative-draft/run2_prep_analysis.md`,
  `docs/history/SOUL_phase1_conservative_2026-08-30.md`.
- **`quota_pacer.py`** — removed entirely (`750d80d`). Do not reference
  it, `quota_pacer_state.json`, or a "weekly quota" as current.
- **OpenCode Go + DeepSeek V4 Flash** — investigated, abandoned (a real,
  unresolved upstream hang). Locked model is still `openai-codex`/`gpt-5.4-mini`.
- **Four Round-2 smoke-test `run_id`s** — superseded, before the real
  tranche started. **Never resume any of them.**
- **Prior repair passes** (pre-tranche crashes/bugs) — no longer
  operationally relevant. Full narrative, if ever needed:
  `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md`,
  `docs/history/HANDOFF_FULL_2026-08-28.md`.

---

## 10. Exact next step

**Two independent tracks are both mid-flight — do not conflate them.**

**Evaluation track:** the read-only evaluation and benchmark/sensitivity
pass on the complete 50-cycle run are done (§7). Next: a human decision
among that report's own three options — (a) existing evidence suffices
for Bayesian optimization, (b) run another unchanged tranche first, or
(c) improve measurement first. **Its own recommendation is (c)**, a
recommendation to weigh, not a decision made. Until that review happens:
no start/reset/resume training; no `RULES.md`/`AGENTS.md`/`SOUL.md`/
model/provider change; no Bayesian optimization; no fundamental-data work.

**Implementation track (agreed NOW order — see `docs/obsidian/03-project-workflow.canvas`):**
step 1, the Obsidian vault, is **done**. Step 2, **Dashboard V2**
(`dashboard-v2/`, full IA + visual rebuild) **exists as an unactivated
candidate, not yet cut over** — a pass correcting unsupported UI claims
(naming, open-position price/days-held source, the Decisions funnel) is
done; awaiting visual review and an explicit cutover decision. V1 stays
active until then. Then the individual-trade view, then the
experiment/optimization framework. NEXT/LATER canvas items stay
discussion-only until the user explicitly approves moving one.

---

## 11. Minimal source-file map

| File | What it is |
|---|---|
| `RULES.md` | Authoritative, locked rules — position sizing, caps, circuit breaker, prohibitions. Aggressive Phase 1 profile. |
| `AGENTS.md` | The trading agent's operating procedure, step by step. |
| `SOUL.md` | The trading agent's identity/temperament (Phase 2, active). |
| `backtest-harness/run_backtest.py`, `anonymize.py`, `backtest_state.json` | Harness/orchestrator (masking, checkpointing, escalation, evidence capture); local gitignored in-progress portfolio state. |
| `risk-checker/risk_checker.py` | Deterministic rule enforcement, shared live/backtest. |
| `trade-journal-mcp/server.py`, `schema.sql` | Neon reads/writes, idempotency, action-aware `expected_reward_risk` validation, `backtest_cycle_evidence`. |
| `alpaca-mcp/`, `maverick-mcp/`, `google-news-rss-mcp/`, `execution-sim-mcp/` | The other four tool containers. |
| `dashboard/index.html` | Local-only dashboard frontend (V1 — see §10 for V2). |
| `docker-compose.yml` | Full stack topology, pinned image digests. |
| `watchlist/watchlist.json` | The fixed 10-ticker eligible universe. |
| `docs/PHASE1_AGGRESSIVE_DESIGN.md` | The aggressive profile — numbers, escalation mechanism, what stays mandatory. |
| `docs/evaluations/round2_50cycle_evaluation_extract.md`, `round2_benchmark_sensitivity.md`, `round2_trade_journey.md` | **Active** — compact evaluation brief + benchmark/sensitivity report + per-trade journey report — see §7. |
| `docs/obsidian/` | **Active** — architecture/cycle/roadmap/presentation canvases, native Obsidian Canvas — see its own `README.md`. |
| `docs/history/` | Archived/superseded material — see §9. Consult only when a task needs that specific detail. |

---

## 12. Resume command — reference only, guarded by explicit approval

**Do not run this until the user has reviewed §10 and explicitly says to
continue training this run.**

```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days <N>
```

Run from `backtest-harness/`. `backtest_state.json`'s `run_id`/cursor
(`e68525f9-...`/`2026-04-29`) reuse automatically as long as `--start`/
`--end` match exactly. **Never include `--reset`** — it discards the 50
cycles this file documents. (Registered `training`; `--run-kind` is fresh-run-only.)

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
