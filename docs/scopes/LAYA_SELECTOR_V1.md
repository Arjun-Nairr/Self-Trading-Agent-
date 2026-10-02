# Laya candidate-selector — scope v1

**status: approved phased prototype — Phase 1 (foundation) and Phase 2 (opt-in experimental wiring) implemented; no selector evaluation or activation has happened**

This is the single current scope record for the Laya candidate-selector
prototype. It records the fixed-universe experiment approved for phased
implementation and the gates that must pass before it can influence trading.
Phase 1 (the isolated foundation) and Phase 2 (strictly opt-in wiring into
the experimental backtest path — see the two "implemented" sections below)
are built and tested. With `--selector-mode` omitted (`off`, the default) the
backtest is unchanged. **No run has used the selector, no selector evaluation
has been done, nothing is activated, and no performance claim is made.** No
rules, risk-limit, schema, dashboard, Docker, or baseline change accompanies
either phase.

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
10; nothing in the default (`off`) backtest, live path, risk checker, or
Docker mount reads the new file — only the Phase 2 opt-in mode does.
`universe_hash` (SHA-256 over the sorted symbol/sector/name/brand triples and
the word-like-ticker list) is recorded in every selection snapshot. The config
also carries best-effort extra masking `brands` for some names and a
`word_like_tickers` list (`CF`, `DINO`, `RYAN`) for the masker's case rules.

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

### Components (Phase 1 — wired into a run only by Phase 2's opt-in mode)

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

### Remaining after Phase 1 (status as of Phase 2)

Done in Phase 2: the real card producer, the opt-in flag, `not_selected`
status, selected-only evidence fetches, opaque masking, snapshot replay,
held/pending exclusion, and the fail-closed fallback. Still open: verifying the
20 added names against the screen criteria and news coverage (preflight
command exists, not yet run against real data); Neon selector evidence;
pre-registered control arms and a shadow evaluation before any activation.

---

## Phase 2 — implemented (opt-in experimental wiring; no evaluation, no activation)

### How to turn it on (and why nothing else can)

```
python backtest-harness/run_backtest.py --run-kind smoke \
    --selector-mode laya_fixed30_experimental \
    --laya-python <laya-venv>/python --laya-checkpoint <provisioned checkpoint dir> ...
```

`--selector-mode` defaults to `off` and is never read from the environment, a
manifest, Docker, or the dashboard. On a fresh run it must be passed
explicitly; a resumed run keeps its stored mode and a conflicting request
fails closed; `--laya-python` / `--laya-checkpoint` are rejected unless the
experimental mode is selected. The selector modules are imported lazily inside
the opt-in branch, so the default path never imports them, torch, or laya.
Experimental runs record `selector_mode`, the universe hash, and the declared
word-like-ticker set in `backtest_state.json` (those keys exist only in
experimental runs); a changed universe file fails a resume.

### What the experimental mode does each day

1. **Universe/config.** Loads only `watchlist/experimental_fixed30.json` (never
   `watchlist.json`); the 30 names become the run's frozen watchlist snapshot
   — so the risk checker's watchlist membership is simply the frozen
   universe, exactly as for the fixed-10 baseline. No rule or limit changes.
2. **Eligibility.** Names already held (or with an unresolved pending entry)
   are excluded from new-entry selection. They keep their own independent
   technicals/news fetch and appear in Hermes' position list, so exit
   management never depends on the selector.
3. **Cards.** For each remaining name, the exact eight-field card is built from
   daily OHLCV through the simulated date, read from maverick's existing
   smart-cache price source (the one `fetch_technicals` uses). Every bar after
   the date is dropped on the host; a card whose as-of bar is missing is stale;
   missing/short/degenerate history makes the card `data_unavailable`, never a
   technical rejection. Formulas (`laya_cards.py`): 5-day return = c[t]/c[t-5]-1;
   SMA-20 distance = c[t]/SMA20-1; Wilder RSI-14; MACD direction =
   tanh(histogram / close / 0.005) with MACD(12,26,9); relative volume = v[t] /
   mean of the 20 prior volumes; realized volatility = stdev of the last 20
   daily log returns x sqrt(252); 20-day range position = (c-low20)/(high20-low20).
   At least 40 usable bars are required.
4. **Scoring — one private local worker.** The controller process never loads
   Laya. It starts `laya_worker.py` once, lazily, under the *separate* Laya
   virtualenv's python, and talks to it over that child's own stdin/stdout
   pipes — no port, no HTTP, no public endpoint. The worker loads the pinned
   checkpoint once (package version, repo commit, and weight SHA-256 verified;
   local directory; CPU), blocks all network access, runs with a scrubbed
   environment (no API keys/DB URLs), and exits when the controller does. Each
   day's eligible cards go in **one batched request** (never a call per card).
   The handshake must match the pinned scorer or the day fails closed.
