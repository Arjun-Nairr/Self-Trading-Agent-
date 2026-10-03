# Laya selector — standalone current-data demo, 2026-10-03

**Result: the demo works end to end on real data.** One command fetches real
prices for the frozen fixed-30 universe, builds the seven-number cards, scores
them in one batch in the pinned, isolated Laya worker container, selects a
top 10, saves a write-once artifact, and replays it without fetching or
scoring.

The selection itself has a clear limitation: before scoring real companies,
the project's Laya question turned out to be one the English checkpoint
answers "no" regardless of the card. It was corrected to a versioned format
(below). Even so, the corrected question discriminates only weakly, and on
real cards every score sat below 0.5 in a narrow band. The ranking is a
demonstration of the machinery. It is **not** evidence of market judgment,
calibration, or usefulness, and nothing here is a buy signal.

Scope held: no Hermes call, no trade or order, no backtest or evaluation run,
no live-paper activation, no rules/risk/Neon schema/dashboard change, and the
scalable screener was not run.

---

## 1. Command

```
python backtest-harness/laya_demo.py run
python backtest-harness/laya_demo.py replay backtest-harness/.laya_demo/<cutoff>_<run time>/artifact.json
```

Prerequisites:
- the compose `maverick-mcp` service running;
- the worker image `hermes-laya-worker:2`;
- the pinned checkpoint in the standard Hugging Face cache, or `--checkpoint DIR`.

Output (git-ignored), under `backtest-harness/.laya_demo/`:
- `artifact.json`: shareable, with no tickers or names;
- `private_mapping.json`: row ID → ticker, plus raw fetch errors;
- a worker log.

## 2. Question-format concern — research and decision

**Source check.** The installed `laya` 0.3.23 is the version this project
pins. Its README ("Honest limits") and upstream `main` (0.3.24, checked
2026-10-03) say the same three things, and the package code
(`common.render_options`) confirms the first:

- A `noul` with no `criteria` renders one generic option pair for every
  state: "no, the statement does not hold" / "yes, the statement holds".
- On the English `laya` checkpoint, that pair "carries the whole decision,
  so it answers 'no' whatever the state".
- Boolean-word labels can dominate on this checkpoint (upstream issue #156,
  reported against 0.3.4 and closed). No retrained English checkpoint
  changes this for our pin.

**Our schema was affected.** v1 asked the question as a JSON-schema boolean.
laya's `structured._noul_field` turns that into a `noul` with
`instructions` only, with no criteria, which is exactly the generic form.

**Synthetic diagnostic.** The diagnostic used only synthetic identity-free
cards, the pinned checkpoint in-process on CPU, and Python sockets denied
after load. The script lives in the scratchpad and was not committed. It
compared four formats:
- **F0:** the current v1 path.
- **F1:** an explicit `noul` with true/false criteria describing a notable
  versus an ordinary card, with default labels.
- **F2:** F1 with neutral `A`/`B` labels.
- **F3:** F2 with the labels swapped.

| Format | Sample 1 (10 quiet / 10 active / 30 random): AUROC active>quiet | Sample 2, held-out (40 / 40): AUROC | Share of cards < 0.5 | Score σ |
|---|---|---|---|---|
| F0 v1 (no criteria) | **0.40** | **0.364** | 100% / 98.8% | 0.05–0.12 |
| F1 criteria, default labels | 0.695 | 0.659 | 100% | 0.016–0.017 |
| F2 criteria, A/B labels | 0.635 | 0.605 | 100% | 0.025–0.028 |
| F3 criteria, B/A swapped | 0.56 | 0.588 | 100% | 0.021–0.024 |

Other measurements:
- F0's scores vary, but in the *wrong* direction: busy cards rank below quiet
  ones. So "varied scores" on v1 did not mean the question was being read.
  F0's median `answer_confidence` was 0.96, a confident "no".
- **One-feature sweeps:** each feature was moved alone from a quiet baseline.
  F1 moved the score by at most 0.039 (realized volatility); F2 and F3 by at
  most 0.066–0.074 (five-day return). Moves were often non-monotonic. F0
  moved more (up to 0.18), but not in the direction the question asks.
- **Label swap:** F2 vs F3 correlate at 0.92, so the A/B text does not
  dominate. Repeat runs were byte-identical.

**Decision: v2 = F1** (`QUESTION_FORMAT = "noul-criteria-v2"` in
`laya_adapter.py`). It is the smallest compatible change and measured best on
both samples:
- same package, checkpoint, `decide_batch` path, batch size, and P(true)
  meaning;
