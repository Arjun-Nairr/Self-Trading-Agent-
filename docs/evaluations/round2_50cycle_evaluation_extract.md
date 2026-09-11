# Round 2 — Evaluation Brief (compact, cycles 1–50)

**This is the active, current evaluation document.** It is a compacted
summary derived entirely from two full raw evaluation reports, now
archived at:

- `docs/history/evaluations/round2_tranche1_evaluation_extract.md` — full detail, cycles 1–25
- `docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md` — full detail, cycles 1–50 (supersedes the first for anything both cover)

Consult the archived reports directly for: the complete 16-trade ledger
with verbatim reasoning/invalidation text, all 10 reflections verbatim,
per-cycle SQL/query definitions, and row-level rejection detail. This
brief carries only headline facts, clearly labeled by confidence — it
does not reproduce them.

**Labeling key used throughout:** 🟢 **Verified** = a direct database
fact or a mechanical recomputation from stored fields, reconciled
against ≥1 independent source in the raw reports. 🟡 **Interpretation**
= a reasonable reading of verified facts, explicitly not a database
fact. 🔴 **Unknown** = the raw reports state this cannot be concluded
from this dataset.

---

## 1. Run identity and state — 🟢 Verified

| Field | Value |
|---|---|
| `run_id` | `e68525f9-ce89-4588-a57e-866f28cd99fd` |
| Completed cycles | 50 (cycles 1–25 = tranche 1, 26–50 = tranche 2) |
| Cursor | `2026-04-29` |
| Open position | 1 (masked alias `DELTA`) |
| Pending orders | 0 |
| Active checkpoint | none |
| Closed trades | 16 |
| Status | Complete through cycle 50; not resumed, not reset, no training in progress |

No duplicate `cycle_id`/`decision_id` anywhere in this run; no other
historical run_id's rows mixed into any figure below (raw report §0–§1).

---

## 2. Performance summary — 🟢 Verified

| Metric | Value |
|---|---|
| Starting equity | \$100,000.00 |
| Ending equity | \$103,696.08 |
| Total return | +3.70% |
| Realized P&L (harness-authoritative) | +\$3,443.73 |
| Unrealized P&L (open position) | +\$252.35 |
| Max drawdown (full run) | −2.46% (cycles 1–25); −1.88% (cycles 26–50) |
| Closed / open positions | 16 / 1 |
| Win rate (all 16 closed) | 50.0% (8W / 8L) |
| Payoff ratio / profit factor (all 16) | 1.63 / 1.63 |
| Expectancy per closed trade | +\$215.32 |
| Largest win / loss | +\$1,704.24 / −\$1,811.13 |
| Max consecutive wins / losses | 4 / 4 |
| Avg. holding span (day-label Δ) | 4.75 days, median 6.0 |

**Known, pre-existing \$1.31 gap** between the two independent P&L
sources (harness ledger vs. fill-price re-derivation), isolated to one
tranche-1 trade's 2-decimal-precision `fill_price` storage (predates the
later `NUMERIC(18,6)` widening fix; not reproducible going forward, not
a new finding — raw report §2.2).

### Exit-reason mix (16 closed trades) — 🟢 Verified

**Corrected 2026-09-11** (`trade_journey_report.py`'s provenance
reconciliation, `docs/evaluations/round2_trade_journey.md`): the split
below between mechanical stop hit / mechanical time stop was wrong in
the original hand tally — proven from `trades.confidence`/
`primary_signal`/`invalidation_condition`'s structural fingerprint
(exactly what `run_backtest.py`'s `record_exit` writes vs. what a real
Hermes decision must satisfy), not re-derived from note wording. Total
mechanical (8) and total discretionary (8) are unaffected.

| Trigger | Count |
|---|---|
| Mechanical stop hit | 5 |
| Mechanical time stop | 3 |
| Hermes-initiated discretionary time-stop SELL | 7 |
| Discretionary thesis-invalidation SELL | 1 |

🟡 **Interpretation:** `AGENTS.md` Step 3 asks Hermes to check the time
stop itself, independently of the harness's own mechanical enforcement —
so the 7 discretionary time-stop exits are expected dual-path behavior,
not a contradiction between the two mechanisms.

**Invalidation-condition check:** of the 6 stop-hit exits, **100%**
matched their own stated invalidation price exactly. The other 10 never
tested their invalidation condition — the 5-trading-day time stop fired
first (structural consequence of the aggressive profile's short time
stop, not a reasoning defect).

---

## 3. Tranche 1 vs. tranche 2 — 🟡 Interpretation (observation only)

**No rule, prompt, model, or watchlist change occurred between the two
ranges** — only instrumentation (`bfb16fe`, `af0921d`) and one
validation-contract fix (`7c4ac06`, affecting journaling only, zero
trading decisions). Every difference below is an observation, not a
causal claim.

