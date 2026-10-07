# Final Laya acceptance smoke: selector and injection monitor together (2026-10-07)

**Verdict: GREEN.** This test checks operational integration only. It
makes no claim about profit, selector benefit or reliable injection
detection.

## Run

- **Run ID:** `0e95b7be-8cf6-48a0-82c5-275b7cc19743`.
- **Setup:**
  - registered as `smoke`, July 1–8 2022, with the July 4 holiday skipped
    by the trading calendar;
  - a fresh `--state-dir` outside the repository;
  - a disposable Hermes profile.
- **Launches:** one, with no retry or resume.
- **Code:** HEAD `6d0bf53`, recorded as the run's `git_head_sha`. The
  config's `git_dirty` flag reflects pre-existing user-owned uncommitted
  files.

Command (the scratchpad path is shortened as `<scratch>`):
```
python backtest-harness/run_backtest.py --state-dir <scratch>/final/state --run-kind smoke --seed 42
  --selector-mode laya_fixed30_experimental
  --selector-universe watchlist/experimental_pools/candidates60-v1-pool-2022-07-01-aafefb19a6c5.json
  --laya-checkpoint <hf cache>/models--convaiinnovations--laya/snapshots/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851
  --laya-image hermes-laya-worker:2 --injection-monitor --start 2022-07-01 --end 2022-07-08 --days 5
```

**Settings:** the same pool, seed and selector settings as the selector
closeout smoke, with fundamentals off. Model, rules, masking and risk are
unchanged.

## Preflight

- **Docker:** the engine was running, and all 7 compose services were up.
- **Source parity:** service and selector-module code is unchanged since
  the parity-verified commit `e09773c`. That check matched trade-journal,
  execution-sim, alpaca-mcp, maverick-mcp and the selector worker's
  modules.
- **Images:** `hermes-laya-worker:2` = `d423bbf7…` and
  `hermes-laya-injection-monitor:1` = `7006c5ef…`.
- **Checkpoint:** revision `55cf4c4e…` (weights hash verified at
  closeout); the worker re-verifies it on load.
- **Hermes:** the base backtest profile is `gpt-5.6-terra` /
  `openai-codex` / `medium`, the only profile present.
- **Backtest preflight:** passed (the run started).
- **Snapshots taken:** protected-file hashes and per-run database counts
  for the 27 earlier runs.

## Acceptance audit (persisted evidence; 0 problems)

1. **Sessions:** 5/5 completed, exit 0, with no failed cycles.
2. **Records:**
   - 5 selector snapshots, one per session date;
   - 5 evidence rows, each with a hash-verified prompt and response and
     schema 4;
   - 5 portfolio snapshots.
3. **Point-in-time and presentation:**
   - Every scored card was `current` to its session.
   - 48 names were eligible each day and 10 selected.
   - The rendered candidates equal the shortlist, in neutral alias order.
   - No Laya score, rank, confidence or row ID reached Hermes.
   - The shortlist rotated (6/8/6/5 names changed between sessions, and 8
     names left and later re-entered).
4. **Identity:**
   - All 5 decision prompts and the one pre-fill prompt had 0 listed cues,
     0 alias residues, 0 raw tickers and 0 masker self-check failures.
   - All 3 BUY proposals used the observed S1 as their stop.
5. **Monitor coverage:**
   - **Records:** 6 private record files (5 decision, 1 pre-fill), 75
     snippets in all: 71 decision and 4 pre-fill.
   - **Exact text:** each fingerprint equals the SHA-256 of the snippet
     actually rendered on its path:
     - decision: recomputed from the stored `input_payload` with the
       prompt builder's own renderer, and equal to the prompt's news
       lines in order;
     - pre-fill: parsed from the stored gate prompt, whose hash names the
       record.
   - **Identity:** every record carries question `injection-noul-v1`,
     package 0.3.23, checkpoint revision `55cf4c4e…`, image
     `hermes-laya-injection-monitor:1` with ID `7006c5ef…`, and timings.
   - **Scored:** **75/75 scored, 0 unavailable.** Snippets existed every
     session, and the monitor was available for all of them.
6. **Non-interference:**
   - No monitor term appears in any prompt.
   - On July 8, 1 of the 4 pre-fill snippets was flagged, yet the gate
     verdict was `confirm`. That comes from Hermes' own classification:
     the flag did not touch the prompt or the verdict.
   - The decisions, rejections and queued orders are the same as the
     monitor-off closeout smoke `5e977606` over the same window.
7. **Reconciliation:** cash reconciles to $100,000, and the filled trades
   reconcile with holdings (none).
   - At the boundary, one queued BUY was resolved by
     `eval_suite.resolve_episode_end_pending` ("episode window ended before
     execution").
   - The journal and state each hold 0 pending, with **no open positions**
     to value and no liquidation.
8. **Preservation and cleanup:**
   - Protected files and trees are byte-identical: Round 2 state,
     RULES/AGENTS/SOUL, the watchlist and pools, the paired-experiment
     config and output, the evaluation manifests, the earlier smoke state
     and the risk checker.
   - All 27 earlier runs are unchanged. The only new database rows are this
     run's: 2 trades, 1 insight, 20 activity, 5 snapshots, 5 evidence, 1
     registry and 1 configuration.
   - Only the base Hermes profile remains, with 0 Laya worker or monitor
     containers.
   - Nothing was written to default locations.
   - User-owned files are unchanged.

## Operational metrics (no performance claim)

- **Model calls:** 6 (5 decisions and 1 pre-fill confirmation), 48,114
  tokens.
- **Monitor time:** the first record took 35.4 s, including the one model
  load. Later decision records took 6.7–8.3 s, and the pre-fill record
  3.0 s.
- **Monitor input size:** 27–86 state tokens per record's largest snippet,
  well under the 405-token room.
- **Monitor scores:** 31 of 75 real historical snippets were flagged at the
  uncalibrated 0.5 line; median 0.41, range 0.10–0.96.
  - These are ordinary 2022 market headlines with no injected attacks, so
    almost all of the flags are likely false.
  - This matches the false flags on financial imperatives seen in the
    fixture check, and confirms that the flags are notes for review, not a
    detector.

## Paths exercised

**Exercised naturally:**
- Laya selection and rotation;
- decision-time risk rejections (§2/§3 sizing);
- BUYs queued with observed-S1 stops;
- a pre-fill news confirmation by Hermes, with the monitor recording it;
- a fill-time risk rejection after the overnight gap;
- boundary resolution of a pending order;
- the reflection insight.

**Covered only by offline tests:**
- a filled trade;
- held-position management;
- stop, target and time exits;
- a queued SELL;
- an identity-check cancellation in the pre-fill gate;
- a whole-day selector failure;
- monitor-unavailable paths (oversized input, deadline, stalled worker).

## Conclusion

**GREEN, with no blocker.** This closes the agreed experimental Laya
implementation: the pool selector plus the shadow injection monitor.

**Limitations that remain:**
- trading value and Laya calibration are unproven;
- monitor detection quality is poor on real headlines, and it stays
  logging-only;
- masking is finite known-cue protection;
- the universe is fixed and retrospective;
- levels are minimal observed extremes;
- live paper is inactive;
- the selector transport keeps its small-request write residual.
