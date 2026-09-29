# Laya candidate-selector — scope v1

**status: discussing — no implementation approved**

This is the single current discussion/scope record for the proposed Laya
candidate-selector work. It exists to hold the settled architecture in one
place and to make the genuinely open questions explicit, so a future
implementation pass has something concrete to check itself against — not to
authorize starting that pass. Nothing described here is installed,
downloaded, benchmarked, or integrated. No code, rules, masking, schema, or
dashboard change accompanies this document.

This supersedes `docs/PROJECT_BACKLOG.md` §2's now-outdated "point-in-time
dynamic candidate selector — cancelled for this project" line (see that
file, which now points here) and reopens that decision under the name
"Laya," per `docs/evaluations/project_integrity_and_laya_readiness_audit.md`
§7.9/§8.

---

## What Laya is, and is not

Laya is a **daily attention selector** — a lightweight local scoring pass
over a broad, point-in-time-eligible universe, whose only job is to narrow
that universe down to a bounded set of names worth spending the existing
(expensive, careful) technical-screen and Hermes-reasoning pipeline on.

Laya is **never**: a buy signal, a risk checker, a news interpreter, or a
trade decider. It cannot create, block, size, or veto a trade — every one
of those decisions still happens exactly where it happens today
(`risk_checker.py`, the news pre-fill gate, Hermes' own reasoning over the
final masked packet). Laya's failure mode, if it is ever wrong, is "the
right ticker wasn't even offered to the existing pipeline this cycle" —
never "a bad trade was approved because Laya said so."

---

## Settled architecture

1. **Broad point-in-time eligible universe.** A larger candidate pool than
   today's fixed 10-ticker watchlist, constructed so that on any simulated
   (or live) date, only names that were genuinely tradeable/known as of that
   date are eligible — no lookahead, no survivorship bias baked in by
   construction.
2. **Deterministic basic eligibility filter.** A cheap, non-Laya,
   non-Hermes filter (liquidity/listing/data-availability style checks)
   narrows the broad universe before Laya ever scores anything.
3. **Daily local Laya scoring of minimal numeric cards only.** Laya runs
   locally, once per completed trading day, over compact numeric feature
   cards — never raw prices, never text, never anything identity-bearing.
4. **The controller chooses exactly 20**: the top 15 Laya-ranked names plus
   5 deterministic, seeded exploration picks (drawn by a fixed, reproducible
   rule — not Laya, not Hermes) so the system doesn't only ever look at
   names Laya already likes.
5. **The selected 20 receive the EXISTING full technical screen and news
   veto** — unchanged from today's pipeline (`maverick-mcp` technicals,
   the pre-fill/decision-time news gate). Laya's selection is a pre-filter
   in front of that pipeline, not a replacement for any part of it.
6. **Hermes receives only the final masked candidate packet** — exactly the
   same shape of input it receives today, at whatever count survives the
   technical/news screen. Laya's own scores, ranks, or reasoning are never
   shown to Hermes.
7. **Minimal Laya card schema** — the only fields a card may carry:
   - opaque row ID (not a ticker or name)
   - 5-day return
   - SMA-20 distance
   - RSI-14
   - normalized MACD direction
   - relative volume
   - realized volatility
   - 20-day range position
   - data-complete flag
8. **What never reaches Laya**: real ticker or company name, real calendar
   date, news, SEC filings/fundamentals, reflections, trade outcomes, or any
   historical record. Laya sees the same category of point-in-time-gated,
   identity-free numeric evidence the rest of the backtest already insists
   on — nothing more.
9. **Cadence.** Laya runs once after each completed trading day. A retry of
   that day reuses the day's already-stored selection — it never re-scores
   or re-selects.
10. **Exclusions before selection.** Currently-held tickers and tickers with
    an unresolved pending order are excluded from the selection pool before
    Laya runs. Exits remain completely independent of Laya — a held
    position's exit logic never depends on whether Laya would still select
    it today.
11. **Packet freshness.** Full technical/news packets are refetched fresh on
    every new simulated date. Only a same-date retry/replay may reuse the
    date's already-cached packets — never a stale prior date's.
