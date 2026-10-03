# Connected Laya → Hermes smoke test — 2026-10-03

**Verdict: RED: not launched.** The three integration fixes are done and
tested, and they are what this report delivers. The smoke run itself was not
launched: the documented coverage preflight refused every window checked, and
the task required stopping in that case. No Hermes call, Laya scoring of a
run, journal write, simulated fill, or Neon write took place. Round 2, the
frozen V1/V2 runs, the evaluation manifest, holdouts, the baseline watchlist,
and RULES.md are unchanged.

---

## 1. Implementation changes (all tested)

### 1.1 `--state-dir` (isolated run state)

The runner previously wrote all run-owned files to fixed paths in
`backtest-harness/`, where the preserved Round 2 `backtest_state.json`
(run `e68525f9…`, cursor 2026-04-29) lives. A fresh run there needs
`--reset --confirm-destructive-reset`, which deletes that file.

Tracing the actual writes found four run-owned locations, now all routed
under the new flag:

| File | Default (unchanged) | With `--state-dir DIR` |
|---|---|---|
| state | `backtest-harness/backtest_state.json` | `DIR/backtest_state.json` |
| cycle checkpoint | `backtest-harness/cycle_checkpoint.json` | `DIR/cycle_checkpoint.json` |
| Hermes-reply diagnostics | `backtest-harness/.diagnostics/` | `DIR/.diagnostics/` |
| selector snapshots + Laya worker log | `backtest-harness/.selector_snapshots/` | `DIR/selector_snapshots/` |

Behaviour of `apply_state_dir()`:
- **Ordering:** `main()` calls it immediately after argument parsing, before
  Hermes profile provisioning, any reset, any state read, run registration,
  or any write.
- **Refusals:**
  - the default harness directory under any alias (exact path, different
    case, `/.`, `../backtest-harness`), so an isolated run can never resume
    or `--reset` Round 2;
  - a path that exists but is not a directory.
- **Without the flag:** nothing moves.

Tests (`test_state_dir.py`, 8) drive the real `_run_backtest_main` up to its
first database call, with that call stubbed to stop the run. They cover:
- default locations;
- routing;
- refusals;
- a fresh run (new run ID, `smoke` registered, nothing written before a cycle
  commits);
