# Workflow dashboard page — implementation and verification, 2026-10-03

**Result:** a Dashboard V2 **Workflow** page shows one backtest run's main
data flow per simulated day:
- two connected paths of major stages;
- replay of recorded outcomes;
- a live "follow current" mode driven by new structured progress events.

Verified against the real 60-pool smoke run `5573597c…` (replay) and one
fresh two-session smoke run, `a0f96768-2e36-4674-9303-e9a3c120f64b` (live
follow). Deployed to the established Cloudflare Pages preview (see the end of
this record).

Code: commit `e18f7d3`. Trading logic, risk rules, execution order and
existing public views are unchanged.

## 1. What shipped

**Progress events (telemetry only)**

| Piece | What it does |
|---|---|
| `trade-journal-mcp/workflow_contract.py` | The public-safe contract (12 stages; 4 statuses: started / completed / failed / skipped). Per stage, an allowlist of metric keys whose values must be ints, bools, 4-decimal fractions, rule codes or fixed enums. Unknown fields are rejected, not dropped. No ticker, alias, prompt, reasoning, tool payload, path or free text can be stored. |
| `server._record_workflow_events` | Validates each event, inserts it idempotently on `(invocation_id, seq)`, and reports rejections individually. |
| `schema.sql` (additive migration, applied) | New `workflow_events` table, not granted to `anonymous`. New `public_workflow_events` view, SELECT-granted. Nothing else changed. |
| `backtest-harness/workflow_events.py` | Enqueue-only emitter (details below). |
| `run_backtest.py` | Emits each event at the moment its stage happens (sequence below). |

The emitter:
- never raises, and evaluates metrics inside its own `try`;
- is bounded: when the queue is full it drops and counts, never blocks;
- writes batches from one background thread through the existing
  trade-journal container path;
- retries a batch once (idempotent), then counts and prints the failure;
- gives every harness invocation a fresh `invocation_id`, so retries and
  resumes stay distinguishable.

The events `run_backtest.py` emits, in order:
1. `run` and `cycle`;
2. the order path: `prefill_news`, `execution` (one event per order,
   cancelled / rejected / superseded / filled, linked by its existing
   `decision_id`), and `positions` (one event per exit with its kind and
   reason);
3. the candidate path: `population`, `laya` (aggregate counts and the
   min/median/max `review_probability` only), `shortlist`, `evidence`,
   `hermes`, and `decision_risk`;
4. per proposal, a `queued` event (`decision_id`) or a `rejected` event
   (positional key `p<day>-<n>` plus allowlisted rule codes; never a fake
   trade row);
5. a `journal` indicator after the snapshot/activity write.

The trade-journal image was rebuilt for the new `server.py` and contract
(`78a81a64…` → `f6d95565…`, 0 restarts, both files match the repository by
SHA-256). No other image changed. `eval_suite` episodes do not emit
telemetry (they never pass an emitter).

**Dashboard (`dashboard-v2/`)**

| Piece | What it does |
|---|---|
| `js/workflow-model.js` | Pure model. Merges events with existing journal records; anything else is "not recorded". Uses the latest attempt per day and lists earlier attempts. Links orders across days by `decision_id`. Run status is completed / halted / running / interrupted. |
| `js/pages/workflow.js` | Day selector, Play/Pause, Previous/Next, Follow current, expandable details. |
| `js/workflow-text.js` | Laya's seven inputs and versioned question/criteria, checked against `laya_adapter.py`. |
| `js/data.js` | Three read functions with explicit column lists over public projections only. |
| `js/app.js` | Nav item, plus a separate Workflow run selection that includes labelled smoke runs on this page only. |
| `css/app.css` | Horizontal flow, vertical stack at ≤980px, reduced-motion rules, and a ≤360px bottom-nav fix. |

Run status meanings:
- **Interrupted:** no progress for 15 min with no completion reported. It
  never reads as complete.
- **Completed:** a completed invocation still shows its open positions and
  pending orders.

## 2. Truthful ordering (from the code, confirmed by real events)

Within one simulated day, `run_day` runs the **order path at the open**
first:
1. the pre-fill news check on queued BUYs;
2. the opening-gap and 09:45 execution-time risk recheck and fill;
3. intraday and time-stop exits.

Only then does it run the **candidate path after the close**:
1. eligibility, then Laya, then the shortlist;
2. the technicals/news fetch, for the shortlist plus held names;
3. Hermes, then decision-time risk, then queue or reject.

The controller does every fetch, check and journal write; Hermes only
receives masked context and returns decisions. Held positions are monitored
whatever the shortlist says.

## 3. Verification

| Area | Evidence |
|---|---|
| Unit / integration | `test_workflow_events.py`, 13 tests: contract accept/reject; rule-code extraction; ordered, idempotent writes with retry; failure counted without raising; full queue drops without waiting; contract rejections reported. Plus the real `run_day` with mocked I/O: selector cycle stage order and links; next-day fill linked to its decision; pre-fill cancellation linked; mechanical exit; selector-off skips; Hermes failure; telemetry failure leaves the cycle byte-identical. |
| Dashboard fixtures | `node test/workflow.test.mjs`: 10 checks on synthetic rows. They cover event-sourced replay and links; journal-only reconstruction (no invented timings, supersession handling); silence = interrupted and recent = running; halted and retried attempts; selector-off and fundamentals-off; end-state positions and pending orders; run list (smoke included here, still excluded elsewhere); Laya text drift; the read-only public data boundary. |
| Regression | Harness suite **507 passed**. `verify.py` **12/12** (an existing static guard caught one comment naming the private run-configuration table; reworded). `calc.test.mjs` 48/48. `pytest watchlist` 156. `git diff --check` clean. |
| Anonymous access (real Data API) | `public_workflow_events` → 200, with only the 14 contract columns. `workflow_events`, `backtest_cycle_evidence`, `backtest_run_configuration` → **403**. Existing `public_trades` unchanged. The new run's 41 public events contain no ticker or alias. |

