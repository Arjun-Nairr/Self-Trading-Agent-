# Laya dynamic shortlist selector: closeout (2026-10-07)

**Verdict: GREEN.** The experimental selector implementation is closed out,
and the acceptance smoke passed.

**What GREEN means:**
- Laya picks a changing shortlist of up to 10 names from the frozen
  eligible population.
- Held and pending names are handled correctly, and held positions are
  managed independently of the shortlist.
- Evidence and failure behaviour are checked.
- The repaired pipeline completed a short real run.

**What GREEN does not mean:** that Laya improves trading results, that its
probabilities are calibrated, or that historical universe bias is solved.

Code provenance:
- **`fd4b409`:** the integrity repair (identity checks and observed-only
  levels).
- **`e09773c`:** the pre-fill gate edge case and the remaining offline
  coverage. It was committed before the smoke, and it is the `git_head_sha`
  the run recorded.

## 1. Pre-fill gate edge case (`e09773c`)

**Before:** if identity verification of the pre-fill confirmation prompt
failed, the error was raised out of `run_day`. That happened before the
day's queued SELLs and mechanical exits had run.

**Now:**
- **The unsafe prompt is never sent.** Only that confirmation batch is
  cancelled, with a new reason, `pre_fill_identity_check_failed`. It is
  neither the model's adverse-news veto nor an unavailable feed, and never
  "no adverse news".
- **Everything else still executes:** queued SELLs, BUYs already
  confirmed, and held-position stop, target and time exits.
- **What is recorded:**
  - The verdict uses the existing gate checkpoint and replay.
  - The rejection uses the existing journal path, which only updates a
    still-pending row, so it is idempotent.
  - The private verdict keeps the check type and a hash of the unsent
    prompt, never its text. Public activity lines carry only the fixed
    reason.
- **What is caught:** only `LeakDetected` and `IdentityCueDetected`.
  Provider, journal and execution errors still propagate (tested).
- **Unchanged:** the main decision-prompt check still halts the cycle.
- **No container change:** the new reason maps to an empty allow-listed
  rule-code list, so neither the journal contract nor any container
  changes.

**Regressions:**
- **Same-day run:** an unsafe BUY-news batch is cancelled with no model
  call, while a queued SELL fills and a held position exits at its stop.
- **Checkpoint replay:** the cancellation stays resolved with no news
  re-fetch, no confirmation call and no second journal outcome.

All of these regressions fail on the pre-fix code.

## 2. Offline verification

| Requirement | Evidence |
|---|---|
| Shortlist ≤ 10, no duplicates, held/pending excluded | existing selector and wiring tests, plus a new rotation test |
| Aliases stable when names leave and re-enter | new rotation test (three cycles, names out and back) and existing opaque-alias stability tests |
| Rotation never drops a held position from management | rotation test: the held name stays in Hermes' positions and the simulator's universe every cycle |
| Selector failure blocks entries, not exits | existing `test_selector_failure_blocks_new_entries_but_exit_management_stays_available` |
| Unsupported stops cannot queue; observed stops can pass | integrity-repair stop-evidence tests |
| Final decision and pre-fill prompts reject known cues | integrity-repair tests plus the new gate regressions |
| Generated technicals script | **executed for real** against stub provider modules. Checks the request dates (as-of and history window), the returned bar format, that a bar after the cutoff is dropped, observed-level packets with provenance, that maverick's synthetic levels are not read, and that a history failure gives no levels |

**Results:**
- **Backtest-harness suite:** 597 passed.
- **Offline guard** (Docker, Hermes and non-loopback sockets blocked): 597
  passed with 0 call attempts.
- **`python verify.py`:** 12/12.
- **`git diff --check`:** clean.

## 3. Preflight

- **Docker:** the engine was already running.
  - All 7 compose services, including execution-sim, were up.
  - Source/image parity matched the repository for trade-journal,
    execution-sim, alpaca-mcp, maverick-mcp (93 files) and the three
    modules in `hermes-laya-worker:2`.
  - Nothing was restarted or rebuilt.
- **Model:** the base backtest profile is `gpt-5.6-terra` /
  `openai-codex` / `medium`, matching the locked configuration.
- **Laya weights:** SHA-256 matches the pin.
- **Pool:** `candidates60-v1-pool-2022-07-01-aafefb19a6c5` re-verified (48
  members), and its universe, content and evidence hashes match the frozen
  experiment config. It was not regenerated.
