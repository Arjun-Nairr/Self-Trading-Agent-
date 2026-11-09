# Laya, prompt schema 5: July 2022 single-arm rerun plan, v3 (2026-11-09, AUTHORIZED)

**Status: authorized by the user on 2026-11-09.** This is one fresh July 2022 episode, Laya arm only, run once and never
retried or resumed. It replaces the halted v2 attempt (`6cc8687b-696d-4a80-b23a-c2a754c2aa37`), which stays preserved
as is.

## Same as v2 (`laya-schema5-2022-07-v2`)

- **Population:** the frozen 48-name pool `watchlist/experimental_pools/candidates60-v1-pool-2022-07-01-aafefb19a6c5.json`
  (content `c56a4672…`, evidence `aafefb19…`, universe `1f3d88b6…`). Hindsight-selected.
- **Window and settings:** 2022-07-01 to 2022-07-31; masking seed 42 (opaque aliases, the same alias mapping as v2);
  k = 10; $100,000; fundamentals off.
- **Laya:** worker `hermes-laya-worker:2` (`sha256:d423bbf7…`), checkpoint `55cf4c4e…`, weights `891102d3…`.
- **Hermes:** `gpt-5.6-terra` / `openai-codex` / `medium`, tool-free disposable profile.
- **Decision path:** prompt schema 5, with controller-owned BUY quantity and explicit news status.
- **Execution:** simulated only; `run_kind=evaluation`.
- **Tracing:** backtest tracing from the frozen masking through episode-end resolution.

## What changed since v2 (code, not configuration)

1. **Masking:** a variant for the short company name that left a name word next to its alias in the 2022-07-05
   headlines. The identity guard is unchanged.
   - Offline check over all 674 July decision-window articles for the 48 names (1,348 renderings): 0 alias residues,
     0 known cues, 0 masker leaks after the fix, against 8 residue renderings before.
2. **Halt reporting:** a halt after journaled activity is reported as `unreconciled`, with no return or drawdown.
   Episode end reports what the journal actually did.
3. **Tracing:** identity-guard failures are exported only as a reason code.

## Command (after the scoped commit and authorization)

```
python backtest-harness/laya_vs_random.py --config backtest-harness/experiments/laya_schema5_2022_07_v3.json run --arm laya --laya-checkpoint C:/Users/dwish/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851
```

## Reported (descriptive only)

As in the v2 plan, plus the portfolio-reconciliation status and the journal-reported outcome at episode end.
