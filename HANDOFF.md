# Handoff briefing — Hermes trading agent

*Compacted 2026-09-01. Full pre-compaction history preserved at
`docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md` — read it only if
you need the exact chronological repair/verification narrative behind a
claim below; do not treat it as more current than this file.*

---

## 1. How assistants use this file

**Read this file first, completely, before touching anything else.**
Repository state (git log, current file contents, `backtest_state.json`,
Neon) is authoritative over any compacted conversation history you may
carry in — if this file and your own memory of a prior conversation
disagree, trust the repository.

**Open only the specific source files directly relevant to your assigned
task.** Do not reread the full repository, `docs/history/`, vendored
third-party code (`maverick-mcp/`), or unrelated test files unless
verification genuinely requires it. This file exists precisely so that
scan is unnecessary.

The working split: **discussion/strategy/second opinions happen in chat;
Claude Code (or Codex/OpenCode) handles execution** — reading/editing
code, running the harness, touching Neon, committing. Don't propose direct
code edits as if you'll apply them yourself; propose the *decision*, and
relay it to the executing session.

A separate personal study artifact, "The Hermes Ledger," is the user's own
optional reading — **not** required context and not authoritative. This
file and the repo's own source (`RULES.md`, `AGENTS.md`, `SOUL.md`, actual
code) are.

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
- **16 closed trades total** across the run (win/loss mix documented per-
  tranche in `docs/evaluations/round2_tranche1_evaluation_extract.md` for
  cycles 1–25; cycles 26–50 not yet separately evaluated — see §10).
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

**Verified via real, run-scoped Neon queries as of this compaction:** no
duplicate `cycle_id` or `decision_id` anywhere in this run; exactly one
`cycle_failed` activity row exists (the Day 50 incident below — preserved
intentionally, not deleted); `backtest_cycle_evidence` holds exactly 25
rows for this run (cycles 26–50 only — see §7); all 35 tranche-1 (cycles
1–25) `trades` rows are byte-for-byte unchanged from their original
values.

**Git:** branch `round2-prep-judgment-layer`, HEAD `7c4ac06`, clean except
two pre-existing untracked items (`docs/PROJECT_BACKLOG.md`,
`docs/research/` — a separate, still-iterating document; leave untouched
unless the user says otherwise). Relevant recent commits, most recent
first:

| Commit | What it did |
|---|---|
| `7c4ac06` | Fixed the Day 50 `expected_reward_risk` BUY/SELL contract mismatch (see below), migrated Neon, resumed and completed the run to 50 cycles |
| `af0921d` | Fixed 4 integrity gaps in the evidence-instrumentation review (checkpoint-replay determinism, `active_limits` capture, runtime-metadata identity handling, one evaluation-doc correction) |
| `bfb16fe` | Added `backtest_cycle_evidence` per-cycle instrumentation (exact masked prompt/inputs/response persisted per cycle) — backtest-only, no trading-behavior change |
| `3b9a22c` | Added the read-only tranche-1 (cycles 1–25) evaluation extract |

