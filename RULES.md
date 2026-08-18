# RULES.md — DRAFT v2, not final

**Status: full draft, numbers proposed but not locked.** Every number below
carries its own justification and a confidence tag:

- `[GROUNDED]` — matches a real, published, externally-enforced standard.
- `[DERIVED]` — calculated from another rule here; the math is shown.
- `[JUDGMENT]` — a reasoned choice, but no external source. **These are the
  ones most worth checking against real-world rules before locking.**

This file is meant to become the **locked, not-agent-writable** rules layer
(per the original plan document's §4, not this file's own §4) — mounted
read-only once finalized, so a bad reflection cycle can't quietly edit it.
Not mounted yet; still a plain draft.

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

**Risk budget: 1.0% of equity per trade** `[GROUNDED]`

This is the single most established number in retail risk management —
"the 1% rule." It means: if the stop is hit, the account loses 1%, no
matter what the stock did. Professional desks often run tighter
(0.25–0.5%) on much larger accounts; 2% is considered aggressive.

Why 1% and not something bigger — the survival math is brutal and
non-obvious: **at 1% risk, a 10-trade losing streak costs ~10% and you
keep trading. At 10% risk, the same streak costs ~65%, and recovering from
that requires a +186% return.** Losses compound against you harder than
gains compound for you.

> **Live-trading caveat:** standard guidance for an *unproven* trader is
> 0.5%, moving to 1% only after months of consistent results. Hermes has
> no track record at all. **Recommend 1% for backtesting** (it's the
> conventional number, which makes results comparable to normal
> benchmarks) but **starting live at 0.5%** until real performance exists.

### 2b. Concentration cap (the ceiling on top)

**No single position may exceed 10% of equity.** `[DERIVED]`

Here is why this is not redundant with 2a, and it is the most important
idea in this file:

**Stop-losses can be jumped.** A stop at $95 does not guarantee a $95 exit
— if the stock closes at $100 and opens at $78 the next morning on an
earnings miss, the stop fills near $78. The "1% risk" promise silently
breaks exactly when it matters most.

The concentration cap is insurance against that. The derivation:

```
a realistic bad overnight gap        = −20%
we accept at most a ~2% account hit  from one such gap
max position = 2% ÷ 20%              = 10% of equity
```

Note how badly 2a alone can fail without this: a tight 2% stop with a 1%
risk budget implies a position of **50% of equity**. A single gap on that
would be catastrophic. The cap exists to catch precisely that case.

---

## 3. Exposure limits

**Total invested ≤ 60% of equity at any time.** `[JUDGMENT]`

Rationale: a broad market selloff hits every holding at once, so per-
position caps don't protect against correlated moves. At 60% invested, a
−10% market-wide drop costs 6% of the account — painful but inside the
circuit breakers below. It also keeps 40% cash available.

**Minimum viable trade size: skip a trade if it would leave less than 5%
of equity in spare cash.** `[JUDGMENT, revised]` This is NOT a redundant
restatement of the 60% cap above — the 60% cap already guarantees ≥40%
cash on its own (100−60=40 ≥ 5), so a flat "5% must stay uninvested" rule
could never actually bind on anything, which is what the original wording
literally said and was wrong to say. The real, non-redundant check this
performs: before placing a *specific* trade, would it leave so little cash
behind that a good setup showing up right after can't be acted on, or that
the trade itself is too small for its cost/slippage to be worth it? That's
a per-trade check, not a portfolio-wide percentage. Added after comparing
against a similar real system that keeps a hard cash minimum.

**Maximum 8 open positions.** `[JUDGMENT]`

This is *not* a risk rule — §2b and the 60% cap already bound the risk.
It's an attention limit: each open position carries a thesis that must be
re-evaluated every cycle, and a portfolio of 20 names means none of them
get genuinely re-examined.

> **Known deferral:** neither cap is correlation-aware. Eight positions
> that happen to all be semiconductors is effectively one large bet. The
> agreed plan is to add sector-aware caps only once the watchlist is large
> enough for it to matter — with 8–12 hand-picked names (§8), sector spread
> is controlled by the watchlist itself.

