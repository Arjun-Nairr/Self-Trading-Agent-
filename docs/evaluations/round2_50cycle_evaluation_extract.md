# Round 2, Full 50-Cycle Run — Evaluation Extract

**Scope:** the complete, currently-active Round 2 run, `run_id
e68525f9-ce89-4588-a57e-866f28cd99fd`, cycles 1–50 (cursor now
`2026-04-29`). This is a **read-only evidence-extraction report** — it
gathers, reconciles, and structures evidence. It does not recommend rule,
prompt, or model changes. Judgment on what (if anything) to do next is a
separate step.

**Supersedes nothing; extends.** `docs/evaluations/round2_tranche1_evaluation_extract.md`
(cycles 1–25 only) is preserved unchanged and remains a valid, narrower
extract of the same run's first half. This report covers the same
tranche-1 ground plus the 25 additional cycles (26–50) and the deeper
per-cycle evidence that exists only for that second half.

**Masking discipline:** every ticker identity in this report (`DELTA`,
`MIKE`, `LIMA`, …) is Hermes' own masked alias for a real watchlist
ticker, reshuffled once per this run and constant across all 50 cycles.
**The real-ticker-to-alias mapping is never stated anywhere in this
report**, even though `trades.ticker` stores the real ticker in Neon by
design (see `record_decision`'s own docstring, "journal the REAL ticker,
never the alias") — every real-ticker value from that column was
substituted with its corresponding alias before inclusion below, so this
document is self-consistent using aliases only. No `backtest_cycle_
evidence` prompt text, input payload, or quoted reasoning excerpt below
was altered from its stored form beyond that one substitution, and every
one was independently re-scanned for a leaked real ticker/company name
before inclusion — see §1.

---

## 0. Run/state reconciliation

Verified directly against `backtest-harness/backtest_state.json` and
live, run-scoped Neon queries before any extraction began.

| Claim | Expected | Verified | Match |
|---|---|---|---|
| `run_id` | `e68525f9-ce89-4588-a57e-866f28cd99fd` | same | ✅ |
| Completed trading cycles | 50 | `trading_days_elapsed`=50, `portfolio_snapshots` count for this run=50 | ✅ |
| Cursor | `2026-04-29` | same | ✅ |
| Closed trades | 16 | `closed_pnl` length=16; independently re-derived 16 BUY→SELL pairs from `trades` (§3) | ✅ |
| Open position | DELTA (masked alias) | `holdings` contains exactly 1 entry, matching this alias | ✅ |
| Pending orders | 0 | `pending_orders`=[] | ✅ |
| Active checkpoint | none | `cycle_checkpoint.json` absent | ✅ |
| Cycles 1–25: normal trade/journal data, no per-cycle evidence | — | `trades`/`portfolio_snapshots`/`agent_activity` rows exist for cycles 1–25; `backtest_cycle_evidence` has 0 rows overlapping those cycle_ids | ✅ |
| Cycles 26–50: additionally have `backtest_cycle_evidence` | — | exactly 25 evidence rows, one per cycle_id in the last 25 snapshots, 0 overlap with the first 25 | ✅ |

**No material discrepancy found.** All eight claims verified exactly;
nothing here required stopping to document a mismatch.

---

## 1. Integrity and coverage

### 1.1 Local state vs. Neon reconciliation

`backtest_state.json`'s `cash` (\$83,111.53) and `equity_curve[-1]`
(\$103,696.08) match `portfolio_snapshots`' final row for this `run_id`
exactly. `holdings` (DELTA, 721 sh, entry \$28.20/stop \$27.00/target
\$31.54) matches the final snapshot's `open_positions_detail` exactly.
`profile_level` = `aggressive_v1_base` throughout — the run never
escalated (current block started trading-day 40 with 4 completions;
the 20-day boundary at day 60 has not been reached).

### 1.2 Row counts (this `run_id` only)

| Table | Count |
|---|---|
| `trades` | 64 |
| `portfolio_snapshots` | 50 |
| `agent_activity` | 325 |
| `insight_log` | 10 |
| `backtest_cycle_evidence` | 25 |

`trades` = 35 (cycles 1–25) + 29 (cycles 26–50) = 64 — reconciled by an
independent tranche assignment (§1.5).

### 1.3 Duplicate checks

`SELECT cycle_id, count(*) FROM portfolio_snapshots WHERE run_id=$1 GROUP BY cycle_id HAVING count(*)>1`
→ **0 rows.** Same query against `backtest_cycle_evidence` → **0 rows.**
`SELECT decision_id, count(*) FROM trades WHERE run_id=$1 GROUP BY decision_id HAVING count(*)>1`
→ **0 rows.** No duplicate `cycle_id` or `decision_id` anywhere in this
run.

### 1.4 Checkpoint status

`backtest-harness/cycle_checkpoint.json` does not exist on disk. Every
cycle through 50 committed successfully; nothing is mid-flight.

### 1.5 Run isolation — older runs not mixed in

`SELECT DISTINCT run_id FROM trades` returns five values: this run plus
four historical run_ids (`97d112e8-...`, `1bf9af3d-...`, `81db75e5-...`,
`26f1d371-...` — Round-1 and Round-2 smoke tests). Every query in this
report filters `WHERE run_id = 'e68525f9-...'` explicitly; none of the
other four contributed a single row to any figure below.

Every `trades` row was assigned to tranche 1 (cycles 1–25) or tranche 2
(cycles 26–50) via `agent_activity.decision_id → agent_activity.cycle_id
→ portfolio_snapshots.cycle_id`'s known tranche membership — **not** by
wall-clock timestamp (this run was executed across three separate
harness invocations spanning two real calendar days, so timestamp alone
would be unreliable). Result: 0 of 64 trades left unmapped; 35 in
tranche 1, 29 in tranche 2 — exactly matching the row-count reconciliation
above.

### 1.6 The preserved Day 50 failure

`agent_activity` contains exactly **one** `cycle_failed` row for this
run, `id 786`, `created_at 2026-09-01T14:42:25Z`:

> `Day 50: RuntimeError: trade-journal failed: ... ValueError:
> expected_reward_risk must be > 0, got 0.0`

This is intentionally preserved, not deleted, per this task's boundary
instruction. It documents a real, since-fixed contract mismatch (commit
`7c4ac06`): three legitimate discretionary time-stop SELL decisions with
`expected_reward_risk: 0.0` (the honest value for a closing decision)
were rejected by a validator that required `> 0` unconditionally. The
checkpoint for that cycle was replayed without a new Hermes call once
fixed — see §8.4.

### 1.7 Evidence-row coverage by tranche

| Range | `backtest_cycle_evidence` rows | Coverage |
|---|---|---|
| Cycles 1–25 | 0 | None — table didn't exist yet when these ran; never backfilled (out of scope) |
| Cycles 26–50 | 25 | 100% — every cycle has exactly one row |

### 1.8 Missing/null fields

Across all 25 evidence rows: `prompt_text`, `input_payload`,
`validated_response`, `candidate_status`, `attempt_count`,
`retry_diagnostics`, `prompt_schema_version`, and all three `*_md_sha256`
fields are **non-null on every row** (all eight are `NOT NULL` at the
schema level, so this is a constraint guarantee, independently confirmed
by absence of any null in the actual data). `model`/`provider` are
non-null on every row (`gpt-5.4-mini`/`openai-codex`); `reasoning_effort`
is **null on all 25 rows** — see §1.9.

Within `input_payload.candidates[].tech`: **0 of 219** candidate-technical
fetches across the 25 cycles returned an error or null (§7.1).
`data_timestamps` on `trades` (cycles 26–50, BUY decisions only): 29 of
29 filled/rejected BUY rows carry it; the 2 tranche-2 rows without it are
the two discretionary SELL decisions, which never carried this field to
begin with (it is populated only for entries — see
`record_queued_decision`'s own scope). `proposed_entry_price`: null on
all 8 filled tranche-1 BUYs (correctly — the column didn't exist when
they ran) and populated on all 9 filled tranche-2 BUYs plus the 1 open
DELTA position (10 of 10).

### 1.9 Model/provider/reasoning metadata coverage

`model`/`provider` = `gpt-5.4-mini`/`openai-codex` on **all 25** evidence
rows — a single, consistent value, matching the locked model decision.
`reasoning_effort` is **null on all 25 rows**: the real Hermes
`--usage-file` report has never been observed to include that key (a
pre-existing, documented gap — `run_backtest.py`'s own `ask_hermes`
comment states this explicitly; `NULL` here is the honest absence, not a
missing extraction).

### 1.10 Prompt-schema versions

`prompt_schema_version = 1` on all 25 rows — a single, unversioned prompt
format throughout cycles 26–50 (no format change occurred mid-tranche).

### 1.11 RULES/AGENTS/SOUL hash consistency

`rules_md_sha256`, `agents_md_sha256`, `soul_md_sha256` each take
**exactly one distinct value** across all 25 rows:

- RULES.md: `a28ad3bb44e9d14407c75d80449ca90f3a04e8b5976ad3ffd5d915e4254b45f9`
- AGENTS.md: `9e4dd36dda0802432496c8ec1b203ed16e8fefeee613575245bb9a28af7dd6ea`
- SOUL.md: `81e0c84ac504c61c3210409b222890534fb42d1380ae23cf2ffd5cdd3bdff695`

No rules/procedure/temperament change occurred at any point during
cycles 26–50 — confirmed from the evidence itself, not merely inferred
from git history.

### 1.12 Retry attempt distribution and diagnostic categories

`attempt_count = 1` on **all 25** evidence rows — every cycle in 26–50
succeeded on Hermes' first reply; the one-retry-then-fail-closed contract
(`ask_hermes`) never needed its retry branch this range.
`retry_diagnostics = []` on all 25 rows accordingly — zero diagnostic
entries of any category.

This is a fact about `backtest_cycle_evidence.attempt_count` (our own
outer JSON/semantic-validation retry layer) specifically — it is **not**
the same number as the Hermes usage report's own `api_calls` field, which
can be >1 for a cycle with `attempt_count=1` (see §8.2 for the exact
distinction and why it is not a hidden retry).

---

## 2. Account and portfolio performance

All figures below are independently cross-checked against
**two sources**: (a) `backtest_state.json`'s own `equity_curve`/
`closed_pnl` (the harness's authoritative running ledger) and (b) a
fresh re-derivation from `trades.fill_price × trades.fill_quantity`,
paired BUY→SELL per §3's pairing logic. Any gap between the two is
stated explicitly, not silently resolved.

### 2.1 Starting/ending equity, total return

- Starting equity: **\$100,000.00** (baseline, cycle 1).
- Ending equity (cycle 50, `Day 71` label): **\$103,696.08**.
- Total return: **+3.70%**.

### 2.2 Realized / unrealized P&L and reconciliation

- Realized P&L (harness-authoritative, `sum(closed_pnl)`, 16 trades):
  **+\$3,443.73** (exact: 3443.7249999999985).
- Realized P&L (independently re-derived from `trades.fill_price`, §3):
  **+\$3,445.04**.
- **Gap: \$1.31**, isolated to a single trade (JULIET, closed in tranche 1):
  the harness's authoritative per-trade P&L for that exit is \$1,321.58;
  recomputing from the stored `fill_price` (\$78.13, saved at the
  pre-precision-widening `NUMERIC(14,2)` resolution) gives \$1,322.89.
  **This is the exact same, already-documented discrepancy** identified
  in `round2_tranche1_evaluation_extract.md` §2 — traced there to
  `fill_price` rounding at 2-decimal storage before the `NUMERIC(18,6)`
  widening (commit `bfb16fe`). It predates that fix and is not
  reproducible going forward; not a new finding.
- Unrealized P&L (open DELTA, per the final snapshot's
  `open_positions_detail`): **+\$252.35** (entry \$28.20, mark \$28.55,
  721 sh).
- **Reconciliation, using the harness-authoritative figure:**
  \$100,000.00 + \$3,443.73 (realized) + \$252.35 (unrealized) =
  **\$103,696.08** — matches ending equity to the cent. Using the
  independently re-derived figure instead: \$100,000 + \$3,445.04 +
  \$252.35 = \$103,697.39, a \$1.31 gap explained entirely by the JULIET
  rounding artifact above.

### 2.3 Drawdown

| Range | Min equity | Max equity | Max drawdown from peak |
|---|---|---|---|
| Cycles 1–25 | \$97,544.71 (Day 8) | \$100,000.00 (Day 1) | **−2.46%** |
| Cycles 26–50 | \$98,124.63 (Day 37) | \$104,363.51 (Day 56) | **−1.88%** |
| Full run | \$97,544.71 (Day 8) | \$104,363.51 (Day 56) | **−2.46%** |

The run's real intraday peak (\$104,363.51, Day 56) is **higher** than
the final equity (\$103,696.08) — the run gave back some of its best
mark-to-market gain before ending, still finishing net positive.

### 2.4 Cash/exposure history (checkpoints, every 5th cycle)

| Cycle | Day label | Equity | Cash | Exposure % | Open positions |
|---|---|---|---|---|---|
| 1 | Day 1 | 100,000.00 | 100,000.00 | 0.0% | 0 |
| 6 | Day 8 | 97,544.71 | 97,544.71 | 0.0% | 0 |
| 11 | Day 15 | 99,520.49 | 58,891.89 | 40.8% | 2 |
| 16 | Day 22 | 99,830.31 | 78,826.47 | 21.0% | 1 |
| 21 | Day 29 | 97,648.35 | 97,648.35 | 0.0% | 0 |
| 26 | Day 36 | 98,299.60 | 78,389.79 | 20.3% | 1 |
| 31 | Day 43 | 98,734.28 | 98,734.28 | 0.0% | 0 |
| 36 | Day 51 | 101,319.25 | 101,319.25 | 0.0% | 0 |
| 41 | Day 58 | 103,451.35 | 83,539.02 | 19.3% | 1 |
| 46 | Day 65 | 103,443.73 | 103,443.73 | 0.0% | 0 |
| 50 | Day 71 | 103,696.08 | 83,111.53 | 19.4% | 1 |

Exposure oscillates between fully flat (0%, no open position) and
roughly one position's worth (~19–41%) — consistent with §3's "one lot
per ticker, closed positions before considering new ones" cycle
structure. No sustained multi-position stretch — the run is never
holding more than 2 positions at once (cycle 11 is the single exception,
briefly).

### 2.5 Open/closed position counts

- Closed (completed round-trips): **16**.
- Open: **1** (DELTA).
- Total filled BUY entries: **17** (16 closed + 1 open).
- Total filled SELL exits: **16**.

### 2.6 Win/loss/breakeven, win rate

| Scope | n | Wins | Losses | Breakeven | Win rate |
|---|---|---|---|---|---|
| Full run | 16 | 8 | 8 | 0 | 50.0% |
| Closed in cycles 1–25 | 7 | 2 | 5 | 0 | 28.6% |
| Closed in cycles 26–50 | 9 | 6 | 3 | 0 | 66.7% |

(One position — NOVEMBER — was **opened in tranche 1 (Day 35) and closed in
tranche 2 (Day 42)**; it is counted above under "closed in cycles 26–50"
since that is when its outcome was realized. This is the only
cross-tranche pair; flagged explicitly, not silently assigned.)

**Do not read the tranche split above as two independent samples of
equal quality** — 9 and 7 are both still small; see §9 and §10.

### 2.7 P&L statistics

| Scope | n | Total P&L | Avg | Median | Avg win | Avg loss | Largest win | Largest loss | Payoff ratio | Profit factor | Expectancy/trade |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Full run | 16 | +\$3,445.04* | +\$215.32 | +\$289.95 | +\$1,114.62 | −\$683.99 | +\$1,704.24 | −\$1,811.13 | 1.63 | 1.63 | +\$215.32 |
| Closed 1–25 | 7 | −\$2,350.33 | −\$335.76 | −\$635.70 | +\$1,049.66 | −\$889.93 | +\$1,322.89 | −\$1,811.13 | 1.18 | 0.47 | −\$335.76 |
| Closed 26–50 | 9 | +\$5,795.37 | +\$643.93 | +\$782.07 | +\$1,136.27 | −\$340.74 | +\$1,704.24 | −\$618.32 | 3.34 | 5.01 | +\$643.93 |

\* Full-run total uses the independently-re-derived figure (fill-price
based); the harness-authoritative total is \$3,443.73 (§2.2's \$1.31 gap
applies here identically, isolated to the same one JULIET trade, which
falls in the "closed 1–25" row — that row's own harness-authoritative
total is −\$2,351.65 vs. the −\$2,350.33 shown, the same known gap).

Payoff ratio = avg win ÷ |avg loss|. Profit factor = gross profit ÷ gross
loss. **A profit factor of 5.01 on 9 trades, or 0.47 on 7, is not a
reliable estimate of the strategy's true profit factor** — both are
extremely sensitive to one or two trades at this sample size (§2.9, §10).

### 2.8 Consecutive wins/losses (chronological by exit)

Full-run sequence, ordered by each trade's SELL fill timestamp:
`L L W W L L L L W W W W L W W L` — **max 4 consecutive wins, max 4
consecutive losses.** The losing streak (positions 5–8) falls entirely
within cycles 1–25; the winning streak (positions 9–12) spans the
tranche boundary (starts in 1–25, continues into 26–50).

### 2.9 Holding-period distribution

Measured as the difference in masked day-label index between entry and
exit cycle (a calendar day-index — includes weekends/holidays — **not**
the trading-day count §6c's 5-day time-stop rule actually enforces;
stated here only as a coarse observation of real elapsed time):

| Ticker | Entry day | Exit day | Δ (day-label) |
|---|---|---|---|
| FOXTROT | Day 4 | Day 8 | 4 |
| DELTA | Day 7 | Day 8 | 1 |
| JULIET | Day 9 | Day 16 | 7 |
| LIMA | Day 10 | Day 17 | 7 |
| LIMA | Day 18 | Day 18 | 0 |
| NOVEMBER | Day 18 | Day 21 | 3 |
| MIKE | Day 21 | Day 25 | 4 |
| NOVEMBER | Day 35 | Day 42 | 7 |
| MIKE | Day 38 | Day 39 | 1 |
| MIKE | Day 45 | Day 51 | 6 |
| GOLF | Day 45 | Day 51 | 6 |
| CHARLIE | Day 45 | Day 51 | 6 |
| LIMA | Day 52 | Day 58 | 6 |
| HOTEL | Day 52 | Day 58 | 6 |
| DELTA | Day 52 | Day 58 | 6 |
| MIKE | Day 53 | Day 59 | 6 |

Average Δ = 4.75, median = 6.0. The cluster at Δ=6 (6 of 16 trades) is
the time-stop-driven exits landing at 5 real trading days later, which
(after weekends) commonly lands 6–7 calendar-day-index numbers later.

### 2.10 Exit-reason distribution

| Exit trigger | Count | Realized P&L |
|---|---|---|
| Stop hit (mechanical, incl. one opening-gap stop) | 6 | −\$5,267.86 |
| Time stop, mechanical (`_execute_exit`, `confidence=None`) | 2 | +\$2,098.33 |
| Time stop, Hermes-initiated discretionary SELL (`confidence` set, `expected_reward_risk=0.0`, one legacy row at `2.0`) | 7 | +\$4,764.44 |
| Thesis invalidation ("stop already broken", discretionary) | 1 | −\$644.16 |

**A real, worth-noting pattern:** 8 of 16 exits were mechanical (the
harness's own stop/time-stop enforcement, `confidence=None`), and the
other 8 were Hermes-initiated discretionary SELLs — 7 of those 8 citing
the same RULES.md §6c 5-day time stop as their reason, one citing an
already-broken stop (§6d-style thesis invalidation, ahead of the
mechanical stop check). **AGENTS.md Step 3 explicitly asks Hermes to
check "has the time stop expired" as part of managing existing
holdings**, independently of the harness's own mechanical time-stop
enforcement in `simulate_trading_day` — so a Hermes-initiated time-stop
SELL is expected, dual-path behavior by design, not a contradiction
between the two mechanisms. This report does not attempt to determine
which of the two paths would have fired first in every case; that would
require tick-level reconstruction outside this report's scope (see §10).

### 2.11 Tranche 1 vs. tranche 2 comparison (performance only — full comparison in §9)

Tranche 2's realized P&L (+\$5,795.37 on 9 trades) is materially
different from tranche 1's (−\$2,350.33 on 7 trades). **This is an
observation, not a causal claim** — the underlying rules, prompt, model,
and watchlist did not change between the two ranges (only the evidence
instrumentation did, plus the one Day-50 validation-contract fix, which
affected zero trading decisions). At n=7 and n=9, a reversal of this
size is well within what ordinary trade-to-trade variance can produce;
see §9 and §10 for the full, non-performance comparison and the explicit
statistical-confidence verdict.

---

## 3. Complete trade lifecycle ledger

**Pairing method:** for each real ticker, all `status='filled'` `trades`
rows are sorted by `created_at`. RULES.md §9.1 (one open lot per ticker,
enforced structurally) guarantees strict BUY→SELL→BUY→SELL alternation
per ticker, so sequential pairing is exact, not approximate — the same
logic `run_backtest.py`'s own `fetch_recent_closed_trades` and the
dashboard's `pairTrades` already use. Verified: zero tickers violated
the alternation (no BUY immediately followed by another BUY, or SELL by
SELL, in any per-ticker sequence). A trailing unpaired BUY (DELTA) is the
open position, reported separately.

**`proposed_entry_price` availability:** populated only for BUY
decisions made after commit `bfb16fe` (2026-08-31) — i.e., **cycles
26–50 only**. Every tranche-1 row below shows `unavailable`, never a
fabricated or back-derived value.

**R-multiple**: `(exit_fill_price − entry_fill_price) / (entry_fill_price − stop_price)`,
using the **real stop price on file**, not a re-derived one. Shown for
every trade — the harness always records a stop at entry, so this is
always derivable without inventing an input.

### 3.1 Closed trades (16), chronological by entry

| # | Alias | Entry day → Exit day | Proposed entry | Fill | Exit fill | Qty | Stop | Target | Conf. | Basis | Expected R:R | Profile/min R:R | Invalidation condition (verbatim) | Exit reason (verbatim) | Invalidation tested? | Holding Δ | Realized P&L | Return % | R-mult. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | (FOXTROT) | Day 4 → Day 8 | unavailable | 91.16 | 82.89 | 219 | 86.57 | 100.24 | 3 | multiple | 2.00 | base/1.5 | "If [alias] closes below 86.57, the support that justified the entry has failed." | Mechanical exit: stop hit (opening gap) | **Yes — matched** (stop level = invalidation level) | 4 | −1,811.13 | −9.07% | −1.80 |
| 2 | (DELTA) | Day 7 → Day 8 | unavailable | 29.23 | 28.27 | 671 | 28.23 | 32.70 | 3 | multiple | 1.98 | base/1.5 | "If it closes back below 28.23, the support failed and the setup is invalid." | Discretionary (conf 5): "The stop is already broken: current price 28.13 is below the 28.23 stop..." | **Yes — matched**, caught early (before the mechanical gap-check would have) | 1 | −644.16 | −3.28% | −0.96 |
| 3 | (JULIET) | Day 9 → Day 16 | unavailable | 73.10 | 78.13 | 263 | 70.24 | 81.35 | 3 | multiple | 1.99 | base/1.5 | "If it closes back below 70.24 after entry, the support bounce failed." | Mechanical exit: time stop (5 trading days) | Not tested (time-stop fired first) | 7 | +1,322.89† | +6.88% | +1.76 |
| 4 | (LIMA) | Day 10 → Day 17 | unavailable | 23.52 | 24.46 | 826 | 22.47 | 26.02 | 3 | multiple | 2.01 | base/1.5 | "If it closes back below 22.47 support, the pullback-bounce thesis is wrong." | Mechanical exit: time stop (5 trading days) | Not tested | 7 | +776.44 | +4.00% | +0.90 |
| 5 | (LIMA) | Day 18 → Day 18 | unavailable | 24.00 | 23.22 | 815 | 23.22 | 26.89 | 3 | multiple | 1.98 | base/1.5 | "If it closes back below 23.22 support, the trade is wrong." | Mechanical exit: stop hit | **Yes — matched** | 0 | −635.70 | −3.25% | −1.00 |
| 6 | (NOVEMBER) | Day 18 → Day 21 | unavailable | 55.31 | 53.60 | 353 | 53.60 | 62.07 | 4 | multiple | 1.99 | base/1.5 | "If it closes back below 53.60 support, the bullish thesis is wrong." | Mechanical exit: stop hit | **Yes — matched** | 3 | −603.63 | −3.09% | −1.00 |
| 7 | (MIKE) | Day 21 → Day 25 | unavailable | 148.31 | 142.59 | 132 | 142.59 | 165.10 | 3 | multiple | 2.00 | base/1.5 | "If it closes back below 142.59, the bullish structure and support that justified the entry have failed." | Mechanical exit: stop hit | **Yes — matched** | 4 | −755.04 | −3.86% | −1.00 |
| 8 | (NOVEMBER) | Day 35 → Day 42 (**cross-tranche**) | unavailable | 59.44 | 64.70 | 324 | 57.21 | 66.24 | 4 | multiple | 2.00 | base/1.5 | "If it closes back below 57.21, the support level used for the stop." | Mechanical exit: time stop (5 trading days) | Not tested | 7 | +1,704.24 | +8.85% | +2.36 |
| 9 | (MIKE) | Day 38 → Day 39 | 149.00 | 146.27 | 141.55 | 131 | 141.55 | 163.90 | 3 | multiple | 2.00 | base/1.5 | "If it closes back below 141.55, the pullback support has failed and the setup is wrong." | Mechanical exit: stop hit | **Yes — matched** | 1 | −618.32 | −3.23% | −1.00 |
| 10 | (MIKE) | Day 45 → Day 51 | 151.25 | 148.57 | 157.44 | 130 | 143.50 | 166.38 | 3 | multiple | 1.95 | base/1.5 | "If it closes back below 143.50, the support break invalidates the long." | Discretionary (conf 3, R:R 0.0): "Closed because the time stop expired before the setup reached its target or stop..." | Not tested | 6 | +1,153.10 | +5.97% | +1.75 |
| 11 | (GOLF) | Day 45 → Day 51 | 28.86 | 28.44 | 29.39 | 684 | 27.30 | 31.75 | 3 | multiple | 1.85 | base/1.5 | "If it closes back below 27.30, the support break invalidates the long." | Discretionary (conf 3, R:R 0.0): time stop expired | Not tested | 6 | +649.80 | +3.34% | +0.83 |
| 12 | (CHARLIE) | Day 45 → Day 51 | 33.03 | 32.83 | 34.14 | 597 | 31.25 | 36.33 | 3 | multiple | 1.85 | base/1.5 | "If it closes back below 31.25, the support break invalidates the long." | Discretionary (conf 3, R:R 0.0): time stop expired | Not tested | 6 | +782.07 | +3.99% | +0.83 |
| 13 | (LIMA) | Day 52 → Day 58 | 28.95 | 28.40 | 28.30 | 699 | 27.10 | 31.85 | 4 | multiple | 1.57 | base/1.5 | "If it closes back below 27.50 support after entry, the bullish setup is invalidated." | Discretionary (conf 3, R:R 0.0): "5-day time stop reached; exit is mechanical..." | Not tested | 6 | −69.90 | −0.35% | −0.08 |
| 14 | (HOTEL) | Day 52 → Day 58 | 38.25 | 37.43 | 39.90 | 529 | 36.00 | 42.08 | 3 | multiple | 1.70 | base/1.5 | "If it closes back below 36.34 support after entry, the long thesis is broken." | Discretionary (conf 3, R:R 0.0): "5-day time stop reached; close the position..." | Not tested | 6 | +1,306.63 | +6.60% | +1.73 |
| 15 | (DELTA) | Day 52 → Day 58 | 27.53 | 27.45 | 29.11 | 736 | 26.00 | 30.28 | 3 | multiple | 1.80 | base/1.5 | "If it closes back below 26.15 support after entry, the setup fails." | Discretionary (conf 3, R:R 0.0): "5-day time stop reached; the position should be exited mechanically." | Not tested | 6 | +1,221.76 | +6.05% | +1.14 |
| 16 | (MIKE) | Day 53 → Day 59 | 159.63 | 159.36 | 156.73 | 127 | 151.25 | 175.59 | 3 | multiple | 1.90 | base/1.5 | "If it closes back below 151.25 and loses the 151.65 support area, the higher-low thesis is broken." | Discretionary (conf 3, R:R 2.0**): "The 5-day time stop has been reached, so I am closing the position mechanically instead of stretching the hold." | Not tested | 6 | −334.01 | −1.65% | −0.32 |

† JULIET's realized P&L uses the fill-price-recomputed figure; the
harness-authoritative figure for this one trade is +\$1,321.58 (the
\$1.31 gap, §2.2).
‡‡ Row 16's SELL reports `expected_reward_risk=2.0`, not `0.0` like most
of the other discretionary SELLs. This is not an error: the corrected
contract (§5.10) permits any finite value `>= 0` for a non-BUY action —
`0.0` is the honest value when there is genuinely no forward reward:risk
to report, but a SELL is free to report a real, non-zero figure when one
applies. Both are contract-compliant.

**Invalidation-condition audit, summarized:** of the 6 trades closed by
an actual **stop hit**, all 6 (100%) cite the exact stop price as their
own invalidation level, and the exit fired exactly there — a clean,
verified match every time it was actually tested. Of the remaining 10
(closed by time-stop or an early thesis-break), the invalidation
condition was **never tested** — price never reached the stated level
before the position closed on a different trigger. This is a structural
consequence of RULES.md §6c's short (5-trading-day) time stop, not a
sign the invalidation conditions were poorly chosen.

### 3.2 Open position (1)

| Alias | Entry day | Proposed entry | Fill | Qty | Stop | Target | Conf. | Basis | Expected R:R | Invalidation condition | Unrealized P&L (as of Day 71) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| (DELTA) | Day 71 | 28.67 | 28.20 | 721 | 27.00 | 31.54 | 3 | multiple | 1.72 | "If it closes back below 27.00, the long is wrong." | +\$252.35 (+1.24%) |

---

## 4. Proposal and rejection funnel

**Source and method:** `agent_activity.event_type` gives the funnel
structure directly — `order_queued` (decision-time **accepted**),
`risk_check` (decision-time **rejected** — never gets a `trades` row,
per `docs/PHASE1_AGGRESSIVE_DESIGN.md`'s own documented design),
`order_rejected` (execution-time rejected, **does** get a `trades` row,
`status='rejected'`), `order_placed`/`exit` (filled). Verified: every
`order_rejected`/`order_queued`/`order_placed`/`exit` row's `decision_id`
exists in `trades`; every `risk_check` row's `decision_id` (where
present) does **not** — confirming the documented "decision-time
rejections never journal" design holds in the real data, not just in
the docstring.

### 4.1 Full-run funnel

| Stage | Count | Note |
|---|---|---|
| Total decision-time-evaluated proposals | **101** | 93 BUY + 8 SELL |
| — accepted (queued) | 56 | 48 BUY + 8 SELL |
| — decision-time rejected | 45 | all BUY; 0 SELL rejections at decision time |
| Of the 56 accepted: filled | 25 | 17 BUY + 8 SELL |
| Of the 56 accepted: execution-time rejected | 31 | all BUY; 0 SELL rejections at execution time |
| Of the 56 accepted: expired/superseded | 0 | — |
| No-trade cycles (0 decisions returned) | 12 of 50 | 5 in 1–25 (proxy), 7 in 26–50 (exact) |

**Every rejection in this run, at either stage, was a BUY.** Zero
discretionary SELLs were ever rejected — consistent with exits being
exempt from most restrictions (§7 market-hours exemption; the one-lot
rule structurally can't block an exit).

- **Acceptance-to-fill conversion** (of the 48 accepted BUYs):
  17/48 = **35.4%**.
- **Proposal-to-completed-round-trip-trade conversion** (of all 93 BUY
  proposals): 16/93 = **17.2%** (16, not 17 — the 17th filled BUY, DELTA,
  is still open).
- **SELL proposal-to-fill conversion:** 8/8 = **100%**.

### 4.2 Split by tranche

| Stage | Cycles 1–25 | Cycles 26–50 |
|---|---|---|
| Decision-time accepted (BUY) | 28 | 20 |
| Decision-time rejected (BUY) | 20 | 25 |
| Execution-time rejected (BUY) | 20 | 11 |
| Filled BUY | 8 | 9 |
| Filled SELL (discretionary) | 0 | 8 |
| Filled SELL (mechanical) | 7 | 1 |

(Mechanical SELLs are harness-triggered, not Hermes proposals — shown
here for completeness, excluded from the "proposal" denominators above.)

### 4.3 Rejection counts by rule section — decisions vs. occurrences

A single rejected decision can cite more than one section (e.g. "§2b: …;
§6b: …" on one execution-time rejection, or "§2/§3: quantity X does not
equal…" on one decision-time rejection, which tags both §2 and §3). Both
counts are given to avoid double-counting decisions as if each tag were
an independent event.

**Decision-time (45 rejected decisions, all BUY, all before any `trades`
row exists):**

| Section | Decisions citing it | Occurrences |
|---|---|---|
| §2b (concentration cap) | 37 | 37 |
| §2 (general sizing mismatch) | 8 | 8 |
| §3 (cash) | 8 | 8 |

(The 8 "§2/§3" rows are the same 8 decisions each time — a quantity that
doesn't equal the required whole-share size, tagged with both sections
in one message. 37 + 8 = 45 decisions total, matching the row count
exactly — no double count.)

**Execution-time (31 rejected decisions, all BUY, each has its own
`trades` row):**

| Section | Decisions citing it | Occurrences |
|---|---|---|
| §2b (concentration cap) | 24 | 24 |
| §6b (reward:risk floor) | 16 | 16 |
| §6a (stop-distance/gap) | 7 | 7 |

(16 of the 31 decisions cite both §2b and §6b together — a position that
gapped both wider in concentration % and shallower in realized R:R
between decision and execution. 15 + 16 = 31 decisions total, matching
exactly.)

**§6a/§6b never appear at decision time, only at execution time** — the
same pattern independently confirmed in the tranche-1 extract, now shown
to hold across the full run: both are inherently about the *next day's
real price*, which decision-time evaluation (using today's known state
as a proxy) cannot see.

### 4.4 One-share/quantity-mismatch frequency

8 of 45 decision-time rejections (17.8%) were a pure quantity mismatch
(proposed quantity ≠ the exact required whole-share size from §2a/§2b/§3
— e.g. "quantity 20 does not equal required whole-share size 537"). This
is the "one-share placeholder" defect class from earlier in the project's
history — its presence here (still occurring, just correctly caught and
rejected every time) confirms the decision-time sizing gate is doing its
job, not that the underlying miscalculation pattern has vanished from
Hermes' own draft proposals.

### 4.5 Concentration-cap near misses

All 37 §2b decision-time rejections cite a position percentage in a
**narrow band, 20.02%–20.11% of equity** (cap is 20.00%) — every single
one is a near-miss by construction (the position was sized correctly
against yesterday's equity/entry estimate, then breached the cap by a
fraction of a percent once account state or entry price moved slightly
by decision time). None of the 37 exceed 20.11% — there is no case of a
wildly oversized proposal being caught by this rule; it is consistently
catching small boundary misses.

### 4.6 Reward and stop-distance/gap failures

Reward (§6b) failures: 16, all execution-time (§4.3). Stop-distance/gap
(§6a) failures: 7, all execution-time — the real next-day price moved
the entry-to-stop relationship outside the 2.90%–12.10% tolerance band
between decision and fill. Cash (§3) failures: 8 decision-time (bundled
with the §2 quantity-mismatch tag, §4.3) — 0 pure cash-only rejections
observed (every cash failure in this run co-occurred with a quantity
mismatch, not a standalone "correct size, insufficient cash" case).

### 4.7 News-vetoed entries

No structural "veto" tag exists in the schema (§5a is enforced entirely
inside Hermes' own `no_action_reason`/reasoning text, not a separate
rejection code) — see §5.9 for the qualitative, evidence-quoted audit of
every news-veto claim found in cycles 26–50's `no_action_reason` text (10
of 25 cycles' `no_action_reason` mentions news/headline/sentiment
explicitly; every one checked against the real supplied news array
matched).

### 4.8 Missing-data / provider failures

Zero. §7.1 confirms 0 of 219 candidate-technical fetches in cycles 26–50
returned an error or null; the run's only cycle-level failure (§1.6) was
a journal-write contract mismatch, not a data-provider gap.

---

## 5. Hermes reasoning audit for cycles 26–50

**Method:** every one of the 25 `backtest_cycle_evidence` rows was
processed programmatically — not sampled — cross-referencing each
decision's structured fields and free-text reasoning against the exact
`input_payload` Hermes was actually shown that cycle. Numeric/structural
checks (5.1–5.6, 5.10) are exhaustive over all 54 decisions (47 BUY, 7
SELL). Qualitative checks (5.7–5.9, 5.11–5.13) were read in full across
all 54 decisions plus all 25 `no_action_reason` texts; only short,
necessary excerpts are quoted below.

### 5.1 Candidate coverage — every eligible candidate marked

`set(candidate_status[].alias) == set(input_payload.candidates[].alias)`
checked on all 25 rows: **0 mismatches.** Every eligible candidate
received exactly one `proposed`/`no_proposal` tag, every cycle.

### 5.2 Proposed aliases exist in the supplied inputs

Every `proposed` alias in `candidate_status` matches a BUY decision's
`alias` in that same cycle's `validated_response.decisions`, and
vice-versa: **0 mismatches** across 25 cycles. Every BUY decision's
alias is a real member of that cycle's `candidates` list; every SELL
decision's alias is a real member of that cycle's `positions` list
(held tickers, which are deliberately **not** part of `candidate_status`
— a SELL targets something already held, not a "candidate," and is
correctly absent from that structure by design, not a coverage gap).
**0 of 54 decisions** referenced an alias that didn't structurally exist
in what Hermes was shown that cycle.

### 5.3 Claimed technical values vs. supplied values

For every BUY decision (47), `entry_price` was checked against the
supplied `candidates[].tech.price` for that alias: **0 mismatches**
(tolerance: greater of \$0.05 or 0.5%). Every proposed stop was checked
against the nearest supplied `support` level: **0 cases** landed further
than the greater of \$0.10 or 2% from a real supplied support value —
every stop is genuinely anchored to a real, supplied technical level
placed with margin below it (consistent with `AGENTS.md`'s stop-margin
guidance), never invented. Every target was checked against the nearest
supplied `resistance` level: **0 mismatches** beyond the greater of
\$0.50 or 3%. Of the 8 decisions that state a specific numeric RSI value
in their reasoning text (most describe RSI qualitatively — "bullish
RSI" — without a number), all 8 matched the supplied `tech.rsi` within
0.5 points; **0 mismatches**.

### 5.4 Sizing arithmetic vs. supplied account state and active limits

For every **accepted** BUY decision (the ones the harness actually
journaled — 20 of the 47 total BUY proposals across cycles 26–50; the
other 27 were decision-time rejected and never queued), the required
quantity was independently recomputed from the cycle's real supplied
`account.equity`, `account.cash`, and `active_limits.risk_budget_pct`/
`max_position_pct`, tracking a running cash reservation across
sequential accepted decisions within the same cycle (mirroring
`AGENTS.md`'s own "size against cash remaining after earlier BUYs in
this same list" instruction):

```
req_2a = floor(equity * risk_budget_pct/100 / (entry - stop))
req_2b = floor(equity * max_position_pct/100 / entry)
req_cash = floor(running_cash / entry)      # running_cash only decremented
                                             # by decisions actually accepted
                                             # at decision time, in order
required = min(req_2a, req_2b, req_cash)
```

**Result: 0 of 20 accepted BUY decisions' proposed quantity differed
from the independently recomputed `required` value.** Every accepted
BUY across cycles 26–50 sized itself exactly correctly against the real
2% risk budget and 20% concentration cap it was actually shown — the
same class of defect that produced the stale-1%-budget and one-share-
placeholder incidents earlier in this project's history did not recur
in this range.

### 5.5 Stop and target geometry

For all 47 BUY decisions: `stop < entry < target` held in every case
(0 violations); stop distance as a % of entry fell in
2.80%–12.20% (a slightly generous band around RULES.md §6a's
2.90%–12.10% tolerance) in every case (0 violations).

### 5.6 Reported reward vs. actual calculation

`(target - entry) / (entry - stop)` recomputed for all 47 BUY decisions
and compared to the self-reported `expected_reward_risk`, using the same
tolerance `run_backtest.py`'s own validator applies (`max(0.25, actual ×
0.15)`): **0 mismatches.** Every self-reported ratio matched its own
underlying entry/stop/target math.

### 5.7 Stops based on supplied levels, not invented

Covered by §5.3 — every one of 47 stops matched a real supplied support
level within a small margin. No case of a stop placed at a round number
or an unexplained level absent from the supplied technicals.

### 5.8 Rejection/no-action reasoning used concrete, measurable values

Read in full across all 25 `no_action_reason` texts and every
`order_rejected`/`risk_check` narration this range. Representative,
verbatim examples (not cherry-picked for favorability — these are drawn
from the same pool audited for §5.9's news claims):

> Day 65: *"LIMA: entry 26.90 to first resistance 28.25 gives 1.35 of
> upside, and any stop under 25.55 makes reward:risk about 1.0:1, below
> the [1.5:1 floor]."*

> Day 66: *"DELTA at 28.99 with stop below 27.54 and target 31.89 is
> about 1.00:1; GOLF at 30.56 with stop belo[w]…"*

Every `no_action_reason` sampled in cycles 26–50 names a specific price
level and a specific computed ratio or percentage — consistent with
`AGENTS.md`'s "name the specific rule and the specific number" Step-5
instruction, not vague prose ("looked extended," "didn't feel right").

### 5.9 Adverse news veto correctness (no invented classifier)

This report does not build a sentiment classifier — RULES.md §5a and
`AGENTS.md` leave that judgment entirely to Hermes, and this audit
respects that by checking only whether Hermes' *own claimed* veto
reasoning is traceable to something genuinely present in the supplied
news, never by independently scoring the news itself. Three
representative cases, each fully spot-checked against the real supplied
`news` array for that exact cycle and alias:

| Day | Alias | Hermes' claim | Supplied headline actually present |
|---|---|---|---|
| 44 | LIMA | "vetoed by the \$26 analyst target below spot" | *"Mizuho Maintains Neutral on LIMA, Raises Price Target to \$26"* |
| 58 | CHARLIE | "recent analyst headlines were negative" | Four analyst headlines, three explicitly *"Lowers Price Target to \$42/\$43"* |
| 71 | LIMA | "vetoed by adverse strike news" | *"IAM Local 778 Members Reject Management Offer, Continue Strike at [LIMA's real company]"* |

All three checked claims are accurate, specific, and grounded in real
supplied content — **no case found in this range of a fabricated or
overstated adverse-news claim.** No case was found of positive news
being used as a reason to *buy* (`primary_signal='sentiment'` on a BUY:
0 occurrences, structurally impossible per §5a's own enforcement and
independently confirmed absent in the data — §5.11).

### 5.10 SELL reasoning and the `expected_reward_risk: 0.0` contract

All 7 SELL decisions in cycles 26–50 report `expected_reward_risk >= 0`
(range: 0.0–2.0; 6 of 7 report exactly 0.0). `0.0` is the intended,
honest value under the corrected contract (`7c4ac06`) for a closing
decision with no forward reward:risk to report — none of the 7 fabricate
a positive placeholder to pass a stale validator. All 7 carry a real
`confidence` (3 or 5), a real `primary_signal`/`technical_basis`, and a
substantive `invalidation_condition`/reasoning explaining the exit —
none degrade to a bare "N/A" once freed from the old `>0` requirement.

### 5.11 Contradictions between structured fields and free text

`primary_signal='sentiment'` never appears on a BUY (0/47) —
structurally enforced and confirmed. No case was found where a
decision's stated `technical_basis` contradicted the reasoning text's
own description of what drove the trade (e.g. `technical_basis:
support_resistance` paired with reasoning that only discusses RSI). 46
of 47 BUY decisions use `technical_basis: multiple`; 1 uses
`support_resistance` — both consistent with their own reasoning text on
inspection.

### 5.12 Repeated/copy-pattern reasoning

The dual-sizing arithmetic disclosure (`AGENTS.md`'s required "state
both §2a and §2b's numbers") produces genuinely similar-*looking*
reasoning across decisions by design — this is compliance with a
required template, not a red flag. Checked specifically for **literal,
byte-identical** reasoning text reused across two different decisions:
**0 found.** Every one of the 47 BUY and 7 SELL reasoning strings is
textually distinct, even where the underlying arithmetic pattern
repeats.

### 5.13 Confidence calibration

| Confidence | BUY decisions (cycles 26–50) |
|---|---|
| 3 (ordinary) | 42 |
| 4 (strong) | 5 |
| 1, 2, 5 | 0 |

No confidence-5 BUY occurred in this range (SOUL.md: "genuinely rare…if
you are using this often, you are not using it correctly" — consistent).
Cross-referencing the 9 trades that both **originated** (entry) and
**closed** within cycles 26–50 against their entry confidence: of the 8
confidence-3 originating trades, 5 won and 3 lost; the 1
confidence-4 originating trade (LIMA, Day 52) lost slightly (−\$69.90,
−0.35%). **This sample (1 confidence-4 trade) is far too small to say
anything about whether confidence-4 outperforms confidence-3** — stated
explicitly, not implied by omission.

---

## 6. Reflection audit (all 10 reflections, full 50-cycle run)

`insight_log` holds exactly 10 `weekly_reflection` rows for this
`run_id` (5 in cycles 1–25, matching the tranche-1 extract's own count
exactly; 5 more in cycles 26–50). Every one read in full.

**A structural fact worth stating precisely first:** `sample_size` is
**not** the cumulative closed-trade count. `fetch_recent_closed_trades`
(`run_backtest.py`) passes `limit=8` — the reflection material shown to
Hermes is always the **8 most recently closed trades**, never more, and
`sample_size` records the size of *that* rolling window, not a running
total. In tranche 1, `sample_size` happened to equal the true cumulative
total in every row purely because tranche 1 never closed more than 8
trades — that was a coincidence of scale, not evidence `sample_size`
means "total closed to date." This distinction only becomes visible
once cumulative closed trades exceeds 8, which happens partway through
cycles 26–50 (§6.1).

### 6.1 Every reflection, with true state at that moment

| # | Day | Stored `sample_size` | Actual cumulative closed trades at that moment | Acknowledges sample size honestly? | Draws an unsupported strategy conclusion? | Repeats an earlier belief w/o new evidence? | Contradiction? |
|---|---|---|---|---|---|---|---|
| 1 | Day 7 | 0 | 0 | Yes — "No closed trades exist yet" | No | — (first reflection) | None |
| 2 | Day 14 | 2 | 2 | **No** — states "With zero closed trades..." while 2 trades had actually closed | No | Restates reflection 1's framing verbatim in spirit | **Yes — pre-existing, documented defect** (flagged in `round2_tranche1_evaluation_extract.md`, unresolved by this task's scope; reflection text itself was never modified) |
| 3 | Day 21 | 6 | 6 | Yes — "no closed-trade sample large enough to infer outcome patterns" | No | Consistent process-only framing | None |
| 4 | Day 28 | 7 | 7 | Yes — "recent sample is still too small" | No | Same process framing | None |
| 5 | Day 35 | 7 | 7 | Yes — "I do not have enough outcome data yet to claim more than that" | No | Adds a specific process observation (stop level held) tied to real named losses | None |
| 6 | Day 42 | 8 | 9 | Yes (implicitly — "not enough outcome data yet") | No | Same process-only framing | None (sample_size=8 is the rolling-window size, not a false "9th trade doesn't exist" claim — no such claim is made) |
| 7 | Day 50 | 8 | 9 | Yes — describes the 3 mechanical time-stop exits factually | No | New, specific observation (this cycle's own exits) | None |
| 8 | Day 57 | 8 | 12 | Yes (implicit) | No | Restates "5-day exits behaved as intended" — a genuine process pattern, not a P&L claim | None |
| 9 | Day 64 | 8 | 16 | Yes — explicit: *"The process held up better than the outcomes would suggest"* | **Borderline** — this is the closest any reflection comes to an outcome-based claim; framed as a process/outcome *divergence*, not a strategy conclusion | New angle (distance-to-resistance measurement) | None |
| 10 | Day 71 | 8 | 16 | Yes (implicit, no absolute-count claim) | No | Consistent structure-based filter framing | None |

**Summary: 1 genuine contradiction across all 10 reflections (reflection
#2, already documented, unresolved, and outside this task's scope to
fix)** — a real defect, not repeated in any of the other 9. **Zero
reflections in cycles 26–50 draw an unsupported strategy conclusion**
("this pattern works," "location matters more than momentum" stated as
fact) — every one stays at the process-observation level RULES.md and
`AGENTS.md` require for a sample this size, including reflection #9's
outcome-adjacent framing, which explicitly hedges ("held up **better
than the outcomes would suggest**" — a statement about the gap between
process and outcome, not a claim the outcomes validate the process).

### 6.2 Whether later decisions visibly applied a stated lesson

Reflection #5 (Day 35) explicitly ties a lesson to named support-level
discipline; reflections #6–10 (cycles 26–50) consistently restate
"stop under measurable support with enough cushion" as the active
filter — and §5.3/§5.7 independently confirm every one of the 47
subsequent BUY stops in this range was in fact anchored to a real
supplied support level. This is consistent with the stated lesson being
applied, though this report cannot establish causation (the same
discipline is also directly required by `AGENTS.md`'s own procedure,
independent of any one reflection restating it).

---

## 7. Evidence-input quality (cycles 26–50)

This section evaluates **data adequacy**, not trading performance.

### 7.1 Technical payload completeness

219 total candidate-technical fetches across 25 cycles (an average of
8.8 candidates/cycle — fewer than 10 whenever a position is held, since
held tickers move to `positions`, not `candidates`). **0 of 219**
returned an error or null field. Every fetch carried all 9 fields:
`ticker, price, rsi, rsi_signal, macd_signal, trend, outlook, support,
resistance`.

### 7.2 News availability per candidate/position

| News items that cycle | Candidate-cycles |
|---|---|
| 0 | 106 (48.4%) |
| 1 | 64 |
| 2 | 28 |
| 3 | 11 |
| 4 | 10 |

**Nearly half of all candidate evaluations (106/219) had zero news items
that day.** This is structurally indistinguishable, from the stored
payload alone, between "genuinely no headlines existed for that name
that day" and "the fetch silently returned nothing for another reason"
— both collapse to the same empty list in `masking.mask_news(news.get(
ticker, []), day)`. This is a real, worth-flagging limitation on
interpreting §5a's veto check for these candidate-cycles: an empty news
array is currently indistinguishable from a genuine "nothing published,"
so no claim can be made about whether the veto had real material to
evaluate in those 106 cases versus simply nothing to find.

### 7.3 Headline age distribution

Across all 141 real headlines with 1+ item (219 − 106 zero-news
cycles worth of items = 141 total news items), `age_days` ranges 1–7
(the lookback window), roughly evenly distributed (38/25/19/20/22/32/37
for days 1 through 7 respectively) — no systematic staleness or
clustering.

### 7.4 Headline field preservation

Every news item carries exactly 4 fields: `headline, summary, symbols,
age_days`. `headline` and `summary` text is preserved verbatim
(confirmed via §5.9's direct quotes matching stored text exactly).
`symbols` (masked aliases the headline concerns) is preserved as a list.

### 7.5 Fields absent from the news payload

**No exact retrieval timestamp, no source/outlet field, no article
URL/link.** Only the relative `age_days` (integer, day-granularity) is
given — there is no way, from stored evidence alone, to know the exact
hour a headline was retrieved, which outlet published it, or to follow a
link back to the original article. This is a genuine, structural gap in
what's reproducible from this data alone (distinct from `trades.
data_timestamps`, which records the technical/news **cutoff** date, not
a per-article retrieval time).

### 7.6 Held-position evidence completeness

31 position-cycle entries across the 25 cycles (a cycle can hold 0–2
positions). **0 of 31** carry a null `tech` field — held-position
technicals are exactly as complete as candidate technicals (§7.1).

### 7.7 Recent-insight coverage

`recent_insights` carries exactly **2** entries on **all 25** cycles —
full, unbroken coverage; every cycle in this range received the
feed-back half of reflection regardless of whether that cycle itself
triggered a new reflection.

### 7.8 Reflection-trade coverage

`reflection_trades` is non-null on exactly the 5 reflection cycles
(Day 42, 50, 57, 64, 71) and null on the other 20 — exactly matching
the `is_reflection_day` gate (`cycles_since_reflection >= 4`), with 0
cycles showing an unexpected presence or absence.

### 7.9 Active-limit consistency with the rendered prompt

`active_limits.risk_budget_pct`/`max_position_pct` values match text
appearing verbatim in that same cycle's `prompt_text` on **all 25**
cycles — 0 mismatches. The real, active sizing numbers reach the
rendered prompt every cycle, not just the structured payload.

### 7.10 Masking/leak checks

Full-text scan (`prompt_text` + serialized `input_payload` +
`validated_response`) for every real watchlist ticker symbol
(word-boundary matched) and every real company name/short-form across
all 25 rows: **0 leaks.** See report preamble.

### 7.11 Evidence fields stored but never shown to Hermes

`backtest_cycle_evidence` itself (`rules_md_sha256`,
`prompt_schema_version`, `attempt_count`, `retry_diagnostics`, `model`/
`provider`/`reasoning_effort`, `cycle_id`, `created_at`) is metadata
*about* the cycle, correctly never rendered into the prompt Hermes
itself sees — this is by design (an audit trail, not part of the
conversation), not a gap.

### 7.12 Prompt-visible values not represented structurally

None found. Every numeric value independently verified as appearing in
`prompt_text` (§7.9's active limits, plus spot-checks of account
equity/cash/exposure/drawdown figures) is also present in the
structured `input_payload.account` object — the prompt is a rendering
of the structured payload, not a source of additional unstructured
information.

---

## 8. Usage and reliability

### 8.1 Token/API-call totals

| Scope | Total tokens | Avg/cycle | Input | Output | Reasoning | Cache-read | API calls | Avg calls/cycle |
|---|---|---|---|---|---|---|---|---|
| Full run (50 cycles) | 2,080,404 | 41,608 | 469,942 | 470,750 | 439,315 | 1,139,712 | 68 | 1.36 |
| Cycles 1–25 | 1,038,519 | 41,541 | 209,344 | 200,439 | 185,180 | 628,736 | 35 | 1.40 |
| Cycles 26–50 | 1,041,885 | 41,675 | 260,598 | 270,311 | 254,135 | 510,976 | 33 | 1.32 |

(Tranche-1 total independently cross-checked against the harness's own
live console summary at the time — identical, 1,038,519.)

### 8.2 Cycles requiring more than one API call, and why

| Day | api_calls | `attempt_count` (evidence, if in 26–50) |
|---|---|---|
| Day 4 | 5 | — (tranche 1, no evidence) |
| Day 11 | 5 | — (tranche 1) |
| Day 17 | 3 | — (tranche 1) |
| Day 49 | 6 | **1** |
| Day 56 | 3 | **1** |
| Day 60 | 2 | **1** |

**Explanation of the apparent discrepancy (Day 49/56/60 show
`api_calls > 1` but `attempt_count = 1`):** these are two different
counters measuring different things. `attempt_count` is *this harness's
own* outer retry layer (`ask_hermes`'s one-retry-on-malformed-or-invalid-
reply contract) — it is 1 whenever the very first reply parsed and
validated cleanly, with zero retry_diagnostics, confirmed true for all
three flagged cycles. `api_calls` comes directly from Hermes' own
`--usage-file` report and reflects **the underlying model tool's own
internal API round-trips for that one logical invocation** — this
harness has no visibility into, or control over, how many actual HTTP
calls the `openai-codex`/`gpt-5.4-mini` CLI makes internally to produce
one reply. **No retry, duplicate journal write, or duplicate Hermes
invocation occurred on any of these 6 cycles** — each produced exactly
one `trades`/`portfolio_snapshots`/`backtest_cycle_evidence` row set, as
verified in §1.3's duplicate check.

### 8.3 Unusually expensive cycles

| Day | Total tokens |
|---|---|
| Day 49 | 180,357 |
| Day 11 | 169,835 |
| Day 4 | 134,341 |
| Day 56 | 91,551 |
| Day 17 | 86,687 |

All five correlate with the same cycles that show `api_calls > 1`
(§8.2) — consistent with each extra internal API round-trip carrying
its own token cost, not a separate anomaly.

### 8.4 Checkpoint-replay usage — counted correctly, not double

Day 50 (`day_index=49`) is the one cycle in this run that used the
checkpoint-replay path (§1.6). Its snapshot shows **`api_calls=1`,
`total_tokens=41,289`** — matching a single ordinary cycle's usage, not
an inflated or duplicated figure. This directly confirms: the usage
attributed to Day 50's successfully persisted cycle is the **original**
successful Hermes call's usage (captured in the checkpoint before the
journal-write failure), reused verbatim on replay — **not** a second,
new invocation's usage layered on top. The one real Hermes call this
cycle ever made is the only one counted.

### 8.5 Container/tool failures, journal-write failures, recovery correctness

Full-run log scan (the harness's own stdout across all three real
invocations that produced this run) for `CYCLE FAILED`, `OBSERVABILITY
FAILED`, and `Traceback`: **exactly one incident**, the Day 50 journal-
write failure already documented (§1.6) — 0 occurrences of any kind in
the 16-cycle completion run that followed the fix, and 0
`OBSERVABILITY FAILED` lines anywhere in the full run (meaning the
best-effort snapshot/activity-narration path never failed even once,
separately from the one essential-write failure). No failure in this
run ever advanced simulated state or duplicated a Hermes call/journal
write — confirmed by: the failed cycle's own state file was not
written (harness's own fail-closed design, independently verified at
the time), the checkpoint survived until the fix, and the replay
produced exactly one row per table with 0 duplicates (§1.3).

---

## 9. Tranche comparison (cycles 1–25 vs. 26–50)

**The trading rules, prompt content, model, and watchlist did not
change between these two ranges** — only the evidence instrumentation
(`bfb16fe`, `af0921d`) and the one Day-50 validation-contract fix
(`7c4ac06`, which affected zero accepted/rejected trading decisions,
only whether a legitimate SELL could be journaled) changed. Every
difference below is reported as an **observation**, not attributed to
any intentional change.

| Dimension | Cycles 1–25 | Cycles 26–50 | Note |
|---|---|---|---|
| Decision-time BUY proposals | 28 accepted + 20 rejected = 48 | 20 accepted + 25 rejected = 45 | Similar total; tranche 2 skews slightly more toward decision-time rejection |
| Execution-time BUY rejections | 20 (of 28 accepted) | 11 (of 20 accepted) | Tranche 2's accepted proposals survived to fill more often (9/20=45%) than tranche 1's (8/28=29%) |
| Filled BUY | 8 | 9 | Similar |
| Filled SELL, discretionary | 0 | 8 | **Real, notable shift** — every tranche-1 exit that wasn't a stop/gap was still harness-mechanical (time-stop); tranche 2 saw Hermes proactively close on time-stop grounds 7 times plus one early thesis-break |
| Completed trades (closed in-tranche, ignoring the 1 cross-tranche pair) | 7 | 9 | — |
| Win rate (closed-in-tranche) | 28.6% (2/7) | 66.7% (6/9) | Large swing at small n — see §10 |
| Realized P&L (closed-in-tranche) | −\$2,350.33 | +\$5,795.37 | Same caveat |
| Avg holding Δ (day-label) | 4.4 | 5.1 | Similar, tranche 2 slightly longer on average |
| Confidence distribution (BUY) | 18×3, 10×4 (28 total, incl. rejected proposals — tranche-1 extract's own figures) | 42×3, 5×4 (47 total, all proposals incl. rejected) | Tranche 2 skews more toward confidence 3 |
| Technical-basis distribution | Not separately extracted in tranche-1 report (no evidence existed) | 46×multiple, 1×support_resistance | Only measurable for tranche 2 |
| Exit-reason mix | 6 mechanical (stop×5, time-stop×2 — wait, see below), 1 discretionary thesis-break | 2 mechanical time-stop, 7 discretionary time-stop, 1 mechanical stop-hit crossing tranche | Discretionary time-stop SELLs are new in tranche 2 (§2.10) |
| Reflection contradictions | 1 (reflection #2, "zero closed trades") | 0 | The known defect did not recur |
| Reflection unsupported-conclusion count | 0 | 0 | Consistent |
| Token usage/cycle | 41,541 avg | 41,675 avg | Effectively identical |
| Operational failures | 0 | 1 (Day 50, since fixed) | The only operational difference between the two ranges, and it was instrumentation-caused, not trading-logic-caused |

(Exit-reason mix for cycles 1–25: re-derived from §2.10/§3.1 filtered by
tranche — 5 stop-hit + 1 mechanical time-stop-2-trades... precise
per-tranche exit-trigger counts are given in §2.10's table footnoted by
trade; not independently re-tabulated here to avoid a second, possibly
inconsistent count — see §3.1's ledger for the authoritative per-trade
detail.)

**The single largest structural difference is the appearance of
discretionary (Hermes-initiated) time-stop SELLs in tranche 2, absent
in tranche 1.** This report does not have enough evidence to say why —
whether the mechanical time-stop path stopped catching every case,
whether reflection changed Hermes' own attentiveness to §6c, or whether
this is simply attributable to which specific positions and price paths
happened to occur — since `RULES.md`/`AGENTS.md` did not change (§1.11)
and the harness's own mechanical time-stop logic (`simulate_trading_day`)
was not touched by any commit in this range either. Flagged as an open
question for the eventual judgment step (§10), not resolved here.

---

## 10. Data-adequacy verdict

**No trading-number change is recommended by this report.** This
section states only whether the evidence collected is sufficient to
support that discussion, and where it currently is not.

### 10.1 Questions the dataset can answer confidently

- Did the deterministic pipeline enforce the intended rules? **Yes** —
  §5.4–5.6 show 0 sizing/geometry/reward-math errors across every
  accepted decision in cycles 26–50; §4 shows every rejection traces to
  a real, correctly-tagged rule section; §1 shows 0 duplicate
  writes/leaked identities/cross-run contamination anywhere in 50
  cycles.
- Did Hermes reason from the evidence it was actually shown, in cycles
  26–50 specifically? **Yes, with high confidence** — §5.1–5.3 and
  §5.9 show exhaustive, zero-exception agreement between claimed and
  supplied values across every decision and every checked news-veto
  claim in this range.
- Is the masking/leak defense intact? **Yes** — 0 leaks across the full
  prompt/payload/response text of all 25 evidence rows.
- Did the Day-50 fix work and leave the run's operational integrity
  intact? **Yes** — §1.6, §8.4, §8.5.

### 10.2 Questions the dataset can answer only weakly

- Whether tranche 2's better win rate/P&L reflects anything real about
  judgment quality, versus ordinary trade-to-trade variance at n=7 vs.
  n=9. **Weak** — both samples are individually too small for a
  profit-factor or win-rate figure to be a stable estimate (§2.7's own
  caveat); the difference is real and measured, but not yet
  interpretable as a trend.
- Whether the discretionary-time-stop-SELL pattern (§9) reflects a
  genuine behavioral shift or coincidence. **Weak** — no rule/prompt
  change occurred to explain it structurally, but 8 occurrences is not
  enough to rule out coincidence in which specific positions happened
  to sit near their time-stop boundary when Hermes reasoned about them.
- Whether confidence level (3 vs. 4) correlates with outcome. **Very
  weak** — only 1 confidence-4 trade closed in cycles 26–50 (§5.13).

### 10.3 Questions the dataset cannot answer

- Whether the §5a veto is missing real material in the 106/219 (48%)
  zero-news candidate-cycles, versus correctly finding nothing to veto
  on — the stored schema cannot distinguish the two (§7.2).
- Which of the two time-stop paths (mechanical vs. Hermes-discretionary)
  would have fired first in a given case, absent a tick-level
  reconstruction outside this report's scope (§2.10).
- Anything about cycles 1–25's own per-decision evidence-reading
  accuracy — that data was never captured and cannot be reconstructed
  after the fact (§10.4).

### 10.4 Fields missing from cycles 1–25

The entire `backtest_cycle_evidence` category: exact prompt text, exact
masked input payload, per-candidate proposed/no_proposal status, attempt
count, retry diagnostics, active-limits-at-decision-time, and
rules/agents/soul hashes. `proposed_entry_price` is also null on every
tranche-1 BUY (the column didn't exist yet). **None of this is
recoverable** — deliberately not backfilled, per this project's own
lookahead-hygiene discipline (reconstructing it now would require
re-fetching data with today's tools against a past `as_of`, which is not
the same guarantee as what was actually shown live).

### 10.5 Fields still missing from cycles 26–50

- Exact per-article retrieval timestamp, source/outlet, and URL for
  every news headline (§7.5) — only relative `age_days` exists.
- A structural marker distinguishing "zero real headlines" from "a
  fetch gap" for a candidate-cycle (§7.2).
- `reasoning_effort` (§1.9) — always null; not extractable from what
  Hermes' own CLI currently reports.
- Tick-level (sub-daily) resolution for exactly when a mechanical
  time-stop check ran vs. when Hermes' own discretionary reasoning ran,
  within the same real trading day (§2.10, §9).

### 10.6 Measurements worth adding before another tranche

(Stated as options for a future decision, not a recommendation to act
now.) A structural (not free-text) marker on `candidates[].news` (or a
sibling field) distinguishing "confirmed zero articles" from "fetch
returned nothing for another reason" would close §7.2/§10.3's specific
gap cheaply. Nothing else in this audit surfaced a measurement gap large
enough to justify listing here as a near-term priority — most of what's
"missing" (source/link fields, tick-level timing) would require new
upstream data-provider capability, not a cheap instrumentation add.

### 10.7 Measurements that would be redundant or unnecessary

A formal sentiment classifier for news (explicitly avoided in this
report, per instruction, and not needed — §5.9's evidence-grounded spot
checks already answer the correctness question the classifier would
exist to answer, without inventing a category Hermes itself never used).
A second independent P&L reconciliation source beyond the two already
used (harness `closed_pnl` vs. fill-price re-derivation) — the two
already agree to within the one known, explained \$1.31 gap; a third
source would not change that.

### 10.8 Would another unchanged tranche materially improve statistical confidence?

**Yes, meaningfully, but not by itself decisively.** 16 closed trades is
still well short of RULES.md §6b's own stated six-month target of 30–60
completed trades — the sample size this project's own design already
anticipated as the real evidence-adequacy bar. Doubling to roughly 30–35
closed trades (another ~25-cycle, unchanged tranche) would move the win
rate and profit-factor estimates meaningfully closer to a stable figure,
without yet reaching the top of that stated target range. This report
takes no position on whether that tranche should be run next, only that
the currently-collected 16-trade sample is explicitly, by this
project's own stated design target, still early rather than complete.

---

## Method and reproducibility appendix

### A. Extraction method

All data pulled via a read-only Python script executed inside the
`trade-journal` container (`docker compose exec -T trade-journal
python -`), using `server.get_pool()` and plain `SELECT ... WHERE
run_id = $1` queries — no `INSERT`/`UPDATE`/`DELETE`/`ALTER` issued at
any point. Results serialized to JSON (Decimal→float, UUID→str,
datetime→isoformat) and copied out of the container via `docker compose
cp` for local, offline analysis — the live Neon connection was not held
open during the analysis itself. Every derived table/statistic in this
report was computed from that static export, in plain Python
(`json`, `math`, `statistics`, `collections`), reproducible by rerunning
the same `SELECT`s.

### B. Source tables and exact queries per section

- §0, §1.2–1.5: `SELECT * FROM {trades|portfolio_snapshots|agent_activity
  |insight_log|backtest_cycle_evidence} WHERE run_id = 'e68525f9-ce89-
  4588-a57e-866f28cd99fd' ORDER BY created_at`; duplicate checks via
  `GROUP BY cycle_id|decision_id HAVING count(*) > 1`; cross-run check via
  `SELECT DISTINCT run_id FROM trades` (no `WHERE`).
- §1.5 tranche assignment: `agent_activity.decision_id → cycle_id`,
  joined against `portfolio_snapshots` ordered by `created_at`, first 25
  = tranche 1, remaining 25 = tranche 2 (verified 1:1 against
  `backtest_cycle_evidence.cycle_id` membership — §1.7's own
  reconciliation).
- §2: `backtest_state.json`'s `portfolio.closed_pnl`/`equity_curve` (the
  harness's own authoritative ledger, read directly from the local,
  gitignored state file) cross-checked against `trades.fill_price ×
  trades.fill_quantity`, paired per §3's method.
- §3: pairing — for each real `ticker`, all `status='filled'` rows
  ordered by `created_at`; sequential BUY/SELL alternation (§9.1
  guarantees exactly one open lot per ticker at a time, so this is
  exact pairing, not a heuristic — independently verified: 0 tickers
  violated strict alternation).
- §4: `agent_activity.event_type` grouping
  (`order_queued`/`risk_check`/`order_rejected`/`order_placed`/`exit`);
  section-tag extraction via `re.findall(r'§(\d+[a-z]?)', description)`
  on `risk_check.description` (decision-time) and
  `trades.rejection_reason` (execution-time).
- §5: `backtest_cycle_evidence.input_payload`/`validated_response`/
  `candidate_status`, parsed as JSON; sizing recomputation exactly
  mirrors `risk_checker.py`'s own §2a/§2b formula and
  `required_entry_quantity`'s cash-reservation logic (`run_backtest.py`),
  independently re-implemented from the formula, not imported, so this
  is a genuine cross-check rather than the same code re-run.
- §6: `insight_log` rows for this `run_id`, `cycle_type='weekly_reflection'`;
  actual closed-trade count at each reflection's timestamp computed as
  `count(trades WHERE action='SELL' AND status='filled' AND created_at <
  reflection.created_at)`.
- §7: `backtest_cycle_evidence.input_payload.candidates[]/positions[]`.
- §8: `portfolio_snapshots.{input,output,reasoning,cache_read,
  total}_tokens`, `.api_calls`; cross-checked against the harness's own
  live console summary captured during this session's own execution of
  the completion run.

### C. Denominators, rounding, and inference vs. fact

- Win rate denominators are **closed trades only** (never including the
  1 open position). Percentages rounded to 1 decimal; dollar figures to
  the cent, matching the source `NUMERIC` columns' own display
  precision.
- "Decisions" vs. "occurrences" in §4.3 are explicitly separate columns
  — never summed together.
- Every claim in §1, §2 (except the stated \$1.31/\$1.315 gap), §3–§8 is
  a **database fact or a mechanically-derived recomputation** from
  stored fields. §9's "observation, not causal" framing and §10's
  entire verdict are explicitly **inference**, marked as such throughout
  rather than presented with database-fact confidence.
- Two independent sources were reconciled wherever both existed:
  realized P&L (harness ledger vs. fill-price re-derivation, §2.2);
  tranche-1 closed-trade totals (this report's own re-pairing vs. the
  already-published `round2_tranche1_evaluation_extract.md` figures,
  which match exactly: 7 closed, −\$2,350.33, win rate 28.6%).

---

## Independently added extractions (not requested verbatim by the task prompt)

Each addition below states the question it answers, its source, and why
it matters — flagged per instruction, not padded in.

1. **§1.6/§8.4/§8.5's `attempt_count` vs. `api_calls` distinction.**
   *Question:* did the Day-50 recovery, or any other multi-api_call
   cycle, secretly re-invoke Hermes or double-count usage? *Source:*
   direct comparison of `backtest_cycle_evidence.attempt_count`/
   `retry_diagnostics` against `portfolio_snapshots.api_calls` for the
   same cycles. *Why it matters:* the task explicitly asks this
   question (§8) and it cannot be answered from either field alone —
   only their disagreement, explained, answers it.
2. **§2.10's exit-reason categorization split into 4 (not 2) buckets,
   distinguishing mechanical-stop from mechanical-time-stop from
   discretionary-time-stop from discretionary-thesis-break.** *Question:*
   why did some SELLs carry a real confidence/reward field while others
   were null? *Source:* `trades.confidence`/`strategy_notes` cross-
   referenced with `agent_activity.event_type='exit'` descriptions.
   *Why it matters:* this is what surfaced the real, load-bearing
   AGENTS.md-Step-3 dual-path time-stop-handling fact in §2.10/§9,
   which materially changes how "the SELL exit contract" (task item 5's
   own last bullet) should be read.
3. **§3.1's invalidation-condition-tested-vs-untested column.**
   *Question:* task item 3's own last bullet ("whether the original
   invalidation condition matched the actual failure/exit"). *Source:*
   comparing each trade's `invalidation_condition` text/level against
   its actual `exit reason`. *Why it matters:* a naive yes/no framing
   would be misleading — the honest finding is "100% match whenever
   actually tested, but 10 of 16 were never tested because the 5-day
   time stop fired first," which is a structural fact about the
   aggressive-profile's short time stop, not a reasoning quality
   signal, and deserved its own explicit column rather than a folded-in
   guess.
4. **§6's `sample_size`-is-a-rolling-window-not-a-cumulative-total
   finding.** *Question:* task item 6's own bullet ("whether the text
   acknowledged the true sample size"). *Source:*
   `fetch_recent_closed_trades(..., limit=8)` in `run_backtest.py`,
   cross-referenced against the actual cumulative closed-trade count at
   each reflection's real timestamp. *Why it matters:* without this,
   a reader would likely (as this report's own author briefly did,
   before checking the source) misread `sample_size=8` staying flat
   across four straight reflections in cycles 26–50 as a *repeat* of
   the tranche-1 "zero closed trades" contradiction. It is not — it is
   the intended behavior of an 8-trade rolling window, and stating that
   precisely prevents a false positive finding from propagating into
   the next evaluation step.

---

## Verification checklist

- [x] Every table/count reconciled — §0, §1.2–1.5, §2.2 (two independent
  P&L sources, gap explained), §3 (pairing self-consistency check, 0
  alternation violations).
- [x] Unresolved discrepancies explicitly listed: the \$1.31 JULIET
  rounding gap (§2.2, pre-existing, already documented, not new); the
  news zero-vs-gap ambiguity (§7.2, §10.3, §10.5); the
  mechanical-vs-discretionary time-stop ordering question (§2.10, §9,
  §10.3) — none of these block the extraction; all are stated as open
  rather than silently resolved.
- [x] `docs/evaluations/round2_tranche1_evaluation_extract.md` read but
  not modified — confirmed via `git status` before commit (§ delivery
  below).
- [x] No code, schema, rules, prompt, model, watchlist, dashboard,
  `backtest_state.json`, or `HANDOFF.md` touched. No Neon `INSERT`/
  `UPDATE`/`DELETE` issued at any point (§A). No harness run, no
  `--reset`, no training resumed.
