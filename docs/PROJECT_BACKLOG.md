# Trading agent project backlog

The single permanent store for possible and approved future work,
organized by stable engineering sector — not a scattered feature list.
`HANDOFF.md` is current verified operational state; this file never
restates it. `docs/DISCUSSION_QUEUE.md` is unresolved decisions only;
once a discussion resolves into an approved or candidate item, it moves
here (and is removed there — never duplicated in both places).

**Status:** `discussing`, `approved`, `implemented`, `deferred`, or
`rejected`. **Priority (non-trivial items only):** `Now`, `Soon`,
`Later`. Major architecture or trading-rule changes require an explicit
user decision before Claude Code changes files.

---

## Global rules (apply across every sector)

- **Round 2 baseline is frozen.** No casual training runs, no
  `RULES.md`/`AGENTS.md`/`SOUL.md`/model changes, no Bayesian
  optimization, no fundamental-data *agent integration or trading-behavior
  change* — until the measurement/evaluation-track decision (`HANDOFF.md`
  §10) is made. (A standalone, offline SEC EDGAR data-foundation module
  was implemented 2026-09-12, `fundamentals/` — §2 below — explicitly
  scoped to never touch the trading path; the freeze on injecting it
  into agent reasoning stands.)
- **Dashboard V2 is deployed** (Cloudflare Pages, `mobile-test` preview
  branch) for manual mobile feedback. Only real phone issues found
  during that testing should reopen dashboard work — not new features.
- **No LLM chart-pattern / graph-vision system.** Numeric technical
  indicators (RSI/MACD/trend/support-resistance) remain the only
  chart-derived evidence. This is a locked scope boundary, not an open
  item.
- **Human-approval boundary is absolute.** An evaluator (or any future
  agent) may *recommend* changes but may never rewrite `RULES.md`,
  `SOUL.md`, or trading-system behavior itself. Recommendations stay
  advisory through the user/Claude workflow.

---

## 1. Orchestration, Context & Memory

**Mission:** keep the Hermes harness's rules/context packaging and the
reflection-memory mechanism honest — never mistake "the agent received
information" for "the agent learned something real."

**Implemented:** `RULES.md` mounted read-only, re-read in full every
cycle (no partial-rules router); sentiment-is-veto-only (§5a), the
project's deliberate prompt-injection defense; deterministic §6b
activity-escalation (harness-controlled, never trading-agent-chosen);
reflection injection (latest 2 run-scoped notes/cycle, new one every
5th cycle).

- **Now:** none — baseline frozen.
- **Soon:** Audit cycles 26–50 `backtest_cycle_evidence` for whether
  reflections measurably changed later decisions (not just were
  received). Status `discussing`. Prerequisite: none to *run* the
  audit (read-only, no training); required *before* any change to
  reflection retention/cadence/design. See `docs/DISCUSSION_QUEUE.md` #3.
- **Later:** Split `RULES.md` into always-on binding rules + optional
  harness-selected reference material (never trading-agent-selected).
  Status `deferred`. Prerequisite: baseline unfreezes AND `RULES.md`'s
  real length is judged an actual problem. See
  `docs/DISCUSSION_QUEUE.md` #5. Do not introduce `MEMORY.md`/`USER.md`
  or any other uncontrolled backtest memory source.

**Rejected:** OpenCode Go / DeepSeek V4 Flash gateway switch —
unresolved upstream hang; locked model stays `openai-codex`/`gpt-5.4-mini`.

---

## 2. Market Intelligence & Data

**Mission:** expand what evidence the trading agent can see without
ever letting a new source become a disguised buy trigger.

**Implemented:** technicals (`maverick-mcp`: RSI/MACD/trend/support-
resistance) as entry evidence; news (`google-news-rss-mcp`/backtest
historical) as a negative veto/exit signal only, never standalone;
fixed 10-ticker watchlist universe. **SEC EDGAR Fundamentals V1 data
foundation** (`fundamentals/`, 2026-09-12): `get_fundamentals_snapshot(ticker, as_of_date)`
resolves ticker→CIK, fetches/caches SEC submissions + company-facts
(SQLite cache, gitignored, standard-library HTTP client, required
`SEC_EDGAR_USER_AGENT`, ≤5 req/s, bounded retries), and returns
point-in-time-safe, provenance-carrying revenue/net income/operating
cash flow/cash/total debt/current-assets-liabilities plus derived YoY
growth/net margin/current ratio — only when comparable source periods
exist, never a custom-tag guess. Validation CLI only
(`python fundamentals/edgar.py --ticker X --as-of YYYY-MM-DD`); see
`fundamentals/README.md`. **Acceptance milestone complete** (2026-09-12,
`docs/evaluations/fundamentals_v1_acceptance.md`): validated live
against WMG/AAPL/JPM/BABA, 0 provenance/as-of/unit/form violations,
conservative fail-closed behavior confirmed for a bank (JPM) and a
foreign private issuer (BABA). Verdict: **accepted for future
integration design.**