**Replay against real run `5573597c…`** (pre-telemetry, so rebuilt from
journal records; local preview, real public data):

| Day | Queued in | Fills / rejections | Open after | Exits | Queued |
|---|---|---|---|---|---|
| 1 | not recorded | — | 0 | 0 | 2 |
| 2 | 2 | 2 filled | 2 | 0 | 1 |
| 3 | 1 | 1 filled | 3 | 0 | 2 |
| 4 | 2 | 2 rejected | 3 | 0 | 1 |
| 5 | 1 | 1 rejected | 2 | 1 | 1 |

The end state shows 1 pending (ALGM ×515). All of this equals the run's
audited record. Laya, eligibility, shortlist and evidence correctly read
"Not recorded" for this run. During replay **0 network requests** were
made, and the steps advanced in controller order (order path, then
candidate path).

**Live follow, fresh smoke run `a0f96768…`**
- **Configuration:** 2026-09-28..29, 2 sessions; the same frozen
  `candidates60-v1-pool-2026-09-28-eec1e3f2203f`; `hermes-laya-worker:2`;
  `noul-criteria-v2`; fundamentals off; a fresh `--state-dir`; smoke kind.
- **Result:** exit 0, 2/2 sessions, 18:40:36Z→18:47:02Z (first to last event; launched 18:40:06Z, exited 18:47:11Z).
- **Trades:** Day 1 queued EMR and NTAP. Day 2: pre-fill news confirmed both
  (0 overnight articles), both filled at 09:45, and BRKR was queued. The
  invocation ended with 2 open and 1 pending (BRKR); nothing was forced.
- **Telemetry:** 41 events written, 0 duplicates, 0 dropped, 0 rejected,
  0 failed batches. One invocation; contiguous sequence; each event written
  **4.2–9.1 s** after the stage happened (median 4.5 s).
- **The page while following:** it showed *Running*. The highlight moved in
  real time:
  - Day 1: Laya scoring → technicals & news → Hermes → decision risk →
    queued.
  - Day 2: pre-fill news → execution → Laya scoring → technicals & news.
  - It then switched to *Invocation completed* and stopped polling by
    itself.
- **Reconciliation:**
  - Events vs private cycle evidence: Day 1, 51 eligible = 51 scored; 41
    not selected + 10 shortlisted; 2 proposals. Day 2, 49 eligible = 51 − 2
    held.
  - Laya min/median/max vs the private selection snapshots: identical
    (Day 1: 0.1701 / 0.1906 / 0.2318).
  - Order links: the Day 1 `queued` decision IDs are exactly the Day 2
    `filled` IDs; BRKR's queued ID is the pending trade row.
- **Isolation and cleanup:** Neon deltas are only the new run's rows, and
  no other run changed. Round 2 `backtest_state.json` is unchanged
  (`4b428d35…`). No Laya worker container is left, and only the base
  `backtest` Hermes profile remains.

**UI checks (local preview, real data):**
- **Desktop 1366px:** both paths on one row, boxes ≥157px, no overflow.
- **375px:** vertical stack with downward arrows; no page-level horizontal
  scroll.
- **320px:** the six bottom-nav items no longer clip. A 2px clip was found
  and fixed; labels stay 12px, and tap height is 44px.
- **Keyboard:** arrow keys move between boxes, and Enter/click opens
  details, with `aria-expanded` and focus kept.
- **Shell refresh:** 0 requests over 16 s idle on the Workflow page.
- **Isolation from other pages:** leaving the page stops polling (0 further
  requests). Overview still defaults to `e68525f9…` and shows its
  documented parity figures (equity $103,696.08), with no smoke runs listed.

## 4. Limitations (stated, not hidden)

- **Visual and motion checks:**
  - The preview pane could not be screenshotted (the window was not
    drawing), so layout was verified by measured geometry, not by eye.
  - Reduced motion is implemented as CSS (`prefers-reduced-motion` disables
    the transitions and the pulse, and replay steps more slowly), and was
    checked in code, not by OS emulation.
- **Hidden-tab shim:** during live follow the pane reported
  `document.hidden = true`, and the page correctly showed "Paused while this
  tab is hidden". To exercise the visible-tab polling path, `document.hidden`
  was overridden in the test session only; the shim never shipped.
- **Coverage gaps:**
  - Runs before this change show journal outcomes only. Their proposal
    rejections before journaling are "not recorded" (some appear only as
    risk-check counts).
  - `eval_suite` episodes emit no progress events.
  - The pre-fill gate's Hermes classification path, stop/time-stop exits,
    and a whole-day selector failure were not exercised live; they are
    covered by tests only.
- **Interruption detection:** a run killed outright leaves its last stage
  "in progress" until the 15-minute staleness rule labels it interrupted.
  It is never labelled complete.
- **`HANDOFF.md`:** has uncommitted user edits, so it was left untouched.
  Pending update: mention the Workflow page and `public_workflow_events`
  under the dashboard section.

## 5. Deployment

Deployed via the established mechanism (`npx wrangler pages deploy dashboard-v2
--project-name trading-agent-dashboard-v2 --branch mobile-test`) from a clean
checkout of the pushed commit. The project has no Git integration, so a
push alone does not deploy. The deployed page was confirmed to load and read
the public data; see the commit that records this file for the deployment ID.
