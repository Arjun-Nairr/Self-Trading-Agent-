# Laya vs. random selector, downstream pilot: frozen plan (2026-10-06)

**Status:** frozen and committed before either arm runs. Authorized by the
user on 2026-10-06 for exactly these two runs.

**Not authorized:** other months, extra trading runs, tuning, retries,
changes to existing baselines.

**Machine-readable configuration:**
`backtest-harness/experiments/laya_vs_random_2022_07.json`. It pins this
file's SHA-256. The entry point refuses to run if the plan, config, pool
or experiment code differ from what is committed.

**Question.** Over one July 2022 month, what does the production Laya
selector put in front of Hermes each day, compared with a seeded uniform
random draw from the same pool? What trading behaviour follows?

This is practical evidence from one path per arm. It is not a
significance test, and it is not a claim that either selector causes
better trading.

## 1. Design

| | |
|---|---|
| Window | 2022-07-01 .. 2022-07-31: 20 sessions (July 4 is a market holiday) |
| Run order | **Laya arm first, then random**; sequential, once each |
| Entry point | `python backtest-harness/laya_vs_random.py run --arm laya --laya-checkpoint <pinned snapshot dir>`, then `... run --arm random` |
| Classification | `run_kind` **evaluation** in `backtest_run_registry`, with a private `backtest_run_configuration` row (provenance: git HEAD, Hermes profile, model) |
| Private evidence | git-ignored `backtest-harness/.laya_vs_random/laya-vs-random-2022-07-v1/<arm>/` |

**Not the V1/V2 manifest.** The approved eval-suite manifest and its
fixed-10 watchlist approval are unchanged. They are **not** this
experiment's approval, and this experiment does not use `eval_suite.py`'s
episode runner. Its own authorization is this plan plus the config.

**Arm guards** (enforced in code and tested):
- Each arm runs once. A write-once marker is created before registration
  or any Hermes call.
- Random runs only after a **completed** Laya arm with byte-identical
  experiment code and config.
- A halted arm stops the experiment: no retry, no resume, no repair
  between arms, and the other arm is not launched.

### Arms

**Laya (production selector):**
- `laya` 0.3.23, checkpoint `convaiinnovations/laya` @ `55cf4c4e…`
  (weights `891102d3…`);
- question `noul-criteria-v2`;
- worker image `hermes-laya-worker:2` (`sha256:d423bbf7…`), isolated
  (`--network none`, read-only, cap-drop, non-root);
- one batch per day; top 10 by `review_probability` with the seeded
  (42) row-ID tie-break. Unchanged.

**Random control** (`laya_experiment.RandomSelectorContext`):
- **Draw:** up to 10 cards drawn uniformly **without replacement** from
  the sorted row IDs of the selectable cards.
- **Draw seed per session:** the first 8 bytes (big-endian) of
  `sha256("laya-vs-random-2022-07-v1|<session ISO date>")`.
- **Recorded:** a snapshot with provider `random_control`, the experiment
  seed in its settings, and each selected row's draw seed and position.
- **No probabilities or confidences** are ever produced for random picks.

### Shared by both arms

**Selection and eligibility:**
- **Universe:** the frozen pool `candidates60-v1-pool-2022-07-01-aafefb19a6c5` (§2).
- **Eligibility:** held names and names with a pending order are
  excluded before selection; exit management never depends on selection.
- **Card building:** the same point-in-time builder (`laya_cards`, bars
  ≤ session date).
- **Availability:** the same rule (`select_pool`'s: incomplete or stale
  cards are `data_unavailable`).
- **Shortlist size:** the same up-to-10 limit, never padded.
- **Selector failure:** fails closed for that day's new entries only.

**Masking and presentation:**
- **Masking:** opaque scheme, seed 42, one map over all 48 names. The
  alias-map hash must be identical in both arms.
- **Presentation order (new, both arms):** shortlisted candidates are
  shown to Hermes in **alias order**, the order the masked rules block
  uses. Previously the order followed Laya's rank, which leaked it.
- **Never shown to Hermes:** Laya scores, confidences, ranks, the
  random draw, or which arm this is.

**Trading environment:**
- **Starting point:** a fresh $100,000 portfolio and a disposable Hermes
  profile per arm.
- **Model:** Hermes `gpt-5.6-terra` / `openai-codex` / medium, verified
  from the runtime's config before registration; a mismatch aborts.
- **Rules:** `RULES.md` (`cf775be9…`), `AGENTS.md` (`4959d86f…`),
  `SOUL.md` (`ed2465b5…`), aggressive Phase 1 profile. The §6b
  escalation mechanism is unchanged; July has 20 sessions, so the
  base tier holds throughout.
- **Fundamentals:** OFF.
- **Execution:** risk checker, next-day 09:45 fills via execution-sim
  (5 bps), the pre-fill overnight news gate, mechanical
  stop/target/time-stop exits.

**Episode boundary:** `eval_suite.resolve_episode_end_pending`, the
existing evaluation-episode semantics.
- Orders still pending after the last session are resolved as rejected
  ("episode window ended before execution").
