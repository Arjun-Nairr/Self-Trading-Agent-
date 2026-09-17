# Fundamentals V1 measurement block (2026-09-15) — blocked before any run

**Status (2026-09-16 update): the selection RULE that caused this block's infeasibility has been corrected — see "2026-09-16 correction" at the end of this report for the new paired per-regime design and the resulting valid, verified `manifest_version: 4` manifest. Its status is still `"proposed"`, not `"approved"` — no episode has run under it. Everything below this line is the original 2026-09-15 finding, preserved unedited as the honest record of why the original design failed.**

**Original status: manifest generation infeasible under the frozen design. Zero Hermes calls, zero evaluation episodes, zero trading decisions were made under this block.** This report documents why, exactly as the block's own instructions required ("if this exact design cannot produce a valid manifest, stop before any Hermes call or evaluation run").

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

---

## 2026-09-16 correction: the selection rule was the problem, not the range

The user chose option (c) above: replace `select_holdout_pool`'s single globally-trailing-block holdout requirement with a **deterministic paired per-regime design** — for each of the 4 required regimes independently, one comparison episode and one strictly-later holdout episode of the *same* regime, never requiring every holdout episode to be later than every comparison episode across regimes. The fixed watchlist, eligible range (`2022-06-01`..`2026-01-31`), 4 required regimes, model/provider, and trading rules were all left exactly as they were.

### Why the old rule was infeasible, restated precisely

`select_holdout_pool` required ALL 4 regimes to appear inside ONE contiguous trailing calendar window. Both real `selloff` months (2022-09, 2022-12) fall early in the range, while every real `sideways` month falls no earlier than 2023-04. Any trailing window wide enough to reach back and capture a `selloff` month necessarily swallows every `sideways` month too, leaving nothing for the comparison side. No boundary position satisfies both sides at once — this was a structural property of the data under that specific rule, not a bug and not fixable by re-tuning `HOLDOUT_MONTHS_COUNT` or the eligible range (already shown in the original finding above).

### Why the paired design resolves it

The paired design never asks for a single shared window. Each regime's pair is chosen independently: `selloff`'s only two eligible months (2022-09, 2022-12) become its own comparison/holdout pair, in chronological order, by construction — regardless of where any *other* regime's months happen to fall. `sideways` is free to draw its own later pair from its own 2023-04-onward pool without needing to coexist with `selloff` inside one window. This is a genuine change to the selection RULE (`backtest-harness/eval_suite.py`'s `select_paired_episodes`, replacing `select_holdout_pool`), not a parameter retune, and not a relaxation of any requirement the original design actually needed (every regime still gets exactly one comparison + one later holdout of the same regime; comparison/holdout still never overlap).

### The scientific limitation — stated plainly

**This is not a single globally-later holdout period.** Under the old (infeasible) design, if it had worked, every holdout episode would have been drawn from calendar time strictly after every comparison episode — a clean "trained years, tested on the following year" story. Under the paired design, that global ordering does **not** hold: a regime's holdout episode is only guaranteed later than *that regime's own* comparison episode. For example, in the manifest below, `sideways`'s comparison episode (2024-10) is chronologically *after* `rally`'s holdout episode (2023-01). A reader evaluating results from this manifest must not describe the holdout set as "the later, out-of-sample period" in the aggregate sense — only per-regime. The manifest's own `holdout_design.caveat` field states this exact limitation so it travels with the data, not just this report.

### The new manifest — generated and verified, not approved

Generated live via `python eval_suite.py propose --seed 20260915` (real SPY + real fixed-watchlist technicals/closes through the existing `maverick-mcp`/yfinance path — the same production data path every prior manifest attempt used):

- **File:** `backtest-harness/eval_suites/manifest_20260916T154923_30f4c12e0f7e.json`
- **`manifest_version`: 4** (bumped from 3 — `require_approved` now refuses any manifest generated under the old algorithm, including the committed stale v2 file, which was already refused on version grounds and remains untouched)
- **`status`: `"proposed"`** — **not approved**, per this block's own instruction to stop here
- **`content_hash`**: `30f4c12e0f7ed7bb...` (verified against a fresh recomputation — `verify_manifest` passes, untampered)
- **`selection_seed`: 20260915**
- **`eligible_range`**: `2022-06-01` .. `2026-01-31` (unchanged)

