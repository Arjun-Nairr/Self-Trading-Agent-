# RULES.md — the locked baseline law for this training run

**Status:** this file **is mounted read-only** into the `gateway` container
(`docker-compose.yml`: `./RULES.md:/opt/data/workspace/RULES.md:ro`) and
**is** the locked, not-agent-writable rules layer described below. A bad
reflection cycle cannot quietly edit it: the mount itself, not just intent,
is what enforces that. Every number in this file is the real, currently-
binding limit.

**Phase 1 aggressive profile, implemented — see `docs/PHASE1_AGGRESSIVE_DESIGN.md`
for the full rationale and `HANDOFF.md` for the exact date.** Round 1 ran
under a conservative profile and produced 125 cycles, zero filled trades
(one entry reached the queue but was rejected at execution) — too little
evidence to judge any of these numbers by. The numbers below are a
**deliberate, approved experiment**: looser limits, chosen specifically to
generate more real trading outcomes per unit of time in this **permanent
paper-money sandbox**. They are not a claim that these are the *correct*
numbers for real capital — see `docs/PHASE1_AGGRESSIVE_DESIGN.md`'s own
framing.

- `[GROUNDED]` — matches a real, published, externally-enforced standard.
- `[DERIVED]` — calculated from another rule here; the math is shown.
- `[EXPERIMENTAL]` — the approved aggressive-profile choice: not claimed to
  be independently well-grounded, chosen deliberately to trade some safety
  margin for evidence-generation speed. See §10 for the full list and the
  honest accounting of what each one gives up.

Everything is expressed as a **% of account equity**, never dollars, so
nothing breaks when the account size changes.

---

## 1. Scope — what may be traded at all

- **US-listed common stocks only.** No crypto, no options, no futures, no
  forex, no ETFs holding derivatives.
- **Long only.** No short selling.
- **No leverage, no margin.** Position sizes are capped by settled cash.
- **Watchlist only** (§8). A ticker not on the watchlist is not tradable,
  no matter how good the setup looks.

Why restate what `alpaca-mcp` already enforces structurally: the tool
prevents crypto/options because that code was never written. This section
covers the cases code does *not* block — shorting and margin are ordinary
Alpaca stock operations, so only a rule stops them. Short selling has
unbounded loss (a stock can rise forever), which breaks the entire
position-sizing model below — that model assumes a knowable worst case.

---

## 2. Position sizing

Two independent calculations; **take whichever gives the smaller position.**

### 2a. Stop-based sizing (the primary method)

```
position size = (equity × risk budget %) ÷ (entry price − stop price)
```

**Risk budget: 2.0% of equity per trade** `[EXPERIMENTAL]`

Retail risk-management convention treats 1% as standard and 2% as already
"aggressive" — this profile deliberately runs at that aggressive end. It
means: if the stop is hit, the account loses 2%, no matter what the stock
did. The honest cost, stated plainly: at 2% risk, a 10-trade losing streak
costs ~18% of the account (vs. ~10% at 1%), and recovery from a genuinely
bad stretch takes longer. This is accepted deliberately, in a permanent
paper-money sandbox, to make each trade's outcome move the needle enough to
be worth learning from — see `docs/PHASE1_AGGRESSIVE_DESIGN.md`.

### 2b. Concentration cap (the ceiling on top)

**No single position may exceed 20% of equity.** `[EXPERIMENTAL, derived]`

Still not redundant with 2a, for the same reason as before:

**Stop-losses can be jumped.** A stop does not guarantee its exact price —
if the stock closes well above it and opens sharply lower the next morning
on bad news, the stop fills near the gapped-down open. The "2% risk"
promise silently breaks exactly when it matters most.

The concentration cap is insurance against that, using the same derivation
method as before with a deliberately larger accepted single-gap hit:

```
a realistic bad overnight gap        = −20%
we accept at most a ~4% account hit  from one such gap (was ~2%)
max position = 4% ÷ 20%              = 20% of equity
```

**Stated honestly:** doubling the accepted single-gap hit from ~2% to ~4%
of the account is exactly the kind of number this profile is experimenting
with loosening. It is not free — a genuinely bad gap on a maxed-out
position now costs twice what it used to. Accepted deliberately as part of
the approved aggressive profile, not an oversight.

---

## 3. Exposure limits

**Limited only by available cash. No margin, ever — cash may never go
negative.** `[EXPERIMENTAL]`

