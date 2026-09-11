# Round 2 per-trade journey evaluation

Run: `e68525f9-ce89-4588-a57e-866f28cd99fd` · Generated: 2026-09-11T23:30:02 · Source: direct asyncpg (NEON_DATABASE_URL), read-only SELECT

Offline, read-only, human-evaluation-only report (same boundary as `geometry_scan.py`/`counterfactual.py`/`benchmark_sensitivity.py`). Never wired into `run_backtest.py`, never read by Hermes. Built entirely from `trades` and `portfolio_snapshots.open_positions_detail`, already written by the existing harness — no new capture, no new table, no new tool. Trade identities are masked labels; see `trade-journal-mcp/schema.sql` for the real (unmasked) stored rows.

## Summary

- Closed trades: 16
- Full daily-journey coverage: 14
- Zero-day (same-session exit, no intermediate journey by construction): 2
- Partial coverage (should not occur — flagged if it does): 0

**Exit provenance** (WHO/WHAT caused the exit — see the module docstring's reconciliation note for how historical rows are proven, not guessed):

- Deterministic harness exits: 8 — `harness_stop` 5, `harness_target` 0, `harness_time_stop` 3 (in-the-money at exit: 3, underwater: 0)
- Agent-requested exits: 8 (of which 7 carry the secondary `agent time-stop rationale` label — the agent's own stated reason, never counted as harness-mechanical)
- Legacy/unresolved (provenance not provable from available evidence): 0

## Trades

### TRADE-01

- Entry fill: $91.16 x 219 @ 2026-08-31 16:08:57.854245+00:00
- Exit fill: $82.89 @ 2026-08-31 16:16:15.467002+00:00
- Realized P&L: $-1,811.13 (-9.07%)
- Exit provenance: `harness_stop` — raw note: 'Mechanical exit: stop hit (opening gap)'
- Trading-day snapshots while open: 2/2 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 4 | $90.65 | $-111.69 (-0.56%) | $4.08 | $9.59 |
| Day 7 | $90.81 | $-76.65 (-0.38%) | $4.24 | $9.43 |

Max daily-close P&L: $-76.65 · Min daily-close P&L: $-111.69

### TRADE-02

- Entry fill: $29.23 x 671 @ 2026-08-31 16:12:21.474538+00:00
- Exit fill: $28.27 @ 2026-08-31 16:15:45.464761+00:00
- Realized P&L: $-644.16 (-3.28%)
- Exit provenance: `agent_requested_exit` — raw note: 'The stop is already broken: current price 28.13 is below the 28.23 stop, so the long should be closed before considering anything new.'
- Trading-day snapshots while open: 0/0 (zero_day)

_No intermediate journey — same-session exit, no end-of-day mark was ever recorded while open._

### TRADE-03

- Entry fill: $73.10 x 263 @ 2026-08-31 16:20:25.379693+00:00
- Exit fill: $78.13 @ 2026-08-31 16:39:35.386602+00:00
- Realized P&L: $1,322.89 (+6.88%)
- Exit provenance: `harness_time_stop` — raw note: 'Mechanical exit: time stop (5 trading days)'
- Trading-day snapshots while open: 5/5 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 9 | $73.71 | $160.43 (+0.83%) | $3.47 | $7.64 |
| Day 10 | $75.73 | $691.69 (+3.60%) | $5.49 | $5.62 |
| Day 11 | $73.87 | $202.51 (+1.05%) | $3.63 | $7.48 |
| Day 14 | $74.01 | $239.33 (+1.24%) | $3.77 | $7.34 |
| Day 15 | $77.99 | $1,286.07 (+6.69%) | $7.75 | $3.36 |

Max daily-close P&L: $1,286.07 · Min daily-close P&L: $160.43

### TRADE-04

- Entry fill: $23.52 x 826 @ 2026-08-31 16:23:21.293691+00:00
- Exit fill: $24.46 @ 2026-08-31 16:43:24.154950+00:00
- Realized P&L: $776.44 (+4.00%)
- Exit provenance: `harness_time_stop` — raw note: 'Mechanical exit: time stop (5 trading days)'
- Trading-day snapshots while open: 5/5 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 10 | $23.91 | $322.14 (+1.66%) | $1.44 | $2.11 |
| Day 11 | $25.37 | $1,528.10 (+7.87%) | $2.90 | $0.65 |
| Day 14 | $25.41 | $1,561.14 (+8.04%) | $2.94 | $0.61 |
| Day 15 | $24.36 | $689.71 (+3.55%) | $1.89 | $1.66 |
| Day 16 | $24.47 | $784.70 (+4.04%) | $2.00 | $1.55 |

Max daily-close P&L: $1,561.14 · Min daily-close P&L: $322.14

### TRADE-05

- Entry fill: $55.31 x 353 @ 2026-08-31 16:47:24.971118+00:00
- Exit fill: $53.60 @ 2026-08-31 16:50:28.544444+00:00
- Realized P&L: $-603.63 (-3.09%)
- Exit provenance: `harness_stop` — raw note: 'Mechanical exit: stop hit'
- Trading-day snapshots while open: 1/1 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 18 | $56.24 | $328.29 (+1.68%) | $2.64 | $5.83 |

Max daily-close P&L: $328.29 · Min daily-close P&L: $328.29

### TRADE-06

- Entry fill: $24.00 x 815 @ 2026-08-31 16:47:39.933636+00:00
- Exit fill: $23.22 @ 2026-08-31 16:47:48.023615+00:00
- Realized P&L: $-635.70 (-3.25%)
- Exit provenance: `harness_stop` — raw note: 'Mechanical exit: stop hit'
- Trading-day snapshots while open: 0/0 (zero_day)

_No intermediate journey — same-session exit, no end-of-day mark was ever recorded while open._

### TRADE-07

- Entry fill: $148.31 x 132 @ 2026-08-31 16:50:24.782303+00:00
- Exit fill: $142.59 @ 2026-08-31 17:01:12.189178+00:00
- Realized P&L: $-755.04 (-3.86%)
- Exit provenance: `harness_stop` — raw note: 'Mechanical exit: stop hit'
- Trading-day snapshots while open: 4/4 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 21 | $156.67 | $1,103.52 (+5.64%) | $14.08 | $8.43 |
| Day 22 | $159.12 | $1,426.92 (+7.29%) | $16.53 | $5.98 |
| Day 23 | $157.47 | $1,209.12 (+6.18%) | $14.88 | $7.63 |
| Day 24 | $146.17 | $-282.48 (-1.44%) | $3.58 | $18.93 |

Max daily-close P&L: $1,426.92 · Min daily-close P&L: $-282.48

### TRADE-08

- Entry fill: $59.44 x 324 @ 2026-08-31 17:16:56.503916+00:00
- Exit fill: $64.70 @ 2026-09-01 14:11:36.348700+00:00
- Realized P&L: $1,704.24 (+8.85%)
- Exit provenance: `harness_time_stop` — raw note: 'Mechanical exit: time stop (5 trading days)'
- Trading-day snapshots while open: 5/5 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 35 | $59.26 | $-58.32 (-0.30%) | $2.05 | $6.98 |
| Day 36 | $61.45 | $651.24 (+3.38%) | $4.24 | $4.79 |
| Day 37 | $60.91 | $476.28 (+2.47%) | $3.70 | $5.33 |
| Day 38 | $63.23 | $1,227.96 (+6.38%) | $6.02 | $3.01 |
| Day 39 | $63.74 | $1,393.20 (+7.23%) | $6.53 | $2.50 |

Max daily-close P&L: $1,393.20 · Min daily-close P&L: $-58.32

### TRADE-09

- Entry fill: $146.27 x 131 @ 2026-09-01 14:02:38.938296+00:00
- Exit fill: $141.55 @ 2026-09-01 14:07:52.005240+00:00
- Realized P&L: $-618.32 (-3.23%)
- Exit provenance: `harness_stop` — raw note: 'Mechanical exit: stop hit'
- Trading-day snapshots while open: 1/1 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 38 | $143.94 | $-305.23 (-1.59%) | $2.39 | $19.96 |

Max daily-close P&L: $-305.23 · Min daily-close P&L: $-305.23

### TRADE-10

- Entry fill: $148.57 x 130 @ 2026-09-01 14:26:51.848024+00:00
- Exit fill: $157.44 @ 2026-09-01 15:13:29.664820+00:00
- Realized P&L: $1,153.10 (+5.97%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: 'Closed because the time stop expired before the setup reached its target or stop; exits come before new entries.'
- Trading-day snapshots while open: 2/2 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 45 | $146.63 | $-252.20 (-1.31%) | $3.13 | $19.75 |
| Day 49 | $147.96 | $-79.30 (-0.41%) | $4.46 | $18.42 |

Max daily-close P&L: $-79.30 · Min daily-close P&L: $-252.20

### TRADE-11

- Entry fill: $28.44 x 684 @ 2026-09-01 14:27:06.454092+00:00
- Exit fill: $29.39 @ 2026-09-01 15:13:33.781676+00:00
- Realized P&L: $649.80 (+3.34%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: 'Closed because the time stop expired before the setup reached its target or stop; exits come before new entries.'
- Trading-day snapshots while open: 2/2 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 45 | $28.90 | $314.64 (+1.62%) | $1.60 | $2.85 |
| Day 49 | $28.89 | $307.80 (+1.58%) | $1.59 | $2.86 |

Max daily-close P&L: $314.64 · Min daily-close P&L: $307.80

### TRADE-12

- Entry fill: $32.83 x 597 @ 2026-09-01 14:27:13.085902+00:00
- Exit fill: $34.14 @ 2026-09-01 15:13:37.820489+00:00
- Realized P&L: $782.07 (+3.99%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: 'Closed because the time stop expired before the setup reached its target or stop; exits come before new entries.'
- Trading-day snapshots while open: 2/2 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 45 | $33.51 | $405.96 (+2.07%) | $2.26 | $2.82 |
| Day 49 | $34.15 | $788.04 (+4.02%) | $2.90 | $2.18 |

Max daily-close P&L: $788.04 · Min daily-close P&L: $405.96

### TRADE-13

- Entry fill: $28.40 x 699 @ 2026-09-01 15:19:22.380654+00:00
- Exit fill: $28.30 @ 2026-09-01 15:32:59.044049+00:00
- Realized P&L: $-69.90 (-0.35%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: '5-day time stop reached; exit is mechanical and does not require waiting for stop or target.'
- Trading-day snapshots while open: 3/3 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 52 | $28.41 | $6.99 (+0.04%) | $1.31 | $3.44 |
| Day 53 | $28.97 | $398.43 (+2.01%) | $1.87 | $2.88 |
| Day 56 | $29.45 | $733.95 (+3.70%) | $2.35 | $2.40 |

Max daily-close P&L: $733.95 · Min daily-close P&L: $6.99

### TRADE-14

- Entry fill: $37.43 x 529 @ 2026-09-01 15:19:28.838509+00:00
- Exit fill: $39.90 @ 2026-09-01 15:33:02.824492+00:00
- Realized P&L: $1,306.63 (+6.60%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: '5-day time stop reached; close the position before looking for new entries.'
- Trading-day snapshots while open: 3/3 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 52 | $38.00 | $301.53 (+1.52%) | $2.00 | $4.08 |
| Day 53 | $37.59 | $84.64 (+0.43%) | $1.59 | $4.49 |
| Day 56 | $39.04 | $851.69 (+4.30%) | $3.04 | $3.04 |

Max daily-close P&L: $851.69 · Min daily-close P&L: $84.64

### TRADE-15

- Entry fill: $27.45 x 736 @ 2026-09-01 15:19:35.230969+00:00
- Exit fill: $29.11 @ 2026-09-01 15:33:06.594498+00:00
- Realized P&L: $1,221.76 (+6.05%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: '5-day time stop reached; the position should be exited mechanically.'
- Trading-day snapshots while open: 3/3 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 52 | $28.13 | $500.48 (+2.48%) | $2.13 | $2.15 |
| Day 53 | $28.50 | $772.80 (+3.83%) | $2.50 | $1.78 |
| Day 56 | $28.67 | $897.92 (+4.44%) | $2.67 | $1.61 |

Max daily-close P&L: $897.92 · Min daily-close P&L: $500.48

### TRADE-16

- Entry fill: $159.36 x 127 @ 2026-09-01 15:22:56.067016+00:00
- Exit fill: $156.73 @ 2026-09-01 15:38:30.964637+00:00
- Realized P&L: $-334.01 (-1.65%)
- Exit provenance: `agent_requested_exit` · agent time-stop rationale — raw note: 'The 5-day time stop has been reached, so I am closing the position mechanically instead of stretching the hold.'
- Trading-day snapshots while open: 3/3 (full)

| Day | Price | Unrealized P&L | Dist to stop | Dist to target |
|---|---|---|---|---|
| Day 53 | $162.22 | $363.22 (+1.79%) | $10.97 | $13.37 |
| Day 56 | $163.78 | $560.70 (+2.77%) | $12.53 | $11.81 |
| Day 57 | $163.14 | $480.06 (+2.37%) | $11.89 | $12.45 |

Max daily-close P&L: $560.70 · Min daily-close P&L: $363.22

## Limitations

- This evaluates what happened under the actual five-day time stop as it was run — not what a different time-stop horizon would have produced.
- It does not prove what a longer hold would have done after the real exit: no price is stored (or fetched here) for any date after a trade's actual exit.
- It cannot evaluate alternative stop-distance bands from daily closes: a stop is evaluated intraday (15-minute bars) against bar low/high, and only the end-of-day close is stored in `portfolio_snapshots`.
- No counterfactual claim (a wider/narrower stop, a longer hold) is supported by this report without separately re-fetching existing historical intraday bars (`alpaca-mcp`'s `get_intraday_bars`, already used by `run_backtest.py`'s own `fetch_intraday_bars`) for the specific dates in question — this report does not do that.