| Regime | Comparison | Holdout | Holdout later than its own comparison? |
|---|---|---|---|
| `high_volatility` | 2022-06 | 2022-11 | yes |
| `rally` | 2022-07 | 2023-01 | yes |
| `selloff` | **2022-09** | **2022-12** | yes — the only 2 eligible months, forced into the pair in chronological order, exactly as required |
| `sideways` | 2024-10 | 2025-11 | yes |

All 8 episodes passed the existing fixed-watchlist viability audit with zero unavailable tickers (confirms `2022-06-01` clears the RYAN/ALGM IPO-history floor identified 2026-09-13). Verified programmatically, not just visually: all 4 regimes present exactly once on each side; zero date overlap between comparison and holdout; every pair's holdout strictly later than its own comparison; no episode date repeated across all 8; `role`/`pair_id`/`watchlist_viability` present on every episode; `holdout_design` records the method and states the per-regime caveat verbatim.

**Still not approved, still no episode run.** Fundamentals V1 remains unproven — this block only proves a valid manifest *can* now be generated and verified under the corrected design; it gathers zero comparison or holdout evidence by itself. The conclusion in this report's own "Conclusion on Fundamentals V1" section above is unchanged: **keep Fundamentals V1 as unproven context.**

**The one explicit next human approval needed before any evaluation can run:** review this manifest (`manifest_20260916T154923_30f4c12e0f7e.json`) and, if satisfied, flip its `"status"` field from `"proposed"` to `"approved"` (the one edit excluded from the content hash) — then give an explicit separate go-ahead to actually execute `technical_news_baseline` and `technical_news_fundamentals_v1` against it. Neither approval nor execution happened in this block.

---

## 2026-09-16b: approved, comparison-stage run started — interrupted

The manifest above was reviewed and approved (commit `be1b4b9`). Executing `run_configuration` for the `technical_news_baseline` configuration surfaced a real infrastructure gap on its very first call: `backtest_run_registry`'s `run_kind` CHECK constraint had never actually been widened live on Neon to permit `'evaluation'`, despite the schema.sql migration existing since the September correction pass (a prior activation task widened `backtest_run_configuration` but not this sibling constraint). Applied the already-committed, purely additive migration live and verified it (row count unchanged, every existing `run_id`/`run_kind` pair byte-identical before and after).

Retried. The baseline configuration then ran for real against the `high_volatility` comparison episode (2022-06) — **run `e9e5644e-fd56-4be8-a8b6-5132be7a71c7`** — before being **deliberately interrupted by the user** after 12 of June 2022's ~22 simulated trading days (29 journaled decisions, 19 fills, 0 left pending), once a real gap was identified: queued BUYs were never re-checked against headlines published in the overnight gap between their decision-time news check and their actual 09:45 ET fill.

A strictly read-only audit (no resume/retry/delete/approve/revert) confirmed: zero holdout episodes touched, zero non-`evaluation` runs created, zero live-paper/broker orders, Round 2 completely unchanged (Neon `trades`/`portfolio_snapshots`/`backtest_cycle_evidence` counts and local `backtest_state.json` all byte-identical throughout). The interrupted run's own Neon rows were left in place — a separate cleanup task, not addressed here — but **this run is excluded from all performance analysis**: it is invalid by construction (both genuinely incomplete and missing the gate the rest of this document addresses), not by judgment call.

## 2026-09-17: the missing gate implemented

**Correction notice (2026-09-17b, see the new section at the end of this report): the claim in this section that the window started "strictly after the original decision day's close" was NOT actually true as implemented — the real code started the window at the FOLLOWING MIDNIGHT instead, silently skipping the after-close/pre-midnight gap. This section is preserved unedited as the honest historical record of that first (buggy) implementation; do not rely on its window-boundary claim without reading the correction below.**

