# Handoff briefing — Hermes trading agent

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
vendored third-party code (`maverick-mcp/`), or unrelated tests unless
verification genuinely requires it; this file exists so that scan is
unnecessary.

The working split: **discussion/strategy/second opinions happen in chat;
Claude Code (or Codex/OpenCode) handles execution.** Don't propose direct
code edits as if you'll apply them yourself; propose the *decision*, and
relay it to the executing session. A separate personal study artifact,
"The Hermes Ledger," is the user's own optional reading — **not**
required context and not authoritative. This file and the repo's own
source (`RULES.md`, `AGENTS.md`, `SOUL.md`, actual code) are.

---

## 2. Project purpose

Hermes: an LLM-driven agent that trades US equities — **long only, paper
account, swing-trading horizon** (days to weeks) — reasoning over
technicals and news, gated by a hard-coded risk checker before anything
executes. Two modes: **backtest** (point-in-time historical simulation,
identity-masked so the model can't recall what really happened) and
**live** (real Alpaca paper-trading API, never yet run). This is a
**permanent paper-money sandbox** — the goal is honest evidence about
whether Hermes' trading judgment is any good, not protecting a balance or
preparing for real capital.

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
| `gateway` | Runs Hermes. Backtest: never calls a tool itself. Live: calls tools directly. |
| `maverick-mcp` | Technicals (RSI/MACD/support-resistance/trend) + price history, point-in-time gated. |
| `google-news-rss-mcp` | Live-mode headlines only. |
| `alpaca-mcp` | Live orders/account/positions + backtest's historical news and intraday-bar data. |
| `trade-journal-mcp` | All Neon reads/writes — decisions, executions, activity, insights, snapshots, cycle evidence. |
| `execution-sim-mcp` | Backtest-only fill simulator (slippage-aware, real historical ranges). |
| `dashboard` | Static frontend, local-only (`http://127.0.0.1:8842`, loopback-bound). |

**Backtest mode:** `backtest-harness/run_backtest.py` fetches everything,
masks tickers/dates, hands Hermes one prompt per simulated trading day.
One "cycle" = one simulated trading day. A discretionary decision made on
day T is queued and resolves no earlier than day T+1's real open — no
same-day lookahead. Essential cycle data commits atomically before
best-effort observability is attempted — a crash after commit can lose
dashboard detail but never duplicate a trade or a Hermes call.

**Live mode:** Hermes calls MCP tools directly against real (paper)
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
  read-only into `gateway`, re-read every cycle. Hermes may refine
  judgment through reflection but may **never** loosen a limit — the one
  approved exception is the deterministic §6b activity-escalation
  mechanism, which is harness-controlled, never Hermes' own choice.
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
  cap; never loosen a limit through Hermes' own reasoning (escalation
  mechanism excepted, and that is harness-controlled, not Hermes deciding).
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
  triggered, harness-controlled, never a trade count shown to Hermes.
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
  possible to audit whether Hermes actually read what it was shown —
  something tranche 1 cannot support.
- Real runtime `model`/`provider`/`reasoning_effort` fields on evidence
  rows are `NULL` for every row so far (the real Hermes usage report has
  never included those keys) — not a bug, will populate automatically if
  a future Hermes version reports them.

**Evaluation status:** the full 50-cycle run has been evaluated on two
levels. Read first: `docs/evaluations/round2_50cycle_evaluation_extract.md`
— the active, compact evaluation brief (headline facts only, clearly
labeled verified/interpretation/unknown). For row-level detail (the
complete trade ledger with verbatim reasoning, all 10 reflections
verbatim, exact SQL) consult the archived raw reports it links to:
`docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md`
(cycles 1–50) and `docs/history/evaluations/round2_tranche1_evaluation_extract.md`
(cycles 1–25, narrower). Do not open the archives unless the task
genuinely needs that row-level detail.

