# Connected Laya → Hermes smoke run — 60-candidate frozen pool, 2026-10-03

**Verdict: GREEN.** The connected experimental pipeline behaved correctly over
one five-session smoke episode (2026-09-28 .. 10-02), end to end:

> 60 candidates → coverage-qualified frozen pool (51) → held/pending exclusion
> → point-in-time cards → one Laya batch per day → top-10 shortlist → full
> technical/news packets → masked Hermes judgment → risk checks → simulated
> execution → persisted evidence.

The fill path was exercised: 3 BUY fills, plus 1 mechanical target exit. The
fill-time risk-rejection path was also exercised: 3 queued BUYs were
rejected.

GREEN says nothing about profitability, selector quality, or Laya's
usefulness. The equity figures below are operational, from one week.

Earlier attempt: `docs/evaluations/laya_connected_smoke_2026-10-03.md`
(RED, not launched, because the fixed-30 universe failed coverage in every
window). That report is preserved unchanged.

---

## 1. What changed in this block

| Change | Where | Why |
|---|---|---|
| 60-company candidate config `candidates60-v1` | `watchlist/experimental_candidates60.json` | approved population; fixed-30 and fixed-10 untouched |
| Coverage qualification and **frozen pools** | `laya_preflight.py --universe … --freeze`, `build_pool`, `write_pool`, `load_frozen_pool` | qualify each candidate; freeze only the passing subset |
| `--selector-universe <pool>` | `run_backtest.py` | a run uses exactly a verified frozen pool for masking, prompt packaging, risk membership and selection; resume reuses it |
| Packaging label `frozen-universe-v2` | `run_backtest.py` | was `fixed30-universe-v1`; same text, the label was misleading for other sizes |
| Batch comment fix | `laya_adapter.py` | "32 ≥ universe size" was false for a 51-name pool |
| Worker image rebuilt | `hermes-laya-worker:2` → `sha256:d423bbf7…` | only `laya_adapter.py`'s comment changed; rebuilt for source parity, dependency layers cached; the three modules inside match the repository by SHA-256 |

**Unchanged:**
- the pinned package (laya 0.3.23), checkpoint (`55cf4c4e…`, weights
  `891102d3…`), question format `noul-criteria-v2`, and the top-10 cap;
- the selector-off path;
- the mode name `laya_fixed30_experimental`, kept for compatibility (its
  help text now describes pools);
- the legacy fixed-30 behaviour when `--selector-universe` is omitted.

**Frozen-pool guarantees** (all tested in `test_frozen_pool.py`, 13 tests):
- **Evidence:** every candidate gets a record (qualified/excluded, specific
  reasons, point-in-time warm-up bars, retrospective in-window bars, distinct
  per-probe and total news counts). A failing name is excluded and does not
  block the others.
- **Refusals:** freezing is refused below 20 names, and when fetch failures
  or unverifiable coverage hit more than 10% of candidates, so a
  provider-wide failure never becomes a pool.
- **Write-once.**
- **Re-verification** by `load_frozen_pool` on every load. It rejects a
  tampered content or evidence hash, a changed parent config, a different
  coverage policy, membership that is not exactly the parent's qualified
  names, a status that disagrees with its reasons, and fewer than 20
  members.
- **Runner:** it refuses a tampered pool. A resume reuses the stored pool
  path and rejects a different one; a legacy fixed-30 run cannot switch. It
  never re-screens.

**Worker batching (real `:2` worker, 60 synthetic cards, 2 deliberately
incomplete):**
- the 58 scored cards span internal batches of 32;
- batch scores equal per-card scores (0 of 58 differ);
- a repeat batch is identical, and shuffled input gives identical mapping
  and selection;
- the incomplete cards were never sent;
- 2 tied rows were resolved by the seeded tie-break;
- warm 58-card batch: 36.7 s.

## 2. Candidate selection (recorded before any check or run)

- **Composition:** all 30 `fixed30-v1` names, plus exactly 3 additions per
  sector (6 per sector).
- **Picked for:** liquidity, sector spread and plausible provider coverage,
  from long-listed US common stocks.
- **Excluded a priori:**
  - biotech and recent IPOs;
  - tickers that are ordinary words (DOW, MOS, KEYS, HAL);
  - names with a known pending take-private, breakup or merger (EA, KHC,
    KDP, IPG).