- no labels override, no model upgrade, no fine-tuning;
- the criteria text is now part of the scorer's `settings`, so worker
  handshakes, backtest snapshots, and demo artifacts all pin it.

The criteria wording is itself a design choice: it tells the model that
"notable" means a large move, a stretched price or RSI, a strong MACD swing,
or unusual volume. The model's ranking reflects how it reads that text
against the numbers.

**Worker contract recheck.** The worker image now carries the new adapter,
so it was rebuilt under a new tag, `hermes-laya-worker:2` (id
`sha256:fe5163cd…`). The pinned dependency layers were reused from cache, and
the three modules inside match the repository by SHA-256. Against the real
checkpoint:

| Check | Result |
|---|---|
| Handshake | matches v2 |
| Live `docker inspect` isolation | no problems |
| Repeat batch | identical |
| Containers left after close | none |
| Old `:1` image | refused: "worker handshake does not match the pinned scorer" |
| Container vs Windows in-process F1, 50 cards | 49 identical; 1 differs by 0.0001 (a 4th-decimal rounding boundary); same top-10 set |

The demo scores only in the container. The existing opt-in backtest selector
mode uses the same adapter, so it now also asks v2; it has never been run on
real data.

## 3. The real run

| | |
|---|---|
| Started | 2026-10-03T15:02:38Z (11:02 ET, Saturday); exit 0 |
| Cutoff | **2026-10-02** (Friday). The latest allowed date is 10-02, because a Saturday before 17:00 ET allows the previous day. 29 names had the 10-02 bar; no later bar was used. |
| Code | HEAD `79afa75` + this change set (uncommitted at run time; `git_dirty` recorded) |
| Scorer | laya 0.3.23, checkpoint `convaiinnovations/laya@55cf4c4e…`, weights `891102d3…`, `noul-criteria-v2`, image `hermes-laya-worker:2` (`sha256:fe5163cd…`), loaded once, one batch |
| Universe | `fixed30-v1`, hash `ef837768785a…`, 30 names |
| Timing | total **74.4 s**: price fetch 31.9 s; model load + one 29-card batch 42.4 s. Wall clock 78 s. Replay: 0.6 s. |
| Artifact | `backtest-harness/.laya_demo/2026-10-02_20261003T150238Z/artifact.json` (sha256 `3fcf149e…`; git-ignored, not committed) |

Printed output (verbatim):

```
Laya fixed-30 demo -- cutoff session 2026-10-02 (run 2026-10-03T15:02:38+00:00; latest completed weekday)
  universe fixed30-v1 (30 names, hash ef837768785a); scorer laya 0.3.23 @ 55cf4c4ebb4e, question noul-criteria-v2
  considered: ALGM, ATI, BRKR, BRX, CASY, CF, CHD, CINF, DINO, DVN, EQR, IDXX, LKQ, LYV, NUE, OLN, OMC, PLNT, PWR, RJF, ROL, RYAN, SFM, STE, TER, TOL, TRGP, WMG, WPC, ZBRA
  cards scored: 29 of 30 (one batch); selected 10 of k=10

  rank  name   review_probability  answer_confidence (logged only)
     1  DVN                0.2398             0.7602
     2  ALGM               0.2234             0.7766
     3  ROL                0.2231             0.7769
     4  OLN                0.2199             0.7801
     5  TRGP               0.2108             0.7892
     6  RYAN               0.2087             0.7913
     7  SFM                0.2038             0.7962
     8  ZBRA               0.2025             0.7975
     9  PWR                0.1997             0.8003
    10  LKQ                0.1980             0.8020
  all scored: min 0.1605  max 0.2398  distinct 29/29  (rank 10 vs 11 gap: 0.0009)
  unavailable: EQR (insufficient_or_degenerate_history)
  elapsed: total 74.4s (fetch 31.9s, model load + score 42.4s)
```

**Missing data: EQR.** maverick returned a single EQR bar, dated 2026-08-17.
A direct read-only yfinance query in the same container also returned one
row in six months and no current market price. The card was therefore
incomplete and was not scored. The cause is external to this code; whether
it is a corporate action or a provider gap was not established. This is also
the first real evidence on the fixed-30 additions, which were never
coverage-checked (the preflight has still not been run).

**What the real scores look like:**
- **Range:** all 29 scores fall in 0.1605–0.2398 (median 0.193, σ 0.017), so
  the model says "no" to every name. The ranking is relative within that band.
- **Ranks 10 and 11** differ by 0.0009, which is nine units of the model's
  4-decimal output.
