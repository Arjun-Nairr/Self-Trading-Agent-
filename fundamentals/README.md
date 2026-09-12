# SEC EDGAR Fundamentals V1

Data foundation only -- see `docs/research/sec_edgar_v1_data_design.md`
for the design and `HANDOFF.md` for current status. Not yet wired into
the trading agent, dashboard, or any MCP tool.

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
