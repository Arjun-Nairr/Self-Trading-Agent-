# Backtest Langfuse tracing: acceptance (2026-11-06)

**Verdict: GREEN, closed.**
- The smoke run proved the wiring and trace quality.
- The closeout pass fixed the sizing gap, added episode-end tracing, and found and fixed an identity leak path
  (decision IDs) plus three misleading labels.
- All of that is verified **offline with fixtures**. The hosted smoke traces predate it.
- This was a no-fill smoke, so it proves nothing about the filled-order lifecycle, and nothing about profitability
  or trading benefit.
- The second corrections pass changes the decision path (prompt schema 5):
  - evaluation episodes are traced, including episode-end resolution;
  - news has an explicit empty vs. unavailable status;
  - BUY quantity is controller-owned.

  Fixture evidence only; no run or model call.

## What was built

- `backtest-harness/backtest_tracing.py`: an optional, export-only, fail-open adapter.
  - Reuses `live/tracing` (`otlp.Exporter` and `controller.Tracer`, by appending `live/` to `sys.path`); no live
    orchestration is imported.
  - `run_day` is wrapped once per run. While each day runs, its existing seams are wrapped, then restored on exit.
  - Every exported record passes one scrubber before export:
    - a real ticker or company name becomes the run's alias (the run's own masking patterns);
    - every UUID except the run ID becomes a keyed `ref-…` digest (see Closeout);
    - a record that cannot be scrubbed is dropped.
- Shared files, with live defaults unchanged:
  - `otlp.to_otlp` and `Exporter` gain a `scope` parameter;
  - `controller.Span.end` passes `usage` and `input_limit` through.
- `run_backtest.py`: +28 lines (unchanged in the closeout).
  - `_start_tracing` runs once the run ID, masking and selector exist.
  - `_close_active_tracing` runs in `main()`'s `finally`.
  - Tracing is off unless `~/.hermes-trading-agent-backtest/tracing.env` exists. That folder is outside the repo,
    and the adapter refuses the live state dir and the repo.
- **Labels:** `environment=backtest`, `service=hermes-backtest-controller`, `scope=hermes-trading-agent.backtest`.
  The session is the run ID. Span times are wall clock; `sim_date`, `day_index` and `day_label` are metadata.
- **Credentials:** the existing Langfuse project's `tracing.env` was copied into that folder (identical, and the
  live file is unchanged). No plugin, credential or tool was added to the base or disposable Hermes profiles.

## Closeout pass (fixture evidence only)

1. **Sizing gate.**
   - `sizing_check` now records `proposed_quantity`, `required_quantity`, `matches_proposal` and `outcome`:
     `accepted`, or `rejected: quantity X does not equal required whole-share size Y`, which is run_day's own rule.
   - `risk_check` now records `check_trade_allowed` (not `allowed`) and a `stage`:
     - `decision_time (not final: BUY sizing, pre-fill news gate and fill-time check follow)`, or
     - `fill_time`.
   - So a passing decision-time check can no longer read as final approval.
   - Test: the real `run_day` path (`check_trade`, then `required_entry_quantity`) runs with the exactly required
     quantity and with that quantity plus one. Each case asserts the span values, the queued or blocked result, and
     identical trading output with tracing on. The parent root's output also matches.
2. **Episode-end resolution.**
   - `eval_suite.resolve_episode_end_pending` becomes its own `backtest_episode_end` trace in the run's session,
     whenever a trace session is open and `eval_suite` is loaded.
   - It has one `journal.rejection` child per pending order, and records each order's actual outcome:
     `rejected; journal row written`, `journal write failed (<type>)` (the helper swallows that failure, so the
     trace is the only place it shows) or `not attempted`.
   - It also records the open positions kept. With no pending orders it records `no pending orders` and an empty
     list.
   - Tests cover 2 pending orders, 0 pending, a failing journal write, and a raising exporter. Each checks an
     identical return value, journal calls and portfolio, plus parent links, labels and aliases only.
   - Reachable since the second corrections pass (below): `eval_suite._run_episode` now starts a trace session.
     `laya_vs_random` still does not trace. The resumable `run_backtest` main runner deliberately never resolves
     pending orders at the window end: they persist for its next invocation.