| Dimension | Cycles 1–25 | Cycles 26–50 |
|---|---|---|
| Closed trades (in-tranche) | 7 | 9 |
| Win rate | 28.6% | 66.7% |
| Realized P&L | −\$2,350.33 | +\$5,795.37 |
| Filled BUY / discretionary SELL | 8 / 0 | 9 / 8 |
| Operational failures | 0 | 1 (Day 50, since fixed) |
| Token use/cycle | 41,541 avg | 41,675 avg (~identical) |

At n=7 vs. n=9, this reversal is well within ordinary trade-to-trade
variance — 🔴 **Unknown** whether it reflects anything real about
judgment quality. The clearest **structural** (not performance)
difference is the appearance of discretionary time-stop SELLs, absent in
tranche 1 — 🔴 **Unknown** why (no rule/prompt change explains it; raw
report §9 flags this as an open question, not resolved).

---

## 4. Proposal / fill / rejection funnel — 🟢 Verified

| Stage | Count |
|---|---|
| Decision-time-evaluated proposals | 101 (93 BUY + 8 SELL) |
| — accepted | 56 (48 BUY + 8 SELL) |
| — decision-time rejected | 45 (all BUY) |
| Of accepted: filled | 25 (17 BUY + 8 SELL) |
| Of accepted: execution-time rejected | 31 (all BUY) |
| No-trade cycles | 12 of 50 |

**Every rejection in the run, at either stage, was a BUY** — zero SELLs
were ever rejected. Acceptance-to-fill: 35.4%. Proposal-to-completed-
trade: 17.2%. SELL proposal-to-fill: 100%.

### Rejection reasons, by RULES.md section (decisions, not double-counted)

| Section | Decision-time | Execution-time |
|---|---|---|
| §2b (concentration cap) | 37 | 24 |
| §2/§3 (sizing/cash mismatch) | 8 | — |
| §6b (reward:risk floor) | — | 16 |
| §6a (stop distance/gap) | — | 7 |

**§6a/§6b never appear at decision time** — both depend on the real
next-day price, which decision-time evaluation cannot see; confirmed
consistent across the whole run. **All 37 §2b decision-time rejections
cluster in a narrow 20.02%–20.11% band** (cap 20.00%) — consistently
small boundary misses, never a wildly oversized proposal.

---

## 5. Key reasoning and reflection findings (cycles 26–50 only) — 🟢 Verified

Exhaustive, not sampled — every one of 54 decisions across all 25
evidence-bearing cycles was checked programmatically against the exact
data Hermes was shown that cycle.

- **0 mismatches** in candidate coverage, proposed-alias structural
  validity, claimed-vs-supplied technicals (price/support/resistance/
  RSI), stop/target geometry, self-reported reward:risk math, and sizing
  arithmetic (all 20 accepted BUYs sized exactly to the real 2%/20%
  limits shown that cycle).
- **0 leaks** of any real ticker/company name across the full text of
  all 25 evidence rows.
- **3 spot-checked news-veto claims, 3/3 accurate** — e.g. Hermes cited
  "vetoed by adverse strike news"; the supplied headline was a real
  labor-strike article. No fabricated or overstated adverse-news claim
  found; no case of positive news used to justify a BUY.
- **0 byte-identical reasoning strings** reused across different
  decisions (similar-looking arithmetic disclosure is a required
  template, not copy-pasting).
- SELL decisions correctly report `expected_reward_risk >= 0` under the
  corrected contract (6 of 7 report exactly `0.0`, the honest value for
  a closing decision) — none fabricate a placeholder.

### Reflections (all 10, full run) — 🟢 Verified, 1 flagged defect

`insight_log.sample_size` is a **capped 8-trade rolling window**, not a
cumulative closed-trade count (`fetch_recent_closed_trades(limit=8)`) —
🟡 this is worth stating precisely: in tranche 1 it happened to equal the
true total only because tranche 1 never closed more than 8 trades.

**1 genuine contradiction found, across all 10 reflections:** reflection
#2 (Day 14, tranche 1) states "with zero closed trades..." while 2
trades had actually closed. Pre-existing, already documented, not
repeated in any of the other 9 reflections (including 5 in cycles
26–50, which correctly never claim a false absolute count). **Zero**
reflections in either tranche draw an unsupported strategy conclusion —
all stay at the process-observation level `RULES.md`/`AGENTS.md`
require for this sample size.

---

## 6. Evidence coverage: cycles 1–25 vs. 26–50 — 🟢 Verified