- **`answer_confidence` is exactly 1 − p on every row.** For a `noul` it is
  max(p, 1 − p), and all p < 0.5, so it carries no independent information.
  It is also higher for *lower*-ranked names.
- **Correlations across the 29 real cards:**

  | Feature | Score correlation | Correlation with distance from neutral |
  |---|---|---|
  | five_day_return | +0.26 | +0.17 |
  | sma20_distance | +0.10 | +0.27 |
  | rsi14 | +0.24 | −0.13 |
  | macd_direction | −0.07 | +0.13 |
  | relative_volume | +0.25 | +0.33 |
  | realized_volatility | +0.39 | +0.39 |
  | range_position_20d | +0.22 | +0.05 |

  These are modest, mostly the sign the criteria describe, and led by
  volatility. They are 29 points from one day, not a validation.

## 4. Verification

| Check | Result |
|---|---|
| New tests (`test_laya_demo.py`, 19) | **pass**. They cover: cutoff (ET clock and 17:00 buffer; a partial Friday bar excluded on a Friday-afternoon run; holiday moves the cutoff back with a note; a name missing the cutoff bar is stale and never sent to the model); missing data (fetch error, short history, provider omission, fewer than 10 → all scorable selected, no padding); one batch; rank by probability with confidence ignored; no ticker/name in the artifact and only the seven floats in model inputs; per-run salted row IDs; write-once; replay without fetch/score; three tamper cases plus a re-hashed row swap rejected; scorer/fetch/no-data/no-cards failures named by stage with nothing written; CLI exit codes 2 (preflight) and 1 (other). |
| Adapter tests | updated to v2 (explicit criteria, criteria in scorer identity); 14 pass |
| Laya/card/selector/hardening/wiring/anonymize suites | 221 passed |
| Full harness suite | **466 passed** (447 before + 19 new; no existing test removed) |
| `python verify.py` | **12/12 suites passed** |
| `pytest watchlist` | 156 passed (unchanged) |
| Real artifact | no ticker or company name (word-boundary scan of all 30 symbols and names); replay verified |
| State after the run | `git status` fingerprint identical before and after (`045c4300…`); no worker container left; Neon not touched (no DB code path) |

## 5. Scoped audit of the diff

- **Trading path:** `laya_demo.py` imports `run_backtest` lazily inside
  `run`, and uses only `fetch_card_history`, a read-only maverick price read.
  Importing `run_backtest` defines constants only (0.6 s; it loads no Laya,
  torch, or database module). The demo never calls `run_day`, Hermes, the
  journal, execution, or risk code. The selector-off backtest path is
  untouched (its equivalence tests pass).
- **Identity:** model input is the seven rounded numbers. The artifact has no
  identities, and the row-ID salt and mapping exist only in the git-ignored
  private file.
- **Duplication:** everything is reused except three small demo-only pieces:
  - the cutoff rule (a majority vote similar in spirit to `laya_preflight`'s
    expected-day vote);
  - a 6-line write-once helper (the same temp-file + `os.link` pattern as
    `write_snapshot`, which is snapshot-specific);
  - the summary printer.
- **Unrelated edits:** none. Tracked changes:
  - the adapter's question;
  - the worker image tag (code, Dockerfile comment, `--laya-image` help);
  - the adapter tests;
  - `.gitignore`;
  - the scope and backlog docs.
  User-owned dirty files were left as they were.

## 6. Limitations

- Every real score is below 0.5, and the band is narrow. Small numerical
  differences, such as the 0.0001 host/container rounding drift, can reorder
  neighbours; ranks 10 and 11 are 0.0009 apart.
- State sensitivity is weak even after the correction. Synthetic AUROC of
  0.66–0.70 against hand-built extremes says nothing about real-market value.
- `answer_confidence` is 1 − p here, uncalibrated, and the provider still
  warns about invalid shipped temperatures.
- The cutoff rule relies on provider bars plus a 17:00 ET clock rule, not an
  exchange calendar; a holiday or provider lag is flagged, not resolved.
- This is one run on one day, current data only. It is not point-in-time
  history, not survivorship-free, and the 20 added names are still not
  coverage-checked (EQR failed here).

## 7. Smallest next block

Wire the demonstrated selector to the project through the path that already
exists: one opt-in `--selector-mode laya_fixed30_experimental` smoke backtest
(a few trading days) with `hermes-laya-worker:2`. Run `laya_preflight.py`
over the same window first, and resolve EQR (replace it as a new universe
version, or record it as unavailable). Then compare its selection snapshots
against a seeded random-10 arm on the same dates. That gives the first
Hermes-side evidence while staying experimental, and it needs its own
approval because it calls Hermes.
