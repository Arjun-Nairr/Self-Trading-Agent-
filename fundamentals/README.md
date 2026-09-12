# SEC EDGAR Fundamentals V1

Data foundation only -- see `HANDOFF.md` for current status. Not yet
wired into the trading agent, dashboard, or any MCP tool.

## Design boundary (self-contained)

- One public operation: `get_fundamentals_snapshot(ticker, as_of_date)`
  in `edgar.py`, returning `available` / `unavailable` / `ambiguous`.
- Point-in-time only: a fact is eligible only if it comes from a
  10-Q/10-K (or amendment) filed on or before `as_of_date` -- never a
  later filing, amendment, or restatement, even if SEC's API shows one.
- Fixed US-GAAP tag lists per metric, documented inline in `edgar.py`
  (`REVENUE_TAGS`, `NET_INCOME_TAGS`, etc.) -- no custom/company-specific
  tag is ever substituted; a metric with no eligible tag is explicitly
  `unavailable`, never guessed.
- **USD-only**: every V1 metric is a monetary quantity. Only facts under
  a tag's `USD` unit key are ever eligible -- a non-USD unit (shares,
  per-share, a foreign currency reported under the same tag) is never
  selected merely because it is the latest-dated entry. `total_debt`
  additionally requires both its current and non-current legs to share
  the same USD unit and the same balance-sheet date, or it is
  `unavailable`.
- **Deterministic reporting-period selection**: SEC can report more than
  one valid fact for the same tag with the same period end and filing
  date (e.g. a 10-Q's quarter-only flow next to its year-to-date flow).
  Selection is ranked, in order: (1) latest eligible period end, (2)
  latest eligible filing date, (3) for a flow fact only, the longest
  reported duration (so a 10-Q prefers its year-to-date fact and a 10-K
  its full-year fact over a shorter one tied on end+filed), (4) the
  accession number as a final stable tiebreak. An instant/balance-sheet
  fact never has a duration invented for it. This ranking never depends
  on the order facts appear in SEC's own response.
- Derived values (`revenue_growth_yoy`, `net_margin`, `current_ratio`)
  are computed only when their source observations share a genuinely
  comparable period, and always retain references to those source
  observations.

## Setup

Requires one environment variable, following SEC's declared-User-Agent
requirement (no `.env.example` exists in this project yet -- every other
credential here is documented the same way, inline where it's read):

```
SEC_EDGAR_USER_AGENT=ProjectName contact@example.com
```

Set it in your shell or `.env` (already gitignored) before running
anything in this module. It is never hardcoded, never defaulted, and
the module fails immediately and clearly if it's missing -- see
`sec_client.required_user_agent()`.

## CLI (validation only)

```bash
python fundamentals/edgar.py --ticker WMG --as-of 2026-04-29
```

Prints a JSON `FundamentalsSnapshot` (availability, metrics, derived
values, source provenance) to stdout. Never prints credentials.

## Tests

```bash
cd fundamentals
python test_sec_client.py
python test_edgar.py
```

All fixture-based, mocked SEC responses -- no live network access.
