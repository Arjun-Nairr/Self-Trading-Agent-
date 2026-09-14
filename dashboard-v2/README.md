# Dashboard V2 — candidate redesign (not yet the active dashboard)

Full IA + visual rebuild of the trading dashboard: Overview / Trades /
Decisions / Runs / Roadmap. Vanilla HTML/CSS/JS — no framework, no build
step, no package manager, no external UI libraries or fonts.

**Dashboard-exposure correction (2026-09-13): backtest-only, live paper
inaccessible.** Live paper trading is not activated and has no route, tab,
selector, or control anywhere in this app — the header carries a fixed
"Backtest research mode" label instead of a mode toggle, and every data
request is scoped to backtest. The underlying live-paper query support
(`js/data.js`'s `MODES.live` mapping, page renderers' `mode === "live"`
branches) is untouched in the repository — this was a dashboard-exposure
change only, not a deletion — but nothing in the shipped app can reach it.
Re-exposing live paper is a separate, explicit, later decision (a
dedicated live-readiness review), not implied by this note.

The **Roadmap** page is a read-only visual projection of
`docs/PROJECT_BACKLOG.md` (six engineering sectors, each grouped into
completed / active / next / deferred). It reads a static local module only
and makes **zero Neon / Data API calls**; the shell issues no request while
it is the active page.

**V1 (`dashboard/index.html`) remains the active dashboard.** This is a
separate candidate site under `dashboard-v2/`; removing this whole directory
leaves the rest of the repository unaffected. The cutover itself — and any
`HANDOFF.md` update — is a separate, explicit decision, not made by this
task.

## Deployment

A preview of this directory is deployed on Cloudflare Pages (project
`trading-agent-dashboard-v2`, `mobile-test` branch):
**https://mobile-test.trading-agent-dashboard-v2.pages.dev** — public,
for manual mobile feedback only. This is a deployed *preview*, not a
production cutover; see the "V1 remains the active dashboard" note
above, which this does not change.

**Not auto-synced.** The Pages project has no GitHub Git integration
(direct-upload only, confirmed via `wrangler pages project list` —
"Git Provider: No") — pushing to `main` does **not** redeploy it. After
any `dashboard-v2/` commit intended for the preview, redeploy manually
from a clean checkout of that commit:

```
npx wrangler pages deploy dashboard-v2 --project-name trading-agent-dashboard-v2 --branch mobile-test
```

Requires a Cloudflare-authenticated `wrangler` (`npx wrangler whoami`
to check). Verified 2026-09-14: deployment history showed commits
`20c38fa`/`2becf9e` were missing from the live preview; redeployed and
reconfirmed live.

## Run it locally

```
python -m http.server 8843
```

Run from `dashboard-v2/`, then open `http://127.0.0.1:8843/`. Any static
file server works — there is nothing to build or install.

Fixture preview (dev-only, not linked from the shipped app — covers all
five pages, Roadmap included):
`http://127.0.0.1:8843/dev/fixtures-preview.html`

## Fixture-based checks

```
node test/calc.test.mjs
```

Plain Node `assert`, no test framework installed. Covers trade pairing,
realized P&L, win rate, max drawdown, total return, the risk-checker
rejection rule, the best-effort activity matcher, the open-position detail
lookup, run isolation, the newest-N decision summary, the Roadmap data
shape / public-safety guard, and null/empty inputs.

## Structure

| Path | Role |
|---|---|
| `js/calc.js` | Pure calculation + formatting functions. No fetch, no DOM. Shared by the app and the tests. |
| `js/data.js` | The **only** module that talks to Neon. Reuses V1's exact auth flow, query helpers, and endpoints. |
| `js/roadmap-data.js` | Static, public-safe roadmap content for the Roadmap page. No fetch, no DOM, no secrets/URLs/IDs. |
| `js/pages/*.js` | One render module per page — `render(container, data, ctx)`, pure DOM building. `pages/roadmap.js` ignores `data` and reads `roadmap-data.js` only. |
| `js/app.js` | Shell bootstrap: mode/run/page state, refresh loop, loading/error states. |
| `js/fixtures.js` | Representative local fixtures (dev preview only — never imported by `index.html`). |
| `dev/fixtures-preview.html` | Dev-only page for inspecting all four pages against fixtures. Not linked from `index.html`. |
| `test/calc.test.mjs` | Fixture-based checks for `calc.js`. |