Replaces the prior 60%-of-equity exposure cap, the 5%-minimum-spare-cash
rule, and the 8-position count limit — all three removed outright, not
widened. The position-sizing math in §2 (risk budget + concentration cap)
already bounds how much any *one* trade can cost; this profile accepts that
the *portfolio-level* correlated-selloff risk those removed caps existed to
manage (§2b's own "Known deferral" below still applies) is a cost worth
paying for not artificially capping how many genuinely eligible setups can
be acted on. **One open position per ticker remains an absolute rule** (see
§9) — this section is about how many *different* tickers and how much
total capital can be deployed, not about pyramiding into one name.

> **Known deferral, unchanged from the prior profile:** neither §2b's cap
> nor this section is correlation-aware. A portfolio concentrated in one
> sector is effectively one large bet regardless of position count. Still
> deferred for the same reason as before — the watchlist (§8) is small and
> hand-picked for sector spread.

---

## 4. Circuit breaker — when to stop trading entirely

**One tier only: −30% from peak equity. Full stop, human review required
before any further trading.** `[EXPERIMENTAL, revised]`

Replaces the prior three-tier structure (daily −3%, weekly −6%, total
−10%) and the separate losing-streak de-risking rule (former §4a) — both
removed outright. The −10% total-drawdown figure matched real funded-
trader firms' own termination threshold; −30% does not claim the same
grounding. It is a genuinely catastrophic-only backstop, deliberately far
looser than before, chosen so the day-to-day/week-to-week noise that used
to halt trading under the conservative profile can no longer do so at all
— only a real, severe capital impairment stops the experiment. **The
breaker halts entries only, never exits** — an existing stop-loss must
still execute while halted, exactly as before.

---

## 5. Entry rules

An entry requires **all** of the following:

1. Ticker is on the watchlist (§8).
2. The market is open (§7).
3. A technical setup exists (from `maverick-mcp`).
4. A valid stop level exists that satisfies §6a.
5. The reward:risk test in §6b passes, against whichever tier (§6b) is
   currently binding.
6. Sentiment does not veto (below).
7. No circuit breaker is active, and both §2b and §3 still allow it.

### 5a. Sentiment is a VETO, never a trigger

Unchanged from the prior profile — nothing about §5a is part of this
experiment. Technicals may trigger an entry; sentiment may only **block**
one. Positive sentiment alone can never cause a trade. Enforced
independently in three places — the decision-time semantic validator
(`run_backtest.py`), the trade-journal's own field validation
(`trade-journal-mcp/server.py`), and this file's own authority over
`AGENTS.md`'s recorded reasoning — so this can never silently regress in
only one of them.

Three reasons:

**1. News is mostly reactive, not predictive.** By the time a headline is
published, the move it describes has usually already happened. Requiring
positive news to enter mostly means buying *after* the move.

**2. It closes the prompt-injection hole we deliberately left open.** When
`google-news-rss-mcp` was built intentionally "dumb" (no interpretation
logic), the defense was explicitly deferred to this rules layer. **This is
that defense.** If sentiment can never *trigger* a trade, then a malicious
headline ("BUY XYZ NOW — urgent") cannot cause the agent to buy anything.
The worst an attacker achieves is *preventing* a trade — a dramatically
less damaging outcome than causing one.

**3. The asymmetry matches how information actually works.** Genuinely bad
news does invalidate a good chart (don't buy a breakout into an SEC
investigation). Good news does not rescue a bad chart.

> **Critical corollary — sentiment may NEVER block an exit.** Exits are
> mechanical (§6). Without this, an attacker could plant reassuring news to
> suppress a stop-loss, which converts the injection from "blocks a trade"
> back into "causes an unbounded loss." Missing/unavailable news is treated
> as an unevaluated veto check on entry (skip the entry — §5a's asymmetry
> means missing evidence can never read as "no adverse news") and never
> blocks an exit either way.

---

## 6. Exit rules

### 6a. Stop-loss — mandatory, set before entry

- Every position has a stop **placed at the same time as the entry**, never
  "added later."
- The stop is set at a **real technical level** (`technical_get_support_
  resistance`), not an arbitrary percentage.
- **The stop must fall between 3% and 12% below entry**, with a **0.10
  percentage-point tolerance** on both edges (so 2.90%–12.10% is accepted
  in practice). `[EXPERIMENTAL, revised]` The tolerance itself is
  unchanged — same cent-rounding worst case as before ($10 price floor ×
  one cent), independent of exactly where the band sits.
  - Closer than 3%: ordinary daily noise will trigger it — even more true
    here than at the prior 5% floor, since this floor is deliberately
    tighter to allow more setups to qualify. Accepted as part of the
    experiment; a real regression to watch for once results exist.
  - Further than 12%: the stock is too volatile for the position math to
    hold up. **Skip the trade** — do not widen the risk budget to fit it.
- **A stop may only be moved in the direction that reduces risk** (up, for
  a long). Never widened, never removed. Widening a stop to avoid taking a
  loss is the mechanism behind most catastrophic retail losses.

### 6b. Profit target — minimum reward:risk, two tiers

**Base tier: 1.5:1.** `[EXPERIMENTAL, revised from 2.0]` At 1.5:1, a
strategy needs to win more than 40% of the time to be profitable (higher
than 2:1's ~33% breakeven) — a real, honest cost of loosening this number.
Chosen anyway because 2.0:1 combined with the conservative stop/exposure
rules left almost no real setups clearing every bar simultaneously (see
Round 1's own evidence in `docs/evaluations/run1_evaluation_extract.md`
and the geometry/counterfactual analysis in
`docs/history/round2-conservative-draft/run2_prep_analysis.md`).

**Escalated tier: 1.25:1**, triggered automatically by a fixed,
deterministic, harness-controlled mechanism — **approved in advance by the
user, not a rule Hermes or the harness applies to itself at will**:

- The run is divided into fixed blocks of **20 real trading days** from
  the run's start (holidays/weekends don't count).
- A trade counts toward a block's activity only once it is **fully
  completed** — a filled entry later closed by a filled exit. A rejected
  or merely-queued proposal does not count.
- If a block completes with **fewer than 5** completed trades, the
  *following* block's minimum reward:risk becomes 1.25:1 instead of 1.5:1.
- Escalation only ever tightens toward more permissive (never below
  1.25:1) and never reverses mid-run once triggered.
- A fresh run (`--reset`) always starts at the 1.5:1 base tier.

**Six-month target: 30–60 completed trades** over the run — a target the
harness tracks, never a quota communicated to Hermes as something to
manufacture. Hermes' prompt receives only the currently-binding threshold
and its profile label (`aggressive_v1_base` or `aggressive_v1_escalated`);
it never sees a trade count to hit. See `AGENTS.md` and
`backtest-harness/run_backtest.py` for the mechanism.

### 6c. Time stop — 5 trading days `[EXPERIMENTAL, revised from 15]`

If a position has hit neither its stop nor its target after 5 trading days
(about a week), close it. Deliberately short, as part of the same
throughput goal as the rest of this profile: a position sitting open for
three weeks (the prior figure) generates one data point per three weeks:
in a permanent paper-money sandbox built to generate evidence, capital (and
the position-count room it occupies) recycling faster is worth more than
giving any one thesis longer to play out. The real cost: a thesis that
would have worked out in eight trading days now gets cut at five,
regardless. Counts real trading days (`trading_days_elapsed`), not
calendar days, same as before.

### 6d. Thesis invalidation — exit regardless of price

Close the position if the reason for entering has stopped being true, even
if neither stop nor target has been touched:

- The technical signal that triggered entry has reversed.
- The ticker no longer meets watchlist criteria (§8).
- Materially adverse company news (the veto condition in §5a, applied to an
  open position).

---

## 7. Market-hours rules

**Confirmed gap this fixes:** an order was placed during testing while the
market was closed (`/v2/clock` reported `is_open: false`), and nothing in
the stack flagged or blocked it — it simply sat pending.

- **Never submit an order while the market is closed.** A queued order fills
  at the next open at an unknown price, which destroys the premise of the
  entire sizing calculation — the position size was computed from a specific
  entry price that no longer applies.
- **Any minute the market is genuinely open permits an entry.**
  `[EXPERIMENTAL, revised]` The prior 15-minutes-after-open /
  30-minutes-before-close buffers are removed outright, not narrowed —
  another deliberate throughput choice. The real cost, stated honestly: an
  opening-minute fill has a wider, less stable spread, and a closing-minute
  entry carries full overnight gap risk before its thesis has had any
  chance to play out. Accepted as part of the experiment.
- Exits are **exempt** from this restriction entirely. Getting out is
  always allowed while the market is open.

---

## 8. Watchlist

**Populated 2026-08-17** by `watchlist/screen_watchlist.py` against live
market data. Canonical machine-readable copy: `watchlist/watchlist.json`
(loaded by the risk checker and the harness) — **keep the two in sync**,
same caveat as the risk checker's mirrored numbers. Unchanged by the Phase
1 aggressive profile — the profile changes numeric thresholds, not the
eligible universe.

| Symbol | Sector | Company |
|---|---|---|
| OLN | Basic Materials | Olin Corporation |
| WMG | Communication Services | Warner Music Group |
| PLNT | Consumer Cyclical | Planet Fitness |
| SFM | Consumer Defensive | Sprouts Farmers Market |
| DINO | Energy | HF Sinclair |
| RYAN | Financial Services | Ryan Specialty Holdings |
| BRKR | Healthcare | Bruker Corporation |
| ATI | Industrials | ATI Inc. |
| BRX | Real Estate | Brixmor Property Group |
| ALGM | Technology | Allegro MicroSystems |

Ten names, ten sectors. Every one verified against real data for **4+ years
of price history** (a recent spinoff was caught and rejected by that check —
it would have produced no technicals at all at a past `as_of`) **and for
real news coverage inside the backtest window** (two names were caught
returning ~zero articles, which would have left the §5a sentiment veto
permanently inert for 20% of the watchlist; both were replaced).

Re-screening? Run **both** `watchlist/screen_watchlist.py` (maverick
container) and `watchlist/check_news_coverage.py` (alpaca container) — the
coverage check cannot run inside the screener, since only the alpaca
container holds news credentials.

### Criteria used

| Criterion | Threshold | Why |
|---|---|---|
| Market cap | **$2B – $50B** (mid-cap) | See the tension below |
| Avg daily volume | **1M – 12M shares** | Floor: our orders must be negligible vs. real volume, or the fill simulator's assumptions (and real fills) break down. **Ceiling added after a real screener run** — sorting for maximum liquidity returned the most heavily *discussed* names (a 109M-share/day airline, plus several retail-favourite tickers). Volume and narrative saturation correlate, so optimising for liquidity was quietly optimising for memorisation leak. Enough liquidity is required; more is not better |
| Price history | **≥ 3 years** | maverick needs ~200 bars of warmup before `as_of` to compute its long SMA; a recent listing yields no technicals at all at a past date |
| Share price | **≥ $10** | Sub-$5 stocks have wider relative spreads and different microstructure |
| News coverage | **Regular coverage required** | The §5a veto is inert on a stock nobody writes about — half the system would never fire |
| Size | **8–12 names**, sector-diversified | Enough for diversification |

### The real tension, stated honestly

Two goals here actively fight each other:

- **Backtest integrity wants obscure tickers.** Testing the no-trained-memory
  gate on real headlines showed mega-caps leak badly: `Microsoft` masks
  cleanly, but `"Xbox"` and `"Activision"` do not — and those identify both
  the company *and* the time period. Less-famous companies have far less
  memorized narrative attached to them.
- **Signal quality wants well-covered tickers.** No news coverage means no
  sentiment signal at all, and thin volume means unrealistic fills.

**Mid-caps are the deliberate compromise** — real coverage and real
liquidity, without the "everyone has written about this company" problem of
mega-caps.

### Exclusion: no binary-event stocks `[JUDGMENT]`

Exclude clinical-stage biotech/pharma with pending FDA decisions, and any
company with a pending merger vote or similar binary outcome.

This is not a sector prejudice — it's that these stocks routinely gap 30–70%
on a scheduled coin-flip that no technical or sentiment analysis can
anticipate. They break the §2b gap assumption by an order of magnitude, so
the entire risk model stops being valid.

---

## 9. Hard prohibitions

Never, regardless of what the reasoning in any cycle concludes:

1. **Never average down.** Adding to a losing position converts a bounded,
   pre-sized loss into an unbounded one, and it is the single most common
   way retail accounts are destroyed. If the thesis is still valid, the
   existing position already expresses it.
2. **Never widen or remove a stop** (§6a).
3. **Never trade off-watchlist** (§1).
4. **Never hold more than one open position in the same ticker at once.**
   A discretionary entry into an already-held ticker is rejected outright
   as averaging down (§9.1) — this is unconditional, unaffected by any
   other number in this file.
5. **Never re-enter a name the same day it stopped out.** Prevents the
   "fight the tape" loop where one bad idea is retried until it works.
6. **Never exceed any cap in §2, §3, or §4** — including "just this once,"
   and including when the reasoning is unusually confident.
7. **Never loosen a limit through Hermes' own reasoning or reflection.**
   The §6b escalation mechanism (base → escalated tier) is the one
   deliberate exception, and it is not this prohibition being violated: it
   is a fixed, deterministic, harness-controlled mechanic approved by the
   user in advance — Hermes never decides to escalate, never sees a
   quota to chase, and never receives anything from this mechanism except
   whichever threshold is currently binding.

---

## ⚠ Changing a number here? Change it in the risk checker too

`risk-checker/risk_checker.py` enforces this file in code, independently of
Hermes' own reasoning — it is what makes these rules binding rather than
advisory. Its `RiskLimits` class **mirrors the numbers above** rather than
parsing this document (parsing prose for figures would be fragile in a way a
safety layer cannot afford). The §6b escalation (base/escalated) lives
entirely in `backtest-harness/run_backtest.py` — `risk_checker.py` itself
has no concept of blocks or tiers, only whatever single `min_reward_risk`
value it's handed for a given check.

That mirroring is a real, if small, drift risk: **a number changed here and
not there is silently unenforced.** `RiskLimits` keeps every value in one
block with `§` references back to this file, so the sync point is a single
obvious place. Change both, together.

---

## 10. Open — the honest accounting of what this experiment gives up

Every `[EXPERIMENTAL]` number above, in one place, with what it costs
relative to the conservative profile Round 1 ran under (see
`docs/PHASE1_AGGRESSIVE_DESIGN.md` for the full design rationale):

1. **Risk budget 1% → 2%** — a losing streak now costs roughly double.
2. **Concentration cap 10% → 20%** — a single bad overnight gap now costs
   roughly double.
3. **Exposure limit 60%/8-position cap → cash-only, uncapped** — portfolio-
   level correlated-selloff risk is no longer bounded by a percentage or
   position count, only by how much eligible setups and available cash
   actually produce.
4. **Circuit breaker 3-tier (daily/weekly/total) → single -30% drawdown**
   — day-to-day and week-to-week losses that used to force a pause no
   longer do; only a severe capital impairment does.
5. **Losing-streak de-risking (§4a) → removed** — three straight losses no
   longer tighten anything.
6. **Stop band 5-10% → 3-12%** — the floor is tighter (more noise-
   sensitive), the ceiling looser (more volatility tolerated).
7. **Min reward:risk 2.0 → 1.5 (base) / 1.25 (escalated)** — a materially
   higher win rate is now required to stay profitable.
8. **Time stop 15 → 5 trading days** — theses get less time to play out.
9. **Market-hours buffers 15min/30min → none** — entries can now happen at
   the most unstable minutes of the session.

**None of these were re-derived from first principles the way the
conservative profile's numbers were** (see the archived
`docs/history/round2-conservative-draft/run2_prep_analysis.md` for that
prior analysis, which argued against loosening §6b in isolation). This
profile is a single, coordinated, approved bet that generating 30-60 real
completed trades over six months is worth more — for the actual goal of
this project, judging whether Hermes' trading judgment is any good at all —
than staying this conservative and generating close to none, as Round 1
did. Revisit once Phase 1's own real results exist, the same way Round 1's
results are what produced this document.

**Unchanged, still worth a second opinion eventually:** the §6a floor's
underlying volatility-microstructure question (now sharper at 3%), and
whether the concentration cap's gap-assumption (a realistic bad overnight
gap of −20%) still holds at 12% max stop distance instead of 10%.

### Disagreements surfaced comparing against a similar real system (2026-08-17)

Historical context, from before the aggressive profile existed — kept for
provenance. **RESOLVED: full judgment, no hard-coded formula, not even a
REJECTED-tier hard skip.** Their system gates entries through a
deterministic CONFIRMED/CAUTION/REJECTED formula (fixed RSI/SMA/volume
thresholds). Decided against adopting any version of that — Arjun's
reasoning: *"we need to build a self-learning system, not a system that
has everything handed to it on the go."* §5's "a technical setup exists"
stays exactly as written: Hermes reads `maverick-mcp`'s raw output and
reasons over it, no formula in between. This holds unchanged under the
aggressive profile — only numeric thresholds moved, not this design
decision.
