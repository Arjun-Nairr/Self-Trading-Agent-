# Laya candidate-selector — scope v1

**status: approved phased prototype — Phase 1 (isolated foundation) implemented; selector NOT wired into any run**

This is the single current scope record for the Laya candidate-selector
prototype. It records the fixed-universe experiment approved for phased
implementation and the gates that must pass before it can influence trading.
Phase 1 (the isolated foundation — see "Phase 1 — implemented" below) is
built and tested, but the selector is **not wired into any backtest, Hermes
prompt, risk check, or live path**, no run has used it, and no performance
claim is made. No rules, schema, dashboard, or baseline change accompanies it.

This supersedes `docs/PROJECT_BACKLOG.md` §2's now-outdated cancellation
line. It does **not** reopen a dynamic market-wide selector: that larger
point-in-time-universe project remains deferred. This V1 is a fixed,
hardcoded 30-company prototype.

---

## What Laya is, and is not

Laya is a **daily attention selector** — a lightweight local scoring pass
over a frozen set of 30 companies. Its only job is to narrow those names to
the bounded set worth spending the existing full-data and Hermes-reasoning
pipeline on.

Laya never emits a BUY, sizes a position, overrides a rule, reads news, or
executes a trade. Those decisions remain with Hermes, the news gates, and
`risk_checker.py`. But because selection changes which names Hermes may see,
Laya is an **indirect strategy component**. It therefore needs a fair
selector experiment before activation; it is not merely harmless plumbing.

---

## Settled architecture

1. **Fixed 30-company experiment universe.** A single hardcoded list of 30
   liquid, sector-diverse US equities is selected and frozen before any
   selector evaluation. The list remains unchanged across every arm and
   episode in the experiment. This is deliberately **not** a claim to be a
   market-wide or survivorship-free historical universe; all results are
   labelled fixed-universe prototype results.
2. **Basic availability gate.** On each date, a frozen-universe name enters
   scoring only when its required point-in-time price card can be built.
   Held positions and unresolved pending entries are excluded from new-entry
   selection, but always retain independent exit management.
3. **Laya is the selected provider.** JEV is not part of this design. Laya
   runs locally, once per completed trading day, over compact numeric feature
   cards — never raw prices, never text, never anything identity-bearing.
   The implementation should retain a narrow provider adapter, but no second
   provider is in scope for V1.
4. **Laya scores every eligible card independently in a batch.** Its one
   question is: "is this identity-free numeric card worth spending the
   existing full technical and news review on today?" The controller records
   both the answer's `review_probability` (Laya's P(true) for that narrow
   question) and `answer_confidence`, then ranks by `review_probability`.
   Neither value means probability of profit, a buy recommendation, or how
   well Hermes will trade the company. They must never be presented as such.
5. **The controller chooses up to 10.** It passes the ten highest-ranked
   eligible Laya cards into the daily mini-pool. If fewer than ten names are
   available, it passes all of them and records the shortfall; it never pads
   the pool with ineligible names. V1 has no exploration picks: Laya, random,
   and deterministic selector arms are evaluated separately instead.

   No hard `90%`/`0.90` threshold is part of V1. A threshold can be proposed
   only after a shadow evaluation calibrates this exact Laya checkpoint and
   review-suitability question against held-out selector data.
6. **The selected mini-pool receives the existing full evidence packet.**
   Maverick technicals and historical news are fetched for the selected names,
   then Hermes makes its existing technical judgment and applies its existing
   news-veto instructions. There is no imaginary controller-side
   "technical/news survivor" filter: today, a successful fetch makes a
   candidate available and Hermes performs the judgment.
7. **Hermes receives only the final masked candidate packet** — the up-to-10
   selected names with their full evidence. Laya's scores, ranks, and
   reasoning are never shown to Hermes.
8. **Minimal Laya card schema** — the only fields a card may carry:
   - opaque row ID (not a ticker or name)
   - 5-day return
   - SMA-20 distance
   - RSI-14
   - normalized MACD direction
   - relative volume
   - realized volatility
   - 20-day range position
   - data-complete flag
