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
- **Dashboard V2 is deployed** — a *preview*, not a production cutover
  (V1 stays the active dashboard, see §6/§10 in `HANDOFF.md`) — on
  Cloudflare Pages (project `trading-agent-dashboard-v2`, `mobile-test`
  branch, https://mobile-test.trading-agent-dashboard-v2.pages.dev) for
  manual mobile feedback. **Not auto-synced**: the project has no
  GitHub Git integration (direct-upload only, confirmed 2026-09-14) —
  pushing to `main` does not redeploy it; see `dashboard-v2/README.md`
  §Deployment for the manual `wrangler pages deploy` command. Only real
  phone issues found during that testing should reopen dashboard work —
  not new features.
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
unresolved upstream hang; provider stays `openai-codex` (model migrated
`gpt-5.4-mini`→`gpt-5.6-terra` 2026-09-12, a runtime-compatibility fix,
not a return to this rejected alternative — see `HANDOFF.md` §5).

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

**Part 4 (2026-09-12, same day) — runtime-model migration, SUCCESS:**
approved decision executed: `openai-codex` stays the provider,
`gpt-5.4-mini` → `gpt-5.6-terra`, reasoning stays `medium`. Effective
config confirmed to live in the PERSISTENT LOCAL gateway config
(`~/.hermes-trading-agent/config.yaml`, `model.default`) — not
`docker-compose.yml` (no env override there) and not any tracked file;
no repository source-of-truth to change, so none was touched. Changed
that one field, restarted only the `gateway` container. Cheap preflight
(one no-tool/no-data `hermes -z` call through the real container path)
confirmed `model: gpt-5.6-terra`, `provider: openai-codex`, a valid JSON
reply, no HTTP/provider error — before any cycle ran.

Retried the identical protected smoke procedure a third time (same
backup/hash/isolate/restore discipline). **Run `31bd7421-42ba-43e3-
a1cd-afb1f9d0d0d1`, classified `smoke`, completed 3/3 cycles** — the
fundamentals-enabled path's first-ever full completion. 3 BUY proposals
across the 3 days, every one `primary_signal: technical` with a valid
`technical_basis`, all ultimately rejected at execution-time risk checks
(real next-day gap/stop-distance) — 0 fills, an honest, real outcome.
Audited: all 3 cycles have an evidence row with non-null
`fundamentals_provenance` carrying genuine CIK/accession/source_url/
filed_at per candidate; zero real ticker/company/CIK/accession/URL/raw
filing date anywhere in prompt/input_payload/validated_response/
`candidate_status`/`strategy_notes` (checked exhaustively against every
real ticker and company name in the watchlist, not just the traded
ones); `anonymous` still has zero grants on `backtest_cycle_evidence`.
Round 2's local state sha256-verified identical to its pre-run backup
again; Neon Round 2 counts (`trades` 64, `backtest_cycle_evidence` 25,
`portfolio_snapshots` 50) unchanged.

**Not yet green for a training decision** — the fundamentals-enabled
*pipeline* now works end-to-end for the first time, but no BUY has ever
actually filled under it (all 3 in this smoke were rejected at
execution, not proof of a working entry path under real fills). **Still
NOT done:** (1) live mode; (2) a fundamentals-enabled cycle with an
actual fill (0 so far); (3) any training-evaluation run — frozen,
unapproved pending that decision.

- **Now:** none — fundamental-data *agent live-integration and any new
  training run* remain frozen/unapproved.
- **Soon:** none.
- **Later:**
  - Live mode fundamentals integration (tool design, freshness policy,
    failure handling). Status `approved` (direction only). Prerequisite:
    baseline unfreezes; a separate explicit design approval — the
    backtest-only integration above is not itself that approval.
  - The first fundamentals-enabled backtest run. Status: **smoke
    complete** (2026-09-12, `run_id 31bd7421-42ba-43e3-a1cd-afb1f9d0d0d1`,
    3/3 cycles, 0 fills — see `HANDOFF.md` §6). Training still not
    started — needs explicit user go-ahead.
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
    Scope (live/current-universe only): `docs/scopes/SCALABLE_SCREENER_V1.md`.
    Stage 1 (screener only, `watchlist/scalable_screener.py`) implemented and
    tested. First real-data `--dry-run` completed 2026-10-03, verdict **AMBER**
    (`docs/evaluations/scalable_screener_first_real_dry_run_2026-10-03.md`):
    every stage ran cleanly (400 names, 75 eligible), but per-name criteria
    could not be checked from dry-run output. Next: one approved weekend
    `--write` run plus a row-level audit. Laya on screened cards, Hermes
    integration, live paper, and historical/backtest support are not started.
  - RAG-backed filing/document retrieval. Status `deferred`. What it is:
    retrieval of the relevant *passages* from a large, point-in-time-
    gated corpus of raw SEC filings, earnings materials, and later news
    — **not** a replacement for normalized financial metrics, and not
    justified merely because the ticker count grows. Prerequisites:
    fundamentals ingestion already exists; primary-source
    citations/provenance are retained on every passage; retrieval is
    cut off by the simulated date (no lookahead); the agent receives
    only the retrieved evidence, never unrestricted document access.
  - Fixed-30 Laya candidate-selector prototype. Status: approved phased
    prototype — **Phase 1 (foundation) and Phase 2 (strictly opt-in
    experimental wiring behind `--selector-mode laya_fixed30_experimental`)
    implemented; no selector evaluation or activation completed.** The
    default (`off`) fixed-10 path, rules, risk limits, Neon schema, and
    dashboard are unchanged. **Standalone current-data demo completed
    (2026-10-03):** `python backtest-harness/laya_demo.py run` scores the real
    fixed-30 cards in the pinned isolated worker and saves a replayable
    top-10 artifact (`docs/evaluations/laya_selector_demo_2026-10-03.md`).
    The Laya question was corrected to a versioned explicit-criteria format
    (`noul-criteria-v2`, worker image `hermes-laya-worker:2`) after the v1
    criteria-less question was shown to answer "no" whatever the card; v2's
    state sensitivity is still weak. User decision: Laya stays as an
    experimental portfolio/CV component. A comparison against random and
    deterministic controls informs its documented limitations and is not a
    removal gate. **Connected Laya → Hermes smoke test (2026-10-03): integration
    fixes done, run NOT launched** (`docs/evaluations/laya_connected_smoke_2026-10-03.md`).
    Done and tested:
    - `--state-dir` isolates run state from the preserved Round 2 files;
    - experimental prompts carry the run's frozen 30-name masked universe
      instead of the baseline §8 table;
    - the coverage preflight now counts distinct articles in non-overlapping
      probes that end before the first simulated day.

    Blocker then: the unchanged fixed-30 universe failed that preflight in
    every window checked. **Resolved by the user-approved 60-candidate
    architecture (2026-10-03):**
    - `candidates60-v1`, qualified for 2026-09-28..10-02 into a frozen
      51-name pool;
    - the first connected Laya → Hermes → risk → simulation smoke run
      completed **GREEN**
      (`docs/evaluations/laya_connected_smoke_60pool_2026-10-03.md`), with
      fills, fill-time risk rejections, and one target exit exercised.

    **Dashboard demonstration done (2026-10-03):** the Dashboard V2
    **Workflow** page shows each simulated day's order/position and candidate
    paths from public, allowlisted workflow progress events (aggregate Laya
    counts only), with replay and live follow
    (`docs/evaluations/workflow_dashboard_2026-10-03.md`; redesigned as an
    agent-centred diagram 2026-10-04).
    **Laya vs. controls comparison done (2026-10-04)**
    (`docs/evaluations/laya_vs_controls_2026-10-04.md`; plan frozen first in
    `laya_vs_controls_plan_2026-10-04.md`). Three retrospective sessions on
    the same 51-name pool:
    - **Functional and reproducible:** inputs and scores identical to smoke
      run `5573597c`.
    - **Responsive:** every input moves the score, but mostly against its
      own stated criteria.
    - **Distinct from the deterministic biggest-mover control:** yes.
    - **Distinct from random selection:** not shown, on overlap, turnover
      or criteria-match.
    - **Useful:** inconclusive. No trading benefit is claimed.
    - **Cost:** about 35 s of CPU per 51-card day, plus a 32 s cold start;
      the controls are free.

    No further selector run is scheduled from this result.
    **Input-framing × answer-label experiment done (2026-10-04)**
    (`docs/evaluations/laya_framing_2026-10-04.md`; plan frozen first in
    `laya_framing_plan_2026-10-04.md`). A 2×2 of explained vs. production
    JSON state and neutral vs. `false`/`true` labels, on the same saved cards
    and question.
    - **Result:** no variant met the pre-registered rule and nothing
      survived confirmation. Explained context shifts and reshuffles the
      scores without following the criteria. Neutral-label identity changes
      the task metric (label bias).
    - **Pre-existing finding:** the production "true" criterion is cut at
      laya's 48-token option cap. Recorded, not changed.
    - **Next:** the production selector is unchanged, and no further
      prompt or label work is justified. Fine-tuning would first need an
      independently human-labelled, date-split card set (see the report,
      §7).

    **Laya vs. random downstream pilot done (2026-10-06)**
    (`docs/evaluations/laya_vs_random_2022-07_2026-10-06.md`; plan frozen first in
    `laya_vs_random_plan_2026-10-06.md`). One fresh July 2022 Hermes episode per arm
    (`af2dfdde…` Laya, `66ef4cdb…` seeded random), same frozen 48-name pool, run_kind
    evaluation.
    - **Operational:** correct, 20/20 sessions each. All 1,007 fetches both
      arms made returned identical data, and no selector data reached a
      prompt.
    - **Selection:** inconclusive. The heuristic hit rate was at the base
      rate, but the heuristic is unvalidated, and chance-level overlap with
      one random draw is not a quality test.
    - **Trading:** the Laya arm had the better observed outcome (+8.26% vs
      +4.02%) from one path each; the cause is unidentified. Earlier entry
      timing could be a selection pathway or variance.
    - **Code change:** shortlisted candidates are now shown to Hermes in
      neutral alias order.
    - **Evidence review 2026-10-07 (report §11):**
      - verified identity cues reached 8/20 Laya vs 14/20 random decision
        prompts, plus 1 Laya gate prompt; the leak checker shares the
        masker's dictionary and cannot see them;
      - all 45 BUY stops sat on maverick's unlabeled synthetic
        `close × 0.95` level.
    - **Next:** a masking and level-provenance repair block, with
      regression tests, before any further performance claim or run.
    - **Repair implemented 2026-10-07**
      (`docs/evaluations/integrity_repair_2026-10-07.md`; offline only):
      - **Masking:** verified identity variants are added; other parties
        are neutrally redacted; an independent known-cue/residue check
        runs on the final decision and gate prompts and at every Hermes
        call.
      - **Levels:** agent-facing levels are observed 30-session extremes
        only, with provenance. A BUY stop must equal a supplied observed
        support level, or the BUY is rejected before queueing.
      - **Checks:** 35 new regressions; suite 593 passed; `verify.py`
        12/12; no container rebuild needed.
      - **Still needed:** a separately authorized acceptance run. The
        pilot's historical caveats stand.
      - **Outside this block:** live-mode maverick levels are still
        synthetic (fixing them needs a `maverick-mcp` rebuild).
    - **Selector closed out 2026-10-07: GREEN**
      (`docs/evaluations/laya_selector_closeout_2026-10-07.md`).
      - **Implementation (`e09773c`):** a pre-fill gate identity-check
        failure now cancels only that BUY batch
        (`pre_fill_identity_check_failed`), with no model call; SELLs and
        exits still run.
      - **Offline checks:** shortlist rotation and the generated technicals
        script run against stub provider modules. Suite 597 passed, with 0
        external calls attempted; `verify.py` 12/12.
      - **Acceptance smoke:** run `5e977606…`, July 1–8 2022, 5/5 sessions.
        The audit found 0 problems: no identity cues or selector data
        reached a prompt, every BUY stop was the observed S1, and the
        shortlist rotated. There were no fills; one pending BUY was
        resolved at the boundary. Protected state is unchanged.
      - **Done as an implementation task;** the selector is not reopened.
        Trading value and calibration are unproven. Masking is finite
        known-cue protection. The universe is fixed and retrospective.
        Levels are minimal observed extremes. Live paper is inactive.
        Market-wide screening is a separate track.
    - **Prompt-injection monitor, shadow only (2026-10-07)**
      (`docs/evaluations/laya_injection_monitor_2026-10-07.md`; code `41f7152`).
      - **What it does:** opt-in `--injection-monitor` (needs
        `--laya-checkpoint`). Laya asks one versioned question
        (`injection-noul-v1`) of each masked news snippet exactly as
        rendered for Hermes, on both the decision and pre-fill paths.
      - **Where it runs and records:** its own isolated image,
        `hermes-laya-injection-monitor:1`. Records are private and
        write-once. It never blocks or alters news or trading, and the
        selector is unchanged.
      - **Offline tests:** 33 new; suite 630 passed with 0 external calls;
        `verify.py` 12/12.
      - **Real check (20 fixed fixtures):** all 10 attacks flagged; 2 of 10
        ordinary snippets falsely flagged (financial imperatives); AUROC
        0.98; load 30.7 s, batch 11.3 s.
      - **Status:** operationally complete; detection quality is
        diagnostic only. Not done: blocking, calibration, fine-tuning.

    **Fine-tuning preparation (2026-10-05); human label review required. Not
    approved for training** (`docs/scopes/LAYA_FINETUNE_PILOT_V0.md`).
    - **Built:**
      - proposed rubric `review-rubric-v0.1-proposed`;
      - a private 24-example pilot (12 reproducibly sampled real cards, 12
        synthetic) with AI-proposed labels and a blind review sheet;
      - offline converters to the upstream training and evaluation
        formats, checked against the pinned `laya` 0.3.23 and tag
        `d8a2e597`;
      - `backtest-harness/laya_ft_pilot.py`.
    - **Rubric/pilot revised (2026-10-06); human review and dataset
      expansion pending.**
      - **Task settled:** unusual technical activity, with downward and
        conflicting activity allowed to warrant review.
      - **Rubric v0.2:** "ambiguous" now means insufficient evidence only;
        borderline values are flagged but follow the anchors; overrides
        are removed.
      - **History kept:** a versioned revision; the v0.1 pilot is
        untouched.
      - **Label changes:** 5 of 24 (real 4/4/4 → 5/6/1 and synthetic
        5/3/4 → 6/4/2, as review-worthy/ordinary/ambiguous).
      - **Review sheets:** blind sheet and proposals sheet are separate;
        25 offline tests.
    - **Not done:** training, downloads, inference, new market data.
    - **Blocked on:** two independent human reviewers for the pilot
      (κ ≥ 0.6 before scaling to about 1,500 cards).
    - **Caveat:** the rubric is a fixed formula. A candidate that only
      matches it would distil the formula, which is then the cheaper
      selector. 55% of the explored real cards sit near a cut point, so
      labels are sensitive to those policy choices.
    **Pending:** the paths not yet exercised (stop/time-stop exits, the
    gate's Hermes classification).
    A dynamic, point-in-time
    market-wide selector stays out of scope. Architecture, the committed
    30-name list, and open decisions live in `docs/scopes/LAYA_SELECTOR_V1.md`.
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
that grant (verified). **2026-09-15 correction pass (item D2, freeform
red-team R2 durability):** each fresh run's frozen watchlist snapshot +
full ticker↔alias mapping now also persists durably in Neon's private
`backtest_run_configuration` (un-granted to `anonymous`, same boundary as
`backtest_cycle_evidence`) — idempotent write before any cycle work, a
resume verifies against it, fails closed on any disagreement. Closes the
long-term half of the alias-mapping reproducibility finding for new runs;
the preserved Round 2 run predates the table and is intentionally
unchanged — its historical mapping cannot be reconstructed retroactively.
**Activation (2026-09-15b):** the additive migration is now applied live
— table/columns/PK/FK confirmed, zero `anonymous`/`PUBLIC` grant, RLS
disabled, and `trades`/`insight_log`/`agent_activity`/`portfolio_snapshots`/
`backtest_cycle_evidence`/`backtest_run_registry` row counts unchanged
before vs. after. `test_run_configuration.py` run live (fresh UUIDs only):
persistence, idempotent retry, conflict rejection (names the conflicting
field), FK enforcement, and `_get_run_configuration` read-back all
verified; its own cleanup left zero residue (confirmed by re-querying
after). Unrelated: 3 pre-existing `smoke`-kind registry rows with real
`agent_activity`/`trades`/`portfolio_snapshots`/`backtest_cycle_evidence`
data were found already present (not from this activation, not from any
test — genuine prior smoke-run history never added to schema.sql's seeded
historical-classification block); left untouched as out of scope here.

- **Now:** none.
- **Soon:** Dashboard authentication / access hardening review. Status
  `discussing`. Why now: Dashboard V2 is genuinely public (Cloudflare
  preview) for the first time, even though read-only and SELECT-only.
  Prerequisite: decide whether masked/aggregate backtest data at this
  sensitivity level needs auth before any wider hosting, or whether
  the current anonymous-SELECT-only boundary is sufficient long-term.
  - **`blockedActivity`'s unbounded query (prior audit M4), evaluated
    2026-09-15 (correction pass item G) and deliberately left unchanged.**
    `dashboard-v2/js/data.js`'s `agent_activity` rejection-distribution
    query (`event_type=in.(risk_check,order_rejected)`) has no `limit`,
    unlike the sibling query at `limit=200` — because
    `rejectionDistribution`'s per-rule counts (Decisions page) are a
    genuinely **run-wide** figure; bounding the query to the newest N rows
    would silently turn it into a recent-window figure, which is worse
    than the current growth risk, not better (explicitly ruled out by this
    correction pass). A CORRECT bounded fix needs server-side aggregation
    (a Postgres view/RPC doing the count, so the client downloads a small
    aggregated result instead of every raw row) — that is a new Neon
    privilege/query surface, out of bounds for this pass and for the
    anonymous-SELECT-only boundary as it exists today. Revisit together
    with the access-hardening review above, not in isolation.
- **Later:**
  - **Live-paper controller (2026-10-08) — `implemented`, mock-verified,
    never run against the broker.** `live/` (policy: `live/LIVE_POLICY.md`;
    runbook, capabilities and limits: `docs/live_paper_runbook.md`). It
    supersedes the four items previously listed here: broker-held
    protection (an OCO sized to the *actual* filled quantity, verified by
    status, quantity AND prices), ambiguous-write reconciliation (intent
    persisted first, then lookup by client order id; restart recovery),
    partial-fill handling (fills recognised while unresolved, pending/
    partial/rejected exits settled across cycles), and the deterministic
    fresh-quote + fresh-news entry gate (missing, stale or failed news
    blocks entries only). First paper session needs its own authorization.
  - Live-paper known limits, all `deferred`: IEX quotes only (no SIP);
    replacing a stop is cancel -> confirm -> resubmit, so protection has a
    brief gap and entries pause until it is re-verified; a broker stop
    does not cap loss across a gap; one process runs everything (a long
    selector refresh delays reconciliation, not broker-held stops);
    an entry order that reaches Alpaca after the 2-minute grace shows as
    an unmanaged position; no intraday-halt or corporate-action handling;
    the injection monitor is not wired into live mode.

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

- **Now:** `baseline_v1` and `baseline_v2` (slippage-adjusted sizing) are
  both **complete** over the same 4 frozen comparison months, and the
  read-only V1/V2 comparison is written
  (`docs/evaluations/baseline_v1_vs_v2_evaluation.md`). Results are mixed:
  V2's return was higher in 2 regimes and lower in 2, and the report does
  not attribute the differences to the sizing fix. It is not a performance
  verdict. The report recommends freezing V1 + V2 as the comparison
  baseline and starting no optimization. That decision is the user's and
  is still open.
- **Also unresolved:** the separate Round 2 evaluation-track decision
  (`HANDOFF.md` §10): (a) existing evidence suffices for Bayesian
  optimization, (b) run another unchanged tranche first, or (c) improve
  measurement first. Status `discussing` — the report's own
  recommendation is (c), a recommendation to weigh, not a decision made.
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
  - Regime-diverse sampled evaluation suite. Status **`implemented`
    (infrastructure only, 2026-09-13, corrected twice same day)** — role
    unchanged: a *prerequisite for* Bayesian optimization, not an
    approved experiment or a run. `backtest-harness/eval_suite.py` (+
    `test_eval_suite.py`): (1) **manifest generation** —
    classifies each calendar month in a fixed, documented eligible range
    by a fixed, pre-committed priority-ordered rule, stratified-samples a
    comparison suite + later disjoint holdout, and writes a versioned,
    SHA-256 content-hashed manifest whose `status` is always `"proposed"`.
    (2) **future runner** — `run_configuration` (the only public entry
    point) / `_run_episode` (internal): refuses an unapproved/tampered/
    audit-incomplete manifest BEFORE any register_run/journal/market-
    data/checkpoint/state call; reuses `run_backtest.py`'s real, unchanged
    per-day loop; isolates a fresh Portfolio/masking/checkpoint per
    one-month episode (never `backtest_state.json`, never Round 2); keeps
    the EXISTING `training`/`smoke`/`archived` classification.
    **First correction pass (same day)**: non-bypassable approval
    (`run_configuration` sole public entry point, proven by zero-call
    tests), `fill_count` derived from `run_day`'s own structured
    `summary["fills"]`/`["proposals"]` instead of ambiguous `decisions`
    text, and guaranteed 4-regime holdout coverage via a documented
    backward pool-expansion search.
    **Single-episode runs (2026-09-19)**: `run_configuration(..., episode_selector=
    EpisodeSelector(year, month, regime))` runs exactly ONE approved comparison
    episode (comparison set only, never holdout; same approval/hash/version/
    viability gates first, no side effect on refusal; a halted episode stays
    halted, never retried). CLI: `python eval_suite.py run-one --manifest <approved.json>
    --year 2022 --month 7 --regime rally --checkpoint-dir <dir>` (real run:
    Docker/Neon/Hermes; add `--fundamentals` only for the fundamentals arm).
    Each episode now persists its frozen `backtest_run_configuration` record
    (watchlist/alias hashes, seed, window, fundamentals flag) before any fetch/
    Hermes call; the masking seed is derived from manifest hash + episode identity
    (shared by paired configs). Suite aggregates count completed episodes only;
    halted ones are listed in `excluded_episodes`, and all-halted gives `performance: null`.
    `run_configuration` also calls `run_backtest.preflight()` (required-service check,
    incl. `execution-sim`) after validation and before any side effect; compose now
    sets `execution-sim` to `restart: unless-stopped`.
    **Second correction pass (same day, experiment design)**: (a)
    `classify_regime`'s `"sideways"` is no longer a residual bucket — it
    now requires both a small net move (±2%) AND low daily variance
    (<1.0 pct pt); any month meeting none of the 4 required regimes is
    `"unclassified"` and excluded from both the comparison and holdout
    pools (never silently forced into sideways) — this in turn exposed a
    latent bug in `select_holdout_pool`'s expansion-stop condition (it
    assumed one classified entry per calendar month; fixed to stop at the
    calendar boundary of the earliest eligible month instead, now correct
    against a sparse eligible set); (b) a new **read-only fixed-watchlist
    viability audit** (`audit_episode_watchlist_viability`) runs for
    every selected episode, through the SAME production path
    `run_backtest.py` itself uses (`fetch_technicals` → maverick's
    `get_full_analysis`) checked at the episode's first/last trading day
    per ticker, plus a purely descriptive (never-gating) monthly
    return/volatility range per ticker; `propose_manifest` fails clearly
    and names the ticker/month if any watchlist name lacks enough
    point-in-time data (low movement alone never fails it);
    `run_configuration` separately refuses to execute any manifest whose
    `watchlist_viability` is missing, malformed, or failing, even if
    approved and hash-valid (content hash also covers the audit, so
    tampering with it is caught the same way as any other field).
    **Manifest regeneration is currently blocked on a real, verified
    finding, not a code defect**: with the existing eligible range
    (`2021-01-01`..`2026-01-31`), the new audit correctly caught that two
    of the ten fixed-watchlist names — RYAN (real IPO 2021-07-22) and
    ALGM (real IPO 2020-10-29) — don't have enough trailing price history
    for maverick's technical service until real dates well into
    2021–2022 (RYAN not until ~2022-06-01, confirmed against real
    yfinance/maverick-mcp data). Narrowing `ELIGIBLE_RANGE_START` to fix
    that breaks the OTHER requirement instead: only 2 real `selloff`
    months exist anywhere after mid-2022 (2022-09, 2022-12), and
    separating them across a chronological holdout boundary leaves the
    comparison pool with zero `sideways` months — verified by actually
    attempting it, not assumed. Per the user's explicit choice (asked
    directly rather than decided unilaterally — this changes a
    pre-committed range/coverage tradeoff), `ELIGIBLE_RANGE_START` was
    left unchanged and no new manifest was generated this pass. The
    previously-committed
    `backtest-harness/eval_suites/manifest_20260913T154853_887d30cff268.json`
    (`manifest_version: 2`, generated before this pass, no
    `watchlist_viability`) is now **stale** and `run_configuration`
    correctly refuses to execute it under the current code — regenerating
    a valid manifest needs a human decision on the range/coverage
    tradeoff first. **Defined but never invoked** either way — no
    episode, comparison, or Bayesian search has run. Still gated on
    `HANDOFF.md` §10's own measurement/evaluation decision, and
    additionally on: a human resolving the above tradeoff and generating
    a fresh manifest, approving it (editing its `status` field — the one
    edit excluded from the content hash), and an explicit go-ahead to run
    comparisons at all.

    **2026-09-15 correction pass (item E, prior audit M1/M2/L4):**
    `backtest_run_registry.run_kind` gained a fourth value, `evaluation`
    (schema CHECK + `trade-journal-mcp/server.py`'s `_VALID_RUN_KINDS`,
    both additive migrations, no existing row touched) — `run_configuration`
    now refuses to register an episode under anything but `EVAL_RUN_KIND`
    (`'evaluation'`), so an episode can never be mistaken for real training
    data or dismissed as infrastructure noise; dashboard-v2's training-run
    selector already excludes it (strict `run_kind === "training"`
    allowlist, unaffected by this addition). `require_approved` now also
    refuses a `manifest_version` mismatch against the running code's
    `MANIFEST_VERSION` — the stale committed v2 manifest above is caught
    directly by this now, not only incidentally by the v3 viability audit.
    An episode's own final pending order (if any) is resolved as rejected
    at episode end (`resolve_trade_rejection`, reused verbatim), never left
    dangling. Still **defined but never invoked** — no episode has run.

    **2026-09-15c fundamentals-measurement block — infeasible, blocked
    before any run:** the user's own explicit range decision
    (`ELIGIBLE_RANGE_START=2022-06-01`, the RYAN/ALGM viability floor
    above) plus `EPISODES_PER_REGIME=1` (exactly one comparison episode
    per condition, the frozen design's own requirement) are now committed
    in `eval_suite.py`. `propose --seed 20260915` still fails
    (`ManifestGenerationError`): the only 2 real `selloff` months in this
    range (2022-09, 2022-12) force the holdout boundary back to 2022-12,
    leaving the comparison pool with zero `sideways` months — a different
    manifestation of the SAME structural tradeoff, not a new problem.
    Full diagnostic (real SPY monthly classification table, exact error,
    honest null-result conclusion): `docs/evaluations/
    fundamentals_v1_measurement_block_2026-09-15.md`. No manifest, no
    episode, no Hermes call, no training/evaluation run. Still open: a
    start date between 2021-01-01 and 2022-06-01 that threads both
    constraints (unchecked), a watchlist exception for RYAN/ALGM, or
    relaxing `select_holdout_pool`'s single-contiguous-block requirement —
    each a real tradeoff needing an explicit human choice, not a retry.
    **Incidental fix (found running this attempt):** `test_eval_suite.py`
    was silently writing real `cycle_failed` rows to Neon whenever Docker
    happened to be running (a missing `fail_logger` mock — `_run_cycle_
    or_halt`'s default binds to the real `log_cycle_failure` at
    `run_backtest.py`'s own def-time, so patching `eval_suite.
    log_cycle_failure` could never have worked before it was imported
    into that module). Fixed at the source; verified zero real writes with
    Docker running. 3 pre-existing orphaned rows from the bug (verified:
    no associated trades/registry/snapshot data) are documented, not
    deleted — the delete was blocked by this environment's own
    shared-resource safety gate.

    **2026-09-16 correction — infeasibility resolved, valid manifest
    generated (still unapproved):** replaced `select_holdout_pool`'s
    single globally-trailing-block holdout rule with
    `select_paired_episodes` — one comparison + one strictly-later
    holdout episode PER REGIME independently (never requiring every
    holdout episode later than every comparison episode across regimes).
    `MANIFEST_VERSION` bumped 3→4 so any manifest from the old algorithm
    is refused, not silently reinterpreted. Fixed range/watchlist/regimes/
    seed/rules untouched. `propose --seed 20260915` now generates a valid
    manifest (`manifest_20260916T154923_30f4c12e0f7e.json`): selloff's
    only 2 eligible months (2022-09, 2022-12) are forced into the pair in
    chronological order exactly as required; all 8 episodes pass the
    fixed-watchlist viability audit; zero date overlap; every regime
    covered exactly once on each side. **Not approved, no episode run** —
    per this block's own instruction to stop after generating and
    verifying. Scientific limitation recorded in the manifest's own
    `holdout_design.caveat` and in the measurement report's "2026-09-16
    correction" section: this is per-regime-later holdouts, not one
    single globally-later holdout period. Full detail:
    `docs/evaluations/fundamentals_v1_measurement_block_2026-09-15.md`.

    **2026-09-16b — approved, comparison-stage run started, interrupted:**
    the manifest above was reviewed and approved (commit `be1b4b9`). A
    real infra gap surfaced on the first execution attempt:
    `backtest_run_registry`'s `run_kind` CHECK constraint had never
    actually been widened live to permit `'evaluation'`, despite the
    schema.sql migration existing since the September correction pass
    (only `backtest_run_configuration` was activated then, not this
    sibling constraint) — applied live, verified (row count unchanged,
    every existing `run_id`/`run_kind` pair byte-identical before/after).
    Retried; the `technical_news_baseline` config then ran for real
    (`run_id e9e5644e-fd56-4be8-a8b6-5132be7a71c7`, the `high_volatility`
    comparison episode, 2022-06) before being **deliberately interrupted
    by the user after 12 of ~22 simulated trading days** (29 journaled
    decisions, 19 fills, 0 left pending) once a real gap was identified:
    queued BUYs were never re-checked against headlines published
    overnight between their decision-time news check and their 09:45 ET
    fill. A strictly read-only audit (no resume/retry/delete/approve/
    revert) confirmed: zero holdout episodes touched, zero non-evaluation
    runs created, zero live-paper/broker orders, Round 2 completely
    unchanged; the interrupted run's Neon rows were left in place
    (separate cleanup task) but this run is **excluded from all
    performance analysis** — its own incompleteness plus the missing gate
    make it invalid for comparison by construction, not by judgment call.

    **2026-09-17 correction — the missing gate implemented:**
    `apply_pre_fill_news_gate` (`backtest-harness/run_backtest.py`) closes
    exactly the gap the interrupted run surfaced. Every queued BUY is
    re-checked, immediately before `simulate_trading_day`, against real
    headlines timestamped strictly after the original decision day's
    close through the actual 09:45 ET fill cutoff (timezone-aware,
    `America/New_York`, both boundaries validated — a naive/unparseable
    timestamp fails the affected order closed rather than approximating).
    Materially adverse news, an unavailable feed, or a malformed
    confirmation reply all resolve the order rejected
    (`pre_fill_news_veto` / `pre_fill_news_unavailable`) via the SAME
    idempotent `resolve_trade_rejection` path every other rejection
    already uses — never a new journal mechanism. Queued SELLs and
    mechanical exits are structurally untouched (the gate only ever
    inspects `action == "BUY"` orders). `risk_checker.py` remains pure
    math/rules, zero external tool access — the classification judgment
    stays with Hermes, via one small, constrained, batched confirmation
    call per cycle (never a second full trading cycle) that can only ever
    return `CONFIRM`/`CANCEL`, with headline/summary text explicitly
    treated as untrusted data in the prompt itself. 14 new focused tests
    (`test_execution_semantics.py`), zero real Docker/Neon/Hermes/broker
    I/O in any of them. The approved manifest's `status` was reverted
    `approved` → `proposed` (single-field edit, content hash unchanged,
    verified) — a fresh comparison-stage run needs a fresh human review
    and approval of this exact fix first. **Live parity, explicitly not
    built speculatively:** live mode currently has no queued/delayed BUY
    path at all (`alpaca-mcp`'s `place_order` executes immediately) for
    this gate to extend — documented here as a real prerequisite before
    any future delayed live-execution design, not invented ahead of need.

    **2026-09-17b — same-day correction: the timestamp boundary itself
    was wrong.** The block above's own claim ("headlines timestamped
    strictly after the original decision day's close") was not actually
    true: the FIRST version recorded only a bare `queued_date` (a
    calendar date) and started its window at the FOLLOWING midnight —
    silently skipping every headline published between the real close
    and midnight, exactly the window earnings and major announcements
    often land in. Corrected:
    - `QueuedOrder.queued_date` replaced by `decision_cutoff_at` — the
      REAL, timezone-aware regular-session-close timestamp
      (`_session_close_at`, sourced from `fetch_trading_calendar_day`,
      half-day-aware — never a hard-coded 16:00). A pre-migration order,
      or one written under the first (buggy) version, has no valid value
      here and fails closed for BUY execution rather than guessing.
    - The window is now exactly `decision_cutoff_at < headline.created_at
      <= fill_cutoff_at` (fill_cutoff_at = Day T+1 09:45 ET) — proven via
      a new test asserting a 16:05 ET Day-T headline (5 minutes after a
      16:00 close) IS included and can cancel the order, and a headline
      timestamped exactly AT the cutoff is excluded.
    - `alpaca-mcp`'s `get_historical_news` now takes exact RFC-3339
      `start_at`/`end_at` (previously a date + `lookback_days`), paginates
      for real via Alpaca's `next_page_token` (capped at
      `MAX_NEWS_PAGES=20`), and returns an explicit `complete` flag —
      hitting the page cap with a token still pending reports
      `complete: False`, which both `fetch_news` (the ordinary decision-
      time fetch) and `fetch_news_window` (the gate's own fetch) treat as
      a hard failure for the affected ticker, never a partial-but-usable
      result. The ordinary decision-time fetch (`fetch_news`) also gained
      the same explicit `end_at` cutoff, closing a related leak: a
      date-only `end` could previously let an end-of-day decision
      silently receive a same-day headline published AFTER that day's
      real close.
    - `watchlist/check_news_coverage.py` and `alpaca-mcp/
      test_historical_news.py` (a real, credentialed proof-of-life,
      never run by `verify.py`) updated to the new signature. A new pure
      test file, `alpaca-mcp/test_historical_news_pagination.py` (mocks
      `httpx` entirely, zero real network), proves the pagination/
      completeness contract directly and is now in `verify.py`'s suite
      list. 6 more tests added to `test_execution_semantics.py` (20
      total across both correction passes) covering the exact-cutoff
      boundary, DST-correct UTC conversion, RFC-3339-not-date-only
      request construction, incomplete-pagination fail-closed, and a
      legacy order with no `decision_cutoff_at` at all.
    - Manifest **left at `proposed`** (was already reverted there by the
      prior pass; this pass never touched it). No run, training,
      evaluation, smoke test, holdout, or live-paper process executed.

    **2026-09-17c — a further, narrower gap: the ordinary decision-time
    fetch never independently checked what it got back.** Review found
    `fetch_news` (the same-day Day-T fetch above, not the gate) sent an
    exact RFC-3339 cutoff to Alpaca but trusted every returned article and
    truncated to 4 headlines BEFORE validating any timestamp — a provider
    article outside the requested range, or with a missing/malformed/
    timezone-naive `created_at`, could have reached the Day-T prompt.
    Corrected: a new pure helper, `_articles_at_or_before_cutoff`
    (`run_backtest.py`), validates every article's `created_at` and keeps
    only those at-or-before `decision_cutoff_at` (single inclusive upper
    bound — no lower bound, since the 7-day lookback is already an
    advisory scoping concern, not an integrity boundary); returns `None`
    — fail-closed — on any missing/malformed/naive timestamp, which
    `fetch_news` now raises on before ever truncating to the 4-headline
    prompt cap. `_to_utc_rfc3339` now rejects a naive `datetime` outright
    instead of silently converting it via the host machine's own local
    timezone. 6 new zero-network tests (`test_execution_semantics.py`):
    post-close exclusion, exact-cutoff inclusion, malformed/naive fail-
    closed (helper + end-to-end `fetch_news`), exact-cutoff forwarding,
    naive-datetime rejection. Pre-fill gate, risk checker, sizing,
    watchlist, and evaluation design all unchanged. Manifest still
    `proposed`, untouched; no run/training/evaluation/smoke/holdout/
    live-paper process executed; no Neon writes.

    **2026-09-17d — the same fetch was still only checking one edge.**
    2026-09-17c closed the upper (cutoff) boundary but still trusted the
    provider's own 7-day lookback start. Corrected: `_articles_at_or_
    before_cutoff` replaced by `_articles_in_decision_window(articles,
    start_at, end_at)`, enforcing `start_at < created_at <= end_at` on
    BOTH edges — an article older than the requested lookback, not only
    one after the cutoff, now fails the same closed path. `fetch_news`
    passes its own already-computed `start_dt`/`end_dt` straight through.
    Also hardened every timezone-awareness check on this timeline
    (`_parse_news_timestamp`, `_pre_fill_news_window`, `_to_utc_rfc3339`)
    via one new shared pure helper, `_is_tz_aware`: a datetime now needs
    `tzinfo is not None` AND a resolving `utcoffset()` — a mis-implemented
    custom tzinfo that returns `None` from `utcoffset()` is rejected the
    same as an outright naive datetime, not silently treated as aware. 6
    more tests (12 total across 09-17c/d) covering the older-than-start
    exclusion, the exact `start_at < created_at <= end_at` boundary, and
    a broken-tzinfo rejection both directly and through `_to_utc_rfc3339`.
    Pre-fill gate semantics, risk checker, sizing, watchlist, fundamentals,
    dashboard, and provider API untouched. Manifest still `proposed`; no
    run/training/evaluation/smoke/holdout/live-paper process executed; no
    Neon writes.

    **2026-09-19/21 — `baseline_v1` COMPLETE.** Manifest
    `manifest_20260916T154923_30f4c12e0f7e.json` approved (v4, hash
    `30f4c12e0f7ed7bb75780b9d84f6f38cf69c84a149c58b3ab66f4d4a97d018fb`,
    unchanged since). Infra gap fixed first: `execution-sim` was stopped
    (`restart: "no"`), so `run_configuration` now calls the same
    `run_backtest.preflight()` required-service check before any side
    effect, and compose sets `execution-sim` to `restart: unless-stopped`
    (`backtest-harness/eval_suite.py`, `docker-compose.yml`). Rules/config
    frozen: fundamentals disabled (`--fundamentals` never passed), fixed
    watchlist, point-in-time data, alias masking, next-day execution,
    news veto — all unchanged from `baseline_v1`'s own definition. All 4
    comparison episodes run via the public `run-one` selector, none
    halted:
    - [x] Jun 2022 / high_volatility — `1c754200-8b87-4abf-b6c8-04bc5d0e6583` — −3.97%, max DD −5.26%
    - [x] Jul 2022 / rally — `295c1bb9-a774-4354-8f10-a6aa74b8d518` — +5.80%, max DD −2.24%
    - [x] Sep 2022 / selloff — `329ec8aa-5aae-4442-bda7-f0364221ef04` — −3.27%, max DD −5.41%
    - [x] Oct 2024 / sideways — `dd02237a-db56-4cbe-bfdd-0fcb45d1129c` — +0.08%, max DD −1.81%

    Diagnosis recorded, not a conclusion: upside participation in the
    rally; weaker performance in selloff/high-volatility; 4 one-month
    samples, no causality claim, no holdout used. Baseline limitation
    observed (not fixed): raw-price sizing plus 5bps simulated fill
    slippage causes marginal >20%-position-cap rejections across all 4
    runs. `baseline_v1` is now **frozen unchanged** — never overwrite,
    delete, relabel, or compare it as though it were a different config.

    **Next gated step:** create a separate `baseline_v2` configuration
    that sizes using the expected slippage-adjusted fill price, then
    rerun the SAME 4 frozen comparison months. `baseline_v2` is a new,
    separate config — not a mutation of `baseline_v1` — and is not yet
    started. No other speculative next step is approved here.

    **Update (status as of 2026-10-04):** that step is done. All 4
    `baseline_v2` comparison episodes completed with no halts, and the
    read-only comparison is in `docs/evaluations/baseline_v1_vs_v2_evaluation.md`
    (generated 2026-09-27). The comparison is mixed and is not a causal or
    profitability claim. No V3, holdout or optimization run has started.

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
the run-wide totals — never a whole-list fetch). **Workflow page**
(2026-10-03): per-day order/position and candidate paths as connected
stages. It is built from `public_workflow_events`, a new allowlisted
progress-event projection written by the backtest controller as each stage
happens, plus existing journal records for older runs ("not recorded"
where nothing exists). It offers local replay and bounded live follow, and
lists labelled smoke runs on that page only
(`docs/evaluations/workflow_dashboard_2026-10-03.md`). **Redesigned
2026-10-04 (approved):** the page is now an agent-centred architecture
diagram. Hermes sits inside a Backtest controller boundary, with tools
beside it and risk/execution below; details open on click. It opens on an
exemplar day (smoke run `a0f96768…`, Day 2), and run/day browsing and
follow sit under "Explore other days". Dashboard V1
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
