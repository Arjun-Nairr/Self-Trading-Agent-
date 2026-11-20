# Laya, schema 5, July 2022 v3: decision review (2026-11-09)

Evidence review of run `c3fe5944-3bab-4956-82da-c1bb144be333` (commit `522754c`, config `laya-schema5-2022-07-v3`).
Read-only: no backtest, no Hermes call, no trace, code, record or policy change.

**Sources**
- [v3 run report](laya_schema5_2022_07_v3_run_2026-11-09.md).
- **Hosted Langfuse:** 21 traces, 477 observations. Re-pulled for this review; byte-identical to the post-run pull.
- **Trade journal:** the run's 15 `trades` rows (SELECT only).
- **Local spool (labelled _local_):** the final `journal.activity`, `observability` and `backtest_episode_end` records.
  They are still unsent.
- **Run directory:** `result.json`.
- **Notation:**
  - **Trace IDs:** first 8 hex characters.
  - **Journal rows:** first 8 characters of the `decision_id`.
  - **S1 / R1:** the only support and resistance levels the prompt supplies, the 30-session low and high.
  - **"Checked-empty":** the news line "No headlines found in the checked window … This is not proof that no adverse
    news exists."

Judgments use only what Hermes had at decision time. Later prices describe outcomes only. They come solely from closes
of names that reappeared in a later shortlist, since no new data was fetched.

## The three highest-impact findings

### 1. Three of the four fill-time rejections were trade geometry, not size. Only one was size-only — *expected enforcement*

Rejections were recomputed from the fill-time `risk_check` spans (the journal rows give the same reasons). The check
prices a BUY at the 09:45 bar open + 5 bps against the unchanged stop and target. The risk-check price below is the
unique cent that reproduces every logged percentage. For KG71 it is pinned only to $215.11–$215.22, and the midpoint
is shown.

| Order (journal row, span) | Decision → risk-check price | Overnight gap | Qty | Stop distance | R:R | Risk vs budget | Failed | Max qty passing §2a | Size-only? |
|---|---|---|---|---|---|---|---|---|---|
| DB72 07-07→07-08 (`5d37c042`, `757bfcf5`) | 61.47 → 62.37 | +1.4% | 279 | 11.66% → **12.94%** | 1.75 → **1.45** | $2,252 / $2,010 | §6a §2a §6b | 249 | **No** |
| KG71 07-19→07-20 (`f2f10329`, `4a30244f`) | 211.52 → ≈215.17 | +1.7% | 90 | 10.53% → 12.05% (passes in the 0.10-pt tolerance) | 2.17 → 1.72 | $2,333 / $2,029 | §2a only | **78** | **Yes** |
| DB72 07-22→07-25 (`6903dbd8`, `d830f9f1`) | 61.06 → 62.34 | +2.1% | 295 | 11.07% → **12.90%** | 1.50 → **1.10** | $2,372 / $2,019 | §6a §2a §6b | 251 | **No** |
| TV77 07-28→07-29 (`c6bc769c`, `62f35fd8`) | 67.70 → 68.35 | +0.9% | 250 | 11.98% → **12.82%** | 1.50 → **1.32** | $2,190 / $2,037 | §6a §2a §6b | 232 | **No** |

**What the arithmetic shows**
- **Only KG71 could be rescued by size.** At 78 shares (−13%) it passes every rule: risk 1.99%, stop 12.05%, R:R
  1.72, position 16.5% of equity.
- **The other three fail §6a and §6b at any quantity.**
- **Why the gap forced the 13% cut on KG71:** a 1.7% gap raised its risk per share from $22.38 to $25.92 (+16%), because
  the gap is divided by a 10.5% stop distance.

**Why every order was this fragile.** For each of the 10 BUYs, this is how far the 09:45 price could rise above the
decision price before the binding rule failed. The expected fill alone adds 0.04–0.07%.

| BUY | Binding rule headroom | Outcome |
|---|---|---|
| JR99 07-01 | §6b +0.08% | filled after a lower open |
| DB72 07-07 | §2a +0.07% | rejected (opened higher) |
| JR99 07-12 | §2a +0.08% | filled after a lower open |
| JG75 07-15 | §6b +0.06% | news veto |
| KG71 07-19 | §2a +0.12% | rejected (opened higher) |
| KP91 07-19 | §2a +0.07% | filled after a lower open |
| RW80 07-19 | §2a +0.06% | filled after a lower open |
| DB72 07-22 | §2a +0.07% (also §6b +0.09%) | rejected (opened higher) |
| TX33 07-22 | §6b +0.07% (also §2b +0.10%) | filled after a lower open |
| TV77 07-28 | §2a +0.09% (also §6a +0.14%) | rejected (opened higher) |