- **Factual check:** a read-only yfinance check through maverick-mcp
  confirmed each addition is a listed EQUITY on NYSE/Nasdaq with the stated
  sector, 1,255 daily bars over 5 years, and ≥ 2.2M average daily volume.

No returns, Laya scores or outcomes were used. Several additions exceed the
baseline §8 mid-cap/volume bands; the 60 are not §8-screened.

This is a fixed-universe prototype chosen with present-day knowledge. It is
survivorship-biased and not historically representative.

**Additions:**

| Sector | Added |
|---|---|
| Basic Materials | FCX, ALB, LYB |
| Communication Services | TTWO, FOXA, NYT |
| Consumer Cyclical | DHI, EBAY, BBY |
| Consumer Defensive | GIS, CAG, CLX |
| Energy | SLB, OXY, MPC |
| Financial Services | SCHW, AIG, HBAN |
| Healthcare | BSX, BAX, ZBH |
| Industrials | CSX, EMR, JCI |
| Real Estate | PLD, VICI, INVH |
| Technology | GLW, HPQ, NTAP |

`candidates60-v1` universe hash: `f854a32661818d1d…`.

## 3. Coverage and the frozen pool

**Window:** the default, 2026-09-28 .. 10-02, checked at 17:16Z. 51 names
qualified, so no other window was checked.

**Policy:** news rule `pre-start-3x14d-distinct-v2`, at least 6 distinct
articles across three 14-day probes, all before day 1:
- (06-15, 06-29]
- (07-30, 08-13]
- (09-13, 09-27] UTC

with complete pagination required. Price: at least 40 warm-up bars before
day 1 (point-in-time), plus a bar on every window session (retrospective
availability only).

**Excluded (9):**

| Name | Reason |
|---|---|
| OMC | news, 4 articles |
| TOL | news, 3 |
| LKQ | news, 2 |
| SFM | news, 4 |
| RJF | news, 4 |
| CINF | news, 1 |
| JCI | news, 5 |
| BRX | news, 5 |
| EQR | no in-window bars, 1 warm-up bar, news 3 |

No fetch failure occurred.

**Frozen pool:** `watchlist/experimental_pools/candidates60-v1-pool-2026-09-28-eec1e3f2203f.json`, committed in `9318e7c`.

| | |
|---|---|
| Members | 51 (4–6 per sector) |
| Universe hash | `536a1ecac51b…` |
| `evidence_sha256` | `eec1e3f2203fd4f0cd761910e91dc65845265aa19f864ffaba81f789d34b1af2` |
| `content_sha256` | `7d1e5f7334dbae5847e348e9213eb678739e3208591a429ce05f4c78bf8f8702` |

Members: OLN NUE CF FCX ALB LYB WMG LYV TTWO FOXA NYT PLNT DHI EBAY BBY CASY
CHD GIS CAG CLX DINO DVN TRGP SLB OXY MPC RYAN SCHW AIG HBAN BRKR STE IDXX
BSX BAX ZBH ATI PWR ROL CSX EMR WPC PLD VICI INVH ALGM TER ZBRA GLW HPQ NTAP.

## 4. Readiness, provenance and command

**Before launch:**
- **Repo:** HEAD `9318e7c` (code, config and pool committed first, so
  provenance points at them); dirty only with the user-owned files.
- **Services:** all 7 compose services running, including `execution-sim`.
- **Parity:**
  - `server.py` matches the repository for `trade-journal` (`b4469caf…`),
    `execution-sim` (`a0c74e81…`) and `alpaca-mcp` (`481929e6…`);
  - maverick's 93 runtime source files match;
  - the gateway runs the pinned digest `7052fcaf…`, and only the base
    `backtest` profile existed.
- **Laya worker:** the `:2` handshake verified with the pinned checkpoint.
- **Preserved state:** Round 2 `backtest_state.json` sha256 `4b428d35…ba6d`;
  Neon per-run row counts captured.
- **Tests:** 494 harness, `verify.py` 12/12, 156 watchlist, `git diff --check`
  clean.

**Command** (from the repository root; fundamentals off by omission; masking
seed default 42):

```
python backtest-harness/run_backtest.py --state-dir <fresh dir outside the repo> --run-kind smoke \
  --selector-mode laya_fixed30_experimental \
  --selector-universe watchlist/experimental_pools/candidates60-v1-pool-2026-09-28-eec1e3f2203f.json \
  --laya-checkpoint <HF cache>/models--convaiinnovations--laya/snapshots/55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851 \
  --laya-image hermes-laya-worker:2 --start 2026-09-28 --end 2026-10-02 --days 5
```