3. **Identity leak path (found in review, fixed).**
   - Decision IDs are `uuid5(run_id | day_index | REAL ticker | action | kind)`. The traces exported them raw next to
     the alias, with the run ID and day alongside, so anyone with Langfuse access and this repo could brute-force
     the alias-to-ticker mapping.
   - The two hosted `journal.queued_decision` spans of the smoke run expose this for 2 aliases.
   - Fix: the scrubber replaces every non-run UUID with `ref-` plus HMAC-SHA256 under a local key
     (`~/.hermes-trading-agent-backtest/decision_ref.key`, created once and never exported). Refs stay stable, so a
     queued order still links to its later rejection.
   - Tested (raw ID absent, run ID kept, ref stable, key not exported).
   - **The 6 hosted smoke traces still carry the raw IDs.** Deleting them in Langfuse is the user's call; I did not
     mutate hosted data.
4. **Labels.**
   - A skipped day's root output is now `{"skipped": "<reason>"}`. Before, a holiday read as an empty session.
   - The root's `proposals` is renamed `queued_proposals`, since it never listed rejected proposals.
   - Both are tested.

**Final review** (whole backtest-tracing diff vs. the approved intent):
- Every wrapper calls the original with the same arguments, returns its result and re-raises.
- The live tracer guards `trace`, `span`, `set` and exit, and a failure creating the ref key turns tracing off at
  load.
- Live code and defaults are untouched in this pass.
- There is no live dir or live spool access, and no Hermes profile change.
- Historical fetches are `controller.fetch.*` spans, never tool spans.

**Checks:**
- `backtest-harness` 650 passed (631 + 19 tracing), `live` 240, `python verify.py` 13/13.
- Ruff 0.17.4 (`E,F,W,B`, 120 characters) is clean on `backtest_tracing.py`, `test_backtest_tracing.py`,
  `live/tracing/otlp.py` and `controller.py`.
- `git diff --check` is clean, and Round 2 `backtest_state.json` is still `4b428d35`.

## Second corrections pass (fixture evidence only; no run, no model call)

This pass changes the **decision path**: prompt schema `PROMPT_SCHEMA_VERSION` goes from 4 to 5. That version is
recorded per cycle in `backtest_cycle_evidence.prompt_schema_version`, alongside the run's `git_head_sha`/
`git_dirty`, which is the existing provenance mechanism (the run-level provenance columns are fixed, so no schema
change was made). Runs at schema ≤ 4 are preserved unchanged, including Round 2 (`backtest_state.json` still
`4b428d35`), the smoke run and the frozen V1/V2 results. **They are not decision-for-decision comparable with
schema 5.**

1. **Tracing in the evaluation runner** (`eval_suite._run_episode`).
   - Tracing starts after the run ID, masking and frozen configuration exist. It stays open through
     `resolve_episode_end_pending` and closes in `finally`, which restores the runner's `run_day` and the
     episode-end helper.
   - Manifest approval, run classification (`evaluation`), the isolated checkpoint and episode-boundary semantics
     are untouched.
   - `TraceSession` gained `run_day_owner`, because `eval_suite` imports `run_day` by name. The seams stay in
     `run_backtest`.
   - The credentials, spool and labels are the existing backtest ones, with `runner: eval_suite._run_episode` in the
     metadata.
   - `backtest-harness/conftest.py` (tests only) points every test at an empty tracing dir, so no test can read
     your home `tracing.env` and export. Confirmed: after the full suite your home folder still holds only
     `tracing.env`.
   - Test: the real `run_configuration` orchestration (only `run_day` and the journal mocked) with 1 pending order,
     0 pending, and a failing journal write. Each case runs with tracing off, on and failing, and the episode report
     and journal calls are identical across all three. The day trace and the episode-end trace share the run's
     session; the rejection is parented to the episode-end root; outcomes show `rejected; journal row written`,
     `no pending orders` or `journal write failed (RuntimeError)`; the decision ID appears only as `ref-…`; no real
     identity leaks.
2. **News status.**
   - `fetch_news` already fails the whole cycle on incomplete pagination or bad timestamps, so a key exists only for
     a complete, validated fetch. That distinction was discarded at one spot: a ticker missing from the provider
     reply became `[]`. It is now left out instead.
   - `run_day` stores `news_status` (`checked`/`unavailable`) on every candidate and position.
   - `build_prompt` renders one of:
     - the existing masked headlines;
     - `No headlines found in the checked window (7 days to the decision cutoff). This is not proof that no
       adverse news exists.`;
     - `UNAVAILABLE … no new entry may be opened`.

     A missing status renders as unavailable.
   - A BUY on a name whose news is not `checked` is rejected by the controller (§5a). Before, this case could not
     arise, because it was silently "empty". The pre-fill gate is unchanged, and its existing tests pass.
   - One side effect: a cycle checkpoint written before this change has no status, so a resumed mid-cycle replay
     rejects its BUYs (fail closed).