| | Cycles 1–25 | Cycles 26–50 |
|---|---|---|
| Trade/journal data (`trades`, `portfolio_snapshots`, `agent_activity`) | Full | Full |
| Exact per-cycle prompt/input/response (`backtest_cycle_evidence`) | **None** — table didn't exist yet | 100% (25/25 cycles) |
| `proposed_entry_price` | Null on all 8 filled BUYs (column didn't exist) | Populated on all 9 filled BUYs + the open position |

Cycles 1–25's missing evidence is **not recoverable** — deliberately
never backfilled (would require re-fetching data against a past `as_of`,
breaking lookahead hygiene).

---

## 7. Data-quality limitations and missing measurements — 🟢 Verified / 🔴 Unknown

- **~48% of candidate-cycles (106/219) had zero news items that day** —
  structurally indistinguishable from "a fetch gap," since both collapse
  to the same empty list in storage. 🔴 Unknown whether the §5a veto had
  real material to evaluate in those cases.
- News items carry `headline`/`summary`/`symbols`/`age_days` only — **no
  exact retrieval timestamp, source/outlet, or URL.**
- `reasoning_effort` is null on all 25 evidence rows — the real Hermes
  usage report has never included that key.
- No tick-level resolution to determine which of the two time-stop paths
  (mechanical vs. Hermes-discretionary) would fire first in a given
  case.
- Technical payload itself is fully complete: **0 of 219** candidate
  fetches (and 0 of 31 held-position fetches) returned an error or null.

---

## 8. Usage, reliability, and the Day 50 failure — 🟢 Verified

| Scope | Total tokens | Avg/cycle | API calls |
|---|---|---|---|
| Full run | 2,080,404 | 41,608 | 68 |
| Cycles 1–25 | 1,038,519 | 41,541 | 35 |
| Cycles 26–50 | 1,041,885 | 41,675 | 33 |

6 cycles show `api_calls > 1` (up to 6) while `attempt_count = 1` on
every evidence-bearing one of them — this is the underlying model CLI's
own internal API round-trips per single logical call, **not** a hidden
retry or duplicate invocation (confirmed: each such cycle produced
exactly one row per table).

**Day 50 failure, resolved:** one cycle (`agent_activity` `cycle_failed`
row, preserved intentionally) halted on a real contract mismatch —
legitimate discretionary SELLs with `expected_reward_risk: 0.0` were
rejected by a validator requiring `> 0` unconditionally. Fixed in commit
`7c4ac06` (action-aware: BUY still requires `>0`, SELL/other now permits
`>=0`). The checkpoint replayed **without a new Hermes call**
(`api_calls=1`, matching a single ordinary cycle, not inflated) and the
run completed cleanly to cycle 50 with zero further failures.

This is the run's **only** operational failure across all 50 cycles —
zero container/tool/observability failures otherwise.

---

## 9. Data-adequacy verdict

**No trading-number change is recommended by either archived report.**
This states only whether the evidence is sufficient to support that
discussion.

**🟢 Can answer confidently:** did the deterministic pipeline enforce
the intended rules (yes — 0 sizing/geometry/reward-math errors); did
Hermes reason from the exact evidence it was shown in cycles 26–50 (yes,
high confidence — exhaustive zero-exception checks); is the masking/leak
defense intact (yes — 0 leaks); did the Day 50 fix work cleanly (yes).

**🟡 Can answer only weakly:** whether tranche 2's better win rate/P&L
reflects real judgment improvement vs. ordinary variance (both samples
too small); whether the discretionary-time-stop-SELL pattern is a
genuine behavioral shift; whether confidence level correlates with
outcome (only 1 confidence-4 trade closed in cycles 26–50).

**🔴 Cannot answer:** whether the ~48% zero-news candidate-cycles missed
real material; which time-stop path would fire first in a given case;
anything about cycles 1–25's own evidence-reading accuracy (never
captured).

**Would another unchanged tranche materially help?** 🟡 **Yes,
meaningfully, but not decisively.** 16 closed trades remains well short
of `RULES.md` §6b's own stated 30–60-trade six-month target — the bar
this project's own design already set for adequate evidence. Roughly
doubling the sample (another ~25-cycle tranche) would move win-rate/
profit-factor estimates meaningfully closer to stable, without yet
reaching the top of that range. No position is taken on whether to run
it.

---

## Methodology note

Both archived reports were built from read-only, run-scoped Neon
queries (`WHERE run_id = 'e68525f9-...'`, zero `INSERT`/`UPDATE`/
`DELETE`) executed inside the `trade-journal` container, cross-checked
against `backtest-harness/backtest_state.json`'s own authoritative
ledger. This compact brief adds no new data — every figure above is
copied or arithmetically summarized from the two archived reports,
re-verified against their own tables before inclusion here. All ticker
identities are Hermes' own masked aliases; the real-ticker mapping is
never stated in either the archives or this brief.

**For full detail** — the complete 16-trade ledger with verbatim
reasoning, all 10 reflections verbatim, the full rejection-by-day
breakdown, exact SQL, and the 19-item data-adequacy coverage matrix —
see:

- `docs/history/evaluations/round2_50cycle_evaluation_extract_RAW.md` (cycles 1–50, primary)
- `docs/history/evaluations/round2_tranche1_evaluation_extract.md` (cycles 1–25, narrower)