**SEC EDGAR Fundamentals V1 — backtest-only integration** (2026-09-12,
`run_backtest.py`): the harness now CAN attach a compact, masked
fundamentals summary to each entry candidate, opt-in via
`--enable-fundamentals` (default off — every existing/default backtest
invocation, including the preserved Round 2 run, is byte-identical
without it). Fundamentals stay contextual evidence only — may
strengthen or temper confidence in an already-valid technical setup,
never independently create a BUY, never a mandatory veto like news
(`AGENTS.md`'s new fundamentals paragraph states this to the trading
agent). Structured per-decision recording (was fundamentals evidence
available; did it support/temper/not-affect the decision; was the entry
technical-only) is embedded as a fixed, harness-controlled
`[fundamentals:...]` prefix on the existing `trades.strategy_notes`
column — no Neon schema change this milestone. Full masked
prompt/input/response evidence is preserved via the existing
`backtest_cycle_evidence` path, unchanged.

**Correction pass** (2026-09-12, same file): three experiment-integrity
gaps closed before any real cycle runs. (1) The fundamentals profile is
now a **durable per-run invariant**, persisted in `backtest_state.json`
(`fundamentals_enabled`) and enforced by `_resolve_fundamentals_enabled` —
a resume reuses its stored setting, a legacy state file with no stored
key (Round 2 included) safely resolves to disabled, and a conflicting
`--enable-fundamentals` on resume fails closed before any fetch/prompt/
journal write. (2) A new **private, non-agent-facing audit column**
(`backtest_cycle_evidence.fundamentals_provenance`, narrow schema change —
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, nullable, no data migrated)
retains the full raw SEC provenance (CIK, entity name, per-metric
filed_at/form/accession_number/source_url) per candidate per cycle,
linked by alias to the masked candidate — that table is already
un-granted to the `anonymous` role and not RLS-enabled, so this reuses
its existing protection boundary rather than a new one; a checkpoint
replay reuses the checkpointed provenance verbatim, never a fresh
re-fetch. (3) **Technical primacy is now enforced**, not just policy
text: when the profile is on, a BUY's `primary_signal` must be
`technical` or `both` (`other` is rejected) — a disabled-profile response
is completely unaffected.

**Operational validation block, part 1 (2026-09-12):** migration applied
and verified live on Neon — `backtest_cycle_evidence.fundamentals_provenance`
(JSONB, nullable) exists; the `anonymous` role has zero grants on that
table (confirmed via `information_schema.role_table_grants`), unchanged
protection boundary. Smoke run not executed that session — blocked by
`SEC_EDGAR_USER_AGENT` unset anywhere in the environment.

**Part 2 (2026-09-12, same day):** `SEC_EDGAR_USER_AGENT` supplied for
this session only (shell env, never written to `.env` — confirmed via
`git status`/grep before and after). Migration reconfirmed unchanged.
Round 2's local state backed up (sha256-hashed) and isolated (moved, not
deleted — no `--reset` used) before the run. **Infra defect found and
fixed (not a fundamentals-milestone code change):** the running
`trade-journal` container's image was built 2026-08-30, predating the
`backtest_run_registry` migration — `register_run()` failed with
`ImportError: cannot import name '_register_run'`. Rebuilt via
`docker compose build trade-journal && docker compose up -d
trade-journal` (picks up already-committed `server.py`, not a code
edit) — this would have blocked **any** fresh backtest run, fundamentals
or not, and stays fixed going forward. One smoke run then executed:
`run_id c5987cda-71f1-4585-85c7-c885817fe187`, classified `smoke`.
**Day 1 of 3 failed and the run halted** — `ResponseValidationError:
response missing a 'decisions' list` (Hermes' real reply didn't match
the required schema across both the original attempt and its one
retry). Confirmed clean failure: no `backtest_state.json`/checkpoint
written, zero `trades`/`backtest_cycle_evidence`/`portfolio_snapshots`
rows for this run_id (only the run's registry classification and one
`cycle_failed` activity row exist). Round 2's state restored immediately
after and its sha256 hash verified identical to the pre-run backup; Neon
Round 2 row counts (`trades` 64, `backtest_cycle_evidence` 25,
`portfolio_snapshots` 50) unchanged.