12. **Snapshots are immutable per date** — each date's selection/packet
    state is its own recorded snapshot, never a mutable pool that a later
    date overwrites or destructively edits.
13. **The real implementation blocker: point-in-time historical universe
    data.** Backtesting Laya requires a genuine point-in-time historical
    universe (what was eligible/listed/liquid as of each simulated date) and
    point-in-time eligibility inputs. This does not exist today and is the
    actual gate on starting implementation — everything else on this list
    can be designed around, but this cannot be faked without reintroducing
    the exact survivorship-bias problem the fixed-watchlist backtest already
    has (`HANDOFF.md` §8, `docs/PROJECT_BACKLOG.md` §4's survivorship-bias
    item).
14. **Backtest masking for a larger, rotating universe.** One private,
    deterministic mapping covering the larger universe is created once at
    run start — not reshuffled per appearance. If a ticker rotates out of
    the selected 20 on some date and rotates back in later in the same run,
    it keeps the SAME alias it had before. This extends (in spirit, not
    yet in code) the existing `RunMasking`/`anonymize.py` approach, which
    today reshuffles a fixed 14-token NATO pool for a fixed 10-ticker
    watchlist per run — a larger universe needs a larger, still-fixed,
    still-per-run-deterministic mapping scheme. Not implemented; no masking
    code changes accompany this document.
15. **Private Neon evidence needed** (additive, never replacing today's
    evidence columns, never granted to `anonymous`): selector
    configuration/model identity/card-schema version/universe-source hash;
    a per-cycle selection funnel (universe size → eligible → Laya-scored →
    top-15 + 5-exploration → survived technical/news screen); per-candidate
    eligibility/exclusion reason; Laya score/rank/route (selected via
    top-15 vs. exploration) per candidate; the final technical/news screen
    outcome per selected candidate. The public dashboard may expose only an
    **aggregate** funnel (counts at each stage) — never per-candidate Laya
    scores, ranks, or any field that could reconstruct which real ticker
    Laya favored on a given date.

---

## Open decisions before implementation

These are genuine unresolved questions — not a checklist to complete
unilaterally, and not implied answers anywhere above.

- **Universe source.** Where does the broad point-in-time eligible universe
  actually come from (a licensed/point-in-time index membership feed, a
  static point-in-time snapshot rebuilt periodically, something else), and
  what does it cost/require operationally?
- **Exact eligibility thresholds.** The "deterministic basic eligibility
  filter" (liquidity/listing/data-availability) has no concrete numbers yet
  — what minimum average volume, price floor, listing-age, or data-history
  requirement actually defines "eligible"?
- **Laya model/checkpoint benchmarking.** No Laya model or checkpoint has
  been selected, installed, or benchmarked. What model/checkpoint, what
  local resource footprint, and what benchmark proves it's actually
  ranking usefully before it's trusted to shape which 15 names get looked
  at?
- **Fallback behavior if Laya or market data is unavailable.** If the local
  Laya scoring pass fails or a card's inputs are incomplete for some
  tickers on a given date, does the day fall back to the current fixed
  10-ticker watchlist, to exploration-only picks, to a halted cycle, or to
  something else? Not decided.
- **Larger-universe masking scheme, concretely.** §14 above states the
  *principle* (one deterministic mapping, aliases persist across
  rotation); the actual pool size, alias naming scheme, and collision
  handling for a universe potentially far larger than 14 tokens is not
  designed.
- **Point-in-time universe backtest blocker (§13), concretely.** No source
  or construction method for point-in-time historical universe membership
  has been chosen — this is the real blocker on implementation, not a
  detail to fill in after starting.
- **Exploration-pick seeding.** "5 deterministic seeded exploration picks"
  names the mechanism's shape (deterministic, seeded, not Laya-chosen) but
  not the actual seed derivation or selection rule.

---

## Explicitly not started

No Laya installation, model download, benchmarking, code, masking change,
Neon schema change, rules change, watchlist change, backtest, evaluation
rerun, or dashboard work accompanies this document. This is a scope record
only.