## Data module boundary

`js/data.js` owns every Neon request. Page renderers never construct a
database request themselves — they receive plain objects from
`loadRunData()`. `backtest_cycle_evidence` is never queried (excluded from
the anonymous grant, and out of scope for this dashboard). Every request is
a `GET`; no write-capable request exists anywhere in this directory.

The **Decisions** page requests only the newest 10 decision rows for the
selected run (a bounded `limit=10` list query) plus its run-wide status
totals from `count`-only queries — it never loads the full decision list.
The **Roadmap** page makes no request at all.

## Metric parity with V1

Verified against the same real run (`e68525f9-ce89-4588-a57e-866f28cd99fd`)
at the same moment, side by side with `dashboard/index.html`:

| Metric | V1 | V2 |
|---|---|---|
| Equity | $103,696.08 | $103,696.08 |
| Cash | $83,111.53 | $83,111.53 |
| Total return | +3.70% | +3.70% |
| Realized P&L | $3,445.04 | $3,445.04 |
| Unrealized P&L | $252.35 | $252.35 |
| Exposure | 19.85% | 19.85% |
| Drawdown | -0.64% | -0.64% |
| Max drawdown | -2.46% | -2.46% |
| Open / closed | 1 / 16 | 1 / 16 |
| Win rate | 50% (n=16) | 50% (n=16) |
| Avg tokens / cycle | 41,608 | 41,608 |
| Retry count | 18 | 18 |
| Failed-cycle count | 1 | 1 |

No metric was redefined; every one above is computed by the same logic V1
uses (`js/calc.js` reproduces `pairTrades`, `isBlockedRow`, and the
drawdown/return formulas unchanged).

## Known limitations (stated, not hidden)

- **No pre-journal proposal funnel.** Decision-time rejections that never
  reach the `trades` table are absent from this anonymous dashboard's data
  entirely — there is no "evaluated → proposed → accepted" count to show.
  The Decisions page shows only exact `trades`-row statuses (total
  journaled / filled / rejected / pending) under "Journaled order
  outcomes," with a note that the full funnel needs cycle evidence this
  dashboard does not query.
- **Open-position current price and unrealized P&L come only from the
  latest scoped snapshot's `open_positions_detail`.** If a ticker has no
  matching detail row, the Overview and Trades pages show `—` — they never
  substitute the entry fill price and label it "Current." Simulated days
  held likewise comes only from a stored `days_held` value when present;
  in backtest mode, with no stored value, it shows `—` rather than being
  computed from the real current date (which is meaningless for a
  simulated trading day).
- **No live benchmark comparison.** SPY / watchlist / SMA(10) figures live
  only in `docs/evaluations/round2_benchmark_sensitivity.md` — HANDOFF.md
  §8 already documents that no benchmark series is wired into the
  dashboard. The Runs page says so explicitly rather than
  inventing a live-computed number.
- **No per-trade decision→activity foreign key.** The schema exposed to the
  anonymous role has no link from a `trades` row to the `agent_activity`
  rows that produced it. The Decisions page's evidence panel does a
  best-effort match (ticker + a time window) and says so; it is never
  presented as an exact join.
- **No per-trade price-path chart.** The repository does not yet store a
  reliable intraday price path per trade (HANDOFF.md §8) — the Trades page
  says so instead of fabricating one.
- **Equity volatility is a different figure from the offline report's.**
  The Runs page computes a real, stored-data-derived population stdev of
  period-over-period equity change — explicitly labeled as a different
  methodology from `round2_benchmark_sensitivity.md`'s annualized figure,
  never presented as the same number.
