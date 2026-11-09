# Laya, prompt schema 5: July 2022 single-arm evaluation plan (2026-11-09)

**Authorization:** the user's request of 2026-11-09 in the Claude Code session. It covers one fresh July 2022
episode, **Laya arm only**, run once and never retried or resumed. No random arm, no other months, no tuning.

## What is fixed (same as `laya-vs-random-2022-07-v1`)

- **Population:** the frozen 48-name pool `watchlist/experimental_pools/candidates60-v1-pool-2022-07-01-aafefb19a6c5.json`.
  - content `c56a4672…`, evidence `aafefb19…`, universe `1f3d88b6…`.
  - Hindsight-selected: it cannot establish trading edge.
- **Window and run settings:**
  - window 2022-07-01 to 2022-07-31;
  - masking seed 42, opaque aliases;
  - k = 10, starting equity $100,000;
  - fundamentals off.
- **Laya:**
  - worker `hermes-laya-worker:2` (`sha256:d423bbf7…`);
  - checkpoint revision `55cf4c4e…`, weights `891102d3…`;
  - question format `noul-criteria-v2`.
- **Hermes:** `gpt-5.6-terra` / `openai-codex` / `medium`, tool-free disposable profile.
- **Execution:** simulated (execution-sim) only, with `run_kind=evaluation`.
- **Episode end:** pending orders are rejected; holdings are kept and marked, never force-liquidated.

## What differs from v1 (why this is not a controlled comparison)

- **Prompt schema 5:**
  - BUY share quantity is computed by the controller (`required_entry_quantity`), and Hermes' own BUY quantity is
    ignored;
  - every name carries an explicit news status: headlines, "No headlines found in the checked window", or
    UNAVAILABLE.
- **Tracing:** controller-side Langfuse tracing (`environment=backtest`, session = run ID) runs from the frozen
  masking through episode-end resolution. It is export-only and fail-open.
- The v1 arms (schema ≤ 4) are historical context only.

## Command

```
python backtest-harness/laya_vs_random.py --config backtest-harness/experiments/laya_schema5_2022_07_v2.json run --arm laya --laya-checkpoint <pinned checkpoint dir>
```

## Reported (descriptive only; one month and one population)

- **Sessions:** completed sessions and halts.
- **Model calls:** count, returned token usage and latency.
- **Shortlist:** the Laya shortlist per day, and what Hermes received.
- **Decisions:** controller-sized BUY quantities and sizing outcomes; legacy quantity fields.
- **News:** checked-empty vs. unavailable status.
- **Gates:** pre-fill vetoes and fill-time rejections, with the gap and the rule.
- **Positions:** fills, holdings, SELLs and exits by reason; episode-end pending outcomes.
- **Results:** return, max drawdown and closed trades.
- **Benchmarks:** compared with cash, and with an equal-weight hold only where an existing benchmark path supports
  the same dates, capital and timing.
- **Traces:** trace coverage vs. the run log and journal, spooled or missing records, and an identity-leak check.
