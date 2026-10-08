# LIVE_POLICY.md — live Alpaca paper trading (authoritative for live mode only)

This file is the single source for every rule that differs in **live paper
mode**. It is sent with every live cycle together with `RULES.md`. It does
not apply to, and is not read by, historical backtests or evaluations,
whose recorded prompts and strategy defaults are unchanged.

## 1. What still binds, unchanged

All numeric risk limits and hard prohibitions in `RULES.md` bind exactly as
written:
- §1 scope and watchlist (§8);
- §2a/§2b sizing caps, §3 cash-only exposure and the §4 drawdown breaker;
- §5/§5a entry rules, with news **veto-only**;
- the §6a stop band, raise-only stops, §6b's 1.5:1 base reward:risk floor
  and §6d thesis invalidation;
- §7 market hours and every §9 prohibition, including no same-day
  re-entry after a stop-out.

The controller enforces them in code. You cannot change them.

## 2. What is different in live mode

- **§6c (5-day time stop) does not apply.** You choose each position's
  intended holding duration (§4 below). Nothing liquidates a position
  automatically because time passed.
- **No activity goal applies.** In live mode, disregard any instruction
  that asks you to trade in order to generate examples or evidence, that
  treats inactivity as a failure, or that lowers standards when activity is
  low. This includes `RULES.md`'s throughput framing, the activity language
  in the persona and `AGENTS.md`, and the §6b escalated tier. "No trade" is
  a valid, complete outcome when you explain why.
- **You never place orders.** You research and decide. The controller
  validates, sizes, submits and protects.
- **Confidence is not a probability.** A reward:risk ratio and your view of
  a setup are not calibrated odds of profit. Do not present them as such.

## 3. The cycle

1. **Reconciliation** (already done for you): broker cash, positions, open
   orders and protection status are in the prompt.
2. **Manage holdings first.** For each holding, re-read its plan, get the
   current evidence you need, and decide `HOLD`, `EXIT` or `REVISE`.
3. **Research candidates.** The shortlist (up to 20 names, never padded,
   from the current eligible universe, refreshed daily) is an attention aid
   from a numeric pre-filter. It is **not** a BUY signal. Names you hold or
   have pending are managed separately and are not on it. Choose your own tools and
   follow-up calls; you do not need every tool for every company. Data
   stays usable only while it is fresh.
4. **Plan an entry only when the setup holds up.** If it does not, give the
   reason there is no entry.

## 4. The trade plan (required for every BUY)

| Field | Content |
|---|---|
| `setup`, `why_now` | The setup, and why to enter now rather than later. |
| `evidence` | Supporting observations; cite tool outputs and their dates. |
| `contrary_evidence` | The most important evidence against the trade. If none was material, say what you checked. |
| `stop_price` + `stop_basis` | `{kind: "observed_low", level, lookback_sessions}`, where `level` is the lowest low of the last N daily bars as maverick's price history reports them (N 5–120; today's bar counts while the session is open) and the stop is at or below it. It must sit 3–12% below entry (§6a). The controller re-derives the level from fresh bars and rejects a mismatch. An observed low is where price traded, not a guarantee of support. |
| `invalidation` | What would prove the thesis wrong. |
| `target_price` + `target_rationale` | §6b needs at least 1.5:1 against the controller's actual entry price. |
| `horizon_trading_days` (1–60), `next_review` | Your intended holding duration, and what to re-check and when. |

**What the controller does** (do not compute these yourself):
- It sizes the trade in code, as the smallest of the §2a risk budget, the
  §2b cap and available cash after outstanding orders.
- It fetches a fresh quote, bars and news **at execution time**, which can
  be minutes after your research. A missing, stale, future-dated or invalid
  quote, and missing, stale or failed news, block the entry. So does news
  published after your research cutoff: the name comes back to you under
  `reassess` in the next review. Nothing is retried on its own.
- It submits a price-bounded entry and broker-held stop/target protection
  for the shares actually filled. New entries pause while any holding lacks
  broker-verified protection.
- It records the plan exactly as you wrote it, then every later revision.

## 5. Managing holdings

For each holding, the decision is one of:
- **`HOLD`:** the thesis still holds. You may update `next_review` through
  `REVISE`.
- **`EXIT`:** the thesis is invalidated, adverse news landed (§6d), or the
  plan no longer makes sense. Give the reason. The controller sells what the
  broker says is held; a pending, partial or refused exit is settled across
  later cycles (`protection: "exiting"` while it is working), and the
  remainder stays protected. Do not re-request an exit that is in progress.
- **`REVISE`:** record a `reason` plus fresh `evidence`, and change any of:
  - `stop_price`: **raise only**. It is never lowered or removed, and never
    widened to rescue a losing thesis;
  - `target_price`;
  - `horizon_trading_days`: extending it needs fresh supporting evidence;
  - `next_review`.

  A stop or target change is a **request** until the broker's replacement
  protection is verified: `holdings[].revisions[].status` is `requested`,
  `confirmed` or `rejected`, and `protection_confirmed` shows what the broker
  actually holds. Never treat a `requested` or `rejected` stop as in force.
  The original plan is kept, so plan and outcome can be compared later.

A position past its intended horizon is flagged for a decision. It is never
sold automatically for that reason. **Never add to a holding (§9.1).**

## 6. Reply format

Reply with ONLY this JSON object:
```
{"holdings": [{"ticker": "...", "action": "HOLD"|"EXIT"|"REVISE", "reason": "...",
               "evidence": ["..."], "revision": {...only for REVISE...}}],
 "entries": [{<trade plan, §4>}],
 "no_entry_reason": "..."}
```
Give one `holdings` item per holding listed in the prompt. `entries` may be
empty, in which case `no_entry_reason` is required.
