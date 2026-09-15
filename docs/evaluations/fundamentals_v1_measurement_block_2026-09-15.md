# Fundamentals V1 measurement block (2026-09-15) — blocked before any run

**Status: manifest generation infeasible under the frozen design. Zero Hermes calls, zero evaluation episodes, zero trading decisions were made under this block.** This report documents why, exactly as the block's own instructions required ("if this exact design cannot produce a valid manifest, stop before any Hermes call or evaluation run").

## Objective

Does Fundamentals V1 improve this trading agent's decisions versus the existing technicals-plus-news-veto baseline? This was to be answered by running two pre-committed configurations (`technical_news_baseline`, `technical_news_fundamentals_v1`) against the same approved comparison episodes, then the same untouched holdout episodes. Neither configuration was ever invoked — see below.

## Frozen design (as specified, unchanged)

- Fixed 10-company watchlist only (`watchlist/watchlist.json`) — no dynamic selection.
- Eligible historical range: **2022-06-01 → 2026-01-31**.
- Required conditions: `rally`, `selloff`, `high_volatility`, `sideways`.
- Exactly one comparison episode per condition; one later, untouched holdout episode per condition; holdout strictly later than comparison.
- Seed `20260915`.
- Existing point-in-time gating, alias masking, next-day execution, slippage, deterministic risk checks, run classification, fresh portfolio per episode — all unchanged, none of this code was touched.

## What was done

1. **Pre-flight confirmations** (before any change): `backtest_run_configuration` migration confirmed live and active (table/columns/PK/FK present, zero `anonymous`/`PUBLIC` grant, RLS disabled — re-verified again at the end of this block, see Test results). `HANDOFF.md` confirmed at 399 lines both before and after.
2. **Evaluation-suite parameters updated** (`backtest-harness/eval_suite.py`) to express the frozen design exactly, and only that:
   - `ELIGIBLE_RANGE_START`: `2021-01-01` → **`2022-06-01`** (the eligible-range end, `2026-01-31`, and the 4 required regimes were already correct defaults — untouched).
   - `EPISODES_PER_REGIME`: `2` → **`1`** (exactly one comparison episode per condition, per the frozen design; `HOLDOUT_EPISODES_PER_REGIME` was already `1`).
   - Both changes are pre-committed parameters read *before* any month is classified — nothing was tuned after seeing results.
3. **Manifest generation attempted**: `python eval_suite.py propose --seed 20260915`, which performs one real ranged SPY fetch through the existing `maverick-mcp`/yfinance path (the only I/O `propose_manifest` ever does — no candidate-configuration result, no trade or agent-decision data is read at this stage).
4. **Result: `ManifestGenerationError`, raised inside `select_holdout_pool`, before any per-episode fixed-watchlist viability audit ever ran** (that audit only runs on months that already survived selection — see `eval_suite.py`'s own module docstring). No manifest file was written (`propose_manifest` raises before `save_manifest` is ever reached — confirmed: `eval_suites/` still contains only the pre-existing stale `manifest_20260913T154853_887d30cff268.json`, untouched).

## The exact feasibility failure

```
ManifestGenerationError: holdout pool reaches all 4 required regimes only by
expanding to the trailing 38 months (from 2022-12), but that leaves the
comparison pool (every month before it) missing ['sideways'] -- cannot
satisfy full 4-regime coverage in both sets simultaneously from this
eligible range; widen ELIGIBLE_RANGE_START or lower HOLDOUT_MONTHS_COUNT
and regenerate. Not fabricating coverage.
```

The real, verified cause (SPY monthly classification over the full eligible range, computed once, read-only, never re-run after inspection):

| Regime | Months found (2022-06 → 2026-01) |
|---|---|
| `rally` | 7 |
| `sideways` | 9 (earliest: **2023-04**) |
| `high_volatility` | 4 |
| `selloff` | **2 only — 2022-09 and 2022-12** |

Both real `selloff` months fall inside the first 7 months of the eligible range. `select_holdout_pool`'s algorithm (unchanged, not touched by this block) requires the *entire* trailing holdout window to contain all 4 regimes as a contiguous block. Reaching a `selloff` month at all forces the holdout boundary back to 2022-12 — but every `sideways` month is chronologically *after* that boundary (first one: 2023-04), so the leftover comparison pool (2022-06 → 2022-11, six months) can never contain a `sideways` example. There is no boundary position that gives both pools all 4 regimes simultaneously under this exact eligible range — this is the algorithm's own documented Case 2 ("expanding the holdout further only shrinks the comparison pool more — it can never recover a regime already lost"), not a bug and not a coincidence of this particular seed (seed only affects *which* month is sampled within an already-covered regime, never which regimes exist in a pool).

Per the block's own explicit instruction, **nothing further was tried**: `ELIGIBLE_RANGE_START` was not widened again, `HOLDOUT_MONTHS_COUNT` was not lowered (it would not have changed the outcome — the boundary that first reaches 4-regime holdout coverage is determined by where the required regimes actually fall chronologically, not by the search's starting size), the viability audit was not relaxed, no condition was dropped, the watchlist was not changed, and no alternate month was hand-picked.

## Required-analysis fields — not applicable, and why