- **Snapshots taken:** protected-state hashes and per-run database counts
  (26 runs).

## 4. Acceptance smoke

**Run `5e977606-fd30-4530-821b-6557f3c54c50`:**
- **Kind:** `smoke`, July 1–8 2022. The July 4 holiday was skipped by the
  trading calendar.
- **Settings:** Laya on, fundamentals off, fresh $100,000, masking seed 42.
- **Isolation:** a disposable Hermes profile and a fresh `--state-dir`
  outside the repository.
- **Config untouched:** the paired-experiment runner, its config and the
  evaluation manifests.
- **Launches:** one, with no retry or resume.

**Results:** 5/5 sessions completed (exit 0); 6 model calls (5 decisions
plus 1 pre-fill confirmation).

**Audit (private script and evidence kept in the session scratchpad): 0
problems.**

- **Records:** each session has exactly one evidence row with a
  hash-verified prompt and response, one selector snapshot and one
  portfolio snapshot. Prompt schema is 4, and zero tools were used.
- **Provenance:**
  - The pool and universe hash are recorded.
  - The alias map rebuilds exactly from the stored watchlist and seed.
  - The run config records `git_head_sha` `e09773c`, the locked model,
    provider and effort, no tools and no memory.
  - The config's `git_dirty` is true. That reflects pre-existing
    user-owned uncommitted files, not harness code.
- **Point-in-time:**
  - Every scored card was `current` (it had the as-of bar, and later bars
    are refused).
  - All 50 rendered candidates had observed levels with method and 30-session
    provenance, and S1 ≤ price ≤ R1.
  - There was no raw support/resistance list and no negative news age.
- **Identity:**
  - Every final decision prompt and the one gate prompt had 0 listed cues,
    0 alias residues, 0 raw tickers and 0 masker self-check failures.
  - No selector score, rank, confidence or row ID reached Hermes.
- **Shortlist:** 48 eligible each day and 10 selected.
  - The rendered candidates equalled the shortlist, in alias order.
  - It rotated: 6/8/6/5 names changed between consecutive sessions, and 8
    names left and later re-entered.
- **Stops:** all 4 BUY proposals had stop basis `observed_low`, equal to
  the candidate's S1. Targets were labelled as derived or as the observed
  R1.
- **Trading:**
  - 2 proposals were rejected at decision time by the existing §2/§3
    whole-share sizing check.
  - 2 BUYs were queued:
    - one was confirmed by the pre-fill gate (4 overnight articles), then
      rejected at fill by the existing §6a/§2a/§6b checks after the
      overnight gap;
    - one was still pending at the boundary.
  - No fills, exits or open positions. Cash reconciles to $100,000 exactly.
- **Boundary:** the remaining queued BUY was resolved with the existing
  `eval_suite.resolve_episode_end_pending` ("episode window ended before
  execution"). Nothing was liquidated, and the journal and state each hold
  0 pending.
- **Preservation:**
  - Protected files and trees are byte-identical: Round 2 state,
    RULES/AGENTS/SOUL, the watchlist, the candidate and pool files, the
    paired-experiment config and output, the evaluation manifests, the
    earlier smoke state, the fine-tuning pilot and the risk checker.
  - All 26 earlier runs' database counts are unchanged; only the new run's
    rows were added.
  - No disposable Hermes profile or worker container remains, and no
    default-location checkpoint or snapshot was written.
  - User-owned files are unchanged.

**Not exercised by real trading, but proven offline:**
- held/pending exclusion at selection time (no position was held, and the
  queued BUY resolved before that day's selection);
- stop, target and time exits; a queued SELL;
- an identity-check cancellation in the pre-fill gate;
- a whole-day selector failure.

**Pre-existing, not changed:** per-cycle evidence leaves `reasoning_effort`
empty (earlier runs do too). The run-level configuration records `medium`.

## 5. Remaining limitations (retained)

- **Trading value:** unproven, and Laya's probabilities are not calibrated.
  This smoke made no fills and is not a performance result.
- **Masking:** known-cue protection is finite. Unlisted cues, paraphrases
  and price fingerprints remain possible.
- **Universe:** a retrospective fixed universe, chosen with present-day
  knowledge, so it is survivorship-biased.
- **Technical levels:** minimal, one observed 30-session low and high per
  name, not support/resistance analysis. Expect few entries.
- **Live paper:** inactive. Live-mode maverick levels are still synthetic.
- **Market-wide screening:** a scalable live-universe screen is a separate
  track.
