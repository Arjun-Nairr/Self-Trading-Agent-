# Laya vs. seeded random, prompt schema 6: three-month plan (2026-11-10, AUTHORIZED)

**Status: authorized by the user on 2026-11-10.** Up to six historical simulation runs, one arm at a time:
July 2022, then September 2022, then October 2024. Each month runs Laya first and then seeded random, and both arms
are reconciled before the next month starts. Simulated execution only.

This is an **exploratory comparison of which names each selector hands Hermes** under the same shortlist cap. One
draw per arm per month on a pool selected in hindsight cannot establish selector value or a trading edge.

## Frozen for all six runs

- **Code:** one local commit holding this plan, the three configs and the runner's explicit coverage-exception
  record. Every arm's fingerprint must match within its pair.
- **Pool:** the frozen 48-name pool `watchlist/experimental_pools/candidates60-v1-pool-2022-07-01-aafefb19a6c5.json`
  (content `c56a4672…`, universe `1f3d88b6…`). Same 48 names in both arms and all months. Hindsight-selected.
- **Shared settings:** masking seed 42 (alias mapping `f903a697…` in every window), shortlist cap k = 10, $100,000,
  fundamentals off, prompt schema 6, the committed RULES/AGENTS/SOUL, risk checker and simulated execution.
- **Hermes:** `gpt-5.6-terra` / `openai-codex` / `medium`, tool-free disposable profile.
- **Laya:** worker `hermes-laya-worker:2` (`sha256:d423bbf7…`), checkpoint `55cf4c4e…`.
- **Random arm:** every session it draws up to 10 names uniformly from the same eligible pool as Laya (the 48 minus
  held or pending names, with usable cards). The draw is seeded by the config's frozen `random_seed` and the session
  date. It never uses a fixed list.
- **Tracing:** optional controller-side backtest tracing to Langfuse, export-only and fail-open.

| Month | Experiment ID | Window | Random seed |
|---|---|---|---|
| July 2022 | `laya-vs-random-schema6-2022-07` | 2022-07-01 → 07-31 | `laya-vs-random-schema6-2022-07` |
| Sept 2022 | `laya-vs-random-schema6-2022-09` | 2022-09-01 → 09-30 | `laya-vs-random-schema6-2022-09` |
| Oct 2024 | `laya-vs-random-schema6-2024-10` | 2024-10-01 → 10-31 | `laya-vs-random-schema6-2024-10` |

## Preflight (read-only, 2026-11-10)

- **Price coverage:** 48/48 in every window. That means 84 point-in-time warm-up bars, a daily bar on every expected
  session and no malformed bars.
- **Decision-time cutoffs:** checked on the first and last session of each window.
  - News: a complete fetch for 48/48, with 0 articles after the 16:00 ET cutoff.
  - Intraday: 09:30 and 09:45 bars for 48/48.
- **Pool news-coverage rule** `pre-start-3x14d-distinct-v2` (at least 6 distinct articles across three 14-day probes
  before day 1):

| Window | Result | Names below the rule (aliases, articles found) |
|---|---|---|
| July 2022 | 48/48 pass | — |
| Sept 2022 | 15 fail | BR44 5, CD91 5, CG64 2, CG96 2, DB37 2, HC52 2, JG75 5, MD38 2, MP36 5, MX45 5, NV28 5, SP56 1, SV40 5, VD62 2, WG86 2 |
| Oct 2024 | 7 fail | CG64 5, JR99 2, KR29 4, MD38 3, NT73 4, NV28 3, SR77 4 |

## Pool-level coverage exception (this experiment only)

By the user's decision on 2026-11-10, the September 2022 and October 2024 configs waive the pool news-coverage rule
for this experiment only, so both arms keep the same 48 names.

- **Recorded explicitly:** each config carries `pool_coverage_exception`, and the runner copies it into every arm's
  `ARM.json` and `result.json`. The runner refuses an exception that is not scoped to the experiment, names a different
  rule or window, or cites a private preflight report (git-ignored under `.laya_vs_random/<experiment>/`) whose hash
  differs.
- **What stays in force:** every daily news check.
  - A completed fetch with zero headlines clears the news gate under schema 6.
  - A failed or incomplete fetch blocks new BUYs.
  - Adverse headlines can still veto a BUY, at decision time or at the pre-fill gate.
- **Effect on results:** names below the rule will mostly appear as "checked, 0 headlines." Checked-empty rates are
  reported by arm.

## Order and stop conditions

1. July 2022: Laya → verify → random → reconcile the pair.
2. September 2022: same sequence.
3. October 2024: same sequence.

Each arm gets a fresh run ID and isolated state, and each process must exit and pass its checks before the next
command. The batch stops at the first halt, failed integrity check or config mismatch, and all completed results and
failure logs are kept.

## Reported per month (descriptive)

Both arms side by side:
- run IDs and sessions;
- proposals, fills, rejection reasons and closed trades;
- realized P&L, ending equity, return and drawdown;
- checked-empty news rates;
- model calls, tokens and wall time, with controller work and Hermes work separated;
- trace examples of the largest shortlist and decision differences;
- hosted and locally spooled trace records, reported separately.