- In practice, an order filled only if the stock opened at or below the prior close.
  - All 5 fills opened lower: −0.4% to −2.1%.
  - All 4 rejections opened higher: +0.9% to +2.1%.
- **Three design choices create this:**
  1. **Controller policy:** sizing is set exactly at the §2a/§2b maximum.
  2. **Controller policy:** the prompt offers one support level (S1) and requires the stop to equal it. That puts stops
     at the 30-session low, a median of 10.1% below price across the 200 candidate rows, close to the 12.10% limit.
  3. **Hermes judgment:** in 6 of the 10 BUYs, Hermes set a derived target at exactly 1.5:1.
- **Consequence:** 4 of 9 queued entries were lost, and the fills that happened were selected by overnight direction.
- **Correction to the v3 run report:** its finding 1 framed this as a sizing problem, but sizing explains only one of
  the four. Its finding 2 called the near-ceiling stops Hermes' choice; they are mostly the S1-only evidence design.

### 2. "No headlines found" works as an entry veto that the controller does not intend — *controller policy*

**Where the conflict comes from**
- Schema 5 rejects a BUY only when news is *unavailable*. Of the 200 candidate rows, 0 were sent as unavailable and 63
  as checked-empty.
- RULES §5a says "Missing/unavailable news is treated as an unevaluated veto check on entry." The controller's own
  checked-empty line adds "This is not proof that no adverse news exists."
- Nothing in the prompt says checked-empty news clears the veto.

**How Hermes applied it**
- Hermes read checked-empty as a veto every time: no BUY was ever proposed on a checked-empty name.
- It often called those names "unavailable" (07-05, 07-07, 07-11, 07-19, 07-22, 07-27, 07-28).
- The 07-08 reflection (`8a47b5c4`, `journal.reflection`) carried the reading forward to later prompts: "Missing news
  must continue to block entries."

**Consequence**
- **15 candidate-days across 8 aliases** otherwise matched the profile of Hermes' own BUYs: bullish RSI and MACD, a trend
  that was not bearish, and S1 inside the stop band. The news line was the only obstacle.
- Hermes said so explicitly on 07-12 (WG86), 07-13 (MP36), 07-18 (RG57) and 07-26 (RW80).
- **3 of them also cleared 1.5:1 against observed resistance:** RG57 07-18 (1.63), CG64 07-21 (1.53) and RW80 07-26
  (2.23).
- Whether these entries would have helped is unassessable. The point is that a third of every shortlist was unbuyable
  through a rule reading the controller does not apply.

### 3. Hermes applied two different target standards, and the validation retry switched between them — *Hermes judgment*

The prompt allows a target that is "an observed resistance (R#) or derived from your reward:risk," and Hermes used both
inconsistently.

- **Derived targets (6 of 10 BUYs):** JR99 ×2, JG75 (after retry), DB72 07-22, TX33 and TV77 used derived targets. R1
  supported only 0.00–1.15:1 for these. TV77 was proposed at its 30-session high (0.00:1).
- **Observed-only rejections:** on other days Hermes rejected setups of the same type because "observed resistance does
  not meet 1.5:1":
  - 07-21: DG41 (1.13) and TX33 (0.63);
  - 07-26: CB88 (0.33) and CG62 (0.20);
  - 07-27: CG64 (0.97).
- **TX33 got both treatments:** rejected on 07-21 at 0.63:1, then bought on 07-22 at 0.33:1 with a derived target.

**The JG75 retry (07-15, `7d92ed1b`)**
- **First reply** (`hermes_call_1` `39d367fa`): it claimed "observed resistance at $34.89 provides 1.52:1."
  - The real ratio is (34.89 − 33.68) / (33.68 − 31.32) = 1.21 / 2.36 = **0.51**.
  - 1.52 is close to (34.89 − 31.32) / 2.36 = 1.51, so Hermes probably measured reward from the stop instead of the
    entry.