`apply_pre_fill_news_gate` (`backtest-harness/run_backtest.py`) closes exactly the gap the interrupted run surfaced. Immediately before `simulate_trading_day` ever sees a queued BUY, the gate re-checks it against real headlines timestamped strictly after the original decision day's close through the actual 09:45 ET fill cutoff — timezone-aware (`America/New_York`), both boundaries explicit; a naive or unparseable timestamp fails that order closed rather than approximating a boundary. Materially adverse news, an unavailable feed, or a malformed confirmation reply all resolve the order rejected (`pre_fill_news_veto` / `pre_fill_news_unavailable`) through the same idempotent `resolve_trade_rejection` path every other rejection in this codebase already uses. Queued SELLs and mechanical exits are structurally untouched — the gate only ever inspects `action == "BUY"` orders. `risk_checker.py` stays pure math/rules with zero external tool access; the materiality judgment stays with Hermes, through one small, constrained, batched confirmation call per cycle (never a second full trading cycle) whose output schema can only ever be `CONFIRM`/`CANCEL` per alias — headline/summary text is explicitly treated as untrusted data in the prompt itself, the same prompt-injection discipline `RULES.md` §5a's sentiment veto already relies on.

14 new focused tests were added (`test_execution_semantics.py`), covering: adverse news cancels; neutral news confirms; an unavailable feed and a malformed/naive timestamp both fail closed without ever calling Hermes; an article after the 09:45 cutoff is excluded; an article from before the original decision cutoff is excluded from the delta check; queued SELLs survive untouched even when an unrelated BUY's feed fails; the gate is deterministic across a simulated retry and `run_day`'s own wiring resolves a cancelled decision exactly once, leaving nothing pending; the confirmation prompt flags headlines as untrusted data; and a malformed/partial confirmation reply fails closed per-alias. Every test injects its own fake `fetch_news_fn`/`call_hermes_fn` — zero real Docker/Neon/Hermes/broker I/O anywhere in the suite.

The approved manifest's `status` was reverted `approved` → `proposed` (a single-field edit; content hash re-verified unchanged) — a fresh comparison-stage run needs a fresh human review and approval of this exact fix before it can proceed. **Live parity, documented rather than built speculatively:** live mode currently has no queued/delayed BUY execution path at all — `alpaca-mcp`'s `place_order` submits immediately — so there is nothing existing for this gate to extend on the live side. This is recorded here as a real, explicit prerequisite for any future delayed live-execution design, not implemented ahead of need.

**Nothing has run under either configuration to completion.** The conclusion stated above is unchanged: **keep Fundamentals V1 as unproven context.**

---

## 2026-09-17b: the timestamp boundary itself was wrong — corrected

### The defect

The section above's own documentation claimed the pre-fill window started "strictly after the original decision day's close." That was aspirational, not accurate: the actual implementation recorded only `QueuedOrder.queued_date` (a bare calendar date, e.g. `"2026-02-17"`) and computed the window's lower bound as the *following midnight* — `datetime(queued_date + 1 day, 00:00, tzinfo=ET)`. Any headline published between the real regular-session close (typically 16:00 ET) and that midnight — exactly the window earnings releases and major after-hours announcements land in — was silently invisible to the gate. This is a real, structural gap, not an edge case: an adverse earnings miss released at 16:30 ET on the decision day would never have been seen by either the decision-time news check (which only covers up through that day) or the pre-fill gate (which, under the bug, only started checking at midnight).

### The correction

**1. Durable, timezone-aware `decision_cutoff_at`, not a date.** `QueuedOrder.queued_date` is replaced by `decision_cutoff_at` — the REAL regular-session-close timestamp for the day the order was queued, computed once via `_session_close_at(day, calendar_close)` where `calendar_close` comes from the existing `fetch_trading_calendar_day` (already used elsewhere in the harness for half-day awareness — e.g. the day after Thanksgiving closes at 13:00, not 16:00). This is fetched unconditionally, once per cycle, and frozen onto every order queued that cycle — never inferred later from `queued_day_index` or a calendar date. A pre-migration order (no field at all) or one written under the first, buggy gate version (a bare date, which parses as a *naive* datetime) both fail closed for BUY execution — `_pre_fill_news_window` returns `None` for anything missing, unparseable, or lacking a timezone.