**The Day 50 incident, resolved:** cycle 9 of a resumed 25-cycle attempt
(trading-day 49, real calendar date 2026-04-07) halted because Hermes
correctly returned three discretionary time-stop SELL decisions with
`expected_reward_risk: 0.0` (the honest value — prospective entry reward
doesn't apply to a closing decision), but `trade-journal-mcp`'s Python
validation and its Neon `CHECK` constraint both still required
`expected_reward_risk > 0` for *every* judgment decision regardless of
action. `7c4ac06` made the contract action-aware everywhere (backtest
response validator, journal validation, DB constraint, tool docstring,
tests) — BUY still requires finite `> 0`, SELL/other now correctly permits
finite `>= 0`. No value is ever fabricated. The fix was verified against
the real checkpointed Day 50 payload, the migration was applied to Neon
and confirmed idempotent, and resuming (no `--reset`) correctly replayed
the checkpoint with **no new Hermes call**, journaled all three SELLs, and
the run continued cleanly to completion at 50 cycles with zero further
failures. The historical `cycle_failed` Neon row from the halt is
preserved as an honest record, not deleted.

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
same-day lookahead. Essential cycle data (checkpointed response, portfolio
state, decisions, risk checks, cycle evidence) commits atomically before
best-effort observability (snapshots, activity narration) is attempted — a
crash after commit can lose dashboard detail but never duplicate a trade
or a Hermes call.

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
  per-cycle checkpoint (a retry after any failure past the Hermes call
  never re-invokes Hermes), essential-vs-best-effort write ordering.
- **Deterministic activity-escalation mechanism** (`run_backtest.py`):
  20-trading-day blocks, ≥5 completed trades keeps the base tier,
  otherwise the next block escalates to 1.25:1 — permanent once
  triggered. A queued order stores and resolves against the exact tier
  binding when it was decided, never whatever's "current" later. Hermes'
  prompt shows only the currently-binding threshold/label, never a trade
  count.
- **Risk checker** (`risk-checker/risk_checker.py`): pure, deterministic,
  fail-closed enforcement of every `RULES.md` numeric rule, shared by live
  and backtest.
- **Point-in-time masking** (`backtest-harness/anonymize.py`): reshuffled
  ticker/company aliases per run, verified leak-free against real
  headline text (`test_anonymize.py`).
- **Trade journal** (`trade-journal-mcp/`): idempotent decision/execution
  recording with full field-mismatch detection on any `decision_id`
  collision (fails loudly, names the mismatched field); run/mode
  isolation (backtest vs. live, run-to-run) enforced at both write and
  read boundaries.
- **Per-cycle evidence instrumentation** (`backtest_cycle_evidence` table,
  added `bfb16fe`, hardened `af0921d`) — see §7.
- **Dashboard**: local-only, read-only against Neon's Data API, real
  token/retry/rejection counts, per-run scoping, mobile-responsive.
- **Offline diagnostics** (not drafts, executable): `geometry_scan.py` /
  `counterfactual.py` — used to justify the Round-1→Round-2 rule changes;
  not part of the live decision path.

---

## 7. Evidence and evaluation coverage

Two different levels of detail exist across the 50 completed cycles —
**do not conflate them**:

- **Cycles 1–25 (tranche 1, `run_id` same as above): complete trade and
  outcome data**, but **no exact per-cycle prompt/input/response
  evidence** — the `backtest_cycle_evidence` table did not exist yet when
  these cycles ran, and they were deliberately never backfilled (out of
  scope, would require re-deriving masked inputs after the fact). Fully
  evaluated in `docs/evaluations/round2_tranche1_evaluation_extract.md`:
  run-integrity extraction, account reconciliation (a real, documented
  $1.31 gap traced to one trade's stored `fill_price` precision, now
  fixed going forward), complete verbatim trade ledger, performance
  statistics with sample sizes, rejection/no-trade breakdown by
  `RULES.md` section, all reflection entries with `sample_size` accuracy
  independently checked, a 19-item data-adequacy coverage matrix, and a
  SQL/formula reproducibility appendix. Two corrections applied in
  `7c4ac06`: a lower BUY fill vs. proposed price is price-favourable
  movement, not "adverse slippage," and the 7-of-8 pattern is too small a
  sample (n=8) to explain, only to keep tracking.
- **Cycles 26–50 (25 more cycles, same `run_id`): exact masked-prompt/
  input/response evidence exists for every one** — `backtest_cycle_evidence`
  holds one row per cycle: the verbatim rendered prompt, the exact masked
  structured inputs (including `active_limits`, the real sizing numbers
  that shaped that prompt), the final validated response, every eligible
  candidate marked structurally `proposed`/`no_proposal`, Hermes' attempt
  count and retry diagnostics, and SHA-256 hashes of `RULES.md`/
  `AGENTS.md`/`SOUL.md` as they stood that cycle. This makes it possible
  to audit not just *what* Hermes decided in this range but whether it
  actually read what it was shown — something tranche 1 cannot support.
  **Not yet evaluated** — no report exists for cycles 26–50 yet. This is
  the next task (§10).
- Real runtime `model`/`provider`/`reasoning_effort` fields on evidence
  rows are `NULL` for every row so far — the real Hermes `--usage-file`
  report has never been observed to include those keys. Not a bug; will
  populate automatically if a future Hermes version reports them.

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
  (needed against Neon's pooled/PgBouncer endpoint after DDL) is only
  applied in `trade-journal-mcp/server.py`; other services connecting to
  Neon would need the same fix if they ever start doing so.
- **No benchmark series** — no index comparison line wired in.
- **Neon's anonymous-role exposure** — the dashboard's read role has no
  auth beyond SELECT-only; anyone with the endpoint URL can read the same
  journal rows. `backtest_cycle_evidence` is deliberately excluded from
  this grant (no anonymous read access at all, verified live).
- **Per-trade (vs. per-cycle) token attribution** remains only
  approximate — documented in the tranche-1 coverage matrix, unaffected
  by any later fix.
- **Cycles 1–25 have no exact evidence record** (see §7) — a real,
  permanent, accepted gap for that range, not an oversight to fix later.
- **Open §10 questions from `RULES.md`** worth a second opinion once more
  of Phase 1's real results exist: the §6a stop-floor's volatility-
  microstructure grounding at 3%, whether the §2b concentration-cap gap
  assumption still holds at a 12% max stop distance. Not urgent; not
  blocking.

**Ground rule:** `RULES.md`'s hard limits are not up for negotiation via
"better performance" arguments alone — even Hermes itself is barred from
loosening one through its own reflection. Any proposed change to a hard
number needs to go through the user explicitly.

---

## 9. Superseded decisions — brief pointers only

- **Round 1's conservative rules profile** — superseded by the aggressive
  Phase 1 profile. `docs/evaluations/run1_evaluation_extract.md`.
- **A first, conservative Round-2 draft** (kept every `RULES.md` number,
  fixed judgment purely through `AGENTS.md`) — superseded before Round 2
  started by the aggressive-profile decision instead. Archived, not an
  active option: `docs/history/round2-conservative-draft/run2_prep_analysis.md`.
- **`SOUL.md`'s Phase 1 (conservative) temperament** — replaced by the
  active Phase 2 temperament. Archived:
  `docs/history/SOUL_phase1_conservative_2026-08-30.md`.
- **`quota_pacer.py` and the 175-call/week gate** — the gate was removed
  as an ungrounded artificial limit, then the local weekly counter itself
  was deleted entirely (`750d80d`) since real per-cycle token/API-call
  metrics already covered what it existed to surface. **This file and any
  code no longer exist — do not reference `quota_pacer.py`,
  `quota_pacer_state.json`, or a "weekly quota" figure as current.**
- **OpenCode Go + DeepSeek V4 Flash** — investigated as an alternative
  model/provider, abandoned (a real, unresolved upstream hang on that
  exact combination). Not an active implementation path; the locked model
  is still `openai-codex`/`gpt-5.4-mini`.
- **Four Round-2 smoke-test `run_id`s** (`97d112e8-...`, `c55f0fc2-...`,
  `1bf9af3d-...`, `81db75e5-...`) — all superseded/completed smoke tests
  from before the real training tranche started. **Never resume any of
  them.** Full detail, if ever needed for provenance:
  `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md`.
- **Prior repair passes** (null-usage-accumulator crash, stale 1% sizing
  in prompts, day-20 escalation-boundary bug, one-share position-sizing
  defect) — all fixed and verified before the real training tranche
  began; no longer operationally relevant. Full narrative:
  `docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md` and
  `docs/history/HANDOFF_FULL_2026-08-28.md`.

---

## 10. Exact next step

**A read-only evaluation of the complete 50-cycle run — not more
training.** Specifically, it must distinguish:

- Performance and lifecycle evidence available across **all 50 cycles**
  (trade ledger, P&L, rejections, sizing correctness — the same kind of
  extraction tranche 1 already received).
- The deeper prompt/evidence auditing available **only for cycles
  26–50** (§7) — whether Hermes actually read the technicals, headlines,
  reflections, and account state it was shown, not just what it decided.
- **Tranche 1 (cycles 1–25) vs. tranche 2 (cycles 26–50) behaviour** —
  whether judgment quality, rejection patterns, or the §6a stop-margin
  rate changed across the resume, and whether the Day 50 contract-mismatch
  incident affected anything beyond the three SELLs it originally blocked.
- **Whether this 50-cycle, 16-closed-trade dataset is adequate for
  changing judgment guidance** (`AGENTS.md`/`SOUL.md`) — or still too
  small, the same honesty standard `RULES.md`/`SOUL.md` already apply to
  Hermes' own reflections.
- **Whether any measurement gap remains** before further training would
  be worth running (e.g., anything §8's limitations list still leaves
  open).

**Do not, until that evaluation is reviewed with the user:** start, reset,
or resume training; change `RULES.md`/`AGENTS.md`/`SOUL.md`; change the
model/provider/reasoning-effort configuration; do fundamental-data work;
redesign the dashboard; or begin any live deployment work.

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
| `docs/evaluations/round2_tranche1_evaluation_extract.md` | Cycles 1–25 full extract — see §7. |
| `docs/history/` | Archived/superseded material — see §9. Consult only when a task needs that specific detail. |

---

## 12. Resume command — reference only, guarded by explicit approval

**Do not run this until the user has reviewed the §10 evaluation and
explicitly says to continue training this run.**

```
python run_backtest.py --start 2026-02-17 --end 2026-08-14 --days <N>
```

Run from `backtest-harness/`. `backtest_state.json`'s stored `run_id`
(`e68525f9-ce89-4588-a57e-866f28cd99fd`) and cursor (`2026-04-29`) are
reused automatically as long as `--start`/`--end` match exactly (they do
above).

**This must never include `--reset` when continuing this run** —
`--reset` deletes local state and starts a brand-new, unrelated `run_id`,
discarding the 50 cycles this file documents.

---

*Repo: `github.com/Arjun-Nairr/Self-Trading-Agent-`.*