---

## 4. Circuit breakers — when to stop trading entirely

Three tiers. Each is a **hard halt**, not a suggestion.

| Trigger | Threshold | Action |
|---|---|---|
| Daily loss | **−3% of equity in one day** `[JUDGMENT]` | Halt new entries for the rest of the day |
| Weekly loss | **−6% of equity in one week** `[JUDGMENT]` | Halt for the week; reflection cycle required before resuming |
| Total drawdown | **−10% from peak equity** `[GROUNDED]` | Full stop. **Human review required** before any further trading |

The −10% total figure matches the maximum drawdown limit used by real
funded-trader firms (FTMO and peers use 5% daily / 10% total, enforced with
real money and real account termination). Adopting a genuinely enforced
industry number beats inventing one.

The −3% daily is deliberately set *below* the industry 5%, on the reasoning
that we want to stop and think well before approaching a level that real
firms treat as terminal. **The −6% weekly is the weakest-grounded number
here** — it sits between the daily and total limits, but I have no external
source for it. Flagged for checking.

**Circuit breakers halt entries, never exits.** Existing stop-losses must
still execute while halted — otherwise a breaker triggered during a selloff
would trap losing positions open and make things worse.

### 4a. Losing-streak de-risking `[JUDGMENT, added]`

**If the last 3 closed trades were all losses, cut new position sizes 25%
and require a stronger setup before entering.** ("Stronger" — Hermes'
reasoning should note multiple aligned signals and above-average
conviction, not just a bare pass. No formal tier system defines this
precisely; per §10's resolved disagreement, that's deliberate — Hermes
judges what "stronger" means here too, not a fixed formula.)

Added after comparing against a similar real system that has this
mechanism and the equity-based circuit breakers above don't. This catches
a genuinely different failure pattern: several small, unrelated losses in a
row can easily stay under a −3% daily / −6% weekly threshold while still
being a real signal that current judgment is off. Cheap to implement — it
only reads trade history that's already recorded, no new tool needed.
Resets the moment a trade closes in profit.

---

## 5. Entry rules

An entry requires **all** of the following:

1. Ticker is on the watchlist (§8).
2. The market is open, with the timing restrictions in §7.
3. A technical setup exists (from `maverick-mcp`).
4. A valid stop level exists that satisfies §6a.
5. The reward:risk test in §6b passes.
6. Sentiment does not veto (below).
7. No circuit breaker is active, and both §2b and §3 caps still allow it.

### 5a. Sentiment is a VETO, never a trigger — recommended change

The original plan required technicals **and** sentiment to *agree* before
entering. **I recommend replacing that with an asymmetric rule:**
technicals may trigger an entry; sentiment may only **block** one. Positive
sentiment alone can never cause a trade.

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
> back into "causes an unbounded loss."

---

## 6. Exit rules

### 6a. Stop-loss — mandatory, set before entry

- Every position has a stop **placed at the same time as the entry**, never
  "added later."
- The stop is set at a **real technical level** (`technical_get_support_
  resistance`), not an arbitrary percentage.