**2. The exact window.** `decision_cutoff_at < headline.created_at <= fill_cutoff_at`, where `fill_cutoff_at` is Day T+1 at 09:45 ET — the same fixed point `simulate_trading_day` itself already fills queued BUYs at. Proven directly: a headline at 16:05 ET on the decision day (5 minutes after a 16:00 ET close) is now included and can cancel the order; a headline timestamped *exactly* at the cutoff is excluded (the lower bound is exclusive, matching "already visible to that day's own decision-time check"); a headline after 09:45 ET on the fill day is excluded; a half-day session correctly uses its own earlier real close.

**3. RFC-3339 timestamps and real pagination, not date-only strings.** `alpaca-mcp/server.py`'s `get_historical_news` previously took `(ticker, as_of, lookback_days)` and sent Alpaca's News API a *date-only* `start`/`end` — ambiguous with respect to same-day time boundaries and incapable of expressing a precise decision cutoff. It now takes `(ticker, start_at, end_at)` as exact RFC-3339 timestamps (this project always sends UTC, converted from the real ET-aware boundary via `_to_utc_rfc3339`, which is DST-correct because `zoneinfo` resolves the real UTC offset for the actual date rather than assuming a fixed −4/−5). It also now paginates for real via Alpaca's own `next_page_token`, capped at `MAX_NEWS_PAGES=20`, and returns an explicit `{"articles": [...], "complete": bool}` — hitting the cap with a token still pending reports `complete: False`. Both callers (`fetch_news`, the ordinary decision-time fetch, and `fetch_news_window`, the gate's own fetch) treat `complete: False` as a hard failure for the affected ticker, never a silently-partial result. The final cut against the precise window boundary always happens client-side against each article's own real `created_at` (`_articles_in_window`) — the provider query is only ever trusted as a superset.

**4. The ordinary decision-time fetch also gained an explicit cutoff.** `fetch_news` (unrelated to the pre-fill gate — this is the fetch that feeds the SAME-day decision itself) previously used a date-only `end`, which risked the mirror-image leak: an end-of-day decision could, depending on how the provider interpreted a bare date's day boundary, silently receive a same-day headline published *after* the real decision cutoff. It now receives the same `decision_cutoff_at` explicitly as its real `end_at`, so both the decision-time check and the pre-fill gate now share one consistent, non-overlapping, non-gapped timeline: `(existing decision, decision_cutoff_at] → pre-fill gate (decision_cutoff_at, fill_cutoff_at]`.

**5. Callers updated to the new `get_historical_news` signature.** `watchlist/check_news_coverage.py` (an operational coverage-checking script) and `alpaca-mcp/test_historical_news.py` (a real, credentialed proof-of-life test, never run by `verify.py` or by this correction) were both updated to build explicit UTC timestamps and read the new `{"articles", "complete"}` shape.

### Architecture unchanged, as required

`risk_checker.py` remains pure math/rules with zero tool/network access — the gate and its confirmation call live entirely in the harness. The confirmation step is still constrained to `CONFIRM`/`CANCEL` only. Queued SELLs and mechanical exits remain completely unaffected — the gate only ever inspects `action == "BUY"` orders, unchanged from the first version. Positive/neutral news still can only ever justify `CONFIRM`, never create or strengthen a buy. Unavailable, ambiguous, or malformed data — including, now, unproven-complete pagination — still fails closed.

### Tests