9. **What never reaches Laya**: real ticker or company name, real calendar
   date, news, SEC filings/fundamentals, reflections, trade outcomes, or any
   historical record. Laya sees the same category of point-in-time-gated,
   identity-free numeric evidence the rest of the backtest already insists
   on — nothing more.
10. **Cadence.** Laya runs once after each completed trading day. A retry of
   that day reuses the day's already-stored selection — it never re-scores
   or re-selects.
11. **Exclusions before selection.** Currently-held tickers and tickers with
    an unresolved pending order are excluded from the selection pool before
    Laya runs. Exits remain completely independent of Laya — a held
    position's exit logic never depends on whether Laya would still select
    it today.
12. **Packet freshness.** Full technical/news packets are refetched fresh on
    every new simulated date. Only a same-date retry/replay may reuse the
    date's already-cached packets — never a stale prior date's.
13. **Snapshots are immutable per date** — each date's selection/packet
    state is its own recorded snapshot, never a mutable pool that a later
    date overwrites or destructively edits.
14. **Fixed-30 backtest boundary.** The prototype uses the same frozen 30
    names for every date. It does not solve the historical point-in-time
    membership or delisted-price problem. A future dynamic selector must
    solve those separately before it makes any market-wide claim.
15. **Backtest masking for 30 names.** One private deterministic mapping
    covers all 30 names at run start. A name retains its alias throughout the
    run whether or not Laya selects it that day. The legacy 14-token,
    case-insensitive scheme could not do this; Phase 1 adds an opt-in
    `alias_scheme="opaque"` to `RunMasking` (hashed `KV42`-style aliases that
    are not words, stable when membership changes, and case-sensitive ticker
    matching so word-like tickers do not corrupt prose). The default scheme —
    and so the fixed-10 baseline — is unchanged; the selector wiring phase must
    opt in explicitly.
16. **Private selector evidence needed** (additive, never replacing today's
    evidence columns, never granted to `anonymous`): selector
    configuration/model identity/card-schema version/universe-source hash;
    a per-cycle selection funnel (universe size → eligible → Laya-scored →
    Laya top-10 → full-evidence packet); per-candidate
    eligibility/exclusion reason; Laya `review_probability`,
    `answer_confidence`, model/checkpoint version, rank, and explicit
    selected/not-selected status per candidate; the final Hermes/risk outcome
    per selected candidate. The public dashboard may expose only an
    **aggregate** funnel (counts at each stage) — never per-candidate Laya
    scores, ranks, or any field that could reconstruct which real ticker
    Laya favored on a given date.

---

## Phase 1 — implemented (isolated foundation, unwired)

### Frozen fixed-30 prototype universe

Config: `watchlist/experimental_fixed30.json` (`fixed30-v1`, flagged
`experimental`). It is a separate file from the baseline
`watchlist/watchlist.json`, which is untouched and still exactly the fixed
10; nothing in the default backtest, live path, risk checker, or Docker mount
reads the new file. `universe_hash` (SHA-256 over the sorted
symbol/sector/name triples) is recorded in every selection snapshot.

| Sector | Baseline (kept) | Added |
|---|---|---|
| Basic Materials | OLN | NUE, CF |
| Communication Services | WMG | OMC, LYV |
| Consumer Cyclical | PLNT | TOL, LKQ |
| Consumer Defensive | SFM | CASY, CHD |
| Energy | DINO | DVN, TRGP |
| Financial Services | RYAN | RJF, CINF |
| Healthcare | BRKR | STE, IDXX |
| Industrials | ATI | PWR, ROL |
| Real Estate | BRX | EQR, WPC |
| Technology | ALGM | TER, ZBRA |