- **The stop must fall between 5% and 10% below entry.** `[JUDGMENT, revised]`
  - Closer than 5%: ordinary daily noise will trigger it. You are not
    managing risk, you are paying fees to be shaken out. (Raised from an
    initial 2% floor after comparing against a similar real system — 2%
    was too tight to survive normal price wiggle; 5% matches the low end
    of that system's own swing-trade range.)
  - Further than 10%: the stock is too volatile for the position math to
    hold up. **Skip the trade** — do not widen the risk budget to fit it.
- **A stop may only be moved in the direction that reduces risk** (up, for
  a long). Never widened, never removed. Widening a stop to avoid taking a
  loss is the mechanism behind most catastrophic retail losses.

### 6b. Profit target — minimum 2:1 reward-to-risk `[GROUNDED]`

The distance to the profit target must be **at least twice** the distance
to the stop. If the nearest meaningful resistance is closer than that, the
setup does not pay enough for what it risks — **skip it.**

This is the most widely used minimum in practitioner literature, and it has
a real consequence worth understanding: at 2:1, a strategy can be **wrong
more often than right and still be profitable** (win 40% of the time and
you still make money). It converts "be right" into "be paid enough when
right," which is a far more achievable target for an unproven agent.

### 6c. Time stop — 10 trading days `[JUDGMENT]`

If a position has hit neither its stop nor its target after 10 trading
days (~2 weeks), close it. A thesis that hasn't played out in two weeks is
usually a thesis that was wrong, and the capital has an opportunity cost.
The specific number is tied to our swing-trading horizon, not derived —
worth checking.

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
- **No new entries in the first 15 minutes after the open.** `[JUDGMENT]`
  Opening spreads are wide and prices are unstable; fills are unreliable
  and unrepresentative.
- **No new entries in the final 30 minutes before the close.** `[JUDGMENT]`
  A position opened at the close carries full overnight gap risk (§2b)
  before its thesis has had any chance to play out.
- Exits are **exempt** from both timing windows. Getting out is always
  allowed while the market is open.

---

## 8. Watchlist

**Populated 2026-08-17** by `watchlist/screen_watchlist.py` against live
market data. Canonical machine-readable copy: `watchlist/watchlist.json`
(loaded by the risk checker and the harness) — **keep the two in sync**,
same caveat as the risk checker's mirrored numbers.

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
| Size | **8–12 names**, sector-diversified | Enough for diversification, few enough that §3's attention limit is real |

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
4. **Never re-enter a name the same day it stopped out.** Prevents the
   "fight the tape" loop where one bad idea is retried until it works.
5. **Never exceed any cap in §2, §3, or §4** — including "just this once,"
   and including when the reasoning is unusually confident.

---

## ⚠ Changing a number here? Change it in the risk checker too

`risk-checker/risk_checker.py` enforces this file in code, independently of
Hermes' own reasoning — it is what makes these rules binding rather than
advisory. Its `RiskLimits` class **mirrors the numbers above** rather than
parsing this document (parsing prose for figures would be fragile in a way a
safety layer cannot afford).

That mirroring is a real, if small, drift risk: **a number changed here and
not there is silently unenforced.** `RiskLimits` keeps every value in one
block with `§` references back to this file, so the sync point is a single
obvious place. Change both, together.

---

## 10. Open — needs comparison against real-world rules

The `[JUDGMENT]` numbers, ranked by how much a second opinion would help:

1. **Weekly circuit breaker (−6%)** — weakest grounding of any number here.
2. **Total exposure cap (60%)** and **max positions (8)**.
3. **Time stop (10 trading days)**.
4. **Market-hours buffers (15 min / 30 min)**.

Well-grounded already, lower value in re-checking: total drawdown (10%),
reward:risk (2:1), concentration cap (10%, derived here with the math
shown), stop-distance floor (5%, revised after real comparison — see §6a).

### Disagreements surfaced comparing against a similar real system (2026-08-17)

1. **Risk per trade: 1% (this file) vs. 5% (a friend's similar system).**
   **Still open, deliberately** — decided to settle this empirically
   against real backtest/eval results rather than argue it from theory
   now. Keep 1% until then; revisit once results exist.
2. **Should the technical entry trigger be hard-coded or left to Hermes'
   judgment? RESOLVED: full judgment, no hard-coded formula, not even a
   REJECTED-tier hard skip.** Their system gates entries through a
   deterministic CONFIRMED/CAUTION/REJECTED formula (fixed RSI/SMA/volume
   thresholds). Decided against adopting any version of that — Arjun's
   reasoning: *"we need to build a self-learning system, not a system that
   has everything handed to it on the go."* A hard-coded technical gate,
   even a partial one, would mean testing whether Hermes can follow a
   checklist someone else wrote, not whether it can develop real trading
   judgment — which defeats the actual point of this project (same
   reasoning that rejected paid AI stock-picking services earlier).
   §5's "a technical setup exists" stays exactly as written: Hermes reads
   `maverick-mcp`'s raw output and reasons over it, no formula in between.
