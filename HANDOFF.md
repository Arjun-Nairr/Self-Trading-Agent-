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

- **50 completed trading cycles** (`trading_days_elapsed` = 50). Cursor now `2026-04-29`.
- **Equity: $103,696.08 — total return +3.70%.** Cash $83,111.53.
- **16 closed trades total** across the run — full evaluation (all 50 cycles, tranche comparison, data-adequacy verdict) is in `docs/evaluations/round2_50cycle_evaluation_extract.md`, the active compact evaluation brief — see §10.
- **1 open position: WMG**, 721 shares @ $28.20, stop $27.00 / target $31.54, entered trading-day 50.
- **0 pending orders.**
- **No `cycle_checkpoint.json` present** — every cycle through 50 is durably committed to `backtest_state.json` and Neon.
- **Activity-escalation profile: still `aggressive_v1_base`** (min reward:risk 1.5:1) — never escalated. Current block started trading-day 40 with 4 completions so far (boundary at day 60, not yet reached).
- **Round 2 itself ran entirely under `openai-codex` / `gpt-5.4-mini` / medium reasoning** — unchanged for the full 50-cycle run. The gateway's runtime model was migrated to `gpt-5.6-terra` on 2026-09-12, AFTER Round 2 completed and its state was last touched — see §6's fundamentals entry for the migration and §5 for the current locked value.

**Verified via real, run-scoped Neon queries:** no duplicate `cycle_id` or `decision_id` anywhere in this run; exactly one `cycle_failed` activity row exists (the Day 50 incident below — preserved intentionally, not deleted); `backtest_cycle_evidence` holds exactly 25 rows (cycles 26–50 only — see §7); all 35 tranche-1 `trades` rows are byte-for-byte unchanged from their original values.

