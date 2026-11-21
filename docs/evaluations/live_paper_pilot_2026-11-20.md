# Supervised live-paper pilot, 2026-11-20 (Alpaca paper)

**Window:** 19:00–23:00 Asia/Dubai (10:00–14:00 ET). Paper account only. The purpose was to verify live order
handling, not profitability.

**Outcome:** one real entry (WMG). A code defect left it unprotected for about 10 minutes. The defect was fixed and the
existing reconcile path then protected the position. It ends the window held under a verified broker-held OCO.

## Run and configuration

| Time (Dubai) | Step |
|---|---|
| 18:55:42 → 18:59:12 | `python live/run_live.py dry-run --hermes --universe watchlist`: orders disabled, valid no-entry plan, tracing confirmed |
| 19:00:14 | Pre-execution broker check: $100,000 cash, 0 positions, 0 open orders |
| 19:00:28 → 19:02:08 | `cycle --universe watchlist` (supervised): no entry, broker and journal unchanged |
| 19:02:41 | `loop --universe watchlist` started |
| 20:05:45 | **Incident:** entry cycle crashed after the WMG fill (see below) |
| 20:07:02 | Loop stopped during its sleep. New entries halted. |
| ~20:14 | Adapter fix plus regression test; live tests 240/240 |
| 20:14:52 | `loop --universe watchlist` restarted. Its first tick (reconcile-only) protected WMG at 20:15:43. |
| 22:57:43 | Loop stopped during its sleep, after the 22:57 reconcile tick. No review started after 23:00, and no cleanup ran past it. |

- **Universe and selector:** the frozen ten-name watchlist, chosen explicitly. Laya ran every review (10 scored, 10
  selected).
- **Hermes:** the restricted `live` profile with the `hermes-trace` plugin, tracing on.
- **State and logs:** state is in `~/.hermes-trading-agent-live` (private). The pilot log, check outputs and loop logs
  are in its `pilot_2026-11-20/`.
- **Profile check:** the installed plugin's `otlp.py` predates the repo copy by one change: a `scope` parameter whose
  default is the live scope. Live behaviour is identical, so `setup-profile` was not rerun.

## Reviews and errors

There were 5 Hermes reviews: the supervised cycle plus the loop slots for 10:00, 11:00, 12:00 and 13:00 ET.

| Review | Outcome |
|---|---|
| 19:00 cycle | No entry. Google News returned 429 for ALGM, ATI and SFM, so those entries were blocked as required. |
| 19:02 (10:00 ET) | No entry |
| 20:02 (11:00 ET) | **WMG entry**, then a cycle error |
| 21:04 (12:00 ET) | Held WMG, no entry |
| 22:00 (13:00 ET) | Held WMG, no entry |

There were 40 reconcile-only ticks in all. One cycle failed: `TypeError` at 20:05:45. After the fix, 0 failures.

## Orders

| Order | Detail | Result |
|---|---|---|
| Entry `lv-a8c292b678024f42b0c1-entry0` | BUY WMG 621, IOC limit $32.18. Plan stop $30.90 (5-session low), target $36.20; risk $794.88, notional $19,983.78 | **Filled in full**, 621 @ $32.09 |
| Protection `lv-a8c292b678024f42b0c1-protect0` | OCO SELL 621: target limit $36.20, stop $30.90 | Accepted (`new`, stop leg `held`). Still open at the end. |

Totals: 2 submitted, 0 rejected, 0 partially filled. Hermes proposed one entry plan (WMG), and it passed validation.

## Protection verified

**WMG** was checked independently against the broker at 20:15:59 and 22:58:02:
- exactly one open sell order;
- quantity 621, equal to the held shares;
- stop $30.90 and target $36.20, equal to the plan.

The controller reported `protection: active` on every tick from 20:15:43 to 22:57.

## Incident: unprotected holding, 20:05:41 → 20:15:43

- **What happened:** `enter()` submitted the entry, and the IOC filled. Its follow-up lookup,
  `AlpacaBroker.order_by_client_id`, returned the string `"<Response [200 OK]>"`. The cycle raised `TypeError` before
  placing the OCO.
