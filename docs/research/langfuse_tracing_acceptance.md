# Langfuse live tracing — acceptance report (Tickets 1–3)

Date: 2026-11-02 (closed 2026-11-06). Baseline pinned at commit `de3102b` (nothing staged or committed). Status:
**ACCEPTED.** Hosted verification is complete (§5, run 2: controller, Laya, Hermes, model and 11 MCP tool
observations). Final-record delivery is solved by the approved exit spool (§4: offline tests plus a hosted
replay/dedup probe). Agent behaviour in that trace: `langfuse_agent_behaviour_2026-11-06.md`. Ticket 1 detail: `langfuse_ticket1_gateway_feasibility.md`.

## 1. What is traced now

| Path | Trace | Evidence |
|---|---|---|
| Live cycle (controller) | `live_cycle` root: mode, code version (`release`), policy digest, orders enabled/disabled; reconcile, selection, prompt assembly, Hermes research, reply validation, holdings actions (with the decision as input), entry validation (plan in, plan id out), reconcile after. Reconcile-only ticks say what they skipped. | offline + **real read-only run** |
| Broker | `broker.submit/cancel/replace` = attempts (`operation: mutation`, errors included); `broker.order_by_client_id/orders/positions/latest_quote/news/daily_bars` = observations. Order fields only; no account identifiers. | offline + real (reads) |
| Fills / transitions | Engine events mirrored as `engine.*` event observations (`order_filled`, `exit_settled`, `position_closed`, …). Repeated reconciliation observations are never counted as fills. | offline |
| Journal (Neon) | `journal.decision/result/exit` with plan id; idempotency keys untouched. | offline |
| Laya selection | `selection.universe` (screen identity), `selection.laya` (event, snapshot sha, inputs digest, `inference: ran | snapshot reuse`, full ranking by ticker), `laya.fetch_history`, `laya.inference` (generation: question, checkpoint, identity-free numeric cards, scores), `selection.exclusions` (held/pending removed in rank order, delivered list). Worker untouched: `--network none`, no credentials. | offline + **real inference** |
| Weekly screen | Own trace `weekly_screen` via `python live/trace_screen.py --write`: `screen.universe/facts/history/news` with inputs, row counts, errors, stats; root: screen id, attempt id, content/pool hash, effective date, counts, failure reasons, config, persisted/reused. Screener file unmodified. | offline only |
| Hermes (Ticket 1) | `hermes_run` → `llm_call_n` → MCP tool spans, nested under `hermes_research`. | real gateway (earlier) |
| Correlation | One trace links cycle → screen id / snapshot sha → plan id → client order id (`lv-<plan>-…`) → broker order id → journal rows. No trading identifier changed (tested). | offline |

## 2. Verification

- Tests: `pytest live/` **225 passed** (baseline 170). New: `live/test_tracing_pipeline.py` (13),
  `live/test_tracing_corrections.py` (34, §7). `verify.py` **13/13**
  (backtest 631). `backtest_state.json` `4b428d35`. Shared selector/screener/backtest code: **0 diff lines**.
- Parity: tracing off / null / healthy / throwing / stalled / queue-full / unreachable over 8 cycle scenarios identical
  (decisions, broker calls, journal rows, state); traced vs plain LiveSelector identical (inference, reuse, failure);
  traced vs plain weekly screen records byte-identical; dry-run guard refusals identical with an errored attempt span.
- Redaction/bounds: Ticket 1 suite plus broker payloads (dummy secret in a news article absent from the wire; 50 KB body
  truncated and marked).
- Ruff 0.17.3, pinned rule set `--select E,F,W,B --line-length 120` (the release's default enables 409 rules, incl. a ban
  on `except Exception`, which tracing deliberately uses): **all feature-owned files pass**. Remaining findings in the
  shared files are on pre-existing lines (`run_live.py` 3×E501, `selector.py` 1×E501, `agent.py` unused import).
- No type checker is configured for `live/`; none was run.

## 3. Real read-only run (local evidence; NOT the hosted demonstration)

