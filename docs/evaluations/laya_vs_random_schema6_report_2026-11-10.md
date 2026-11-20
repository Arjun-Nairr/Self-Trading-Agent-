# Laya vs. seeded random, prompt schema 6: three-month report (2026-11-10)

**Exploratory.** There is one draw per arm per month, on a 48-name pool that was picked in hindsight. Nothing here
establishes selector value or a trading edge. Plan and frozen settings:
[laya_vs_random_schema6_plan_2026-11-10.md](laya_vs_random_schema6_plan_2026-11-10.md).

## Integrity (all six arms)

- **Completion:** every arm completed with no halt, at code `614615a`. Each pair's fingerprints are identical, with the
  same pool, masking seed 42, k = 10, Hermes, schema 6 and fundamentals off. The random arms drew daily from the
  eligible pool using seed + date.
- **Journal:**
  - SELL fills equal closed trades, and open BUYs equal holdings.
  - 0 pending rows.
  - Each of the two end-of-window pending BUYs (Sept) got a rejected journal row.
- **Hosted traces:**
  - 0 errors, 0 missing parents and 0 real-name leaks.
  - No credentials in the payloads.
  - One `backtest_day` root per session, plus a skip trace for holidays.
- **Protected state:** before/after snapshots show only each run's own DB rows and arm directory changed.
  `backtest_state.json`, RULES/AGENTS/SOUL, risk-checker, the watchlist and docker-compose are unchanged.
  0 Laya containers are left over.
- **Pool coverage exception:** Sept 2022 and Oct 2024 ran under the recorded exception (15 and 7 names below the
  news-coverage rule). It is copied into each arm's `ARM.json` and `result.json`. Daily news checks stayed in force.

## Summary

| Month | Arm | Run ID | Sessions | Return | Max DD | Realized | Ending equity |
|---|---|---|---|---|---|---|---|
| Jul 2022 | Laya | `900b4ab5` | 20 | **+3.80%** | −0.70% | +1,940.68 | 103,795.96 |
| Jul 2022 | Random | `8833221c` | 20 | +0.04% | −0.95% | −69.84 | 100,040.06 |
| Sep 2022 | Laya | `da93baa5` | 21 | **−0.26%** | −2.52% | −264.63 | 99,735.37 |
| Sep 2022 | Random | `12b86703` | 21 | −2.40% | −2.40% | −2,396.73 | 97,603.27 |
| Oct 2024 | Laya | `5830efb4` | 23 | −1.46% | −1.73% | −1,459.57 | 98,540.43 |
| Oct 2024 | Random | `bcb712dc` | 23 | **+0.78%** | −1.39% | +622.95 | 100,780.45 |

Laya finished ahead in July and September, and random in October. Ending equity includes open positions marked at
the last close:
- **July:** Laya held SV40 and WG86; random held WG86 and CR24.
- **October:** random held DB51.
- **September:** both arms ended flat.

## Per month

### July 2022

| | Laya | Random |
|---|---|---|
| BUY / SELL proposals | 17 / 1 | 13 / 2 |
| BUY / SELL fills · mechanical exits | 7 / 1 · 4 | 6 / 2 · 2 |
| Closed trades (wins) | 5 (4) | 4 (2) |
| BUY rejections (all at fill time) | 10: §6a stop too wide 4; §2b+§6b 4; §2b+§2a(+§6b) 2 | 7: §2a+§6b 3; §6a 2; §2b+§2a+§6b 1; §2b+§6b 1 |
| Checked-empty news, candidates | 59 / 200 (29.5%) | 54 / 200 (27.0%) |
| Checked-empty news, open positions | 12 / 29 | 9 / 23 |

### September 2022 (coverage exception: 15 names)

| | Laya | Random |
|---|---|---|
| BUY / SELL proposals | 14 / 3 | 11 / 4 |
| BUY / SELL fills · mechanical exits | 4 / 2 · 2 | 4 / 4 · 0 |
| Closed trades (wins) | 4 (2) | 4 (1) |
| Rejections | 11: §2b+§6b 7; §2b 1; pre-fill news veto 1; SELL superseded (already exited at open) 1; window ended (KP91) 1 | 7: §2a+§6b 2; §2b+§6b 2; pre-fill news veto 2; window ended (RW80) 1 |
| Checked-empty news, candidates | 74 / 210 (35.2%) | 88 / 210 (41.9%) |
| Checked-empty news, open positions | 8 / 15 | 6 / 10 |
| Shortlist slots on exception names | 64 / 210 (30.5%) | 70 / 210 (33.3%) |

### October 2024 (coverage exception: 7 names)

| | Laya | Random |
|---|---|---|
| BUY / SELL proposals | 11 / 5 | 14 / 2 |
| BUY / SELL fills · mechanical exits | 6 / 5 · 1 | 5 / 2 · 2 |
| Closed trades (wins) | 6 (2) | 4 (1, plus 1 flat) |
| BUY rejections | 5: §2a+§6b 2; §6a+§2a 1; §6b 1; §2b+§6b 1 | 9: §2b+§6b 4; §6b 2; pre-fill news veto 1; §2b+§2a+§6b 1; §6a+§2a 1 |
| Checked-empty news, candidates | 69 / 230 (30.0%) | 62 / 230 (27.0%) |
| Checked-empty news, open positions | 5 / 18 | 10 / 19 |
| Shortlist slots on exception names | 30 / 230 (13.0%) | 26 / 230 (11.3%) |