**Selection rationale (a-priori, transparent):** keep all 10 baseline names
unchanged; add exactly two per baseline sector bucket, so every sector has
three names. Added names were picked by hand, once, before any selector
evaluation, from long-established US-listed common stocks that are widely
traded, are not biotech (the baseline's own exclusion), had no recent IPO,
and whose tickers are not ordinary English words (the baseline's own
word-like `DINO`/`RYAN` are kept unchanged and are covered by the opaque
masking scheme's case-sensitive ticker matching). No backtest outcome, return,
momentum, or predictive score was used.

**What this does NOT establish.** The 20 additions were *not* mechanically
screened in this phase: market cap, average volume, price floor, three-year
history, continuous listing across 2022–2026, absence of pending
mergers/delistings, and per-name news coverage (the existing
`watchlist/check_news_coverage.py` gap) are all unverified and must be checked
before any run. The list is chosen with present-day knowledge, so it is
survivorship-biased for historical windows exactly as the baseline is; every
result must be labelled a fixed-universe prototype result.

### Components (all new, all unwired)

- `backtest-harness/laya_selector.py` — universe loader/validator + hash;
  `CandidateCard` (exactly: opaque row ID, 5-day return, SMA-20 distance,
  RSI-14, normalized MACD direction, relative volume, realized volatility,
  20-day range position, data-complete flag — `build_card` rejects any other
  key, any non-numeric/out-of-range value, and any row ID that is not a
  generated `r_<12 hex>`); the `CardScorer` interface; `score_complete_cards`
  (validates a scorer's contract, fails closed); pure `select_pool` (rank by
  descending `review_probability`, seeded row-ID-hash tie-break, up to 10, no
  exploration, every input labelled `selected` / `not_selected` /
  `data_unavailable`, stale or incomplete or unscored cards unavailable, a
  bar dated after the as-of date raises); and the versioned, byte-stable
  selection snapshot with atomic, never-overwritten per-(run, date)
  persistence (`SnapshotConflict` on a differing rewrite; an identical retry is
  a no-op). `answer_confidence` is recorded and never used to rank or filter.
- `backtest-harness/laya_adapter.py` — local adapter over
  `laya.decide_batch(..., return_details=True)` asking the single boolean
  question above (a Laya `noul`), pinned to `laya==0.3.23`, checkpoint
  `convaiinnovations/laya` at commit `55cf4c4e…`, weights verified against a
  pinned SHA-256, CPU, fixed batch size, no `min_confidence`, loaded from a
  local directory (scoring cannot reach the network). The only network step is
  the separate, explicit `provision_checkpoint()`.
- `backtest-harness/anonymize.py` — opt-in `alias_scheme="opaque"` (see
  decision 15). Verified byte-identical to the previous implementation for the
  default scheme over 200 seeds on the 10 baseline names.
- `backtest-harness/requirements-laya.txt` — optional, pinned, for a separate
  virtualenv; the default harness environment does not need it.
- Tests: `test_laya_selector.py`, `test_laya_adapter.py`, and new 30-name /
  collision regressions in `test_anonymize.py`.

### Real local smoke check (what it did, and did not, prove)

In an isolated scratch virtualenv, the pinned checkpoint (≈843 MB, official
Hugging Face repo) was provisioned once, then loaded and run on CPU with the
network blocked at the socket layer, on **synthetic** cards (deterministic
random numbers, not market data). It scored 29 complete cards in one batch
(≈11 s on CPU, ≈18 s load), skipped the one deliberately incomplete card as
`data_unavailable`, returned distinct probabilities, reproduced identically on
a repeat call and when each card was scored alone, and round-tripped a
snapshot byte-identically. This proves the adapter loads and runs the real
model locally and deterministically. It proves **nothing** about ranking
quality, calibration, or usefulness on real cards; Laya reports `noul` to four
decimals (exact ties are possible and handled by the seeded tie-break) and its
checkpoint is documented as over-confident as shipped.

### Remaining before experimental wiring (Phase 2)

1. Verify the 20 added names against the baseline screen criteria and news
   coverage (above).
2. Build real cards from the existing point-in-time technicals path (no card
   producer exists yet — Phase 1 takes prepared numbers only) and decide the
   exact normalized-MACD and relative-volume formulas.
3. Opt-in flag in the backtest controller; `not_selected` candidate status;
   fetch full evidence only for the selected pool; mask with
   `alias_scheme="opaque"`; checkpoint the day's selection before use and
   replay it on retry; held / pending exclusion; decide the fail-closed
   fallback.
4. Redefine what "watchlist" means to the risk checker for the experiment
   (frozen 30 vs. the day's pool) — a rules-level decision for Arjun.
5. Private Neon selector evidence (additive) — nothing has been written to
   Neon.
6. Pre-registered control arms (random-10, simple deterministic top-10) and a
   shadow evaluation of the Laya scores before any activation.

---

## Build and test phases

1. **Foundation — no trading-path change. ✅ Phase 1 implemented (see above).** Build the fixed-30 configuration,
   numeric-card builder, local pinned Laya adapter, `decide_batch` call,
   deterministic top-10 ranking, and immutable per-date selection snapshot.
   Unit tests prove card shape, identity exclusion, fewer-than-ten behavior,
   retry replay, held/pending exclusion, deterministic ties, and Laya failure
   handling. Hermes, risk, execution, and existing baselines remain untouched.
2. **Selector wiring — new experimental path only.** Expand the backtest
   masker safely from 14 to at least 30 aliases; add an explicit
   `not_selected` candidate status; fetch full evidence only for the selected
   up-to-10 pool; and pass that masked pool to Hermes. Existing fixed-10
   baseline paths remain unchanged.
3. **Controlled selector evaluation — no live paper.** Run separately
   registered experimental arms over the same fixed 30 names, dates, rules,
   aliases, and model: Laya top-10 versus seeded random-10 versus a simple
   deterministic top-10 rule using the same card features. This is a new
   selector experiment, not a continuation or replacement of baseline V1/V2.
4. **Activation decision.** Activate Laya for a future paper-trading path
   only if the pre-registered evaluation says it beats both control arms on
   meaningful selector and trading outcomes without weakening safety or
   reproducibility. Otherwise preserve the existing system and retain Laya as
   an experimental result only.

---

## Open decisions before implementation

These are genuine unresolved questions — not a checklist to complete
unilaterally, and not implied answers anywhere above.

- **Frozen 30-company list — committed (`fixed30-v1`), but unverified.** The
  list and its selection method are recorded above; the 20 added names still
  need the liquidity / listing-history / news-coverage verification before any
  run. A fixed-universe prototype, not a point-in-time market claim.
- **Evaluation definition.** Define in advance which selector outcomes must
  improve: at minimum comparison against random and deterministic controls;
  no forward-return label may become Laya input.
- **Laya model/checkpoint benchmarking.** The English `laya` checkpoint is
  selected and pinned, and a synthetic-card smoke check ran it locally
  (≈843 MB weights, CPU, ≈11 s per 29-card batch) — but it has **not** been
  benchmarked on real cards. What benchmark proves it's actually ranking
  usefully before it's trusted to shape which 10 names get looked
  at? This must include a shadow evaluation of whether its reported
  `review_probability` / `answer_confidence` are calibrated enough for this
  exact card schema and question; until then, they are rank/logging signals,
  not a `0.90` eligibility gate.
- **Fallback behavior if Laya or market data is unavailable.** Decide whether
  an experimental cycle halts fail-closed or records an excluded day. It may
  never silently fall back to a different selector arm.
- **Control-rule definition.** The deterministic control must be deliberately
  simple and fixed before results are seen; otherwise it is an after-the-fact
  competitor rather than a fair baseline.

---

## Explicitly not started

Selector wiring into any run, real-card production, Neon selector evidence, a
rules change, any change to `watchlist/watchlist.json`, any backtest or
evaluation run, calibration/benchmarking of Laya on real cards, and dashboard
work. Phase 1 added code, tests, one config file, and one optional
requirements file only.