`dry-run --universe watchlist` (explicit ten-name universe — no usable weekly screen exists), no `--hermes` (no model
session), scratch state dir, read-only broker, tracing to a local capture server with dummy keys, `--trace-drain 5`.
One connected trace, 15 spans: real broker reads; `laya.fetch_history` 13.1 s; `laya.inference` 35.6 s in the
isolated worker (10 cards, question and checkpoint recorded, scores per ticker); full ranking; 10 delivered, none
excluded; `hermes_research` errored by design (dry run without `--hermes`). Zero submissions (read-only guard); no
account identifiers on the wire; **0 leftover Laya containers** after every run.

| Measured (Windows host, Docker Desktop) | tracing off (2 runs) | tracing on (2 runs) |
|---|---|---|
| wall | 66.9 / 78.7 s | 73.5 / 72.9 s |
| controller process peak working set | 31.8 MB | 32.2 / 32.4 MB |
| controller CPU (user+kernel) | 0.78 / 0.94 s | 1.07 / 0.90 s |
| Laya worker peak memory / CPU | 2.09 / 2.05 GB, ~785 % | 2.04 / 2.05 GB, ~795 % |
| diagnostic drain wait | — | 0.02 s (15/15 delivered) |

Tracing overhead is below run-to-run noise (±6 s, dominated by Laya); per-span cost ~130 µs (Ticket 1). Peak RAM of the
run is the Laya worker (~2.1 GB) + the controller (~32 MB) + the already-running service containers. **No ARM/cloud fit
is claimed.** Hermes was not part of this run; its cost was measured in Ticket 1 (hooks < 1 ms per turn end).

## 4. Delivery of final records (Ticket 1 carry-forward) — exit spool built 2026-11-06

Normal tracing never waits. Measured what survives a one-shot exit (local server with added latency):

| Process | latency | normal (no wait) | with diagnostic drain |
|---|---|---|---|
| controller cycle, 38 spans | 0 / 150 / 400 ms | 31 / 1 / 1 delivered, **root never** | 38/38 each, +0.02 / 0.31 / 0.8 s |
| Hermes plugin, exits at once | 0 / 150 / 400 ms | 0 / 0 / 0 | 41/41 each, ≤ 0.84 s |
| Hermes plugin, real ~0.3 s linger | 0 / 150 / 400 ms | 41 / 41 / 1 | 41/41 each |

The diagnostic drain (`run_live.py dry-run --trace-drain S`, 0 < S ≤ 10, refused for cycle/loop/setup-profile, also
passed to that one Hermes call) is for orders-disabled verification only. **Normal one-shot tracing is therefore not
complete**: `cycle` and `dry-run` without the drain routinely lose the root. `loop` loses only what is queued when it is
stopped.

Smallest proposal for normal delivery (not built — needs your approval): **exit spool.** At `close()` the exporter
writes whatever is still queued (already redacted, ≤ 512 spans, capped at 2 MB) to `<state-dir>/trace_spool/<pid>.jsonl`
— a local append, typically a few ms, no network — and the next process's background worker sends and deletes spool
files first. Hermes would spool inside its profile directory and the next Hermes call sends it. Cost: a bounded local
write at exit, delivery delayed until the next run (≤ 5 min in `loop`), a capped directory. Alternatives: accept the loss
and rely on `loop`; or keep using the drain for verification only.

## 5. Hosted demonstration (Ticket 3) — done 2026-11-06 (run 1 incomplete, run 2 complete)

### Run 1 (04:27 ET)

`setup-profile` (plugin installed in the live profile only), then one
`dry-run --hermes --universe watchlist --trace-drain 5` (orders disabled, read-only broker, scratch state; explicit
ten-name universe, no usable weekly screen). Read back from Langfuse Cloud (server 4.76.0) through
`GET /api/public/v2/observations` (the v1 `traces` read API answers 410 on this account; its notice says the v1 read
APIs are removed on 2026-11-16 — ingestion over OTLP is the supported path and is what tracing uses).

- **One trace, 23 observations, root `live_cycle` delivered, 0 observations with a missing parent.** Controller 21
  (drain: 21/21 sent in 1.34 s, 0 dropped) + Hermes 2: `live_cycle > hermes_research > hermes_run > llm_call_1`.
  All levels DEFAULT.
- Root metadata: mode dry-run, orders "disabled (read-only broker)", release `de3102b+dirty`, policy digest,
  universe source; environment `live-paper`, session `live-20261106`.
