# SEC EDGAR Fundamentals V1 — acceptance milestone

**Scope:** contained data-foundation validation only. This report does
**not** approve, propose, or perform trading-agent integration —
`fundamentals/` still has no path into `run_backtest.py`, `AGENTS.md`,
`RULES.md`, `SOUL.md`, the dashboard, any MCP tool, Docker, or Neon
schema. Round 2 stays frozen; no rules/model/run-state change was made.

## Goal

Determine whether `get_fundamentals_snapshot(ticker, as_of_date)` is
trustworthy enough to become a *future* input to the trading agent, by
running it live against real, structurally varied companies and
programmatically checking every output against the module's own
documented contract (point-in-time gate, USD-only, fixed tag lists,
provenance completeness, derived-metric period compatibility).

## Test matrix and why each case was selected

| Ticker | As-of | Why this case |
|---|---|---|
| **WMG** | 2026-04-29 | The known regression case from the two prior correctness fixes (stale 2020 revenue tag; null `entity_name`) — re-validated live to confirm both stay fixed. |
| **AAPL** | 2026-04-29 | Ordinary large domestic US issuer, clean US-GAAP reporting — the "everything should just work" baseline case. |
| **JPM** | 2026-04-29 | Financial-sector issuer. Banks report an unclassified balance sheet (no current/non-current split), so this tests whether V1's fixed, generic tag set fails **closed** on metrics that structurally don't apply, rather than guessing or mistagging. |
| **BABA** | 2026-04-29 | Foreign private issuer (Cayman Islands holding company, typically files Form 20-F, not 10-Q/10-K, and/or reports under non-matching tags). Tests the point-in-time form gate and the no-custom-tag rule under a company V1's US-GAAP taxonomy structurally does not fit. |

All four run at the same `as_of_date` for a direct side-by-side
comparison; each ticker's own most recent real eligible filing as of
that date is what was actually selected in every case (verified below,
not assumed).

## Available vs. unavailable metrics, and why

| Metric | WMG | AAPL | JPM | BABA |
|---|---|---|---|---|
| revenue | available | available | available | **unavailable** — none of `Revenues`/`RevenueFromContractWithCustomerExcludingAssessedTax`/`SalesRevenueNet` present |
| revenue_growth_yoy | available | available | available | unavailable (revenue unavailable) |
| net_income | available | available | available | **unavailable** — no `NetIncomeLoss`/`ProfitLoss` |
| net_margin | available | available | available | unavailable |
| operating_cash_flow | available | available | available | **unavailable** |
| cash_and_equivalents | available | available | available | **unavailable** |
| total_debt | **unavailable** | available | **unavailable** | unavailable |
| current_assets | available | available | **unavailable** — no `AssetsCurrent` | unavailable |
| current_liabilities | available | available | **unavailable** — no `LiabilitiesCurrent` | unavailable |
| current_ratio | available | available | unavailable (legs unavailable) | unavailable |

**Why, not just what:**
- **JPM's current_assets/current_liabilities/current_ratio are unavailable
  because a bank's balance sheet has no `AssetsCurrent`/`LiabilitiesCurrent`
  concept at all** — this is real, correct US-GAAP reporting practice for
  a financial institution, not a bug or a missing fallback. V1's fixed
  tag list does not attempt to force a bank's balance sheet into a
  generic corporate current/non-current split, exactly as required —
  it reports the metric explicitly unavailable rather than mistagging
  something else as "current assets."
- **WMG and JPM's total_debt is unavailable** because the fixed
  `(LongTermDebtNoncurrent, LongTermDebtCurrent)` composition
  `resolve_total_debt()` requires isn't fully present (for JPM,
  consistent with the same unclassified-balance-sheet reason above).
  Never treated as a partial sum or a zero.
- **BABA is unavailable across every metric** because none of the fixed
  US-GAAP tags this module reads are present for it under an eligible
  10-Q/10-K/amendment as of the test date — consistent with it being a
  foreign private issuer that reports on a different form/taxonomy.
  This is the intended fail-closed outcome: the snapshot's top-level
  `status` is still `"available"` (ticker→CIK resolution and the SEC
  fetch both succeeded structurally), but every individual metric is
  explicitly `unavailable` with a stated reason — nothing was invented.

`entity_name` was populated correctly in all four cases (`Warner Music
Group Corp.`, `Apple Inc.`, `JPMORGAN CHASE & CO`, `Alibaba Group
Holding Ltd`), confirming the `name`-field fix holds live.