**Part 3 (2026-09-12, same day) — diagnosed, not a fundamentals defect:**
per instruction, treated the failure as an undiagnosed response-contract
issue rather than assuming fundamentals caused it; made no trading-logic/
rules/prompt/model change. Added a **failure-only diagnostic seam**
(`ask_hermes` in `backtest-harness/run_backtest.py`): fires ONLY when
both the original reply and its one corrective retry fail to parse/
validate; writes one bounded, redacted record per attempt to local,
gitignored `backtest-harness/.diagnostics/{run_id}_day{N}_attempt{n}.json`
(reply length, JSON-parse success, top-level type/keys, validation error,
a 2000-char redacted raw excerpt) — never Neon, dashboard, `strategy_notes`,
or agent prompts. Fail-closed behavior, retry count, and the exact
exception reaching the cycle-halt path are all unchanged; 6 new pure
tests added and passing. Committed separately
(`a8897c4`) before the retry.

Retried the identical protected smoke procedure once more (same backup/
hash/isolate/restore discipline). **Same failure, now root-caused**: the
diagnostic's redacted excerpt for both attempts was identical —
`HTTP 400: {"detail":"The 'gpt-5.4-mini' model is not supported when
using Codex with a ChatGPT account."}` — the raw text this project's own
`json_from` happily parses as a dict (`{"detail": "..."}`, hence
`json_parse_ok: true`) and then correctly rejects for lacking
`decisions`. This is a **Codex/model-account configuration issue**
upstream of Hermes' own reasoning entirely, **not** a fundamentals or
response-schema defect (`decisions` predates this milestone). Run_id
`917cc076-85b7-4bc8-800e-811c0f47c35f`, classified `smoke`, same clean
fail-closed outcome (0 trades/evidence/snapshot rows), Round 2 restored
and sha256-verified identical again.

**Not green for a training decision** — the fundamentals-enabled path
has still never completed one full cycle end-to-end, now for a reason
entirely outside this milestone's own code: `RULES.md`/§3's locked model
(`openai-codex` / `gpt-5.4-mini`) appears incompatible with the current
Codex/ChatGPT-account auth configuration in this environment. That is a
separate, pre-existing infrastructure/account issue for the user to
resolve — not something this validation block's scope authorized fixing.
**Still NOT done:** (1) live mode; (2) a fundamentals-enabled cycle
actually completing (0/3 so far, blocked on the model/account issue
above); (3) any training-evaluation run — frozen, unapproved.

- **Now:** none — fundamental-data *agent live-integration and any new
  training run* remain frozen/unapproved.