- **Laya:** `laya.fetch_history` 19.2 s (10/10 returned), `laya.inference` 44.3 s (generation; question, checkpoint,
  10 numeric cards, 10 scores), `selection.laya` labelled `inference: ran: 1 scoring call(s)`. **All 10 rows
  `data_unavailable` / "stale inputs: latest bar predates the as-of date"** — the run was at 04:27 ET, before the
  day's bar exists, so the selector (unchanged) delivered an empty shortlist; the trace states why, per ticker.
- **Model request:** `llm_call_1` model `gpt-5.6-terra`, provider usage 10,554 in / 115 out / 56 reasoning tokens;
  3 tool names (schemas not exported); instructions clipped by Hermes (note present), message 0 clipped at 4,000
  chars; `complete: false` — the labels are honest, but the research prompt is only partly visible.
  Langfuse itself priced the call ($0.023) from model + usage; tracing exports no cost.
- **Gap:** the model answered "market closed, no entry" without calling a tool, so **no hosted MCP tool span was
  observed** (tool spans are covered offline and by the earlier local gateway run).
- **Why every row was stale (a defect, not just "market closed"):** `AlpacaBroker.last_completed_session` counted
  today as completed whenever the market was closed, which holds after the close but not pre-market. At 04:27 ET it
  returned 2026-11-06, a session that had not traded, while the latest bars were 2026-11-05. Fixed in
  `live/adapters.py`: today counts only once its calendar close (early closes included) has passed. Freshness rule
  unchanged. Regression test `test_a_closed_market_before_the_open_has_not_completed_today` (failed before the fix).
  In the real loop, reviews run only while the market is open, so the defect affected out-of-hours dry runs.
- **Zero broker submissions/cancellations/replacements** (none in the trace; read-only guard). Broker reads:
  `broker.positions` ×5, `broker.orders` ×1. No Langfuse key in any exported byte (checked against the read-back).
- **No leftovers:** 0 Laya worker containers; inside the Hermes container only the supervised long-running services.

| Measured (Windows host, Docker Desktop), this run, tracing on | value |
|---|---|
| wall (whole dry run, incl. Laya and Hermes) | 137.2 s (`live_cycle` 132.1 s; selection 69.0 s; `hermes_research` 22.0 s, of which the model call 6.8 s) |
| controller process peak working set / CPU | 36.9 MB / 2.8 s (user 1.31 + kernel 1.48) |
| Laya worker peak memory / CPU | 2.14 GB / ~784 % |
| Hermes container peak memory / CPU (gateway, during the run) | 390 MB / ~91 % |
| diagnostic drain wait | 1.34 s |

### Run 2 (06:32 ET, after the fix): all required evidence observed

Same command; Langfuse read-back through the v2 observations API.

- **One trace, 39 observations, root `live_cycle`, 0 missing parents, all DEFAULT.** Controller 21/21 sent (drain
  1.33 s). Hermes: `hermes_research > hermes_run > llm_call_1..3`.
- **Candidate data and selection:** session 2026-11-05 (pre-market: the last completed session); `laya.fetch_history`
  10/10 returned, as-of 2026-11-05; `laya.inference` 47.4 s; `selection.laya`: `inference: ran: 1 scoring call(s)`,
  scored 10, data_unavailable 0, every row `freshness: current`; ranking PLNT, OLN, DINO, ALGM, WMG, RYAN, ATI, BRX,
  SFM, BRKR; exclusions none; 10 delivered.
- **MCP tool calls, as individual observations nested under the model request that asked for them:**
  `mcp__maverick_mcp__market_data_get_market_overview` (input `{}`, 7.5 s) and 10 ×
  `mcp__maverick_mcp__technical_get_full_technical_analysis` (input e.g. `{"ticker": "ALGM", "days": 120, "as_of":
  "2026-11-05"}`, 0.37–3.8 s, results of ~2.8 KB each: trend, outlook, RSI, ...), all children of `llm_call_2`.
  There were also 3 `tool_describe:` lookups under `llm_call_1`. Data observed in a result: the market overview
  returned `NaN` index prices pre-market (a maverick data issue, not tracing).