3. **Controller-owned BUY quantity.**
   - `run_day` computes the BUY quantity with the existing `required_entry_quantity`. It does so after the observed
     stop, the §5.3 candidate check and §9.1, against the decision-time context with cash reduced by earlier BUYs in
     this response (the existing reservation), then passes it to `check_trade`, the queue and the journal.
   - `None` gives `controller sizing gave unusable sizing inputs (entry …, stop …)`; `0` gives `… zero shares`.
     Nothing is fabricated.
   - The old equality gate is gone.
   - A **legacy BUY `quantity`** is accepted by validation whatever its value, never used, and reported in the
     decision line (`controller-sized; Hermes quantity 65 ignored`).
   - SELL quantity validation and exits are unchanged.
   - The fill-time re-check is unchanged and never resizes a queued order.
   - The prompt keeps the §2 formulas, labelled "The CONTROLLER computes every BUY's share quantity". The schema
     says `quantity` is for SELLs only, and the closing instruction says to omit it on a BUY and not to do share
     arithmetic.
   - The `sizing_check` span now records the owner, entry, stop, equity, remaining cash, limits, the computed
     quantity and the outcome. The rule check is labelled `decision_time (not final…)`.
   - Tests (`test_controller_sizing_and_news.py`, the real `run_day`): the two smoke proposals now queue 650 and
     557; risk-limited sizing gives 165, concentration-limited 199, cash-limited 99; two BUYs sharing cash give 199
     then 50; NaN input, a stop above entry and zero shares are rejected explicitly; a legacy quantity of 5000 or
     none both give 199; SELL is unchanged. The tracing parity tests (off, healthy, raising, outage, full queue)
     run on the corrected path, and Hermes quantities of 10, 9999 or none give identical trading output.

**Updated existing tests** (they encoded the old contract): BUY quantity validation, the placeholder test, the
multi-decision test, the schema-version assertion and one candidate-key assertion.

**Checks:**
- `backtest-harness` 668 passed, `live` 240, `verify.py` 13/13.
- Ruff is clean on the new files (`backtest_tracing.py`, both test files, `conftest.py`). The changed legacy files
  show no new findings versus HEAD (`run_backtest.py` 25/25, `eval_suite.py` 6/6, `test_execution_semantics.py`
  25/25, `test_laya_wiring.py` 8/8).
- `git diff --check` is clean.

**Hosted smoke traces still holding raw decision IDs** (left untouched; deleting them needs your authorization):
- `f1471db799ff0a91602a72183587231c` (2022-07-06: `backtest_day` output, `journal.queued_decision`);
- `5a41d4fd119d7046866d3fd4b4d5057d` (2022-07-07: `pre_fill_gate`, `journal.rejection`, `backtest_day`,
  `journal.queued_decision`);
- `2b4e6651b20b213196fb2bd92f266965` (2022-07-08: `pre_fill_gate`, `journal.rejection`).

The other 3 smoke traces contain only cycle IDs, which are not derived from tickers.

## Acceptance trial (hosted evidence)

- **Run:** `b0b16ce1-3dab-4a6d-9a3e-416d7825f7cd`, registered as `smoke`.
  - July 1–8 2022 with `--days 5`; July 4 was skipped by the calendar.
  - Seed 42, Laya `laya_fixed30_experimental` on the frozen pool `candidates60-v1-pool-2022-07-01-aafefb19a6c5`
    (48 names), checkpoint `55cf4c4e…`, image `hermes-laya-worker:2`.
  - Fundamentals off, no injection monitor. A fresh `--state-dir`; one launch.
  - Exit 0, 5/5 sessions, 1,035 s.
- **Evidence (private, outside Git):** the session scratchpad `bt_smoke/` folder holds `run.log`,
  `hosted_observations.json` (the `GET /api/public/v2/observations` read), `hosted_inspect.json` and the
  before/after snapshots.

6 traces and 109 observations, all `environment=backtest` with session = the run ID, 0 missing parents, all
DEFAULT, and no real ticker, company name or key. 7 model calls (5 decisions and 2 pre-fill) sum to **50,453
tokens, equal to the run log**. Prompts (≤ 33K characters) and replies are untruncated. No spool was left at exit.