Per-episode/per-configuration status, return, drawdown, trade/proposal/fill/block counts, win rate, fundamentals availability/effect counts, decision-pairing comparison, and BUY/news-veto grounding checks all require an approved manifest and at least one run. None exists:

- **Exact selected months and conditions:** none were selected into a manifest (the classification table above is diagnostic data explaining the failure, not a selection).
- **Manifest hash and commit:** none generated — nothing to hash or commit.
- **Evaluation run IDs and classification:** none — zero episodes registered, zero rows written under `run_kind='evaluation'` (or any kind) for this block.
- **Baseline vs. fundamentals comparison / holdout outcome:** not measured.
- **Halted episodes:** none started, so none halted in the per-episode sense — the *manifest itself* is what failed to materialize.

## Test results

`python verify.py` (run before the parameter change and again after, from the repo root): **11/11 non-mutating suites passed both times**, `backtest-harness` pytest at 196 (unchanged — the two `eval_suite.py` parameter edits and the fix below did not change the test count).

**Incidental finding, found and fixed as part of confirming "no unintended mutation occurred" for this block:** `test_eval_suite.py`'s `test_run_configuration_halts_cleanly_without_advancing` mocked `eval_suite.run_day`/`register_run`/`fetch_fundamentals_assessment_counts` but not the failure path's logger. `_run_cycle_or_halt`'s `fail_logger` parameter defaults to the *real* `run_backtest.log_cycle_failure`, bound at `run_backtest.py`'s own def-time — patching `eval_suite.log_cycle_failure` (which didn't even exist as a name in that module before this fix) could never have intercepted it. With Docker actually running (needed for this block's own manifest-generation attempt), that test's deliberately-raising fake `run_day` reached the real logger and wrote **3 genuine `cycle_failed` rows to Neon's `agent_activity` table** (`mode='backtest'`, fresh random `run_id`s never registered anywhere, description `"Day 1: RuntimeError: simulated cycle failure"`) during this session's own `pytest`/`verify.py` runs — despite the suite's explicit, repeated claim of zero real I/O. Root-caused, fixed at the source (`eval_suite.py` now imports `log_cycle_failure` and passes it explicitly so it is patchable at this module's own boundary, matching `register_run`'s existing pattern; the test now patches it and asserts the fake, not the real path, was hit) — reran the fixed test with Docker still running and confirmed `agent_activity`'s row count is unchanged (812 before, 812 after). The 3 pre-existing orphaned rows this bug already wrote (verified: no registry row, no trades/snapshots/evidence for any of them — pure debris) were **not deleted**; the delete attempt was blocked by this environment's own shared-resource-modification safety gate. Left in place, reported here precisely instead of silently cleaned up or silently ignored.

Confirmed unchanged throughout, before and after every step of this block: `backtest_state.json` (Round 2's real run_id/cursor/`trading_days_elapsed`/`closed_pnl` sum), `RULES.md`/`AGENTS.md`/`SOUL.md`, model/provider config, the fixed watchlist file, `backtest_run_registry`'s 6 real historical rows plus the 3 pre-existing unrelated `smoke` rows already documented in `docs/PROJECT_BACKLOG.md` §3 (2026-09-15b) — no new row added to that table by this block. No training or live-paper run occurred. No Cloudflare deploy (no dashboard source changed).

## Conclusion on Fundamentals V1

**Keep Fundamentals V1 as unproven context: mixed or insufficient evidence; do not strengthen its role.**

This is the honest fit, not a judgment call disguised as one of the other two options: zero comparison or holdout evidence was gathered this block, so there is nothing directionally-better to justify strengthening its role (ruling out "Keep V1"), and nothing showing no-benefit-or-worse to justify disabling it (ruling out "Disable V1"). The status quo established by the prior acceptance milestone (`fundamentals_v1_acceptance.md`) and the correction-pass integration (contextual-only, never an independent trigger, never a mandatory veto) is unchanged and stays exactly where it was.

## Bayesian optimization readiness

**Not ready, unchanged from before this block, and this block makes the specific reason more precise than before:** the fixed-watchlist-history-vs-four-regime-plus-holdout tradeoff first surfaced 2026-09-13 is now confirmed, with real data, to be **infeasible at 2022-06-01** specifically because both real `selloff` months in the post-2022-06 window (2022-09, 2022-12) sit too early to leave any `sideways` month in a comparison pool once a holdout boundary must reach back far enough to include one of them. Widening the eligible range further back reopens the original RYAN/ALGM viability-audit failure (real IPOs in 2020/2021 lack trailing technical-service history before ~2022-06). Resolving this needs one of: (a) a start date between the original 2021-01-01 and 2022-06-01 that happens to thread both constraints (not yet checked), (b) accepting a watchlist exception/adjustment for RYAN/ALGM specifically (a real universe-composition decision, not a range tweak), or (c) relaxing the "all 4 regimes in one contiguous trailing holdout block" requirement in `select_holdout_pool` itself (a real change to the evaluation-suite's own selection algorithm, not a parameter). None of these is a small technical fix; each is a genuine tradeoff a human needs to choose, same as the range decision that produced this block. Until a manifest actually generates, is verified, and is approved, there is no comparison/holdout evidence of any kind — Bayesian optimization remains categorically premature, independent of this specific blocker.