- **Model requests:** `gpt-5.6-terra`; provider usage per call (e.g. 672 new + 10,752 cache-read input tokens). The
  history was cited, not repeated (`llm_call_3` exports 6 of 33 messages and inherits 0 and 1–8). Instructions:
  `llm_call_2` `same_as` (identical, labelled Hermes-clipped); `llm_call_3` `prefix_of` + `observed_chars` because
  Hermes clipped that larger request's copy to 1,000 chars. That is the correction-pass labelling (§7) on real data.
  All three are `complete: false` (clipped text).
- **Outcome:** Hermes proposed no entry ("market closed; ... fresh market-open data would be required"); reply
  accepted. **Zero broker submissions/cancellations/replacements**; reads only (`broker.positions` ×5,
  `broker.orders` ×1). No Langfuse key in the read-back. **0 Laya worker containers left; in the Hermes container
  only the supervised gateway.** No separate tool-tracing probe was needed.

| Run 2 (tracing on) | value |
|---|---|
| wall | 180.8 s |
| controller peak working set / CPU | 36.8 MB / 2.7 s |
| Laya worker peak memory / CPU | 2.09 GB / ~778 % |
| Hermes container peak memory / CPU | 330 MB / ~101 % |
| Langfuse units | 40 (1 trace + 39 observations) |

No tracing-off counterpart with Hermes was run; overhead without Hermes is below noise (§3). **No ARM/cloud fit is
claimed.**

Monthly usage estimate (Langfuse units ≈ traces + observations; observed counts, existing schedule; allowance per
the earlier research note, unverified): reconcile-only tick 3 units with no positions, ~5–9 with positions (71/day);
review cycle 15 units observed without Hermes or positions, ~38 with an entry, plus Hermes 7 (tiny) to ~60 (real
research, estimated) (~7/day); weekly screen ~6. ≈ 370–1,300 units/day ≈ **8k–27k/month** over 21 sessions — under
50k. Observed in the hosted run: a review cycle with Hermes = 1 trace + 23 observations = 24 units (no entry, no
tool calls, no positions) — inside the estimate; tool-heavy research days remain the upper end.

## 6. Review (one pass, two axes)

**Standards** (repo conventions: stdlib-only tracing, docstring headers, behavioural tests at public seams, fail-closed
isolation; pinned Ruff): fixed — line continuations collapsed by shell edits (`otlp.py`, `run_live.py`), fixture
imports redefined in three test files (moved to `live/conftest.py`), a weak exit-parity test (now compares journal rows
and plan states exactly), long lines/unused imports. Accepted as designed — blanket `except Exception` around
tracing-only work; `trace_screen.py` swaps two screener functions for its own process only (restored in `finally`,
tested).

**Spec** (`.scratch/langfuse-live-tracing/spec.md`): fixed — reconcile-only ticks did not say what was skipped; prompt/
policy version missing from the root. Open — hosted demonstration (credentials); normal final-record delivery (§4);
order lifecycle and weekly screening are offline-only evidence; Hermes clips instructions to 8,000 chars before the hook
(Ticket 1); `account()` reads and tool schemas are intentionally not exported.

## 7. Correction pass (2026-11-02): two findings fixed

Regression tests first (`live/test_tracing_corrections.py`): 16 of them failed on the unfixed code, and all 34 pass now.

**(1) "Unchanged context" was asserted without proving it.** The Hermes plugin compared messages by length plus their
first/last 256 characters, and instructions by their first 500 characters, so different content could be cited as an
earlier span's. Now every item is compared by an exact digest of its observed content (type-tagged walk, bounded at
1 M characters per item / 8 M per request). An item that cannot be compared is never cited: it is listed in
`equality_unverified`, `history_unverified_from_index` replaces a guessed `history_changed_at_index`, and `complete` is
false. Instructions are cited (`same_as`) only when identical; an identical copy Hermes clipped carries
`equality: ...unverified`; a shorter Hermes-clipped copy becomes `prefix_of` (+ `observed_chars`) and is never called
identical. Evidence: same-length messages differing mid-text are exported again (`history_changed_at_index: 1`);
instructions sharing 500+ opening characters are distinguished; unchanged history is still cited (`inherited`, `same_as`,
`complete: true`); clipped copies are labelled; across 5 randomized edit sequences, every message rebuilt from the
exported spans equals what that request sent; two requests with 6 MB of history finish within the 3 s test bound,
and the 5 MB item is marked unverified. The earlier Ticket 1 test that expected `same_as` for a 1,000-character
clipped copy encoded the bug, so it now expects `prefix_of`.

