# Phase 1 aggressive paper-trading profile — approved design

**Status: approved design, not yet implemented.** This document records the
numeric trading profile approved for the next backtest round, as a target
for `RULES.md` and `risk-checker/risk_checker.py` to implement together
(synchronized, per that module's own drift-risk warning — a number changed
in one and not the other is silently unenforced). **No enforcement file
currently reflects these numbers.** `RULES.md`, `risk_checker.py`,
`AGENTS.md`, and the harness all still encode the conservative Round-1
profile as of this document's creation (2026-08-30) — see `HANDOFF.md` for
the exact current state and the pending implementation step.

This is a **permanent paper-money sandbox**, not a path toward real capital.
The point of the aggressive profile is to generate more real trading
evidence per unit of time than Round 1's near-total inactivity did — not to
simulate what a live account should risk.

## The profile

| Parameter | Value | Replaces (Round 1 / RULES.md today) |
|---|---|---|
| Minimum reward:risk | **1.5:1** | 2.0:1 (§6b) |
| Risk budget per trade | **2% of equity** | 1.0% (§2a) |
| Max single position | **20% of equity** | 10% (§2b) |
| Exposure limit | **available cash only; no margin** | 60% of equity, 5% min cash (§3) |
| Position-count / daily-entry limit | **none** | 8 max positions (§3) |
| Same-ticker concurrency | **one open position per ticker** | (unchanged — already the rule) |
| Stop distance | **3–12% from entry** | 5–10% (§6a) |
| Time stop | **5 trading days maximum hold** | 15 trading days (§6c) |
| Circuit breaker | **only a catastrophic −30% drawdown pause** | −3% daily / −6% weekly / −10% drawdown (§4) |
| Activity target | **≥5 completed trades per 20-trading-day period** | none |
| Escalation | **if activity stays below target, propose loosening the next period's minimum R:R to 1.25:1** | n/a |
| Six-month target | **30–60 completed trades** | n/a (Round 1 produced 1) |

**"Proposed escalation" is exactly that — proposed, not automatic.**
Whatever mechanism eventually implements it must surface the loosening as a
recommendation a human approves before it takes effect, not a rule Hermes
or the harness applies to itself. This mirrors `RULES.md`'s own standing
rule that Hermes may never loosen a limit through its own reasoning.

## What stays mandatory, unconditionally

Every one of the following stays exactly as it is today — this profile
changes numeric thresholds, not the safety architecture around them:

- **Lookahead prevention and point-in-time masking** — `as_of`-gated data
  fetches, reshuffled ticker/company aliases, next-day queued execution.
- **Decision-time vs. execution-time validation** — a proposal passing
  decision-time can still be correctly rejected at real execution.
- **Execution mechanics** — real 15-minute intraday bars, the exact
  5bps `execution-sim-mcp` slippage formula, stop-before-target same-bar
  tie-breaking, the opening-gap rule.
- **Missing-data fail-closed behavior** — a genuine data gap on a real
  trading day halts the cycle; it is never silently skipped or fabricated.
- **Queue-time journaling and idempotency** — every decision journaled the
  instant it's queued, `decision_id` collision detection, no fabricated
  fills.
- **Anti-fabrication discipline generally** — no invented price, level,
  stop, or outcome, anywhere in the stack, under any profile.
- **Sentiment-as-veto-only (§5a)** — the prompt-injection defense described
  there does not change with this profile.
- **`RULES.md`'s hard prohibitions (§9)** — no averaging down, no widened
  stops, no off-watchlist trading, no same-day re-entry after a stop-out.

## What this document does not do

It does not edit `RULES.md`'s numbers, `risk_checker.py`'s `RiskLimits`,
`AGENTS.md`'s procedure, or any test. It does not authorize starting a new
backtest run. Implementing it is a synchronized change across `RULES.md`,
`risk_checker.py`, `AGENTS.md`, and the harness's own tests together — see
`HANDOFF.md`'s "Exact next step" for that pending work.