- **Soon:** none.
- **Later:**
  - Live mode fundamentals integration (tool design, freshness policy,
    failure handling). Status `approved` (direction only). Prerequisite:
    baseline unfreezes; a separate explicit design approval — the
    backtest-only integration above is not itself that approval.
  - The first fundamentals-enabled backtest run (smoke, then training).
    Status: implementation complete, **run not yet started** — needs
    explicit user go-ahead per this milestone's own instructions, plus
    a working Docker environment to actually execute a cycle.
  - Cache-first, on-demand fundamentals router wired into a real live
    trading cycle (check stored data first, fetch only when
    missing/stale). Status `approved` (direction only). Prerequisite:
    freshness policy + baseline unfreezes — the V1 foundation's own
    cache already covers "check stored data first" at the module level,
    and the backtest-only integration above reuses that cache directly.
  - Fundamental observation period before entry (let the agent watch a
    company across days when urgency is low). Status `discussing`.
    Prerequisite: must not become a blanket waiting rule that
    suppresses valid trades.
  - Source convergence for any future evidence source (Reddit/social,
    fundamentals): no global percentage-weight scoring; a new source
    needs a defined role + independent corroboration, and positive
    sentiment alone — from any source, alone or combined — can never
    create a buy. Status `deferred`. Prerequisite: a concrete proposal
    to add a new source. See `docs/DISCUSSION_QUEUE.md` #4.
  - Candidate-level rejection provenance. Status `discussing` / `Later`.
    Record why each evaluated candidate was not proposed, using a small
    structured taxonomy rather than free-text-only activity. This must
    distinguish an adverse-news veto (and the evidence it relied on) from
    technical no-setup, invalid geometry, unavailable news, and
    deterministic risk rejection, so source effects can be measured rather
    than inferred from prose. Prerequisite: design it alongside the first
    new evidence source; do not retrofit it as another isolated logging
    feature or alter current entry behavior.
  - Reuse Maverick for broader dynamic screening. Status `discussing`.
    Prerequisite: confirm its real screener fields + point-in-time
    behavior before designing around it.
  - News prefetching/caching outside the trading window. Status
    `discussing`. Prerequisite: freshness/deduplication/point-in-time
    rules; news stays veto/exit-only.
  - Broader dynamic opportunity screening beyond the fixed watchlist.
    Status `discussing`. Prerequisite: avoid hard-coded "safe"
    tickers, survivorship bias, and uncontrolled universe churn.
  - RAG-backed filing/document retrieval. Status `deferred`. What it is:
    retrieval of the relevant *passages* from a large, point-in-time-
    gated corpus of raw SEC filings, earnings materials, and later news
    — **not** a replacement for normalized financial metrics, and not
    justified merely because the ticker count grows. Prerequisites:
    fundamentals ingestion already exists; primary-source
    citations/provenance are retained on every passage; retrieval is
    cut off by the simulated date (no lookahead); the agent receives
    only the retrieved evidence, never unrestricted document access.
  - Point-in-time dynamic candidate selector. Status `discussing`. Goal:
    replace the fixed 10-name watchlist as the *live* system's scaling
    mechanism, without turning selection into a hidden trade signal.
    Pipeline: broad eligible universe → deterministic low-bar selector
    → LLM trading-agent judgment → risk checker → execution. The
    selector only narrows attention — it must never itself judge a
    trade good or create a buy trigger. High-recall only: eligibility,
    liquidity, history, price, and news-coverage criteria — never
    predictive scoring. Must run in both modes: live selects from the
    current broader universe; backtest reproduces the identical
    selection logic using only data available as of each simulated
    date, and selected candidates stay alias-masked before reaching the
    trading agent, same as today. Required safeguards before any
    implementation: point-in-time universe membership (no survivorship
    bias); historical `as_of` gating on every selector input; a durable
    per-cycle record of both selected and rejected names with reasons;
    a fixed-watchlist baseline preserved for comparison; selector impact
    evaluated separately from trading-agent judgment and risk-rule
    effects. Prerequisite: do not implement, alter the current fixed
    watchlist, restart training, or change rules until the frozen
    Round 2 measurement/evaluation decision (`HANDOFF.md` §10) is made.
  - Measurable technical-strategy library. Status `discussing` / `Later`.
    Evaluate a small number of objective swing-trading concepts (for
    example momentum, mean reversion, breakout, relative strength, or
    event response) as separate deterministic evidence features. Do not
    reproduce discretionary named frameworks wholesale or add LLM chart
    interpretation. Each feature needs a defined calculation, point-in-time
    data, a single stated role in entry/exit judgment, and comparison against
    the current technical baseline across the pre-committed regime suite.
    Prerequisite: richer measurement and the evaluation-track decision; add
    one feature at a time, not a combined strategy stack.

**Locked scope boundary:** no LLM chart-pattern/graph-vision analysis
— see Global rules.

---

## 3. Execution, Risk & Security

**Mission:** every dollar-affecting decision passes a deterministic,
fail-closed gate before it can execute, live or backtest — and the
same discipline extends to anything the public can reach.

**Implemented:** `risk_checker.py` — pure, deterministic, fail-closed
enforcement of every `RULES.md` numeric rule, shared live/backtest;
decision-time vs. execution-time double validation; one-lot model (a
SELL must close the whole position); hard prohibitions (§9: no
averaging down, no widening/removing a stop, no off-watchlist trades,
no same-ticker double position, no same-day stop-out re-entry, no cap
overrides); Dashboard V2's public preview is anonymous, **SELECT-only**
against Neon's Data API — `backtest_cycle_evidence` is excluded from
that grant (verified).

- **Now:** none.
- **Soon:** Dashboard authentication / access hardening review. Status
  `discussing`. Why now: Dashboard V2 is genuinely public (Cloudflare
  preview) for the first time, even though read-only and SELECT-only.
  Prerequisite: decide whether masked/aggregate backtest data at this
  sensitivity level needs auth before any wider hosting, or whether
  the current anonymous-SELECT-only boundary is sufficient long-term.