| sim_date | trace id | obs | what the trace shows |
|---|---|---|---|
| 2022-07-01 | `97d3a41fd72b5f95574dabeb59d4ea4a` | 19 | BUY JR99 ×65; check_trade passed; sizing rejected (required 650) |
| 2022-07-04 | `34fcec8b624fc4322b4edc32789f4e05` | 3 | holiday: daily bars fetched (15.5 s) before the calendar said no session |
| 2022-07-05 | `5836afa6dff734a00f12b6d8b4d17ded` | 18 | no proposal |
| 2022-07-06 | `f1471db799ff0a91602a72183587231c` | 20 | BUY NN86 ×798 queued |
| 2022-07-07 | `5a41d4fd119d7046866d3fd4b4d5057d` | 23 | pre-fill gate: NN86 CANCEL; BUY DB72 ×277 queued |
| 2022-07-08 | `2b4e6651b20b213196fb2bd92f266965` | 26 | DB72 confirmed, then rejected at the fill (§6a, §2a, §6b); BUY JR99 ×63 sizing-rejected (required 557); reflection |

**Reconciliation:**
- New rows: 2 trades (NN86 `pre_fill_news_veto`, DB72 with the fill-time rule text, re-read SELECT-only in the
  closeout), 5 evidence, 5 snapshots, 1 insight, 22 activity, 92 workflow, 1 registry and 1 configuration.
- The two sizing-rejected JR99 proposals were never queued, so they have **no trade row**. They appear only in
  activity rows and the traces.
- All 28 earlier runs and the protected files are unchanged. Only the `backtest` and `live` profiles remain, with
  0 Laya containers.

## Behaviour assessment (smoke run, 5 sessions)

Historical Hermes has no tools, so this assesses its use of the evidence it was given. Sources: the generations'
masked prompts and replies, the gate and check spans, and the trade rows.

1. **Sizing arithmetic slips (agent mistake; controller enforcement worked).**
   - JR99 on Day 1 (`97d3a41f…`, hermes_call_1): Hermes states the §2b cap as "$20,000 … limits the position to 65
     shares". $20,000 / $30.74 = 650. On Day 8 (`2b4e6651…`, hermes_call_2): "limits size to 63 shares", where
     $20,000 / $31.34 = 638. There it even computed the binding §2a size, 557, correctly, then took the wrong
     minimum.
   - The other two BUYs did the same arithmetic correctly (NN86: 798; DB72: 277).
   - Consequence: 2 of 4 BUY proposals were lost to a dropped digit, not to judgment.
   - Smallest action: a decision, not code here. The controller already computes the exact required quantity and
     demands equality, so either supply that number in the prompt or let the controller set the quantity.
     **Done in the second corrections pass: the controller sets it (schema 5).**
2. **The fill-time re-check rejects most gapped BUYs by construction (controller and rule design, not an agent
   mistake).**
   - Required quantity is the exact maximum at the prior close plus 5 bps. The fill re-checks §2a, §2b and §6a at
     the next open and never resizes (`resolve_queued_order`, deliberate).
   - DB72 (`2b4e6651…`, fill-time `risk_check`): an open about 1.5% higher (about $62.37 vs $61.47) made the risk
     2.24% instead of 2.00%. The stop distance became 12.94%, over the 12.0% maximum (12.1% with tolerance), and
     reward:risk fell to 1.45.
   - From the arithmetic, any BUY whose size is set by §2a or §2b fails if the open is more than a few cents above
     the sizing price. This is inferred from code and this one case, not measured. Hermes' stop sat 0.44% inside
     the 12.1% ceiling, which made the §6a breach likely too.
   - Smallest action: measure the fill-rejection rate and gap sizes in the next run before changing anything.
     Then decide between quantity ≤ required and a sizing buffer.
3. **Missing news is treated inconsistently (agent and prompt presentation).**
   - JR99 had the same structure every day (bullish, S1 $27.75, R1 $33.95):
     - Days 1 and 8: proposed, with a derived 1.5 target above the only observed resistance.
     - Day 5: rejected because the derived target was "beyond the supplied observed resistance" and "no
       news/sentiment evidence was supplied".
     - Day 7: rejected again for missing news.
   - The prompt simply omits the news line when a name has zero headlines, so "none found" and "feed unavailable"
     look the same. AGENTS.md says to skip only when the feed is unavailable.
   - Smallest action: render an explicit "no headlines in window" vs. "news unavailable" line. That is a prompt
     change, so it is a decision for you. **Done in the second corrections pass (schema 5).**