6 new tests added to `test_execution_semantics.py` (20 total for the pre-fill gate across both correction passes): the 16:05 ET inclusion case; the exact-cutoff exclusion case; a half-day's real earlier close; DST-correct UTC conversion (EDT vs. EST for the same 16:00 ET wall-clock close); `fetch_news_window` sending exact RFC-3339 UTC timestamps (captured via a fake `run_in`, no Docker call made); incomplete pagination failing the affected BUY closed; and a legacy order with no `decision_cutoff_at` at all failing closed through the full gate. A new pure test file, `alpaca-mcp/test_historical_news_pagination.py` (5 tests, `httpx.AsyncClient` entirely mocked, zero real network), proves `get_historical_news`'s own pagination/completeness contract directly and is now part of `verify.py`'s suite list.

`python verify.py`: **12/12 non-mutating suites pass** (backtest-harness pytest 213 → 219, plus the new alpaca-mcp pagination suite). Neon row counts and `backtest_state.json` (Round 2) confirmed byte-identical before and after this entire correction. No training, evaluation, smoke test, holdout, or live-paper process executed. The manifest remains at `status: "proposed"` (already reverted there by the prior 2026-09-17 pass; this correction never touched it, no field changed, content hash unaffected).

**Nothing has run under either configuration. Conclusion unchanged: keep Fundamentals V1 as unproven context.**

---

## 2026-09-17c: the ordinary decision-time fetch never independently checked what it got back — corrected

### The defect

2026-09-17b closed the pre-fill gate's own window precisely, and gave `fetch_news` (the ordinary same-day Day-T fetch, unrelated to the gate) an exact RFC-3339 `end_at = decision_cutoff_at`. But `fetch_news` never verified what Alpaca actually returned against that boundary: it trusted every article in the response and truncated to the first four headlines before checking any `created_at`. A provider article outside the requested range, or one with a missing, malformed, or timezone-naive timestamp, could have reached the Day-T prompt — the same "trust the provider's own boundary interpretation" gap `_articles_in_window` already closed for the pre-fill gate's window, just never applied to this fetch.

### The correction

A new pure helper, `_articles_at_or_before_cutoff(articles, cutoff)` (`backtest-harness/run_backtest.py`), validates every article's `created_at` via the existing `_parse_news_timestamp` and keeps only those at-or-before `cutoff` — a single inclusive upper bound, no lower bound (the 7-day lookback stays an advisory scoping window, not an integrity boundary). It returns `None` — fail-closed — the instant any article's timestamp is missing, malformed, or naive. `fetch_news`'s generated script now returns every raw article per ticker (untruncated); the function applies this helper on the host, before the existing 4-headline prompt cap, and raises if validation fails for any ticker — halting the cycle through the exact same failure path the pre-existing incomplete-pagination check already uses. `_to_utc_rfc3339` was also hardened to raise `ValueError` on a naive `datetime` instead of silently converting it via the host machine's own local timezone (previously a real risk: the same naive input would produce a different, wrong UTC boundary on a different machine).

### Architecture unchanged, as required

The pre-fill gate, `risk_checker.py` (pure math, zero tool access), sizing, rules, watchlist, fundamentals, dashboard, and evaluation design are all untouched. Positive news still cannot create a buy; exits remain unaffected — this correction touches only `fetch_news`'s own internal validation.

### Tests

6 new zero-network tests in `test_execution_semantics.py` (225 total in `backtest-harness`): a post-close article excluded from the ordinary decision-time filter; an article exactly at the cutoff included; malformed and timezone-naive timestamps failing the filter closed, both as a direct helper test and end-to-end through `fetch_news` (raises, halting the cycle); `fetch_news` forwarding the exact RFC-3339 cutoff and 7-day-back start while validating before truncating; and `_to_utc_rfc3339` rejecting a naive `datetime`.

`python verify.py`: **12/12 non-mutating suites pass** (backtest-harness pytest 219 → 225). Neon row counts and `backtest_state.json` (Round 2) unchanged — no run, training, evaluation, smoke test, holdout, or live-paper process executed. Manifest remains `status: "proposed"`, untouched.

**Nothing has run under either configuration. Conclusion unchanged: keep Fundamentals V1 as unproven context.**