## Provenance / as-of verification summary

For every `available` metric's observation (including every
`derived_from` source observation on a derived metric) across all four
cases, a script (not committed — throwaway, run this session) checked:

```
filed_at <= as_of_date
unit == "USD"
form in {10-Q, 10-K, 10-Q/A, 10-K/A}
source_url is present (non-empty)
```

**Result: 0 violations across all 4 tickers.**

Derived-metric period compatibility was also checked directly from the
output data (not just trusted from the code):
- `net_margin`'s two source observations share the identical
  `period_start`/`period_end` in every case it was available.
- `current_ratio`'s two source observations share the identical
  `period_end` (balance-sheet date) in every case it was available.
- `total_debt`'s two source observations (where available, AAPL only)
  share the identical `period_end` and the identical `unit`.
- `revenue_growth_yoy`'s two source observations are 330–400 days
  apart end-to-end in every case it was available, matching the
  documented YoY window — never a mismatched-duration or unrelated
  period compared.

Representative values, cited for the report record (not raw responses):

| Ticker | revenue | period | filed | form | accession |
|---|---|---|---|---|---|
| WMG | $1,840,000,000 | 2025-10-01 → 2025-12-31 | 2026-02-09 | 10-Q | 0001319161-26-000010 |
| AAPL | $143,756,000,000 | 2025-09-28 → 2025-12-27 | 2026-01-30 | 10-Q | 0000320193-26-000006 |
| JPM | $182,447,000,000 | 2025-01-01 → 2025-12-31 | 2026-02-13 | 10-K | 0001628280-26-008131 |
| BABA | unavailable | — | — | — | — |

WMG's revenue is now sourced from a real 2025 10-Q (not a 2020 fact) —
confirming the earlier stale-tag-selection fix holds under a fresh live
fetch, not just the fixture tests.

## Defects found during this milestone

**None.** This acceptance pass found zero new defects. (Two real
defects — stale revenue-tag selection, and a null `entity_name` from
reading the wrong SEC submissions field — were found and fixed in
prior sessions, each with its own fixture-based regression test; this
milestone's job was to confirm they hold under fresh live data across
a wider, structurally varied set of companies, which they do.)

No fixture test, code change, or metric/behavior change was made as
part of this milestone — it is a validation-only report.

## Tests run

```
python fundamentals\test_sec_client.py   -> 28/28 pass
python fundamentals\test_edgar.py        -> 82/82 pass
python -m compileall -q fundamentals     -> clean
```

## Live validation

Ran (network access available, `SEC_EDGAR_USER_AGENT` set in the local
environment for the duration of these calls only — never printed,
committed, or otherwise recorded in this report):

```
python fundamentals\edgar.py --ticker WMG  --as-of 2026-04-29   -> exit 0, status "available"
python fundamentals\edgar.py --ticker AAPL --as-of 2026-04-29   -> exit 0, status "available"
python fundamentals\edgar.py --ticker JPM  --as-of 2026-04-29   -> exit 0, status "available"
python fundamentals\edgar.py --ticker BABA --as-of 2026-04-29   -> exit 0, status "available" (every metric unavailable, as expected)
```

The existing SEC response cache under `fundamentals/.cache/` was **not
cleared** for this milestone; WMG's fetch reused it, and AAPL/JPM/BABA
were fetched live and cached for the first time (all within the
module's existing ≤5 req/s pacing and bounded-retry behavior — no
change to that logic was needed or made).

## Verdict

**Accepted for future integration design.**

The V1 foundation behaves exactly as documented under live, structurally
diverse real-world data: point-in-time filing gate holds, only USD
facts are ever selected, only the fixed standard tag lists are ever
read (confirmed by BABA's and JPM's clean fail-closed behavior — no
custom tag was ever substituted), every observation carries complete
provenance, and every derived value's source periods are verified
compatible.

**This verdict does not approve, authorize, or perform any trading-agent
integration.** It only establishes that the data foundation itself is
trustworthy enough to be the subject of a *future*, separately-approved
integration design (prompt shape, veto/strengthen role, staleness
policy for a live trading cycle) — per `docs/PROJECT_BACKLOG.md` §2's
own stated prerequisites, which still require the frozen baseline to
unfreeze and an explicit integration-design decision before any code
change wires this module into `run_backtest.py`, `AGENTS.md`, or any
live trading path.