**The run:**

| | |
|---|---|
| Run | `5573597c-2f6c-454f-99d3-fdce9f25b62f`, registry kind **smoke** |
| Time | 2026-10-03 17:23:00Z → 17:38:40Z, exit 0, 5/5 cycles, no halt, one attempt per cycle |
| Model and runtime | Hermes `gpt-5.6-terra` / `openai-codex` / medium, disposable profile `btrun-1800d22fa5e64cd4` (0 tools offered, memory off, 0 context bytes) |
| Recorded provenance | `git_head_sha 9318e7c`, `git_dirty true` (user files) |
| Tokens | 49,906 tokens, 5 Hermes calls |
| Run configuration | `fundamentals_enabled false`; `watchlist_snapshot` = exactly the 51 pool members (hash recomputes, `677057b7…`); alias-map hash `8a83f4cd…`; window 2026-09-28..10-02 |

## 5. Daily funnel and outcomes

Funnel per day: 60 candidates → 51 frozen pool → eligible after held/pending
exclusion → complete cards → scored → selected (Laya top 10) → Hermes
proposals.

| Day (date) | Excluded (held/pending) | Eligible | Complete & scored | Unavailable | Selected | Hermes proposals | Outcome |
|---|---|---|---|---|---|---|---|
| 1 (09-28) | — | 51 | 51 | 0 | BSX FCX IDXX CF EMR CASY ALGM BBY NTAP TTWO | BUY EMR (conf 5), BUY NTAP (5) | queued |
| 2 (09-29) | EMR, NTAP | 49 | 49 | 0 | DHI STE CLX CASY BAX IDXX WMG ATI BRKR LYV | BUY BRKR (5) | **filled** EMR 125 @ 159.55, NTAP 97 @ 202.91 at 09:45 |
| 3 (09-30) | EMR, NTAP, BRKR | 48 | 48 | 0 | CSX DINO PLNT MPC ZBH RYAN SCHW EBAY BAX WMG | BUY DINO (3), BUY MPC (3) | **filled** BRKR 310 @ 64.91 |
| 4 (10-01) | EMR, NTAP, BRKR | 48 | 48 | 0 | CAG PLNT HPQ GLW NYT TRGP FCX ROL WPC DHI | BUY HPQ (3) | **rejected at fill** DINO (§2b 20.30% > 20% cap; §6b R:R 1.21 < 1.5), MPC (§2b 20.06%; §6b 1.16) |
| 5 (10-02) | EMR, BRKR | 49 | 49 | 0 | OXY NYT DVN HPQ BAX OLN ALGM EBAY ALB ROL | BUY ALGM (4) | **rejected at fill** HPQ (§6b R:R 1.29); **mechanical exit** NTAP target hit @ 224.85 (+$2,128.18); ALGM queued |

**Laya scores:**
- Every day's scores fell in 0.20–0.25. All are below 0.5, so the model says
  "no" to every card; the ranking is relative within a narrow band.
- Gaps between ranks 10 and 11: 0.0001, 0.0014, 0.0017, **0.0000**, 0.0039.
- Day 4's boundary was a true tie (0.2062 vs 0.2062), resolved by the seeded
  ascending tie-break key, as designed.
- 2–4 tied rows per day overall.

**Operational result** (not performance): equity $100,000 → $101,866.13
(+1.87%) after one week. Cash reconciles by hand with every fill and the
exit:
- 100,000 − 19,943.75 − 19,682.27 = **60,373.98**
- − 20,122.10 = **40,251.88**
- + 21,810.45 = **62,062.33**

## 6. Evidence reconciliation (audit script results; 0 defects)

**Selection snapshots** (5, isolated state directory):
- universe hash = pool, seed 42, k = 10, scorer metadata = the pinned v2
  identity;
- eligible rows = pool minus exactly the held names of that day;
- ranks are contiguous and ordered by probability; selected = min(10,
  scored);
- every card was `current` (latest bar = as-of date), with no stale or
  incomplete card;
- no padding (no unavailable rows existed).

**Hermes inputs:**
- each day's CANDIDATES aliases are exactly the selected 10;
- positions are exactly the held names; held names were excluded from
  selection yet still got packets and exit management (NTAP's day-5 target
  exit happened while it was not shortlisted);