- Open holdings are kept and marked at the last close, with **no forced
  liquidation**.

**Identical historical facts:** each arm records a SHA-256 of every
per-ticker technicals, news, pre-fill news and card-history fetch, keyed by
(kind, ticker, date/cutoff).
- **Compared:** keys both arms requested.
- **Reported:** every discrepancy.
- **Not identical, legitimately:** portfolios diverge, so eligible sets,
  prompts, reflections and holdings will differ after day 1.

## 2. Pool (frozen before any run; read-only checks only)

- **Parent:** `watchlist/experimental_candidates60.json` (`candidates60-v1`,
  universe hash `f854a326…`). No returns, Laya scores or outcomes were
  used.
- **Policy:** the established coverage policy (`laya_preflight.py --freeze`).
  - **Pre-start news (point-in-time):** at least 6 distinct articles in
    three 14-day probes, all before 2022-07-01, with complete pagination.
    Probes: (03-18, 04-01], (05-02, 05-16], (06-16, 06-30].
  - **Pre-start warm-up (point-in-time):** at least 40 price bars before
    day 1.
  - **In-window price availability:** retrospective, a feasibility check
    only.
- **Result:** 48 of 60 qualified.
  - Excluded for news: OMC, TOL, LKQ, CASY, CAG, TRGP, RJF, CINF, ROL,
    ALGM, GLW.
  - Excluded for price: EQR (no in-window bars, no warm-up).
- **Hashes:** `content_sha256 c56a4672…c318`; `evidence_sha256
  aafefb19…6b387`; universe hash `1f3d88b6…4d34`.

**Bias, stated plainly:**
- The 60 candidates were chosen with present-day knowledge, and the
  qualified subset depends partly on retrospective availability.
- This is a **retrospective fixed-universe pilot**, survivorship- and
  selection-biased, not an unbiased point-in-time universe test.

## 3. Evaluation (defined now)

**Sources:**
- the journal (`trades`, `agent_activity`, `portfolio_snapshots`) for
  each `run_id`, fetched in full;
- the private daily ledger;
- selection snapshots.

Counts are reconciled across these sources.

| Metric | Definition |
|---|---|
| Return | (final equity − 100,000) / 100,000. Final equity marks open holdings at the last session close. |
| Max drawdown | Peak-to-trough on the daily closing equity curve |
| Average exposure | Mean over sessions of invested value ÷ equity at the close |
| Proposals | BUY/SELL decisions Hermes queued |
| Explicit passes | Sessions with no proposal (with Hermes's stated reason) |
| Discretionary fills | Queued orders that filled |
| Closed trades | Positions closed in the window; realized P&L reported separately from open positions' unrealized P&L |
| Exit types | `exit_provenance`: harness stop / target / time stop vs agent-requested |
| Rejections | Decision-time blocks, fill-time risk rejections (by rule), pre-fill news vetoes, terminal-pending rejections; full reasons, not truncated lists |
| Selector health | Days failed closed, unavailable cards, eligible counts, held/pending exclusions |
| Turnover | Per arm, the share of each day's shortlist not on the previous day's |
| Overlap | Laya ∩ random shortlist size, only on days whose eligible sets are identical |
| Cost | Wall time, Hermes calls and tokens, Laya worker time |

### Manual review

1. **Daily summary.** For every day: each arm's selections and Hermes
   usage.
2. **Blind review, before any arm runs.** The first, middle and final
   sessions (07-01, 07-15, 07-29), using masked cards and as-of technical
   and news evidence for all 48 pool names.
   - Name plausible review-worthy setups, ordinary names and
     uninterpretable cases, with short concrete reasons.
   - The v0.2 rubric is a diagnostic heuristic only, not ground truth for
     profitability or Hermes suitability.
3. **Post-hoc diagnostics, labelled as such.** Anomalous selections,
   failures and conspicuous outcomes, inspected afterwards.
4. **Never:**
   - call an untraded name a bad pick because Hermes did not trade it;
   - describe how Hermes would have traded an unselected name.

### Report

The report gives three separate verdicts: operational correctness,
selection plausibility, and trading outcomes.

**Public report:** aggregates, per-day counts and masked or generic
descriptions only.

**Kept private:** company or alias identities, per-company scores or
ranks, raw prompts, and the daily ticker shortlists.

## 4. Limitations and stopping conditions

**Limitations:**
- one path per arm;
- one month;
- stochastic Hermes decisions;
- one random seed;
- a retrospective, biased universe;
- historical news retrieved today;
- portfolios diverge after the first session.

No causal or significance claim will be made.

**Stop the experiment, preserve evidence and report** if any of these
happen:
- a preflight check fails;
- the provenance check fails (model, pool, plan, code or alias map);
- an arm halts;
- a selector fails closed on more than a few days (operational issue);
- a material integrity issue appears (a lookahead, a leak into a prompt,
  journal and state disagreement).

**Preserved, before and after:**
- Round 2 `backtest_state.json` (`4b428d35…ba6d`);
- every prior run's per-table Neon counts;
- the eval-suite manifest;
- `watchlist.json`;
- the existing pools.