- **Later:**
  - Live bracket/OCO orders (broker-side protective exits — today's
    `alpaca-mcp` submits a single order only). Status `deferred`.
    Prerequisite: live-mode order design.
  - Ambiguous-write reconciliation/retry (check broker state after a
    timeout/5xx before retrying, so an order is never duplicated).
    Status `deferred`. Prerequisite: needed before serious continuous
    live-paper operation.
  - Broker partial-fill handling (not modeled in the current one-lot
    flow). Status `deferred`. Prerequisite: same as above.
  - Deterministic news-verification gate for live entries. Status
    `approved` / `Later`. Before live-paper activation, a new BUY must
    carry an explicit successful, fresh news-fetch verification; an
    empty-but-successful headline result is valid, but an error, missing,
    or stale result is not. Reject only new entries when verification is
    absent — exits remain permitted regardless of news availability.
    Prerequisite: wire the status into the live entry-validation path,
    not merely agent instructions. The backtest already fails closed when
    its historical-news fetch fails.

---

## 4. Evidence, Evaluation & Optimization

**Mission:** never treat a small or ambiguous sample as proof, and
never optimize before measurement is trustworthy.

**Implemented:** `backtest_run_registry` — durable `training`/`smoke`/
`archived` classification, required before any fresh run or Hermes
call; `backtest_cycle_evidence` (cycles 26–50: full masked prompt/
input/response per cycle); benchmark & sensitivity report
(`docs/evaluations/round2_benchmark_sensitivity.md` — trading system
vs. cash/SPY/watchlist/SMA(10) baseline, plus rule-parameter
sensitivity) — **this was previously misfiled as "deferred" in this
document; it is implemented.**

- **Now:** the evaluation-track decision itself (`HANDOFF.md` §10):
  (a) existing evidence suffices for Bayesian optimization, (b) run
  another unchanged tranche first, or (c) improve measurement first.
  Status `discussing` — the report's own recommendation is (c), a
  recommendation to weigh, not a decision made.
- **Soon:** none — everything below is gated on the §10 decision above.
- **Later:**
  - Review aggressive Phase 1 rule numbers against filled/closed-trade
    evidence. Status `deferred`. Prerequisite: baseline unfreezes;
    never change `RULES.md` from a tiny sample or P&L alone.
  - Richer per-trade measurement capture (time-stop horizon and
    stop-distance-bound sensitivity are not mechanically identifiable
    today; per-trade token attribution is only approximate). Status
    `deferred`. Prerequisite: a future training run designed to
    capture it.
  - Survivorship-bias mitigation (point-in-time universe construction
    — the current watchlist was screened using in-window knowledge).
    Status `deferred`. Prerequisite: baseline unfreezes.
  - Separate evaluation agent that reviews completed cycles and
    proposes judgment/rule/UX/system improvements. Status `discussing`.
    Prerequisite: must use run-isolated, point-in-time evidence and
    report real sample size.
  - Regime-diverse sampled evaluation suite. Status `discussing`. Role:
    a *prerequisite for* Bayesian optimization, not an approved
    experiment. Goal: replace single-window-only evaluation (today's
    evidence is one continuous 50-day window, `docs/evaluations/
    round2_benchmark_sensitivity.md`) with a fixed, pre-committed
    stress suite of separate one-month backtest episodes drawn from a
    broader historical period, testing robustness across rallies,
    selloffs, high-volatility conditions, and sideways markets —
    reducing the chance of optimizing to one continuous window.
    Required design constraints before any implementation: define the
    regime-classification and stratified sampling method *before*
    seeing any candidate results (pure random month selection cannot
    guarantee regime coverage); record the eligible historical range,
    selected months, selection method, and random seed before comparing
    configurations; reset portfolio state independently per one-month
    episode; run every candidate configuration against the identical
    pre-committed episode suite; keep a separate, untouched set of
    later and regime-diverse months for final out-of-sample evaluation;
    preserve point-in-time gating, alias masking, next-day execution,
    and durable `training`/`smoke`/`archived` classification in every
    episode; evaluate selector/configuration impact separately from LLM
    trading judgment and deterministic risk-rule effects. This does not
    approve implementation, restart training, change rules, or begin
    Bayesian optimization — the existing `HANDOFF.md` §10 measurement/
    evaluation decision remains the gate.