- the masked rules block carries `frozen-universe-v2` with exactly the 51
  pool aliases and no baseline table;
- `assert_clean` passes on all 5 prompts, with no raw ticker by word-boundary
  scan;
- no `review_probability`, `answer_confidence`, "Laya", row IDs, or score
  strings appear in any prompt or input payload;
- the only out-of-shortlist alias outside the rules block is NTAP's alias in
  Day 5's "review your last closed trades" reflection section. That is the
  closed trade, which is legitimate.

**Records:**
- `prompt_sha256` and `response_sha256` recompute;
- attempts 1, tools offered/used 0/0, prompt schema 3.

**Cutoffs:**
- `technical_as_of` = decision day;
- every `latest_news_created_at` precedes its 16:00 ET `news_as_of`;
- no negative article ages;
- cards drop post-as-of bars (the code raises on lookahead).

**Pre-fill news gate:**
- 6 checks, windows 16:00 ET → next 09:45 ET;
- all had 0 articles and confirmed without a Hermes call;
- an independent re-query of the same windows also returned 0, while a
  two-week control window returned 5/2/2 articles for EMR/NTAP/BRKR;
- the gate's Hermes classification path was not exercised.

**Journal:**

| Record | Count | Reconciles with |
|---|---|---|
| trades | 8 | 7 BUY proposals (3 filled, 3 rejected, 1 pending) + 1 target-exit SELL filled |
| `agent_activity` | 30 | `order_queued` 7 = proposals; `order_placed` 3 = BUY fills; `order_rejected` 3 = the three fill-time rejections; `exit` 1; `cycle_start` 5; `reasoning` 10; `weekly_reflection` 1 |
| portfolio snapshots | 5 | per day; equal to the console |
| cycle evidence | 5 | — |
| insight | 1 | — |
| registry | 1 | — |
| configuration | 1 | — |

**Terminal boundary:**
- one pending BUY, ALGM × 515 (queued Day 5, stop 37.55, target 43.48). It
  remains pending in the isolated state file and the journal, per
  `run_backtest.py`'s resume semantics: it would be gated and filled or
  rejected at the next session open on a resume. Nothing was forced.
- open positions EMR × 125 and BRKR × 310.

**Cleanup:**
- no Laya worker container left (the model loaded once per run);
- only the base `backtest` Hermes profile remains;
- all run files sit under the isolated state directory;
- nothing was written to `backtest-harness/`; its `.diagnostics/` holds only
  two files dated 2026-09-12 from an old run.

**Isolation from other runs:**
- Neon deltas are exactly the new run's rows (trades +8, `insight_log` +1,
  `agent_activity` +30, snapshots +5, evidence +5, registry +1,
  configuration +1);
- no other run's per-table counts changed (V1/V2, Round 2, the RED
  acceptance run, the three older smoke runs' pending trades);
- Round 2 `backtest_state.json` is unchanged (`4b428d35…`);
- the manifest, holdouts, `watchlist.json`, `experimental_fixed30.json` and
  RULES.md are untouched.

## 7. Limitations

- **One week, one seed, one pool:** the operational P&L is not evidence of
  anything. Hermes used confidence 5 on 3 of 7 proposals, which AGENTS.md
  calls rare; that's an observation about the model, not a pipeline defect.
- **Laya:** scores are all below 0.5 in a narrow band, ties occur at the
  boundary, and state sensitivity is weak (see the demo report). The
  shortlist is a demonstration of the machinery, not a validated selector.
- **Paths not exercised:**
  - stop-loss and time-stop exits;
  - the pre-fill gate's Hermes classification (no overnight news);
  - a whole-day selector failure;
  - a data-unavailable card in a live run (unit-tested only).
- **Pool:** chosen with present-day knowledge. The in-window price check is
  retrospective (labelled as such); news eligibility is pre-start only.
- **Recency:** the window is recent (late Sep 2026); masking hides
  identities and dates, but recency is not a substitute for a held-out
  evaluation.
- **Comparison:** no comparison against random or deterministic controls
  yet. Per the user's decision, that informs limitations and is not a
  removal gate.

## 8. Next block

The connected path works. The next block can be the dashboard
demonstration, an aggregate-only view (funnel counts, outcomes). It should
show no per-candidate Laya scores, ranks or real tickers, per the scope's
public-evidence rule.