4. **The news gates behaved as designed.**
   - The NN86 pre-fill CANCEL (`5a41d4fd…`, `pre_fill_gate` → `hermes_call_1`) was on "Citigroup … Lowers Price
     Target to $27" in the overnight window. That target is below Hermes' own $30.76, so the veto is defensible.
   - The DB72 CONFIRM was on insider buying. Positive news only confirmed and did not resize.
5. **Waste and latency (controller).** About 150–200 s per session:
   - Laya: 56–90 s (inference 32–65 s, history fetch 24–30 s);
   - the decision call: 39–56 s;
   - serial fetches: about 60 s (daily bars 15–25 s, technicals 15–18 s, news 10 s, calendar and insights about
     4.5 s each);
   - journal writes: about 5 s each, 25–30 s per day;
   - the pre-fill gate: about 20 s for one order.

   The holiday fetched 48 daily bars before the calendar check (19.5 s wasted). The daily-bars fetch and the Laya
   history fetch both pull bars for the same 48 names; their overlap is unverified. There were no retries (one
   attempt per decision).
6. **Labels that could mislead an evaluator.** These were fixed for future runs, and the smoke traces still show
   the old forms:
   - `risk_check.allowed: true` on rejected BUYs;
   - the holiday read as an empty session;
   - `proposals` meant queued only.

   Still true, and documented rather than changed:
   - `day_label` counts calendar days ("Day 5" is the second session);
   - pre-fill and decision generations are both `hermes_call_n` (the parent tells them apart);
   - Langfuse shows a list-price cost (about $0.19) although usage is covered by the subscription.

**What this cannot show:** no order filled, so there is no position, exit or P&L. This run establishes nothing about
profitability or trading benefit.

**Proposed next historical run (not launched; needs your go-ahead):**
- **Runner:** the evaluation runner, which now traces episode-end resolution. It uses the approved manifest
  `eval_suites/manifest_20260916T154923_30f4c12e0f7e.json` (v4, `status: approved`), episode 2022-07 `rally`
  (2022-07-01 to 07-31, about 20 sessions). It runs on the fixed 10-name default watchlist (no Laya selector) with
  fundamentals off, `run_kind=evaluation`, a fresh run ID, its own Hermes profile and cleanup, and a fresh checkpoint
  dir outside Git.
- **Comparability:** schema 5, so it is not decision-comparable with any earlier schema ≤ 4 run of this episode.
- **Command:** `cd backtest-harness`, then `python eval_suite.py run-one --manifest
  eval_suites/manifest_20260916T154923_30f4c12e0f7e.json --year 2022 --month 7 --regime rally --checkpoint-dir
  "$HOME/hermes-eval-runs/2022-07-rally-schema5/ckpt"`, with its output kept in a log next to that dir.
- **Predefined metrics:**
  - proposals per session;
  - controller-sizing rejections (unusable or zero);
  - the fill-time rejection rate by rule, with the gap size;
  - the pre-fill veto rate;
  - BUYs blocked for unavailable news, and how many candidates showed "No headlines found";
  - skip reasons;
  - fills, exits by type, and the R-multiple per closed trade;
  - episode-end dangling orders;
  - tokens and wall time per session.
- **Benchmarks:** SPY and an equal-weight hold of the 10 names over the window. Treat P&L as descriptive only at
  this sample size.

## Limitations

- **Not exercised hosted or with a real model:** fills, held positions, stop, target and time exits, queued SELLs,
  episode-end resolution, the new `sizing_check` and `risk_check` stage, `ref-` IDs, the skipped label, the
  evaluation-runner traces, the news-status lines, and whether Hermes follows the schema-5 contract (omitting BUY
  quantity, treating "No headlines found" correctly). These are covered offline only.
- The evaluation test mocks `run_day`; the real `run_day` is covered separately by the tracing parity and sizing
  tests.
- `laya_vs_random` is still untraced.
- The cash reservation after a queued BUY uses `quantity × entry`, while sizing uses entry plus 5 bps. This is
  pre-existing and unchanged; it under-reserves by about 5 bps per earlier BUY.
- Hermes' reasoning is visible only as reply text. `--usage-file` gives totals per call.
- Delivery: a run's last records are sent at exit if the background sender is quick enough. Otherwise they stay in
  `~/.hermes-trading-agent-backtest/trace_spool/` until the next traced run. There is no crash-proof delivery, and
  the run does not count spans emitted.
- Masking uses the run's known-cue patterns. A paraphrased identity in Hermes text would not be rewritten (the same
  ceiling as the prompt masker).
- One five-session window: no statement about typical agent behaviour.