**Benchmark and sensitivity pass (2026-09-05, corrected twice on
2026-09-06):** `docs/evaluations/round2_benchmark_sensitivity.md` —
Hermes vs. cash/SPY/equal-weight-watchlist buy-and-hold/a defined
SMA(10) technical baseline over the identical 50 dates, plus mechanical
sensitivity to `RULES.md`'s risk%/cap%/reward-threshold parameters. Two
2026-09-06 passes fixed ten calculation defects total (entry-day exits
never checked, wrong target/time-stop/same-day-trade ordering, carried
positions valued at cost basis instead of their real 09:45 mark, and
more — see the report's own correction history) and settled the risk/cap
grid on a disclosed, tested `chronological_cash_constrained_approximation`
label — never "fully replayable." Corrected headline: Hermes (+3.70%,
−2.46% drawdown, 20.0% exposure) clearly beat the watchlist (−2.66%) and
technical baseline (−6.34%) and had the best return-per-volatility ratio
of any strategy, though SPY's raw return (+4.20%) edged it out at far
higher exposure — SPY/equal-weight also enter one day earlier than
Hermes/the baseline structurally can. The concentration cap remains the
more consequential lever; time-stop horizon and stop-distance bounds
stay **not mechanically identifiable**. **Verdict unchanged: improve
measurement before optimizing or running another tranche** — not
sufficient for Bayesian optimization, still unimplemented and
unapproved.

---

## 8. Known limitations and deferred work

- **Live bracket orders** — `alpaca-mcp`'s `place_order` submits one
  order, no OCO/bracket; nothing rests a real stop at the broker live.
- **Broker partial fills** — an involuntary partial fill isn't modeled in
  either mode.
- **Circuit-breaker latching** — breakers re-evaluate fresh every cycle
  from live P&L, not latched for the rest of a halted period.
- **Survivorship bias** — the watchlist was screened using data from
  inside the exact backtest window.
- **Asyncpg statement-cache fix's narrow scope** — `statement_cache_size=0`
  is only applied in `trade-journal-mcp/server.py`; other services
  connecting to Neon would need the same fix if they ever start doing so.
- **No benchmark series wired into the dashboard itself** — a real,
  offline benchmark comparison now exists (§7's benchmark/sensitivity
  report), but there is still no live index-comparison line in the
  dashboard UI.
- **Neon's anonymous-role exposure** — the dashboard's read role has no
  auth beyond SELECT-only. `backtest_cycle_evidence` is deliberately
  excluded from this grant (no anonymous read access, verified live).
- **Per-trade (vs. per-cycle) token attribution** remains only
  approximate.
- **Cycles 1–25 have no exact evidence record** (see §7) — a real,
  permanent, accepted gap, not an oversight to fix later.
- **Time-stop horizon and stop-distance-bound sensitivity are not
  mechanically identifiable** from current instrumentation (§7's new
  report) — would need either richer per-trade price-path capture during
  a future run, or re-running Hermes' own judgment.

**Ground rule:** `RULES.md`'s hard limits are not up for negotiation via
"better performance" arguments alone. Any proposed change to a hard
number needs to go through the user explicitly.

---

## 9. Superseded decisions — brief pointers only

- **Round 1's conservative rules profile** — superseded by the aggressive
  Phase 1 profile. `docs/evaluations/run1_evaluation_extract.md`.
- **A first, conservative Round-2 draft** and **`SOUL.md`'s Phase 1
  temperament** — both superseded before/at Round 2's start by the active
  aggressive profile/Phase 2 temperament. Archived:
  `docs/history/round2-conservative-draft/run2_prep_analysis.md`,
  `docs/history/SOUL_phase1_conservative_2026-08-30.md`.
- **`quota_pacer.py`** — removed entirely (`750d80d`); real per-cycle
  token/API-call metrics cover what it existed to surface. Do not
  reference it, `quota_pacer_state.json`, or a "weekly quota" as current.
- **OpenCode Go + DeepSeek V4 Flash** — investigated, abandoned (a real,
  unresolved upstream hang). Locked model is still `openai-codex`/`gpt-5.4-mini`.
- **Four Round-2 smoke-test `run_id`s** — superseded smoke tests from
  before the real tranche started. **Never resume any of them.**
- **Prior repair passes** (pre-tranche crashes/bugs, all fixed and
  verified before real training began) — no longer operationally
  relevant. Full narrative for either item above, if ever needed:
  `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md` and
  `docs/history/HANDOFF_FULL_2026-08-28.md`.

---

## 10. Exact next step

**Both the read-only evaluation and the benchmark/sensitivity pass on the
complete 50-cycle run are done.** See §7 above for both reports.

**The actual next step is a human decision among the benchmark/
sensitivity report's own three options:** (a) existing evidence is
sufficient to begin Bayesian optimization, (b) run another unchanged
tranche first, or (c) improve measurement before either. **That report's
own recommendation is (c)** — two of the five ranked parameters
(time-stop horizon, stop-distance bounds) are not mechanically
identifiable, and n=16 closed trades is still short of `RULES.md` §6b's
own 30–60-trade target. A recommendation to weigh, not a decision made.

**Do not, until that review happens:** start/reset/resume training;
change `RULES.md`/`AGENTS.md`/`SOUL.md`/model/provider/reasoning-effort;
implement Bayesian optimization; do fundamental-data work; redesign the
dashboard; or begin any live deployment work.

---

## 11. Minimal source-file map

| File | What it is |
|---|---|
| `RULES.md` | Authoritative, locked rules — position sizing, caps, circuit breaker, prohibitions. Aggressive Phase 1 profile. |
| `AGENTS.md` | Hermes' operating procedure, step by step. |
| `SOUL.md` | Hermes' identity/temperament (Phase 2, active). |
| `backtest-harness/run_backtest.py` | The backtest harness/orchestrator — masking, checkpointing, escalation mechanism, evidence capture. |
| `backtest-harness/backtest_state.json` | Local, gitignored — current in-progress backtest portfolio + profile state. |
| `backtest-harness/anonymize.py` | Point-in-time masking. |
| `risk-checker/risk_checker.py` | Deterministic rule enforcement, shared live/backtest. |
| `trade-journal-mcp/server.py`, `schema.sql` | Neon reads/writes, idempotency, action-aware `expected_reward_risk` validation, `backtest_cycle_evidence`. |
| `alpaca-mcp/`, `maverick-mcp/`, `google-news-rss-mcp/`, `execution-sim-mcp/` | The other four tool containers. |
| `dashboard/index.html` | Local-only dashboard frontend. |
| `docker-compose.yml` | Full stack topology, pinned image digests. |
| `watchlist/watchlist.json` | The fixed 10-ticker eligible universe. |
| `docs/PHASE1_AGGRESSIVE_DESIGN.md` | The aggressive profile — numbers, escalation mechanism, what stays mandatory. |
| `docs/evaluations/run1_evaluation_extract.md` | Round 1's full extract (125 cycles, zero fills). |
| `docs/evaluations/round2_50cycle_evaluation_extract.md`, `round2_benchmark_sensitivity.md` | **Active** — compact evaluation brief + benchmark/sensitivity report (all 50 cycles) — see §7. |
| `backtest-harness/benchmark_sensitivity.py` | Human-only offline diagnostic behind the sensitivity report — never imported by the live path. |
| `docs/history/evaluations/` | Archived raw Round 2 evaluation reports — consult only for row-level detail. |
| `docs/history/` | Archived/superseded material — see §9. Consult only when a task needs that specific detail. |

---

## 12. Resume command — reference only, guarded by explicit approval

**Do not run this until the user has reviewed §10 and explicitly says to
continue training this run.**

```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days <N>
```

Run from `backtest-harness/`. `backtest_state.json`'s stored `run_id`
(`e68525f9-ce89-4588-a57e-866f28cd99fd`) and cursor (`2026-04-29`) are
reused automatically as long as `--start`/`--end` match exactly. **Never
include `--reset`** — it deletes local state and starts a brand-new,
unrelated `run_id`, discarding the 50 cycles this file documents.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