**Locked principle:** human-approval boundary for evaluator
suggestions — see Global rules. Status `implemented`.

---

## 5. Platform, Reliability & Operations

**Mission:** keep the hub-and-spoke Docker/Neon stack durable and
observable without expanding scope during a healthy run.

**Implemented:** hub-and-spoke Docker topology, pinned image digests,
no tool container can reach another; Neon Postgres as single source of
truth for decisions/executions/activity/insights/snapshots/evidence,
`mode`/`run_id`-isolated; idempotent writes with full field-mismatch
detection on any `decision_id` collision; durable per-cycle checkpoint
(essential-vs-best-effort write ordering — a crash after commit can
lose dashboard detail but never duplicate a trade).

- **Now:** none.
- **Soon:**
  - Verify a finite outer timeout on Maverick/yfinance fetches, so one
    provider hang can't stall a cycle indefinitely. Status `deferred`.
    No architectural approval needed; implementation still reviewed.
  - Preserve failure tracebacks for training-cycle failures
    (observability-only). Status `discussing`.
- **Later:**
  - Continuous live paper-trading scheduler (run the agent
    automatically on a defined cadence after training). Status
    `discussing`. Prerequisite: live mode has never run — cadence,
    recovery, and market-calendar behavior all need design.
  - Host the trading stack on an always-on machine (free Render is
    unsuitable for the full engine). Status `discussing`. Prerequisite:
    hosting, secrets, persistence, and monitoring plan.
  - Host the dashboard separately from the host machine. Status `idea`.
    Prerequisite: the sector 3 authentication review above, before any
    wider public hosting.

---

## 6. Interface, Learning & Communication

**Mission:** make the project's real state legible — to the user on
their phone, in the architecture vault, and to anyone outside the
project — without ever overstating what the data actually shows.

**Implemented:** Dashboard V2 (`dashboard-v2/`) — full IA rebuild
(Overview/Trades/Decisions/Runs & Evaluation/Roadmap), reuses V1's
exact read-only Neon pattern; responsive desktop/mobile shell (verified
1440/768/390/320px); light/dark theme toggle; plain-English rejection
labels; compact 20-position trade ledger; RULES.md-synced strategy
profile card; full-width honest equity chart (data-derived Y/X axes,
no fake data); trade-decision detail panel and historical-run selector
already exist in V2. **Roadmap page** — a static, zero-Neon visual
projection of this file (the six sectors, each grouped into completed/
active/next/deferred), read-only, never an editable task manager.
**Decisions page** requests only the newest 10 decision rows for the
selected run (a bounded list query plus bounded count-only queries for
the run-wide totals — never a whole-list fetch). Dashboard V1
(`dashboard/index.html`) — original monitoring dashboard, stays active
until an explicit cutover decision.
Obsidian vault (`docs/obsidian/`) — engineering-ground-truth canvases.
Public-facing LinkedIn-safe architecture canvas
(`docs/obsidian/05-linkedin-architecture.canvas`) — screenshot-ready,
no secrets/run IDs/paths.

- **Now:** Dashboard V2 manual mobile feedback collection (public
  Cloudflare `mobile-test` preview). Status `discussing`. Only real
  issues found here should reopen dashboard work — see Global rules.
- **Soon:** Dashboard V2 cutover decision (V2 becomes the active
  dashboard; V1's fate). Status `discussing`. Prerequisite: mobile
  testing above genuinely completes. See `docs/DISCUSSION_QUEUE.md` #1.
- **Later:**
  - Individual-trade experience (one trade's full entry/current/exit
    state, reasoning, invalidation condition, activity timeline in one
    place — agreed NOW-order item 3 on
    `docs/obsidian/03-project-workflow.canvas`). Status `approved`
    (direction only). Prerequisite: V2 cutover decision above.
  - Per-trade progress/price chart (movement toward stop/target).
    Status `discussing`. Prerequisite: daily snapshots may support a
    basic version; full intraday history needs additional data.
  - Recruiter/LinkedIn presentation assets beyond the existing
    architecture canvas. Status `idea`. No prerequisite; scope each
    asset individually before building.

**Note:** "The Hermes Ledger" is the user's own optional personal
study artifact — not project scope, not authoritative, not tracked
here as work.

---

*Detailed repair history, benchmark numbers, and completed-fix
narratives live in `HANDOFF.md` and `docs/history/` — never copied
here.*