The checked-empty counts come from the first decision prompt of each session, one count per name. No session showed
a failed or incomplete news fetch.

## Shortlist and decision differences (alias-only traces)

- **Overlap is low, as expected.** The shortlists shared a mean of 1.9 names per day in July, 1.67 in September and
  2.04 in October, out of 10. A random-vs-random pair would share about 2.1. Laya's lists were more repetitive: in
  July its top names appeared 11 times each, against at most 9 for random.
- **Hermes judges the same name the same way in both arms.** When both lists contained the setup, both arms picked
  it.
  - Jul 8: SV40 was on both lists. Both arms called it the only aligned long and proposed it.
  - Oct 2: KW95, NT24, WV31 and XT32 were on both lists. Both arms bought KW95, vetoed NT24 on a downgrade
    headline and passed on WV31 for bearish MACD and too little reward to resistance.
- **The differences come from which names reach Hermes.**
  - Jul 1: Laya surfaced JR99. Hermes proposed it, and it was filled twice that month. Random's list that day had no
    setup Hermes would take, so it made no proposal.
  - Sep 13: Laya's list had no new long, and Hermes held its TX33 and NN86 positions. Random's list contained TX33, the
    name Laya had bought on Sep 9, which random then bought itself. A single name reaching Hermes four days apart
    is the kind of timing difference that drives these P&L gaps.
- **Rejections are mostly at fill time.** Both arms failed mainly on fill-time sizing and reward:risk geometry (§2a,
  §2b, §6b, all recomputed at the 09:45 fill). This is the pattern flagged in the v3 decision review. Schema 6 did not
  change it, and the selector has no influence on it.

## Model calls, tokens and time

The controller makes no LLM calls. All tokens are Hermes (`gpt-5.6-terra`, medium). Laya inference is a local model
that has no token cost.

| Month | Arm | Hermes calls | Tokens (in / out / reasoning) | Wall | Hermes | Selection | Other controller |
|---|---|---|---|---|---|---|---|
| Jul | Laya | 24 | 207,073 (173,740 / 15,413 / 9,682) | 3,812 s | 852 s | 1,136 s | 1,567 s |
| Jul | Random | 23 | 202,844 (181,006 / 12,878 / 7,601) | 3,055 s | 752 s | 499 s | 1,549 s |
| Sep | Laya | 23 | 208,401 (196,205 / 12,196 / 6,972) | 3,542 s | 666 s | 1,164 s | 1,459 s |
| Sep | Random | 24 | 207,626 (197,081 / 10,545 / 5,850) | 2,982 s | 674 s | 526 s | 1,520 s |
| Oct | Laya | 24 | 231,411 (215,512 / 15,899 / 10,202) | 4,328 s | 884 s | 1,411 s | 1,731 s |
| Oct | Random | 26 | 234,507 (219,141 / 15,366 / 9,492) | 3,263 s | 802 s | 541 s | 1,649 s |

Hosted generation usage equals the ledger in every arm. Laya's selection cost about 640–870 s more per month than the
random draw (container start, history fetch and scoring). Hermes cost is about the same in both arms.

## Trace records: hosted vs. local spool

| Arm | Hosted at verification (observations / traces) | Spooled locally at exit | Status now |
|---|---|---|---|
| Jul Laya | 512 / 21 | 3 | delivered |
| Jul Random | 469 / 21 | 3 | delivered |
| Sep Laya | 504 / 22 | 2 | delivered |
| Sep Random | 466 / 23 | 2 | delivered |
| Oct Laya | 533 / 23 | 3 | delivered |
| Oct Random | 519 / 23 | 3 | **still local**: delivered on the next traced run |

## Limits

- **One draw per arm per month.** A single random seed and a single Hermes sampling path cannot separate selector
  effect from luck. The Laya-minus-random return gaps (+3.8, +2.1, −2.2 points) come from a handful of trades each.
- **The pool is hindsight-selected.** The 48 names were chosen with knowledge of the period, which flatters both
  arms equally but not neutrally.
- **News coverage is thinner in Sept and Oct.** Under the exception, names below the rule mostly show as "checked,
  0 headlines" (about 31% and 15% of the pool), which clears the news gate. Checked-empty rates were similar
  between arms, except Sept random (+6.7 points).
- **Fill-time geometry dominates outcomes.** Most rejected BUYs failed at the open-price recheck, not on selection,
  so selector quality is only partly visible in P&L.
- **Tracing:**
  - The random arm's selection span is named `selection.laya`; only its `mode` field is `random_control_experimental`.
  - The trace `release` reads `614615a+dirty` because of unrelated user-owned working-tree files outside the harness.
    The run fingerprints pin `614615a`.
- **Out of scope:** the full-pool test, ML adviser and live-paper work.