- **Cause:** `out = ... else (r.raise_for_status() or r.json())` in `live/adapters.py`. httpx 0.28.1 (in the
  `alpaca-mcp` container) returns the Response from `raise_for_status()`, not `None`, so the lookup yielded the Response
  itself. The adapter test stub's `raise_for_status` returned `None`, so the contract tests could not see the failure.
- **Impact:** every reconcile reads orders by client ID. So, until the fix, the loop could not record the fill, place
  protection or settle exits.
- **Response:**
  - The loop was stopped during its sleep.
  - The fix replaced the expression with explicit statements (404 still returns `None`).
  - The test stub now returns the Response, as httpx does. The existing lookup test failed before the fix and passes
    after it.
  - `live/` tests: 240 passed.
  - On restart, the existing reconcile path marked the entry filled, wrote the journal fill and placed the OCO. No manual
    order was placed.
- **Status:** the fix is **uncommitted**. Commits and pushes were out of scope for this block.

## Exits and reconciliation

- **Exits:** none. No stop or target triggered, and Hermes chose HOLD at 21:04 and 22:00.
- **Journal:** the one `execution_mode='alpaca'` row (WMG BUY) went from `pending` to `filled` at 20:15: 621 @ $32.09,
  stop $30.90, target $36.20. It agrees with the broker and the controller state.
- **Unmanaged positions:** none.

## Traces (Langfuse, environment `live-paper`)

All 44 traces from 18:55 to 22:57 were delivered to the hosted project, with 0 spooled locally.

| Dubai | Trace | Notes |
|---|---|---|
| 18:55 | `2c1fe64a64844deb5b72d6120dd53b5b` | Dry run, 49 observations (controller 21, gateway 28) |
| 19:00 | `72d6c5e7fd1f58f90763ce18bcf5333a` | Cycle. 3 news-tool errors (429). |
| 19:02 | `ad44851a5ea376e9b3308059e696c029` | Loop review |
| 20:02 | `b4fd679561bf8e039381e6217c58f6dd` | WMG entry. `entry_validation` and `live_cycle` spans at ERROR level (the incident). |
| 20:15 | `60dc7c8723476d7fc43a9a66e98b47d5` | Recovery reconcile: fill recorded, OCO placed |
| 21:04 | `22d438503a4cc3ccde67334fa6055d0c` | Review, WMG HOLD |
| 22:00 | `8717c23ebfbefcc24132695f5d52e8a3` | Review, WMG HOLD |

## Final state (22:58:02 Dubai)

| | |
|---|---|
| Equity / cash | $100,220.46 / $80,072.11 |
| Positions | WMG 621 @ $32.09 (last $32.445) |
| Open orders | 1 OCO for WMG: SELL 621, target $36.20, stop $30.90 |

WMG was not liquidated. The broker keeps holding its protection after the controller stops.

## What worked

- IOC limit entry: a full fill at a price better than the limit.
- The broker accepted the OCO in Alpaca's real leg shape (parent limit, stop leg `held`), and the controller's
  protection check recognised it.
- Reconciliation after an interrupted cycle: the persisted intent was recovered with no duplicate order.
- Journal pending → filled, review-slot claiming across a restart, and the Hermes HOLD path.
- Blocking an entry when the news check failed (the Google News 429s).
- Tracing from both the controller and Hermes, fully delivered.

## Still needs a real-order test

- A **partial** or **unfilled** IOC entry.
- **Stop or target fills**, and the closing journal write.
- A Hermes-requested **EXIT** (agent sell) and its settlement.
- **REVISE**, which is cancel → confirm → resubmit of the OCO.
- Cancel of a working order. The engine's `cancel` path was not exercised.
- An entry the broker cannot find (`not_found` after the grace period).
- The screened universe and a pool larger than 20, which needs a usable weekly snapshot.

Open items to carry forward:
- Commit the adapter fix.
- Google News returned 429 during Hermes research at 19:00 (3 names blocked). Watch whether rate limiting recurs at
  this review cadence.
- Have the pilot checker also flag `entry_pending` plans whose broker order is filled. That gap is why the first
  post-incident check reported no problems until the engine state was inspected.