**Git:** branch `round2-prep-judgment-layer`, merged into `main` and pushed (§7's benchmark/sensitivity work is the most recent addition). `docs/PROJECT_BACKLOG.md` and `docs/research/` are separate, still-iterating untracked documents — leave untouched unless the user says otherwise. Full commit history: `git log`, not restated here.

**The Day 50 incident, resolved:** a real `expected_reward_risk` BUY/SELL contract mismatch halted cycle 9 of a resumed attempt (real date 2026-04-07); fixed in `7c4ac06` (made the contract action-aware: BUY requires finite `>0`, SELL/other permits finite `>=0`), verified against the real checkpointed payload, and the run completed cleanly to 50 cycles with zero further failures. Full narrative: `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md`. The historical `cycle_failed` Neon row is preserved as an honest record, not deleted.

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
| `dashboard` | V1, static frontend, local-only (`http://127.0.0.1:8842`, loopback-bound) — active dashboard, see §10. V2 (`dashboard-v2/`) is a separate, genuinely PUBLIC Cloudflare Pages preview — see §10. |

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

- **Model:** `openai-codex` / `gpt-5.6-terra` / medium reasoning — migrated 2026-09-12 from `gpt-5.4-mini`, which Codex stopped serving under the current ChatGPT-account auth (§6). Runtime-compatibility change only, not a strategy decision. Config lives at `~/.hermes-trading-agent/config.yaml` (`model.default`), NOT in this repo — no tracked file controls it. Round 2 ran entirely under the old model (§3).
- **`RULES.md` is the authoritative, locked rules source** — mounted read-only into `gateway`, re-read every cycle. The trading agent may refine judgment through reflection but may **never** loosen a limit — the one approved exception is the deterministic §6b activity-escalation mechanism, which is harness-controlled, never the trading agent's own choice.
- **`risk_checker.py` enforces every numeric rule in code**, independent of the model's own reasoning — pure functions, fail-closed on any unknown/non-finite/non-positive value.
- **Point-in-time masking + lookahead prevention**: masked ticker/company identities (reshuffled per run), `as_of`-gated fetches, next-day queued execution together guarantee a backtest decision never sees information that wouldn't have existed yet.
- **Decision-time vs. execution-time validation**: a proposal is validated twice — decision-time is advisory (today's known state as a proxy); execution-time (real next-day price) is the sole authority on whether a trade actually happens.
- **One-lot model**: a discretionary SELL must close the entire position.
- **Sentiment is a veto only, never a trigger** (§5a) — also the deliberate prompt-injection defense.
- **Hard prohibitions (§9)**: never average down; never widen/remove a stop; never trade off-watchlist; never hold two open positions in the same ticker; never re-enter same-day after a stop-out; never exceed any cap; never loosen a limit through the trading agent's own reasoning (escalation mechanism excepted, and that is harness-controlled, not the trading agent deciding).
- **Aggressive Phase 1 profile is the active rules regime** (not the conservative Round-1 profile) — 2% risk/trade, 20% max position, cash-only exposure with no cap on position count, 1.5:1 base / 1.25:1 escalated min reward:risk, 3–12% stop band, 5-trading-day time stop, single −30%-drawdown circuit breaker only. Full table and the honest cost of each loosened number: `docs/PHASE1_AGGRESSIVE_DESIGN.md` and `RULES.md` §10.
- **`SOUL.md` (Phase 2, active temperament)** is in sync with this profile: search every eligible ticker each cycle, ordinary confidence (3) is sufficient to act, a no-trade cycle is valid only when every candidate has a concrete rejection reason. Honesty, evidence discipline (no strategy lessons from a tiny/empty sample), and prompt-injection resistance are unchanged from Round 1's temperament.
- **`expected_reward_risk` is action-aware** (fixed 2026-09-01, `7c4ac06`):
  BUY requires finite `> 0`; SELL (or any non-BUY action) permits finite
  `>= 0` — 0.0 is the honest value for a closing decision, never
  fabricated. Enforced identically in the backtest response validator,
  `trade-journal-mcp`'s Python validation, and the Neon `CHECK` constraint.

---

## 6. What is implemented

- **Full backtest harness**: chronological intraday simulation (opening gap → time-stops → queued SELLs at 09:30 → first-bar stop/target → queued BUYs at 09:45, re-validated against 09:45-known marks only → bar-by-bar walk for the rest of the session), real 15-minute Alpaca bars, real 5bps `execution-sim-mcp` slippage, deterministic `decision_id`/`cycle_id` (safe retry, no double-journaling), a durable per-cycle checkpoint, essential-vs-best-effort write ordering.
- **Deterministic activity-escalation mechanism** (`run_backtest.py`): 20-trading-day blocks, ≥5 completed trades keeps the base tier, otherwise the next block escalates to 1.25:1 — permanent once triggered, harness-controlled, never a trade count shown to the trading agent.
- **Risk checker** (`risk-checker/risk_checker.py`): pure, deterministic, fail-closed enforcement of every `RULES.md` numeric rule, shared by live and backtest.
- **Point-in-time masking** (`backtest-harness/anonymize.py`): reshuffled ticker/company aliases per run, verified leak-free against real headline text (`test_anonymize.py`).
- **Trade journal** (`trade-journal-mcp/`): idempotent decision/execution recording with full field-mismatch detection on any `decision_id` collision; run/mode isolation enforced at both write and read boundaries.
- **Per-cycle evidence instrumentation** (`backtest_cycle_evidence` table, added `bfb16fe`, hardened `af0921d`) — see §7.
- **Dashboard V1**: local-only, read-only against Neon's Data API, real token/retry/rejection counts, per-run scoping, mobile-responsive. **Dashboard V2 is a separate, genuinely public Cloudflare Pages preview** (SELECT-only against the same anonymous Data API grant, `backtest_cycle_evidence` excluded) — see §10; do not read this bullet as covering V2's exposure.
- **Offline diagnostics** (not drafts, executable, human-only, never wired into the live path): `geometry_scan.py` / `counterfactual.py` (justified the Round-1→Round-2 rule changes) and `benchmark_sensitivity.py` (§7's benchmark/sensitivity report).
- **SEC EDGAR Fundamentals V1** (`fundamentals/`, 2026-09-12) — four
  distinct states, do not conflate: (1) **data foundation** —
  `get_fundamentals_snapshot(ticker, as_of_date)`, CIK resolution,
  cached/rate-limited SEC fetch, point-in-time-safe US-GAAP
  normalization, full provenance; (2) **accepted** for integration
  design (`docs/evaluations/fundamentals_v1_acceptance.md`: WMG/AAPL/JPM/BABA
  live, 0 provenance/as-of violations); (3) **backtest-only integration
  implemented, then correction-pass hardened (2026-09-12)** —
  `run_backtest.py --enable-fundamentals` (opt-in, default off, zero
  effect on any existing/default run incl. preserved Round 2), contextual
  evidence only, never an independent buy trigger or mandatory veto
  (`AGENTS.md`). Correction pass added: a durable per-run invariant
  (`_resolve_fundamentals_enabled`, persisted in `backtest_state.json`,
  fails closed on a resume conflict, legacy state defaults disabled); a
  private non-agent-facing raw-SEC-provenance audit column
  (`backtest_cycle_evidence.fundamentals_provenance`, narrow nullable
  schema addition, reused from checkpoint on replay); enforced technical
  primacy (a fundamentals-enabled BUY must declare `primary_signal`
  technical/both, `other` rejected). **Operational validation (2026-09-12,
  4 attempts, 1–3 blocked on env/image/model-account issues, all
  root-caused and fixed — see `docs/history/` for the full narrative):
  attempt 4 SUCCEEDED** after migrating the runtime model to
  `gpt-5.6-terra`. Smoke run `31bd7421-...` completed 3/3 cycles: 3 BUY
  proposals, all rejected at execution-time risk checks, 0 fills.
  Confirmed: real SEC provenance recorded privately, zero real
  ticker/CIK/filing identity anywhere agent-facing, `anonymous` still zero
  grants, Round 2 untouched throughout (sha256-verified each attempt). (4)
  **still not done:** live mode; any training/evaluation run — the path
  works end-to-end but no BUY has ever filled under it — frozen pending
  that decision.
- **Regime-diverse evaluation suite** (`backtest-harness/eval_suite.py`,
  `test_eval_suite.py` 128 assertions, implemented 2026-09-13, corrected
  twice same day) — infrastructure only, not an approved experiment
  (`docs/PROJECT_BACKLOG.md` §4). Manifest generation classifies SPY
  months by a fixed pre-committed rule, stratified-samples a comparison
  suite + later disjoint holdout, and writes a versioned, SHA-256-hashed
  `"proposed"` manifest. **First correction pass**: non-bypassable
  approval (`run_configuration()` is the only public executor,
  `_run_episode` internal, verified before any register_run/journal/
  market-data call), `fill_count` derived from `run_day`'s new
  structured `summary["fills"]`/`["proposals"]` instead of `decisions`
  text, and guaranteed 4-regime holdout coverage via backward pool
  expansion. **Second correction pass (experiment design)**: (1)
  **"sideways" is now genuinely quiet**, not a residual bucket — requires
  both a small net move (±2%) AND low daily variance (<1.0 pct pt); a
  month meeting none of the 4 required regimes is `"unclassified"` and
  excluded from both pools, never forced into sideways — this exposed
  and fixed a latent bug in `select_holdout_pool`'s own stop condition
  (assumed a dense month list; now stops at the calendar boundary of the
  earliest eligible month); (2) a **read-only fixed-watchlist viability
  audit** now runs for every selected episode — `run_backtest.
  fetch_technicals` (the SAME real production path) checked at the
  episode's first/last trading day per ticker, plus a purely descriptive,
  never-gating monthly movement range; `propose_manifest` fails clearly,
  naming the ticker/month, if any watchlist name lacks enough point-in-
  time data; `run_configuration` now also refuses any manifest whose
  `watchlist_viability` is missing/malformed/failing, even if approved
  and hash-valid. **Manifest regeneration still blocked, by design, on a
  real finding, not a bug** — see §14 for the 2026-09-15 attempt at the
  user's own chosen range (`2022-06-01`), why it's ALSO infeasible, and
  the exact tradeoff still unresolved. The committed
  `eval_suites/manifest_20260913T154853_887d30cff268.json` stays stale
  (`manifest_version: 2`) and `run_configuration` correctly refuses it.
  Still **defined but never invoked** either way — no episode,
  comparison, or Bayesian search has run.

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
  proposed/no_proposal status, and rules-file hashes per cycle — lets a
  later audit check the trading agent actually read what it was shown,
  which tranche 1 cannot support.
- Real runtime `model`/`provider`/`reasoning_effort` fields on evidence rows are `NULL` so far (never reported by the real usage payload) — populates automatically once reported.

**Evaluation status:** the full 50-cycle run has been evaluated on two
levels. Read first: `docs/evaluations/round2_50cycle_evaluation_extract.md`
— the active, compact brief. Row-level detail: the archived raw reports
it links to, `docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md`
(cycles 1–50) and `..._tranche1_evaluation_extract.md` (cycles 1–25) —
open only if genuinely needed.

**Benchmark and sensitivity pass (2026-09-05, corrected across four 2026-09-06 passes — status current as of the fourth):** `docs/evaluations/round2_benchmark_sensitivity.md` — the trading system vs. cash/SPY/equal-weight-watchlist buy-and-hold/a defined SMA(10) technical baseline over 50 identical dates, plus sensitivity to `RULES.md`'s risk%/cap%/reward-threshold parameters (every stop/target walk validates the complete intraday session before use, `MissingDataError` on any gap — none in real data; every %/$ pair comes from one shared-formula function, `reconciliation_table`, never hand-typed — full defect list in the report's own correction-history note). Headline: the trading system (+3.696% full portfolio, −2.46% drawdown, 20.0% exposure) beat the watchlist (−2.66%) and technical baseline (+0.39%, flat, double the volatility) on return-per-volatility, though SPY (+4.20%) edged it on raw return at far higher exposure and a one-day timing head start. The concentration cap remains the more consequential lever (`chronological_cash_constrained_approximation`, never "fully replayable").
**Verdict unchanged: improve measurement first** — not sufficient for Bayesian optimization, still unapproved.

**Per-trade journey report** (offline, read-only, human-evaluation-only, `backtest-harness/trade_journey_report.py`, `docs/evaluations/round2_trade_journey.md`): reconstructs each closed trade's daily price/P&L/stop-target path from data the harness already writes (no new capture). Round 2: 14/16 closed trades have a complete daily journey, the other 2 genuine same-session stop-outs. Explains actual five-day time-stop outcomes only — **not** a longer-hold or stop-distance-band counterfactual (needs re-fetched bars).
Backs this with a durable **exit provenance** field (`trades.exit_provenance`, `schema.sql`) — deterministic harness exit (`harness_stop`/`harness_target`/`harness_time_stop`) or agent-requested, set server-side, never inferred from prose; pre-migration rows read `legacy_unknown`. Reconciled Round 2 from structural evidence, not note wording: 5/0/3 harness stop/target/time-stop, 8 agent-requested (7 with a time-stop rationale, 1 thesis-invalidation) — corrects `round2_50cycle_evaluation_extract.md`'s exit-reason-mix table (was 6/2, proven 5/3). Migration re-verified live on Neon 2026-09-12: column, constraint, and all 16 `legacy_unknown` backfill rows confirmed present, no other row touched.

---

## 8. Known limitations and deferred work

- **Live bracket orders** — `alpaca-mcp`'s `place_order` submits one order, no OCO/bracket; nothing rests a real stop at the broker live.
- **Broker partial fills** — not modeled in either mode.
- **Circuit-breaker latching** — breakers re-evaluate fresh every cycle from live P&L, not latched for the rest of a halted period.
- **Survivorship bias** — the watchlist was screened using data from inside the exact backtest window.
- **Asyncpg statement-cache fix's narrow scope** — `statement_cache_size=0` is only applied in `trade-journal-mcp/server.py`.
- **No benchmark series wired into the dashboard** — an offline comparison exists (§7), no live index-comparison line.
- **Neon's anonymous-role exposure** — read-only, SELECT-only; `backtest_cycle_evidence`/`backtest_run_configuration` excluded (verified live).
- **Per-trade (vs. per-cycle) token attribution** remains only approximate.
- **Cycles 1–25 have no exact evidence record** (see §7) — a real, permanent, accepted gap, not an oversight to fix later.
- **Longer-hold/stop-distance-band counterfactuals not mechanically identifiable** — §7 covers actual time-stop outcomes only; needs re-fetched intraday bars.

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
- **`quota_pacer.py`** — removed entirely (`750d80d`). Do not reference it,
  `quota_pacer_state.json`, or a "weekly quota" as current.
- **OpenCode Go + DeepSeek V4 Flash** — investigated, abandoned (a real, unresolved upstream hang). Provider is still `openai-codex` (model
  migrated `gpt-5.4-mini`→`gpt-5.6-terra` 2026-09-12, §5/§6 — a compatibility fix, not a return to this abandoned alternative).
- **Four Round-2 smoke-test `run_id`s** — superseded, before the real
  tranche started. **Never resume any of them.**
- **Prior repair passes** (pre-tranche crashes/bugs) — no longer operationally
  relevant. Full narrative: `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md`, `docs/history/HANDOFF_FULL_2026-08-28.md`.

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
candidate, not yet cut over to be the active dashboard** — V1 stays
active until an explicit cutover decision, but V2 is already a genuinely
**public** Cloudflare Pages preview (`dashboard-v2/README.md` §Deployment)
— not loopback-only; corrects §4/§6 (prior audit H3). Then the
individual-trade view, then the experiment/optimization framework.
NEXT/LATER canvas items stay discussion-only until explicitly approved.

---

## 11. Minimal source-file map

| File | What it is |
|---|---|
| `verify.py`, `RULES.md` | `python verify.py`: one non-mutating command for every safety-layer suite pytest alone silently skips. `RULES.md`: authoritative, locked rules — position sizing, caps, circuit breaker, prohibitions, Aggressive Phase 1 profile. |
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
cycles this file documents, and now also needs `--confirm-destructive-
reset` (§13). (Registered `training`; `--run-kind` is fresh-run-only.)

---

## 13. 2026-09-15 correction pass — data integrity & reproducibility

Read-only-audit-driven fixes, not a strategy change; Round 2/`RULES.md`/
`AGENTS.md`/`SOUL.md`/model untouched. New FUTURE-run invariants: no raw
SEC magnitudes in fundamentals prompts; a BUY needs its alias in that
cycle's real candidates; a >50% technicals outage halts the cycle, not a
quiet-day entry; `fetch_bars` pins raw prices; a run's watchlist+alias
mapping freezes in `backtest_state.json` and, durably, in Neon's private
`backtest_run_configuration` — **ACTIVE, live-verified 2026-09-15b** (no
`anonymous` grant/RLS, zero existing rows touched); idempotent write,
resume verifies against it, closes the long-term alias-mapping
reproducibility gap for new runs — Round 2 predates it, untouched.
`--reset` needs `--confirm-destructive-reset` over real progress.
`run_kind` gains `evaluation` (`docs/PROJECT_BACKLOG.md` §4 — stale v2
manifest still correctly refused, reconfirmed live). `verify.py` (§11)
covers the non-mutating suites. Still blocked on everything §10 blocks.

---

## 14. 2026-09-15c fundamentals-measurement block — blocked before any run

`ELIGIBLE_RANGE_START=2022-06-01` (RYAN/ALGM viability floor, §6) +
`EPISODES_PER_REGIME=1` now committed in `eval_suite.py`, per the user's
frozen design. `propose --seed 20260915` still fails: the only 2 real
`selloff` months (2022-09/12) force the holdout back to 2022-12, leaving
the comparison pool with zero `sideways` months — infeasible by
construction. **No manifest, no episode, no Hermes call.** Full
diagnostic: `docs/evaluations/fundamentals_v1_measurement_block_2026-09-15.md`.
Conclusion unchanged: **keep Fundamentals V1 as unproven context**.
Incidental fix: `test_eval_suite.py` was silently writing real
`cycle_failed` Neon rows with Docker up (missing `fail_logger` mock) —
fixed at the source; 3 pre-existing orphaned rows are documented, not
deleted (blocked by this environment's own safety gate). Roadmap:
"Dynamic company selector" removed (cancelled, `docs/PROJECT_BACKLOG.md` §2).

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