**(2) The 64 KB attribute cap was enforced by characters.** `encode_attr` built its truncation preview by character
count without rechecking the final size, which gave 90-131 KB for emoji, CJK and escape-heavy text. Now the complete
envelope (`truncated`, `original_bytes`, `original_chars`, `preview_chars`, `preview`) is re-measured in UTF-8 bytes
and the preview shrinks until it fits. Evidence: ASCII, emoji, CJK, combining characters, escape-heavy, control-character
and nested payloads are all at or under 65,536 bytes (`len(out.encode("utf-8"))`), valid JSON, strict UTF-8 and marked
`truncated`. At the boundary (-1, 0, +1, +2 bytes, for four kinds of final character), values at or under the cap are
unchanged and values over it are marked. A dummy secret is absent from the complete exported request body after byte
truncation.

Checks: `pytest live/` 225 passed; `verify.py` 13/13 (backtest 631); `backtest_state.json` `4b428d35`. Ruff 0.17.3
(`--select E,F,W,B --line-length 120 --isolated`) passes on every feature-owned file. The shared files have 5
findings, all on lines this pass did not touch (`run_live.py` 3×E501, `selector.py` 1×E501, `agent.py` F401), the
same as before. `git diff --check` is clean, and the new untracked files were checked with `git diff --no-index
--check`. **These fixes do not change the status:** the hosted trace (§5) and normal one-shot final-record
delivery (§4) are still pending.

## 8. Context-file change (2026-11-06)

The root `HANDOFF.md` workflow was retired at the user's request. The working copy (with uncommitted edits) is
archived byte-identically at `docs/history/HANDOFF_ARCHIVED_2026-11-06.md` (sha256 `e5a823c6...`). Mentions of
HANDOFF in sections above are historical.

## 9. Exit spool (approved 2026-11-06): evidence

Built as proposed in §4 (`live/tracing/otlp.py`; wired in `controller.load_tracer` and the Hermes plugin). Design and
limits are in the runbook (Safety properties). Offline tests are in `live/test_tracing_spool.py` (14, stable over
repeated runs):
- a root and final records survive a one-shot exit and replay with unchanged trace, span and parent ids;
- records stay through an outage (a failed replay deletes nothing) and are sent when it ends;
- the in-flight batch is spooled without waiting, and a double send repeats identical ids;
- the 2 MB per-exit and 16 MB directory bounds hold, with drops counted and logged;
- malformed lines (3 kinds) are dropped and counted while the valid lines are delivered;
- an unwritable spool never raises or waits;
- no credentials or unredacted secrets reach the spool, and files are owner-only on POSIX;
- two concurrent replays never send a file twice;
- an exit during replay hands the unsent rest back;
- a claim left by a dead process is recovered after 15 min;
- exit overhead: 6 ms for 38 spans, 25 ms for 512;
- controller wiring: the next invocation sends the previous root;
- Hermes wiring: the next Hermes call sends the previous `hermes_run`.

The plugin now drains (diagnostic only) before `close()`, so the drain still sends instead of spooling.

**Hosted probe** (synthetic, no model or broker call; trace `474653790b3b484a1b739997ea23d05e`): 2 spans spooled
during a simulated outage → replayed by the next invocation (2 replayed, spool emptied) → the same 2 spans sent
again deliberately (accepted). Read back: **3 displayed observations, one per id** (root, child, the next
invocation's span), with parentage intact. Langfuse deduplicates re-sent ids, so a re-sent in-flight batch is not
displayed twice. This is one probe, not a guarantee of exactly-once delivery.

A test-isolation fix came out of this work: the developer machine has a real `HERMES_HOME`, and the plugin tests had
spooled into it. Those test files were removed, and `live/conftest.py` now clears `HERMES_HOME` for every live test.

Checks: `pytest live/` 240 passed; `verify.py` 13/13 (backtest 631); Ruff (pinned rules) clean on feature-owned and
changed files, with the shared files' 5 pre-existing findings unchanged; `git diff --check` clean.
`setup-profile` was re-run so the live Hermes profile carries the spool-enabled plugin.