- **Validation** (`response_validation` `2b516ebc`, ERROR) rejected the reply.
- **Retry** (`hermes_call_2` `4860e1ee`) kept the entry, stop and confidence 3. It replaced the target with a derived
  $37.22 (= 33.68 + 1.5 × 2.36), shortened the reasoning to one sentence and passed.
- On the observed standard Hermes applied elsewhere, the corrected setup should have been skipped.

**Consequence**
- Entry selection depended on which standard Hermes used that day.
- The derived targets produced the near-zero §6b headroom in finding 1.
- The controller's check verifies only the arithmetic, not whether the basis is consistent.

## Scorecard: all 20 sessions

| Date (trace) | Decision or no-trade reason | Evidence Hermes received | Outcome | Verdict |
|---|---|---|---|---|
| 07-01 (`da7b18f7`) | BUY JR99 30.72, stop S1 27.75 (9.67%), derived target 35.18 | All four signals bullish, the only fully aligned name; R1 33.95 (1.09:1); 1 generic headline | Filled 07-05 at 30.07 (650); time stop 07-12 at 31.24, +$760.50 | Setup supported; target is arithmetic above the 30-session high (permitted) |
| 07-05 (`7e9b7f0f`) | No entry; hold JR99 | 4 checked-empty names, called "unavailable"; BR44's "adverse" headline is about a different company | — | Passes right on technicals (the 4 news-cited names also had out-of-band stops); reasons partly mis-stated |
| 07-06 (`c6d062e1`) | No entry | Only HC52 aligned, S1 15.4% below; the rest bearish | — | Correct, concrete |
| 07-07 (`0595d9cf`) | BUY DB72 61.47, S1 54.30 (11.66%), R1 74.04 (1.75:1 observed) | RSI and MACD bullish, trend neutral; 3 non-adverse headlines | Rejected 07-08 (finding 1) | Best-supported target of the month, but proposed with 0.50% §6a headroom |
| 07-08 (`8a47b5c4`) | No entry; reflection; pre-fill CONFIRM DB72 | SV40 aligned but checked-empty, R1 0.05:1; DB72 overnight headlines positive | DB72 rejected at the fill | CONFIRM correct; reflection hardened the news veto (finding 2) |
| 07-11 (`15d2759b`) | SELL JR99, "held 6 trading days" | Prompt said "held 6d" (calendar days; 4 trading days since entry) | Superseded: the controller's time stop closed it at the 07-12 open | Right action, wrong count; no effect |
| 07-12 (`ef707aad`) | BUY JR99 31.52, S1 27.75 (11.96%), derived 37.18 (R1 0.64:1); WG86 passed on news only | Aligned; favourable analyst headline | Filled 07-13 at 31.27 (531) | Setup supported; target arithmetic; WG86 pass is finding 2 |
| 07-13 (`6a506dab`) | No entry; MP36 passed on news only | MP36 aligned, S1 11.8% below | — | Pass caused by the news policy |
| 07-14 (`4b398237`) | No entry | BL40/NS43 stops too close, NT73 too far, RH50 R:R fails; MP36/XB32 checked-empty | — | Correct, concrete (MP36 again finding 2) |
| 07-15 (`7d92ed1b`) | BUY JG75 33.68, S1 31.32 (7.0%); retry derived 37.22; reflection | RSI and MACD bullish, trend neutral; one unrelated headline | Queued 601 (capped at 20% of equity, §2b); cancelled 07-18 | Arithmetic error caught; retry switched standard (finding 3) |
| 07-18 (`37df2133`) | News veto JG75; SELL JR99 citing the 5-day time stop | Downgrade headline on JG75; JR99 "held 5d" = 3 trading days, all signals still bullish | SELL filled 07-19 at 32.48, +$642.51, one session before §6c would have fired (07-20) | Veto sound; the exit was not justified by its own stated rule (see other findings) |
| 07-19 (`dcfb7126`) | BUY KG71, KP91, RW80 with observed targets (2.17 / 2.59 / 2.55:1) | RSI and MACD bullish, trend neutral; neutral or positive headlines; CG64 checked-empty | KP91 and RW80 filled 07-20 after lower opens; KG71 rejected, size-only | Best-evidenced entries of the month |
| 07-20 (`96632989`) | No entry; hold RW80 and KP91 | CG96/NV28 stops out of band; DB51 checked-empty | — | Correct (DB51 is finding 2) |
| 07-21 (`31f13b55`) | No entry; TX33 rejected for R1 0.63:1; CG64 and DB37 passed on news | CG64 met every rule, including observed R:R 1.53 | — | CG64 pass is finding 2; TX33 contradicts the next day's entry (finding 3) |
| 07-22 (`e8f854d2`) | SELL RW80 and KP91; BUY DB72 (derived 71.20) and TX33 (derived 41.40); reflection | Both holdings' trend turned bearish, matching their stated invalidation condition | Sold 07-25: RW80 44.19 (−$451.26), KP91 86.76 (−$80.77); TX33 filled at 36.31; DB72 rejected | Exits followed their stated rule; entries had about 0.1% headroom |
| 07-25 (`f7bdec1a`) | No entry; hold TX33 | Supports out of band; SP56 R:R; MX45/SV40/XX26 checked-empty | — | Correct (SV40 is finding 2) |
| 07-26 (`eb458d89`) | No entry; RW80 passed on news only | RW80 aligned, observed R:R 2.23; CB88/CG62/SV40 rejected for R1 | — | RW80 is finding 2; the R1 rejections are finding 3 |
| 07-27 (`5cae746b`) | No entry | CG64 merger-approval headline (binary-event exclusion) and R:R 0.97; supports out of band | — | Correct, concrete |
| 07-28 (`6eedba71`) | BUY TV77 67.70, S1 59.59 (11.98%), derived 79.87 at R1 | RSI and MACD bullish, trend neutral; positive corporate headlines | Rejected 07-29 (finding 1) | Proposed with 0.14% §6a headroom; would fill only after a lower open |
| 07-29 (`085b8968`) | No entry; hold TX33; reflection | Supports out of band; checked-empty names; TX33 not at stop, target or time stop | Episode end (_local_ `31e42b86`): no pending orders, TX33 kept | Correct |

