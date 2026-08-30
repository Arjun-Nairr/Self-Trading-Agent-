# Run 1 Evaluation Extract

Read-only extraction against Neon (`mode='backtest'`, `run_id='26f1d371-08db-4885-ab24-b42200792fb7'`) and local state files. No writes to Neon, no state changes, no Hermes calls. Generated 2026-08-29.

---

## 1. Run Metadata

- **run_id:** `26f1d371-08db-4885-ab24-b42200792fb7`
- **--start / --end:** `2026-02-17` / `2026-08-14` (persisted in `backtest_state.json`, confirmed matches the launch command)
- **--days used:** 130 (generous cap; the run stopped on window end, not the days cap or quota)
- **Final cursor:** `2026-08-15` (one day past `--end` -- the loop's own `while ... and cursor <= end` condition ended it naturally)
- **Cycles actually run (real trading days):** 125
- **Cycles halted (cycle_failed):** 0 -- confirmed zero; a halt would have stopped the run mid-window and left the cursor stuck there, but the cursor reached past `--end`, so the run never halted even once
- **Cycles skipped (holiday/weekend):** not directly journaled -- a skip returns before any Neon write happens (by design; nothing happened that day). Inferred: the window spans 2026-02-17 to 2026-08-14 (179 calendar days per `backtest_state.json`'s own BACKTEST WINDOW denominator); 125 were real trading cycles, so the remainder (~54 calendar days) were weekends/holidays skipped before any fetch.
- **Checkpoint file present?** No (`cycle_checkpoint.json` absent) -- confirms the run ended cleanly, not mid-cycle.

**Final portfolio state** (`backtest_state.json`):
- Cash: **$100,000.00** (unchanged from the starting $100,000)
- Equity curve: **flat the entire run** -- 125 snapshots recorded, min equity $100,000.00, max $100,000.00. Zero drift in either direction.
- Holdings: **none** (confirmed empty every single snapshot -- `open_positions` was 0 in all 125 rows)
- Pending orders: **none** (empty at final state)
- `stopped_out_today`: **empty** (nothing was ever stopped out -- consistent with zero positions ever being held)

**Quota:**
- Final `quota_pacer_state.json`: `{"2026-W34": 34, "2026-W35": 131}`
- Hermes invocations charged this run: **125** (2026-W35 climbed from 6 to 131 across the run's lifetime -- some of that 6 predates this run's launch)
- Total real API calls (from `portfolio_snapshots.api_calls`): **125** -- exactly 1 per cycle, meaning the JSON-parse retry (`ask_hermes`'s one allowed retry) was never once needed across all 125 cycles
- Total tokens across the run: **2,558,025**
- Cost: reported as "included in subscription" throughout (no per-call USD cost surfaced by the ChatGPT/Codex backend) -- `estimated_cost_usd` was consistently 0.0 in every snapshot's usage

---

## 2. The Zero-Trade Question

- Cycles with **zero decisions** (a `no_action_reason` reasoning row, no risk_check/order_* rows): **120** of 125
- Cycles with **one or more decisions proposed**: **5** of 125

**This is world A dominant, with a small amount of world B mixed in.** Hermes overwhelmingly (120/125 = 96.0%) concluded nothing qualified before ever proposing anything. In the remaining 5/125 cycles (4.0%) it did propose a real, fully-specified trade -- every one of those 5 was rejected, 4 at decision time and 1 that passed decision-time review, got queued, and was rejected the next day at real execution.

### Distribution of `no_action_reason`, grouped by cause

All 120 `no_action_reason` texts are **individually distinct** (no two cycles produced identical wording) -- this is evidence against a stuck/repeating loop; each cycle's text references that day's actual candidate set by alias. Grouped by dominant theme (keyword-based, a cycle can only be double-counted if it matches no earlier category):

| Count | % | Cause |
|---|---|---|
| 98 | 81.7% | setup exists but sits too close to resistance / extended / overbought (location) |
| 22 | 18.3% | news-veto mentioned as a contributing factor |

**10 representative verbatim quotes** (aliases as stored, masked):

> Cycle at `2026-08-29 10:51:01.444884+00:00`:
> No long setup met the trading constraints: the bullish names were either stretched into resistance, lacked a clean technical edge, or had stop levels that were not safely within the allowed range. The bearish names were not eligible for long entries, and the news flow did not improve any candidate enough to override the technical weakness.

> Cycle at `2026-08-29 10:55:26.184513+00:00`:
> No long-only setup had enough alignment to qualify. The bullish candidates were missing confirmation or had poor timing: LIMA had bearish MACD and weak reward/risk, JULIET had an adverse news veto, GOLF was overbought and near resistance, and INDIA had a bearish MACD despite otherwise bullish trend. The bearish names are not eligible for long entries, so the correct action is to wait.

> Cycle at `2026-08-29 11:02:44.712273+00:00`:
> No long setup has clean enough alignment and room to a nearby level to justify forcing an entry. The bullish names are strong but sit close to resistance rather than offering a cleaner pullback or breakout confirmation, while the rest are mixed or bearish and do not fit a long-only buy.

> Cycle at `2026-08-29 11:09:34.725727+00:00`:
> No candidate offered a clean long setup with trend, momentum, and nearby location all aligned strongly enough to justify risk. Several names had mixed signals or were pressing into resistance rather than breaking out, and the one bearish-news name was not compelling enough to buy through that veto. Restraint is the better decision today.

> Cycle at `2026-08-29 11:17:09.955845+00:00`:
> No candidate had enough agreement between trend, momentum, and location to justify a long entry. The strongest names were already extended or conflicted with nearby resistance, while the oversold names still had bearish trend and/or bearish MACD, so there was no setup with a clear technical edge and acceptable reward:risk.

> Cycle at `2026-08-29 11:29:43.418171+00:00`:
> No candidate had trend, momentum, and location aligned well enough to justify a trade. The closest bullish names were still awkwardly positioned under nearby resistance or had conflicting momentum, and several others had bearish or neutral trend. Following restraint, I passed on all setups.

> Cycle at `2026-08-29 11:51:37.158537+00:00`:
> No candidate has a clean enough location advantage for a long entry. The strongest bullish names are either too close to resistance (LIMA, MIKE, INDIA, NOVEMBER, JULIET) or lack a favorable momentum/technical mix at a better level. The oversold names (FOXTROT, CHARLIE) are still in bearish trends with bearish MACD, so they do not qualify as long setups. No trade is the better decision.

> Cycle at `2026-08-29 12:15:11.044293+00:00`:
> No long setup qualifies. The strongest bullish names are extended into resistance or overbought, while the bearish names do not fit the long-only mandate. I am waiting for clearer room, a real technical stop, and better reward/risk.

> Cycle at `2026-08-29 12:51:33.992557+00:00`:
> No candidate offers both clear room to resistance and a technically obvious invalidation level with sufficient reward:risk. The stronger names are already pressed into nearby resistance, while the weaker names do not have a setup that stands out enough to justify a swing entry.

> Cycle at `2026-08-29 13:13:43.444906+00:00`:
> No candidate offered a clean setup with enough room to target and a clearly defined invalidation level inside the allowed risk bounds. The bearish names are mostly boxed in by nearby resistance, and the bullish names do not provide a favorable long entry with strong reward:risk or clean adverse-news-free confirmation.

---

### Every decision Hermes ever proposed (all 5, in full)

- **[decision-time rejected]** `2026-08-29 10:49:53.007127+00:00` -- ALGM: decision-time risk check rejected -- §6b: reward:risk is 1.95, minimum is 2.0
- **[decision-time rejected]** `2026-08-29 11:01:24.619848+00:00` -- ATI: decision-time risk check rejected -- §2b: position is 15.70% of equity, cap is 10.00%; §6b: reward:risk is 1.97, minimum is 2.0
- **[decision-time rejected]** `2026-08-29 11:03:59.711563+00:00` -- DINO: decision-time risk check rejected -- §6b: reward:risk is 1.92, minimum is 2.0
- **[decision-time rejected]** `2026-08-29 12:35:30.659788+00:00` -- DINO: decision-time risk check rejected -- §2b: position is 19.80% of equity, cap is 10.00%
- **[queued]** `2026-08-29 11:13:46.502521+00:00` -- BUY SFM (JULIET) x123 queued for next trading day's open
- **[execution-time rejected]** `2026-08-29 11:14:56.107293+00:00` -- SFM: queued BUY rejected -- §2b: position is 10.26% of equity, cap is 10.00%; §6b: reward:risk is 0.91, minimum is 2.0

Full `trades` table row for the one decision that made it past decision-time review (SFM):

```json
{
  "decision_id": "5beb5dfd-9039-572d-b96c-b10ad7ff9f30",
  "ticker": "SFM",
  "action": "BUY",
  "status": "rejected",
  "confidence": 3,
  "primary_signal": "technical",
  "technical_basis": "multiple",
  "expected_reward_risk": "2.00",
  "invalidation_condition": "If price closes back below 77.02 support, the bullish thesis is broken.",
  "strategy_notes": "Trend, RSI, and MACD all agree, and price is holding above nearby support with room to the next resistance. News is absent, so there is no veto. The stop is under support and the target at the next resistance gives about 2:1 reward to risk.",
  "stop_price": "77.02",
  "target_price": "89.18",
  "intended_quantity": 123,
  "intended_notional": "9971.61",
  "fill_price": null,
  "fill_quantity": null,
  "rejection_reason": "\u00a72b: position is 10.26% of equity, cap is 10.00%; \u00a76b: reward:risk is 0.91, minimum is 2.0",
  "created_at": "2026-08-29 11:13:37.792585+00:00",
  "updated_at": "2026-08-29 11:14:04.906747+00:00"
}
```

Notice: SFM's *self-reported* `expected_reward_risk` was `2.00` at proposal time (a clean pass) -- the real, slippage- and price-adjusted execution-time reward:risk collapsed to **0.91** against the actual next-day open. This is the decision-time-vs-execution-time gap working exactly as designed: a proposal that looked fine on yesterday's numbers can still fail once it's checked against the real price that actually happened.

---

## 3. Blocked / Rejected

All `risk_check` and `order_rejected` `agent_activity` rows, grouped by § rule cited (a single rejection can cite more than one rule):

| § rule | Count | Meaning |
|---|---|---|
| §6b | 4 | reward:risk below the 2.0 minimum |
| §2b | 3 | position size over the 10% concentration cap |

**All 4 verbatim risk_check rows + the 1 order_rejected row (this is the complete set -- there are only 5):**

- `2026-08-29 10:49:53.007127+00:00` -- ALGM: decision-time risk check rejected -- §6b: reward:risk is 1.95, minimum is 2.0
- `2026-08-29 11:01:24.619848+00:00` -- ATI: decision-time risk check rejected -- §2b: position is 15.70% of equity, cap is 10.00%; §6b: reward:risk is 1.97, minimum is 2.0
- `2026-08-29 11:03:59.711563+00:00` -- DINO: decision-time risk check rejected -- §6b: reward:risk is 1.92, minimum is 2.0
- `2026-08-29 12:35:30.659788+00:00` -- DINO: decision-time risk check rejected -- §2b: position is 19.80% of equity, cap is 10.00%
- `2026-08-29 11:14:56.107293+00:00` -- SFM: queued BUY rejected -- §2b: position is 10.26% of equity, cap is 10.00%; §6b: reward:risk is 0.91, minimum is 2.0

**Dashboard's own "blocked by rule" data** (same underlying query the dashboard's Analysis panel uses): matches exactly -- 5 total blocked rows, 4 tagged §6b, 3 tagged §2b.

---

## 4. Hermes's Own Explanations

**Full `insight_log` dump for this run_id -- all 25 reflections, verbatim, in order (every `sample_size` is 0):**

**1.** `2026-08-29 10:54:11.424216+00:00` (sample_size=0)
> So far the right pattern is patience: I should prefer waiting for alignment across trend, momentum, and a nearby technical level rather than forcing entries from isolated signals. I should also keep treating news as a veto, not a reason to buy, and avoid upgrading confidence unless multiple independent signals truly line up.

**2.** `2026-08-29 10:59:53.174377+00:00` (sample_size=0)
> So far the useful pattern is restraint: I should prefer setups where trend, momentum, and location all point the same way instead of acting on a single strong signal. I should keep using news only as a veto, and I should not raise confidence unless several independent signals line up at once.

**3.** `2026-08-29 11:06:04.302878+00:00` (sample_size=0)
> The useful pattern so far is restraint: I should prefer setups where trend, momentum, and nearby technical location agree before acting. I should keep treating news strictly as a veto and avoid upgrading confidence unless several independent signals line up at once.

**4.** `2026-08-29 11:11:49.490399+00:00` (sample_size=0)
> The useful pattern so far is restraint: I should favor setups where trend, momentum, and location all agree before acting. A single strong signal is not enough, and I should keep using news only as a veto rather than letting it upgrade a weak setup.

**5.** `2026-08-29 11:18:24.408696+00:00` (sample_size=0)
> The useful pattern so far is restraint: one good-looking signal is not enough, and I should keep requiring trend, momentum, and location to agree before acting. News should stay a veto only; the main failure mode to avoid is forcing a trade when the setup is merely plausible instead of clearly aligned.

**6.** `2026-08-29 11:24:53.002194+00:00` (sample_size=0)
> The main process lesson is still restraint: a bullish trend plus bullish momentum is not enough if the entry is sitting awkwardly under resistance. I should keep treating news as a veto only and avoid upgrading a merely plausible setup into a trade just because it looks close to workable.

**7.** `2026-08-29 11:30:44.032146+00:00` (sample_size=0)
> The pattern so far is that a bullish trend or bullish momentum alone is not enough if price is pressing into nearby resistance. I should keep treating news as a veto only and avoid promoting a merely plausible setup into a trade unless trend, momentum, and location all agree clearly.

**8.** `2026-08-29 11:36:06.533154+00:00` (sample_size=0)
> So far, bullish trend or momentum alone has not been enough when price is already near resistance. The useful pattern is restraint: wait for trend, momentum, and location to line up clearly instead of promoting a merely plausible setup into a trade.

**9.** `2026-08-29 11:41:48.895174+00:00` (sample_size=0)
> Bullish trend or momentum by itself has not been enough when price is already close to resistance. The pattern so far is to wait for trend, momentum, and location to line up clearly instead of turning a merely plausible setup into a trade.

**10.** `2026-08-29 11:47:21.465401+00:00` (sample_size=0)
> The main pattern so far is that trend or momentum alone is not enough; entries need a clear location advantage, not just a plausible story. I should keep treating proximity to resistance as a real warning sign rather than trying to convert borderline setups into trades. With no closed trades yet, the right discipline is restraint and consistent filtering, not forcing a sample.

**11.** `2026-08-29 11:52:36.571747+00:00` (sample_size=0)
> Trend or momentum by itself has not been enough to justify entries. The pattern to keep respecting is location first: if price is already near resistance or extended, that is a warning sign rather than something to average away. Until trend, momentum, and room to resistance line up cleanly, passing is the disciplined choice.

**12.** `2026-08-29 11:58:25.284919+00:00` (sample_size=0)
> The main process lesson remains that trend or momentum alone is not enough; I should keep treating proximity to resistance as a real warning sign rather than trying to force borderline setups. With no closed trades yet, the right discipline is restraint and consistent filtering, not taking action to create activity.

**13.** `2026-08-29 12:04:08.177594+00:00` (sample_size=0)
> The pattern so far is that momentum or trend alone is not enough; location relative to resistance matters more than wanting exposure. I should keep filtering out extended setups and wait for names with clear room, a real technical stop, and a cleaner reward:risk alignment.

**14.** `2026-08-29 12:10:16.667050+00:00` (sample_size=0)
> The process lesson remains to treat proximity to resistance as a real warning, even when trend is favorable. Momentum or trend alone has not been enough to justify entries; the better discipline is waiting for clear room, a technical stop that makes sense, and reward/risk that is not forced.

**15.** `2026-08-29 12:16:17.079214+00:00` (sample_size=0)
> The main process check is still location: favorable trend or momentum alone has not been enough when price is sitting under nearby resistance. I should keep requiring clear room, a real technical invalidation level, and reward/risk that is not forced, even when the tape looks constructive.

**16.** `2026-08-29 12:22:17.898563+00:00` (sample_size=0)
> The main process lesson remains location: favorable trend or momentum has not been enough when price is pressed into nearby resistance. I should keep requiring clear room, a real technical invalidation level, and reward:risk that emerges naturally rather than being forced by optimism.

**17.** `2026-08-29 12:28:00.102047+00:00` (sample_size=0)
> So far the main process check is still location: trend and momentum are not enough when price is pressed under nearby resistance. I should keep requiring clear room, a real technical invalidation level, and reward/risk that appears naturally rather than being forced by optimism.

**18.** `2026-08-29 12:33:48.287590+00:00` (sample_size=0)
> Location still matters more than raw momentum or oversold readings. I should keep demanding clear room, a real invalidation level, and reward:risk that appears naturally; otherwise the setup is too easy to rationalize and too hard to manage.

**19.** `2026-08-29 12:40:06.060489+00:00` (sample_size=0)
> Location has remained the main filter so far: momentum and oversold readings alone do not justify a trade when price is pressed against nearby resistance. I should keep demanding a level that matters, a stop that is technically obvious, and reward/risk that emerges naturally rather than being stretched by optimism.

**20.** `2026-08-29 12:45:52.609442+00:00` (sample_size=0)
> Location still dominates the decision: momentum alone is not enough when price is already near resistance or the stop would have to sit in ordinary noise. I should keep requiring clear room, a technically obvious invalidation level, and reward:risk that emerges naturally instead of being forced.

**21.** `2026-08-29 12:51:25.775326+00:00` (sample_size=0)
> Entries that rely on momentum or oversold readings without clear room to resistance have been the weakest pattern so far. I should keep preferring trades where the invalidation level is obvious and the reward:risk emerges naturally; doing nothing is the right call when that structure is missing.

**22.** `2026-08-29 12:56:40.382901+00:00` (sample_size=0)
> So far the useful filter is location, not label: entries need clear space before resistance and a stop that sits at a genuinely meaningful level. Momentum or oversold readings without that structure are not enough, and doing nothing is the better decision when the setup has to be forced.

**23.** `2026-08-29 13:02:19.200553+00:00` (sample_size=0)
> The useful filter remains location, not label: momentum or bullish trend alone does not make a trade actionable when resistance is close. I should keep preferring setups where the invalidation level is obvious and the target can be reached without forcing the trade.

**24.** `2026-08-29 13:07:59.149044+00:00` (sample_size=0)
> The main filter so far is location, not label: a bearish or bullish reading is not enough if price is boxed in by nearby resistance. I should keep preferring setups with clear room to target and a stop at a level that actually proves the thesis wrong; if that structure is missing, doing nothing is the better decision.

**25.** `2026-08-29 13:13:35.248678+00:00` (sample_size=0)
> The main filter so far remains location, not label: trend or momentum alone is not enough when nearby resistance limits room to follow through. I should keep preferring setups where the stop proves the thesis wrong at a real level and the target is reachable without forcing the trade; otherwise, doing nothing is the correct decision.

**`trades` table for this run_id:** contains exactly **1 row** (SFM, shown in full in §2 above), status `rejected`. No trade ever reached `filled`. `strategy_notes`/`invalidation_condition` for that one row are included above -- there is no second row to report.

---

## 5. Data-Feed Health (Starvation Check)

- Cycles where technicals were unavailable for any of the 10 watchlist tickers (would show as fewer than 10 in "Evaluating N candidates"): **0 of 125**. Every single cycle evaluated exactly **10 of 10** candidates -- min 10, max 10, avg 10.0. Technicals never failed once across the entire run.
- Cycles where the news veto fail-closed (a genuine RSS/fetch failure, distinct from a real adverse-news veto): **0 detected** -- no `cycle_failed` rows exist for this run (see §1), and a news-fetch failure inside a cycle would have raised and halted that cycle rather than silently continuing.
- Cycles where the daily-bars/calendar check halted or skipped as an OUTAGE (vs. a genuine holiday): **0** -- again, zero `cycle_failed` rows for this run_id confirms no outage was ever hit; every calendar-ambiguous day (if any occurred) resolved as a genuine holiday/weekend, which skips silently by design and leaves no Neon row.
- **Average candidates shown to Hermes per cycle: 10.0 (min 10, max 10)** -- constant at 10 across the whole run. No feed starvation of any kind is visible in this data.

---

## 6. Context Quality

- **Full prompts:** not retained anywhere, local or in Neon, by design -- `run_backtest.py`'s `build_prompt` constructs the prompt fresh in memory each cycle and it is never written to disk or journaled; only the *response* (via decision/activity/insight rows) and usage numbers survive. Per instruction, this was **not regenerated** -- 5 sample prompts cannot be produced from this run's actual history.
- **Tokens per cycle over time** (from `portfolio_snapshots.total_tokens`):

| Segment | Avg total tokens |
|---|---|
| cycles 1-25 | 20,720 |
| cycles 26-50 | 20,328 |
| cycles 51-75 | 20,332 |
| cycles 76-100 | 20,394 |
| cycles 101-125 | 20,547 |

Overall: min 19,861, max 23,138, avg 20,464. **Flat across the run** -- the 5 segment averages differ by at most ~2%, no meaningful drift up or down. Reasoning-token spend (a rougher proxy for "how hard did it think this cycle") ranged 45-3106 (avg 453) -- the widest swings correspond to the reflection cycles (every 5th), which ask for extra synthesis, not to any shift in general behavior. No evidence of the model's output shrinking, growing, or otherwise changing character partway through the window.

---

## SQL / method used

All queries run inside the `trade-journal` container against Neon, `SELECT`-only, scoped to `run_id = '26f1d371-08db-4885-ab24-b42200792fb7'`:

```sql
SELECT event_type, description, created_at FROM agent_activity
  WHERE run_id = $1 ORDER BY created_at;

SELECT decision_id, ticker, action, status, confidence, primary_signal, technical_basis,
       expected_reward_risk, invalidation_condition, strategy_notes, stop_price, target_price,
       intended_quantity, intended_notional, fill_price, fill_quantity, rejection_reason,
       created_at, updated_at
  FROM trades WHERE run_id = $1 ORDER BY created_at;

SELECT cycle_type, insight_text, sample_size, created_at FROM insight_log
  WHERE run_id = $1 ORDER BY created_at;

SELECT day_label, equity, cash, drawdown_pct, open_positions, total_tokens, input_tokens,
       output_tokens, reasoning_tokens, cache_read_tokens, api_calls, hermes_calls_used,
       weekly_budget, created_at
  FROM portfolio_snapshots WHERE run_id = $1 ORDER BY created_at;
```

No `INSERT`/`UPDATE`/`DELETE` statement was run against Neon. No local state file was modified. This report file is the only write this extraction performed.