5. **Selection.** The existing deterministic top-10: descending
   `review_probability`, seeded row-ID-hash tie-break, no exploration.
   `review_probability` means only "worth full review"; no threshold, and
   `answer_confidence` is logged only.
6. **Immutable per-date snapshot.** Written to the git-ignored
   `backtest-harness/.selector_snapshots/<run_id>/<date>.json`. A retry or
   replay of a date that already has a valid snapshot **reuses it — no
   re-scoring, no history fetch, the model is not even started** — after
   validating universe hash, seed, scorer pins, k, and the eligible set. The
   Phase 1 write race is fixed: publication is an exclusive atomic link, so two
   writers can never replace each other's content (`SnapshotConflict`; the
   first writer wins; an identical rewrite is a no-op).
7. **Full evidence only for the selected pool.** Technicals and news are fetched
   only for the selected names plus held names. Hermes receives only those
   masked candidates — never Laya scores, ranks, probabilities, confidences, or
   row IDs. Candidate status in the private cycle evidence is now explicit:
   `not_selected`, `selector_data_unavailable`, `selector_unavailable`
   (whole-day failure), distinct from `technical_fetch_error`.
8. **Explicit entry boundary.** For experimental new entries, only that day's
   selected names may be bought — a separate "selector boundary" check, not a
   risk-rule change.
9. **Masking.** One private `alias_scheme="opaque"` mapping over all 30 names for
   the whole run. Tickers that are ordinary words/names (`CF`, `DINO`, `RYAN`,
   declared in the universe config) stay case-sensitive; every other ticker is
   matched case-insensitively, so a lowercase leak (`oln`) is now masked and, if
   it slips through, caught by `assert_clean` before the packet reaches Hermes —
   without corrupting ordinary prose. Best-effort extra brand terms
   (Ticketmaster, Orkin, ...) are frozen into the run's watchlist snapshot; they
   are not a complete list, and the known masking ceiling in `anonymize.py`
   still applies.
10. **Fail closed, new entries only.** If history fetch, card building, the worker,
    scoring, or snapshot replay fails, that day has an empty pool and a run-local
    `failed_closed` selector event with the reason (printed as a `SELECTOR` line
    and kept in the cycle checkpoint; no Neon write). Hermes is still called,
    held-position exits are unaffected, and the next day is independent.

### Coverage preflight

`python backtest-harness/laya_preflight.py --start <date> --end <date>` reports
PASS/FAIL per symbol for daily-price availability (a bar on every expected
trading day, enough warm-up history, no malformed bars) and the existing news
criterion (`check_news_coverage.py`: at least 6 articles across three 14-day
probes, complete pagination). Read-only; exit 0 = all pass, 1 = a failure, 2 =
containers unreachable (nothing verified). **It has not yet been run against the
real data: the Docker daemon was not running when Phase 2 was built**, so the 20
added names remain unverified (see the Phase 1 caveat).

### Verified, and what is not

Unit and integration tests drive the real `run_day()` with only I/O mocked and a
deterministic fake scorer; a separate end-to-end check ran the controller →
private worker (real pinned Laya, network blocked) → snapshot → replay on
synthetic bars (no market data, no run). Not done: any real-data card run, any
selector evaluation, calibration or benchmarking of Laya on real cards, Neon
selector evidence, a risk-semantics decision beyond the explicit entry
boundary above, or activation.

### Caveat for the evaluation phase

The masked `RULES.md` block that is part of every prompt still contains the
baseline 10-name watchlist table (as masked aliases) and talks about a 10-name
watchlist. That is unchanged rules text (rules were out of scope) but it means
Hermes sees ten masked rows that are not necessarily today's pool; revisit
before a controlled evaluation.

---

## Build and test phases

1. **Foundation — no trading-path change. ✅ Phase 1 implemented.** Build the fixed-30 configuration,
   numeric-card builder, local pinned Laya adapter, `decide_batch` call,
   deterministic top-10 ranking, and immutable per-date selection snapshot.
   Unit tests prove card shape, identity exclusion, fewer-than-ten behavior,
   retry replay, held/pending exclusion, deterministic ties, and Laya failure
   handling. Hermes, risk, execution, and existing baselines remain untouched.
2. **Selector wiring — new experimental path only. ✅ Phase 2 implemented (opt-in, unevaluated).** Expand the backtest
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

Any selector evaluation or controlled comparison, any backtest/training/
smoke/live-paper run using the selector, running the coverage preflight on
real data, calibration/benchmarking of Laya on real cards, Neon selector
evidence, a rules or risk-limit change, any change to
`watchlist/watchlist.json`, activation, and dashboard work.