## Other findings

- **Time-stop label (*observability*).**
  - The prompt's "held Nd" is a calendar-day difference (`run_backtest.py:5764`), while §6c counts trading days.
  - On 07-18 Hermes read "held 5d" as the 5-trading-day stop and sold JR99 one session early, with its thesis intact
    (journal `874d3a55`).
  - The 07-11 SELL made the same mistake but was harmlessly superseded (`c6e06d98`).
  - Whether the early exit helped or hurt is unassessable: no JR99 price after 07-19 is in the evidence.
- **Exits and veto (*expected enforcement*).**
  - **Time stop:** the JR99 time stop (`position_exit` `4e0b4021`) was mechanical and correct.
  - **RW80 and KP91 exits:** both met their stated invalidation condition ("neutral trend turns bearish").
  - **What happened after those exits:** RW80 later closed at 45.65–47.50 and KP91 at 89.98 (07-27), both above their
    entries. Two cases establish nothing.
  - **JG75 veto:** it matched a company-specific downgrade, consistent with §5a. JG75 later closed at 34.95 (07-21)
    against the 33.68 decision price.
  - **The other four pre-fill CONFIRMs:** all were on non-adverse headlines and were correct.
- **News claims.** Hermes' cited headlines match the supplied lines, with one exception. On 07-05 it called a headline
  about another company "adverse" for BR44, which did not change the outcome. No claims went beyond the supplied news.
- **Identity cues past the guard (*observability*).**
  - **TV77:** its 07-28 decision prompt (`6eedba71`) carried a headline naming the company's sitting CEO.
  - **DB72:** its 07-08 pre-fill prompt (`8a47b5c4`) described a widely reported stake purchase in that company.
  - The guard checks only for a company's own name words next to its alias, so personal names and signature events
    pass. There are no company names or tickers in the hosted data.
  - Whether these cues influenced decisions is unassessable. Both traces are left untouched.
- **Release label (*observability*).** Spans carry `522754c+dirty` because of uncommitted docs. The experiment code was
  clean, as the runner requires, but the label cannot show that.

## Time

- **Wall time:** 3,854 s, of which day traces account for 3,590 s.
- **Where it went:**

  | Stage | Time | Share | Detail |
  |---|---|---|---|
  | Laya selection | 1,294 s | 36% | history fetch 531 s + inference 758 s |
  | Controller data fetches | 1,233 s | 34% | daily bars 335, technicals 375, news 216, insights 95, calendar 85, intraday 80, other 46 |
  | Hermes | 759 s | 21% | 25 calls at about 30 s each; the JG75 retry cost 30 s |