- a fresh run without `--run-kind` failing before any write;
- resume (reads only `DIR`);
- reset (an unconfirmed reset deletes nothing; a confirmed one clears only
  `DIR`'s state and checkpoint);
- `main()` ordering.

Every test also asserts that the default-location state and checkpoint files
are byte-identical afterwards. Round 2's `backtest_state.json` sha256 is
`4b428d35…ba6d` before and after this task.

### 1.2 Experimental rules packaging `fixed30-universe-v1`

The masked RULES.md block in an experimental prompt still carried §8's
baseline ten-name table and its baseline-only prose ("Populated
2026-08-17…", "Ten names, ten sectors…", "keep the two in sync",
"Re-screening?"). The risk checker already used the run's frozen 30-name
watchlist, so Hermes was being shown a different allowed universe from the
one actually enforced.

The fix (`run_backtest.package_rules_for_experiment`, used only when the
selector is on):
- **What is replaced:** in the already masked text, only the part between
  `## 8. Watchlist` and `### Criteria used`.
- **What replaces it:** the run's 30 masked aliases with their sectors,
  sorted by alias, plus three explicit statements:
  - **allowed names** = this table (and what §1 "watchlist only" and §9
    refer to);
  - **new-entry candidates** = only this cycle's CANDIDATES section, i.e.
    today's shortlist;
  - **held positions** keep full exit management whether or not they are
    shortlisted.
- **What is unchanged:** every other RULES.md line is delivered as before,
  and the block header is the same.
- **Failure:** if the §8 headings ever move, it raises rather than silently
  sending the baseline table.

RULES.md and the selector-off prompt are untouched.

Tests (`test_experimental_rules_packaging.py`, 5, plus a `run_day`-level
assertion in `test_laya_wiring.py`) verify:
- all 30 aliases appear, with correct sectors and alias order;
- the obsolete baseline text is absent;
- the rest of the masked RULES.md is present verbatim;
- `assert_clean` passes (no real ticker or name);
- no Laya scores, ranks, `review_probability`, `answer_confidence`, or
  "Laya" in the block;
- the selector-off prompt still equals `mask_text(RULES.md)` with the
  baseline table;
- missing headings fail closed;
- RULES.md still lists the baseline ten.

### 1.3 Coverage preflight news rule v2 (`pre-start-3x14d-distinct-v2`)

**The established criterion** (RULES.md §8, `watchlist/check_news_coverage.py`)
is at least 6 articles summed over three 14-day lookback probes. The probes
were originally Feb / May / Aug, so they were spread out and never
overlapped.

**What v1 of `laya_preflight.py` did on short windows.** It put the probes
at (start+14 d capped at end, midpoint, end). On the July 1–8, 2022 window
they were 07-08, 07-04 and 07-08:
- two probes were the same window;
- the third overlapped them by 10 days, so one article counted two or three
  times;
- every probe used news from inside the episode being qualified.

**v2 keeps the criterion** (three 14-day probes, ≥ 6 articles, complete
pagination required) and changes three things:
- all probes end at 23:59:59 UTC the day before the first simulated session;
- they are spaced 45 days apart, never overlapping;
- articles are counted distinctly by (`created_at`, `headline`), because the
  provider path passes no article ID.

The report header prints the exact probe windows. Tests were updated to use
distinct articles, plus new regressions:
- an article seen by every probe counts once;
- probes end before day 1 and do not overlap;
- the healthy path asserts every probe ends before the start date.

This is equally strict. It removes double counting and lookahead; it does
not lower the bar.

## 2. Window selection (coverage and data availability only)

**Method:**
1. Start from the latest completed five US sessions.
2. Step back one calendar week at a time, at most three windows.
3. None of these weeks contains a market holiday.
4. Run the documented preflight (price: a bar every expected session, at
   least 40 warm-up bars, no malformed bars; news: rule v2) on the
   unchanged `fixed30-v1` universe.

Returns, trades, and Laya scores played no part. Earlier attempt, for the
record: July 1–8, 2022 under the v1 rule had 18 failures, including EQR with
0 price bars.

| Window | Started (UTC) | Exit | Failing names |
|---|---|---|---|
| 2026-09-28 .. 10-02 | 16:29:33 | 1 | SFM, BRX, OMC, TOL, LKQ, RJF, CINF, **EQR** |
| 2026-09-21 .. 09-25 | 16:31:20 | 1 | PLNT, OMC, TOL, CINF, **EQR** |
| 2026-09-14 .. 09-18 | 16:32:55 | 1 | OLN, WMG, PLNT, RYAN, ATI, ALGM, CF, OMC, TOL, CHD, CINF, STE, IDXX, **EQR**, ZBRA |

Distinct articles per name across the three pre-start probes (✗ = below 6;
every other name passed price with all 5 bars and 82–83 warm-up bars):

| Symbol | 09-28..10-02 | 09-21..25 | 09-14..18 |
|---|---|---|---|
| OLN | 18 | 20 | 4 ✗ |
| WMG | 6 | 7 | 1 ✗ |
| PLNT | 7 | 2 ✗ | 2 ✗ |
| SFM | 4 ✗ | 10 | 13 |
| DINO | 11 | 15 | 17 |
| RYAN | 15 | 17 | 4 ✗ |
| BRKR | 12 | 9 | 8 |
| ATI | 10 | 9 | 3 ✗ |
| BRX | 5 ✗ | 9 | 7 |
| ALGM | 10 | 8 | 5 ✗ |
| NUE | 20 | 31 | 20 |
| CF | 10 | 6 | 4 ✗ |
| OMC | 4 ✗ | 5 ✗ | 4 ✗ |
| LYV | 15 | 17 | 15 |
| TOL | 3 ✗ | 3 ✗ | 4 ✗ |
| LKQ | 2 ✗ | 7 | 7 |
| CASY | 11 | 50 | 54 |
| CHD | 9 | 9 | 1 ✗ |
| DVN | 15 | 20 | 16 |
| TRGP | 14 | 11 | 6 |
| RJF | 4 ✗ | 8 | 18 |
| CINF | 1 ✗ | 5 ✗ | 4 ✗ |
| STE | 8 | 7 | 2 ✗ |
| IDXX | 11 | 10 | 1 ✗ |
| PWR | 18 | 19 | 13 |
| ROL | 8 | 12 | 22 |
| EQR | 3 ✗, price ✗ | 4 ✗, price ✗ | 7, price ✗ |
| WPC | 15 | 16 | 10 |
| TER | 22 | 26 | 26 |
| ZBRA | 20 | 17 | 0 ✗ |

**The remaining blocker, exactly:**
- **EQR fails price coverage in every window:** 0 bars in the window and 1
  warm-up bar, matching the standalone demo's single 2026-08-17 bar.
  It also had 0 bars in July 2022.
- **News:** OMC, TOL and CINF fall below 6 distinct articles in all three
  windows. Other names cross the threshold from week to week; the same
  three 45-day-spaced probes shifted by only 7 days move totals a lot
  (CASY 11 → 50; ZBRA 20 → 0).
- **Consequence:** with the universe frozen and the requirement unchanged,
  no recent window can pass.

The cause of EQR's missing provider history was not established, and
nothing was guessed.

**What would unblock it is a user decision:**
1. A new universe version (e.g. `fixed30-v2` replacing EQR, and possibly
   OMC/TOL/CINF, with the same a-priori method); and/or
2. an explicit rule for running with a documented news-coverage shortfall
   (today that is treated as a failure, not as harmless).

## 3. Readiness state at stop

The launch-time checks were not repeated, because nothing was launched. From
the previous attempt (same day):
- `trade-journal`, `execution-sim` and `alpaca-mcp` match the repository;
- `maverick-mcp` matches 93/96 files (3 dev scripts not in the image);
- the gateway runs the pinned digest;
- `google-news-rss-mcp` is stale but outside the backtest path;
- the `hermes-laya-worker:2` handshake matches the pinned checkpoint and
  `noul-criteria-v2`.

The preflight runs in this task made only read-only provider requests:
maverick prices and Alpaca historical news.

## 4. Verification

| Check | Result |
|---|---|
| `test_state_dir.py` | 8 passed |
| `test_experimental_rules_packaging.py` | 5 passed |
| `test_laya_wiring.py` (incl. preflight v2 and the `run_day` packaging assertion) | 42 passed |
| Full harness suite | **480 passed** (466 before + 14 new; three preflight tests and one wiring assertion updated for v2/packaging, none removed) |
| `python verify.py` | **12/12 suites passed** |
| `pytest watchlist` | 156 passed |
| `git diff --check` | clean |
| Round 2 `backtest_state.json` | sha256 `4b428d35…ba6d`, unchanged; no default `cycle_checkpoint.json` created |

## 5. Scoped audit

- **State isolation:**
  - every run-owned write found by tracing is covered;
  - the default directory is refused under aliases;
  - the default behaviour is byte-identical when the flag is omitted (the
    existing selector-off and Round 2 resume tests pass unmodified);
  - `eval_suite` does not call `main()` and is unaffected.
- **Default-path regressions:**
  - packaging is gated on `selector is not None`;
  - `eval_suite` never passes a selector;
  - the selector-off prompt test asserts the exact masked RULES.md.
- **Identity leakage:**
  - the experimental §8 block contains only aliases and sectors, in alias
    order, so baseline/added grouping is not revealed;
  - it passes `assert_clean`;
  - it holds no Laya outputs.
- **Historical coverage gating:**
  - news eligibility now uses only pre-start news, counted distinctly;
  - price coverage still checks for bars in the window, which is a
    data-availability check for the simulation and not an eligibility
    input, and is recorded as such.
- **Accidental activation:**
  - no Hermes, run, journal or Neon write;
  - no screener run;
  - the selector stays opt-in.
- **Unrelated edits:**
  - none;
  - user-owned dirty files were left as they were;
  - runtime outputs (preflight logs) stayed in the scratchpad.

## 6. Next step

Approve a universe decision (above). Then the single smoke run can launch
unchanged:

```
python backtest-harness/run_backtest.py --state-dir <fresh dir outside the repo> --run-kind smoke \
    --selector-mode laya_fixed30_experimental --laya-image hermes-laya-worker:2 \
    --laya-checkpoint <pinned snapshot dir> --start <window start> --end <window end> --days 5
```

Fundamentals stay off (no `--enable-fundamentals`). Note that `run_backtest.py`
does not resolve still-pending orders at the end of an invocation; they stay
pending for a resume. Only `eval_suite` resolves them at an episode boundary.
The smoke report must state this.