- **Largest avoidable delays, as far as the spans support them:**
  1. **Fixed per-call cost.** 173 small calls (journal writes, the calendar, insights, intraday bars, the news window,
     simulated fills) took 791 s, 21% of wall time. They averaged 3.5–5.9 s regardless of payload. The cause is not
     visible in the spans.
  2. **Daily calendar fetch.** The calendar was fetched once per day (21 × 4.1 s) for a month that is known in advance,
     about 80 s.
  3. **Possible repeat history fetches.** Daily bars (16 s) and the Laya history (26.5 s) are both fetched for 46–48
     symbols every session. The spans don't show whether the history windows overlap, so the saving is unquantified.
- All of these are controller costs, not Hermes.

## Context (not evidence of edge)

- **Return:** +2.36%, equity $102,361.48.
  - Realized +$870.98 (+0.87%) from 4 closed trades: JR99 +760.50 and +642.51, RW80 −451.26, KP91 −80.77.
  - The open TX33 position contributes +$1,490.50 (+1.49%): 550 shares marked at $39.02 against a $36.31 entry. It is
    not realized.
- **Maximum drawdown:** −1.57%, from the 07-20 close ($101,951.82) to the 07-22 close ($100,354.03). It came from the
  RW80 and KP91 open losses, which were realized on 07-25.
- **Escalation:** the 1.25:1 escalation triggered only after the final session (`profile_level` escalated). No order
  used it.
- **Limits:** one month, four closed trades, no benchmark, no matched control.
- **Laya:** Laya's shortlist is treated here as the set Hermes received. Crediting Laya with better selection would need
  a matched control arm. A third of its shortlist slots carried no headlines (finding 2), which interacts with any
  selection comparison.

## Sizing recommendation: test a resize at the open (down only, quantity only)

**What the arithmetic establishes**
- **A resize would have rescued one order.** Recomputing the quantity at the 09:45 risk-check price with the same
  §2a/§2b/§3 formula would have filled KG71 at 78 shares instead of rejecting it.
- **The other three rejections would stand.** It would not have changed DB72 ×2 or TV77, whose stops and targets fail
  at any size. Stop, target and every rule stay as they are, so §6a/§6b failures still reject.
- **A buffer is worse.** To rescue KG71, a buffer would have to cut every order by at least 13.3%. That includes the 5
  fills, none of which needed it, since all opened lower. A smaller buffer rescues nothing: at a 10% stop, a 5% buffer
  absorbs only about a 0.5% gap.
- **Keeping the current policy** means losing every size-only order whose stock opens higher. That was 1 of 9 queued
  BUYs here.
- **The resize is narrower than the 2026-08-28 design note assumed.** The `resolve_queued_order` docstring forbids
  resizing because the quantity used to be Hermes' choice. Under schema 5 the controller owns the quantity, so a resize
  replaces no Hermes judgment. Changing it still needs your explicit authorization; nothing was changed here.

**What a future run must measure**
- How often size-only rejections occur over more months (1 in 9 here).
- How rescued fills perform against the orders that filled after lower opens.
- Whether selecting fills by overnight direction biases results.
- This needs several months and a matched comparison, not this month.
- The larger lever, 3 of 4 rejections, is geometry. Changing that is a rules or prompt decision (S1-only stops,
  derived targets at exactly 1.5:1), not a sizing change.

**Smallest test that settles the mechanics**
- A unit test on `resolve_queued_order` with this month's four recorded cases:
  - KG71 (P 215.17, S 189.25, T 259.87, E 101,403) → fills at 78 shares;
  - DB72 ×2 and TV77 → still rejected.
- Then a deterministic offline replay of all 9 recorded queued BUYs against their real 09:45 bars, with no Hermes
  calls.

## Changes the evidence supports (none made)

1. **Clarify checked-empty news (finding 2).** A RULES and prompt decision for you: either checked-empty clears the §5a
   check or it doesn't. Then make the controller and the prompt say the same thing.
2. **Show "held" in trading days**, or label it as calendar days, so it matches §6c.
3. **Test a resize at the open**, as above.
