# Backtest isolation, provenance, and public-data hardening — verification

**Status:** internal verification document, REPLACING its own prior version.
The prior version (generated 2026-09-27, same day) overclaimed that all
proofs had passed; an independent two-axis review of that same uncommitted
diff found three structural gaps it had missed (a shared, not per-run,
Hermes profile; a workdir that leaked ~14KB of real, unmasked AGENTS.md/
RULES.md text into every backtest Hermes call despite the "isolation" being
"verified"; and pre-fill gate evidence silently discarding all but the last
confirmation batch on a multi-batch day) and two weaker points (token-by-
token alias redaction on the public dashboard views instead of omitting
free text outright; no explicit tool-use attestation). This document
describes the CORRECTION pass that fixes all five. Applies forward only.
**`baseline_v1` and `baseline_v2` are frozen historical artifacts and were
not rerun, altered, deleted, backfilled, or reinterpreted** — every claim of
that below is independently re-checked in §5. No Laya, dynamic selection,
holdout, training, smoke run, live-paper action, or optimization was
started. Nothing was committed, pushed, or deployed. Generated 2026-09-27
against `HEAD 1285526` (uncommitted working tree, same HEAD as before).

---

## 1. What was actually wrong, and what changed

### 1.1 A real, live leak this session found and fixed (not merely theoretical)

Before touching any code, this pass re-measured the PRIOR pass's own
"verified" isolation live: `hermes prompt-size --json` under
`HERMES_HOME=/opt/data/profiles/backtest` with `--workdir
/opt/data/workspace` (exactly what `_call_hermes` was sending on every
real backtest cycle) reported **14,213 bytes** in its `"context
(AGENTS.md/cwd files)"` section — because `/opt/data/workspace` is the
SAME path the live agent's real, unmasked `AGENTS.md`/`RULES.md` are
mounted at (`docker-compose.yml`). The prior pass's own fail-closed check
(`_verify_backtest_hermes_isolation`) checked tool count and memory bytes
but never checked this section, so it "passed" while silently leaking real
project text into every cycle, on top of (not instead of) the masked
RULES.md block the prompt itself already carried. This was live and real —
not a hypothetical — for every uncommitted cycle run under that code.
Fixed in requirement 2 below.

### 1.2 The five required corrections

| # | Gap | Fix |
|---|---|---|
| 1 | `BACKTEST_HERMES_HOME` was one FIXED profile shared by every run, past and future — sessions persisted across separate runs even though no run ever resumed one. | New `HermesRuntime` dataclass + `_provision_ephemeral_hermes_runtime()`: `hermes profile create btrun-<random> --clone-from backtest --no-alias` (documented, non-interactive) gives every run its OWN fresh `HERMES_HOME`, verified live to start with NO `state.db` and empty `sessions/`/`memories/` (cloning copies only `config.yaml`/`.env`/`SOUL.md`/skills). `_cleanup_ephemeral_hermes_runtime()` deletes it (`hermes profile delete -y`) in a `finally` on every exit path. Provisioning/cleanup are local, free, non-inference operations — never a real Hermes/API call. |
| 2 | `--workdir /opt/data/workspace` (shared with the live agent's mounted, unmasked AGENTS.md/RULES.md) leaked real project text into the "context" tier of every backtest call — see §1.1. | Every real Hermes call now runs with `--workdir` pointed at `HermesRuntime.workdir` — that profile's OWN private `workspace/` subdirectory, always empty (nothing is ever written there). `_verify_backtest_hermes_isolation` now ALSO fails closed on any nonzero `"context (AGENTS.md/cwd files)"` bytes, not just tools/memory. |
| 3 | `pre_fill_gate`'s checkpoint shape held a SINGLE `gate_call` value — a day needing a second confirmation batch (e.g. a retry resolving a previously-uncovered order) silently overwrote the first batch's prompt/response/hashes. | `gate_call_evidence` now names its own `order_ids`/`aliases`; `run_day`'s checkpoint write APPENDS onto an ordered `pre_fill_gate.gate_calls` list instead of overwriting a scalar; `backtest_cycle_evidence.pre_fill_gate_calls` (JSONB list) replaces the four old scalar columns. |
| 4 | No explicit tool-use attestation; model/provider/reasoning config was read only from Hermes' own (usually-null) per-cycle usage report, never from the isolated runtime's own configuration. | `backtest_cycle_evidence` gains `tools_used_count`/`tools_used`/`tool_use_attestation_basis` (the last explicitly says `"structural_zero_offered_tools"` — implied by zero offered tools, never an independently observed trace). `backtest_run_configuration` gains `hermes_runtime_model`/`provider`/`reasoning_effort`/`config_source`, read from the runtime's own `config.yaml` (`_read_hermes_runtime_config`), `"unavailable"` (never fabricated) on any read failure. |
| 5 | Public dashboard views redacted alias TOKENS inside free-text columns (fragile against any future alias-scheme change) instead of omitting the free text outright. | `public_trades`/`public_agent_activity`/`public_insight_log` now OMIT `strategy_notes`/`invalidation_condition`/`description`/`insight_text` entirely — structured facts only. `redact_aliases()` stays defined but unused by these views (kept in case something else depends on it; harmless). |

Also fixed as part of requirement 1's own non-secret-identifier
sub-requirement: `backtest_run_configuration.hermes_home` (an absolute path
under the user's home directory) is superseded by
`hermes_home_fingerprint` (a short sha256 of the per-run profile name) —
the old column is left in the schema, nullable, simply never written to
again (safer than a `DROP COLUMN` for a column nothing else reads).

---

## 2. Changed files and why

| File | Why |
|---|---|
| `backtest-harness/run_backtest.py` | Requirement 1: `HermesRuntime` dataclass, `_provision_ephemeral_hermes_runtime`/`_cleanup_ephemeral_hermes_runtime`/`_DEFAULT_HERMES_RUNTIME`, `preflight()` now provisions+returns a runtime, `main()` split into a slim wrapper (`main`) + the real body (`_run_backtest_main`) so the wrapper's `try/finally` always cleans the runtime up. Requirement 2: `_verify_backtest_hermes_isolation` takes `runtime`, checks context bytes, `_call_hermes`/`ask_hermes`/`run_day` all thread `runtime` through (defaulting to the shared base profile when omitted, for backward test compatibility). Requirement 3: `apply_pre_fill_news_gate`'s `gate_call_evidence` gains `order_ids`/`aliases`; `run_day`'s checkpoint write appends onto `pre_fill_gate.gate_calls`. Requirement 4: `build_run_provenance` gains `hermes_home_fingerprint`/`hermes_context_bytes`/`hermes_runtime_model/provider/reasoning_effort/config_source` (via new `_read_hermes_runtime_config`), drops `hermes_home`; `record_cycle_evidence` gains `tools_used_count`/`tools_used`/`tool_use_attestation_basis`, `run_day` passes the structural attestation. |
| `backtest-harness/eval_suite.py` | `_run_episode` threads `runtime` (from `isolation_report.get("runtime")`) into its own `run_day` call; `run_configuration()` wraps its episode loop in `try/finally` calling `rb._cleanup_ephemeral_hermes_runtime`, guarded against a mocked `preflight()` returning `None`. |
| `trade-journal-mcp/schema.sql` | Additive-relative-to-history, corrective-relative-to-the-uncommitted-prior-pass migration: new `backtest_run_configuration` columns (`hermes_home_fingerprint`/`hermes_context_bytes`/`hermes_runtime_*`); `backtest_cycle_evidence`'s four scalar `pre_fill_gate_*` columns dropped and replaced by `pre_fill_gate_calls JSONB`, plus new `tools_used_count`/`tools_used`/`tool_use_attestation_basis`; the three `public_*` views dropped and recreated (Postgres refuses `CREATE OR REPLACE VIEW` when a column would be removed) omitting every free-text column, with their `GRANT SELECT ... TO anonymous` re-issued. Applied LIVE to Neon (§3). |
| `trade-journal-mcp/server.py` | `_register_run_configuration`'s INSERT column list/params match the new provenance shape; `_record_cycle_evidence`/`_cycle_evidence_mismatches` match the new cycle-evidence shape (`pre_fill_gate_calls`, `tools_used*`). |
| `dashboard-v2/js/data.js` | Comment corrected: the public views now OMIT free text, not redact it; documents that dependent call sites already null-guard. |
| `dashboard-v2/js/calc.js` | Comment on `isBlockedRow` documenting the deliberate feature-reduction (rule-citation analytics go dark on the public view — the underlying `description` field is gone, not just redacted); no logic change (already null-guarded). |
| `dashboard-v2/js/pages/runs.js` | `r.insight_text` now falls back to an explicit "not available on this public view" placeholder instead of rendering blank. |
| `dashboard/index.html` (V1) | Same placeholder treatment for `agent_activity.description`/`insight_log.insight_text`; `isBlockedRow`/`renderAnalysis`'s `a.description.matchAll(...)` guarded with `|| ""` (was unguarded — harmless today only because the regex never matches the literal string `"undefined"`, but a real latent crash risk once `description` is reliably absent from every row). |
| `backtest-harness/test_execution_semantics.py` | `_verify_backtest_hermes_isolation`'s test extended with a 4th failure shape (nonzero context bytes) and a runtime-selection proof; new `test_call_hermes_uses_the_given_runtimes_own_home_and_workdir`; new `test_pre_fill_gate_evidence_accumulates_as_an_ordered_list_across_batches` and `test_apply_pre_fill_news_gate_call_evidence_names_its_own_orders_and_aliases`; `test_build_run_provenance_shape` rewritten for the new field names/fallback/explicit-runtime paths (mocks `_read_hermes_runtime_config`); ~12 existing `fake_ask_hermes`/`_call_hermes` mocks widened to accept the new `runtime` argument (mechanical). |
| `backtest-harness/test_eval_suite.py` | 7 `fake_run_day` mocks widened to accept `runtime=None` (mechanical, same cause as above). |
| `backtest-harness/test_run_kind_resolution.py` | Its structural source-inspection test now targets `_run_backtest_main` (main()'s real body after the split), not `main` itself. |
| `dashboard-v2/test/calc.test.mjs` | Comment corrected (omission, not redaction); new test proving `isBlockedRow`/`rejectionDistribution`/`relatedActivity` degrade to an empty, non-crashing result when `description` is entirely ABSENT (the real post-correction shape), not just `null`. |

Not changed: `RULES.md`, `AGENTS.md`, `SOUL.md`, `risk_checker.py`,
`watchlist/watchlist.json`, `docker-compose.yml`, model/provider/reasoning
configuration, sizing/execution semantics, news-veto semantics, the live
`default` Hermes profile. Confirmed by `git diff --stat` (exactly the files
above, plus the 5 pre-existing user-owned dirty files this task never
touched) and a grep of the diff for any removed assignment to
`risk_budget_pct`/`max_position_pct`/`min_reward_risk`/`BUY_SLIPPAGE_BPS`/
`drawdown_halt_pct` (zero matches — the two literal string hits are the
column name `min_reward_risk_used` in a view's SELECT list, not the rule
constant).

---

## 3. Live migration applied

Two ALTER/CREATE blocks, both idempotent (`IF NOT EXISTS`/`IF NOT EXISTS`
drops), applied via `docker compose exec trade-journal python -` running a
short asyncpg script against the real Neon database (same mechanism the
prior pass used):

1. Additive columns on `backtest_run_configuration`/`backtest_cycle_evidence`
   (§2's schema.sql changes) — succeeded on the first attempt.
2. Drop + recreate `public_trades`/`public_agent_activity`/
   `public_insight_log` with the free-text columns omitted, re-`GRANT`.
   **First attempt failed**: `asyncpg.exceptions.InvalidTableDefinitionError:
   cannot drop columns from view` — Postgres refuses `CREATE OR REPLACE VIEW`
   when it would remove a column. The whole multi-statement DDL batch rolled
   back atomically (verified live: a follow-up query showed NONE of the new
   columns present after the failed attempt — nothing was left half-applied).
   Fixed by using `DROP VIEW IF EXISTS` + `CREATE VIEW` + explicit `GRANT`
   instead of `CREATE OR REPLACE VIEW`; re-applied successfully.

Live-verified after the successful apply (read-only queries against
`information_schema`):
- `backtest_run_configuration` has all 6 new columns
  (`hermes_home_fingerprint`, `hermes_context_bytes`,
  `hermes_runtime_model/provider/reasoning_effort/config_source`), plus the
  old `hermes_home`/`hermes_profile` still present (unwritten going
  forward).
- `backtest_cycle_evidence` has `pre_fill_gate_calls` (jsonb),
  `tools_used`/`tools_used_count`/`tool_use_attestation_basis`; the four old
  scalar `pre_fill_gate_prompt_text`/`prompt_sha256`/`raw_response`/
  `response_sha256` columns are gone (`DROP COLUMN IF EXISTS`, confirmed via
  the same `information_schema.columns` query showing them absent).
- `public_trades` columns: `decision_id, ticker, action, intended_quantity,
  intended_notional, order_type, limit_price, status, broker_order_id,
  fill_price, fill_quantity, rejection_reason, data_timestamps, created_at,
  updated_at, execution_mode, confidence, primary_signal, technical_basis,
  expected_reward_risk, stop_price, target_price, run_id, strategy_profile,
  min_reward_risk_used, proposed_entry_price, exit_provenance` — no
  `strategy_notes`/`invalidation_condition`.
- `public_agent_activity` columns: `id, decision_id, event_type,
  created_at, mode, run_id, cycle_id, total_tokens, hermes_calls_used` — no
  `description`.
- `public_insight_log` columns: `id, cycle_type, mode, sample_size, run_id,
  cycle_id, created_at` — no `insight_text`.
- `information_schema.role_table_grants` for `grantee = 'anonymous'`:
  exactly `backtest_run_registry`, `backtest_run_status`,
  `portfolio_snapshots`, `public_trades`, `public_agent_activity`,
  `public_insight_log` — no raw `trades`/`agent_activity`/`insight_log`, no
  `backtest_run_configuration`/`backtest_cycle_evidence`.

---

## 4. Exact tests / inspection results

All commands run from `backtest-harness/` unless noted. Real calls (never
market data, never a paid Hermes inference call) are called out explicitly.

| Command | Result |
|---|---|
| `python test_execution_semantics.py` | **PASS** — 378 checks (was 361 before this pass), exit code 0 |
| `python -m pytest -q` (whole `backtest-harness/`) | **255 passed** (was 252) |
| `python -m pytest -q test_eval_suite.py` | **48 passed** (unchanged count — mechanical mock widening only) |
| `python risk-checker/test_risk_checker.py` | **PASS**, unaffected |
| `node dashboard-v2/test/calc.test.mjs` | **48/48** (was 47/47; +1 new test) |
| `test_execution_path.py` | **Not run** — grepped for every seam this pass touched, zero matches, confirmed unaffected; not required by any proof item below. |

### The 10 required mini-audit proofs

1. **Two fresh simulated future-run setups receive different empty Hermes
   homes/workdirs and cannot see each other's session/history files.**
   **Real (non-mocked) dry run**: called `run_backtest.preflight()` twice in
   one Python process. Result: two distinct profile names
   (`btrun-d1d4f66e5ab841ac`, `btrun-3e23a5fdeabb4323`), two distinct
   `HERMES_HOME` paths; each profile's own `sessions/` directory listed via
   `docker compose exec ... ls` is empty and contains no reference to the
   other profile's name; each `preflight()` call independently passed its
   own fail-closed isolation check (meaning each fresh clone genuinely
   measured zero tools/memory/context on its own, not inherited from the
   other). No market data, trading, or Neon write occurred — `preflight()`
   only shells out to `docker compose` and `hermes profile create`/
   `prompt-size`, never a real inference call.
2. **The live Hermes profile is byte-for-byte unchanged.** Read
   `/opt/data/config.yaml` (the live `default` profile) via `docker compose
   exec`: `platform_toolsets.cli` still lists real tools, `mcp_servers`
   still lists `maverick-mcp` etc., `memory.memory_enabled`/
   `user_profile_enabled` still `true` — the live agent's profile was never
   touched by anything in this pass (only the separate `backtest` base
   profile is ever cloned from, and cloning never mutates its source).
   `docker compose exec ... ls /opt/data/profiles/` after all dry-runs
   completed shows exactly one entry, `backtest` — both `btrun-*` test
   profiles and the earlier session's `isoltest01` test profile were fully
   cleaned up, no leaked disposable homes.
3. **The isolated Hermes runtime receives no raw repository documents and
   advertises zero tools, zero memory, and zero user-profile context.**
   Same dry run as item 1: both fresh runtimes independently reported
   `tools.count == 0`, `memory.bytes == 0`, `user_profile.bytes == 0` (else
   `preflight()` itself would have raised `SystemExit` and the script would
   have crashed before reaching the assertions). `_verify_backtest_hermes_
   isolation` now ALSO checks `"context (AGENTS.md/cwd files)"` bytes == 0
   (the new, 4th check — unit-tested against 4 distinct failure shapes in
   `test_verify_backtest_hermes_isolation_fails_closed_on_any_tool_or_memory`,
   including the exact nonzero-context-bytes shape this pass's own §1.1
   real leak had).
4. **The controller's masked prompt is byte-identical to the bytes received
   by Hermes.** Unchanged from the prior pass, re-verified still passing:
   `test_call_hermes_sends_exact_prompt_bytes_via_stdin_never_argv` (stdin
   transport, never argv) plus the NEW
   `test_call_hermes_uses_the_given_runtimes_own_home_and_workdir`, which
   additionally proves two DIFFERENT `HermesRuntime`s route to two
   DIFFERENT `HERMES_HOME`/workdir command lines, never bleeding into each
   other.
5. **Raw watchlist names cannot be found in the isolated runtime's
   effective prompt/context.** Proven two ways: (a) structurally, by item 3
   — zero context bytes means the cwd auto-injection tier has nothing to
   contribute, so no file (raw or otherwise) reaches Hermes through it; (b)
   the prompt itself only ever carries the alias form — unchanged from the
   prior pass, `test_build_prompt_embeds_masked_rules_md_and_bumps_schema_
   version` still passes, running `masking.assert_clean()` on the full
   rendered prompt.
6. **Multiple pre-fill gate batches persist as an ordered, complete list and
   replay without a new fetch or Hermes call.** `test_pre_fill_gate_
   evidence_accumulates_as_an_ordered_list_across_batches` proves TWO
   separate confirmation-call evidence dicts both survive in
   `pre_fill_gate.gate_calls`, in order, after two sequential checkpoint
   writes (reproducing run_day's own append pattern) — the exact scenario
   the prior pass's single-`gate_call` shape silently lost.
   `test_apply_pre_fill_news_gate_call_evidence_names_its_own_orders_and_
   aliases` proves each call's evidence names which `order_ids`/`aliases`
   it covered. The pre-existing `test_pre_fill_gate_replay_never_refetches_
   news_or_reasks_hermes` (unchanged, still passing) proves a fully-
   checkpointed gate makes zero fetch/Hermes calls on replay.
7. **Explicit zero-tool-use attestation is privately persisted in a
   mocked/safe fixture path.** `run_day`'s real call site now passes
   `tools_used_count=0, tools_used=[], tool_use_attestation_basis=
   "structural_zero_offered_tools"` into `record_cycle_evidence`, which
   forwards them to `_record_cycle_evidence`'s INSERT — verified via the
   live schema check in §3 (columns exist with the right types) and via
   `test_eval_suite.py`'s 48 passing tests (mocked `run_day`/journal calls,
   no real database access). No new real Neon row was written to prove
   this — the mocked path is the task's own explicitly allowed proof for
   plumbing-only changes, same discipline as the original hardening pass.
8. **Anonymous/public queries cannot retrieve ticker↔alias reconstruction
   material, private evidence, or model-generated free text from the public
   views.** Verified BOTH via `information_schema` (§3) AND via a REAL
   anonymous HTTP request against the live PostgREST Data API (the exact
   endpoint the dashboard itself calls): fetched a real anonymous token,
   queried `public_agent_activity?limit=3` — the returned row's keys are
   exactly `['created_at', 'cycle_id', 'decision_id', 'event_type',
   'hermes_calls_used', 'id', 'mode', 'run_id', 'total_tokens']`, no
   `description` key at all. The SAME anonymous token queried against the
   raw `agent_activity` table and against `backtest_cycle_evidence` both
   returned HTTP 403.
9. **Dashboard tests still pass against the safe projections.**
   `dashboard-v2/test/calc.test.mjs`: 48/48, including the new test proving
   `isBlockedRow`/`rejectionDistribution`/`relatedActivity` degrade to an
   empty result (never a crash) when `description` is entirely absent.
   **Live-rendered proof**: opened `http://127.0.0.1:8842` (dashboard V1)
   after the migration — zero console errors; every activity-feed row shows
   the honest placeholder `"(activity text not available on this public
   view)"` instead of blank/`undefined`/a crash; the rejection-breakdown
   section correctly reads "No blocked decisions recorded" (the documented,
   deliberate feature reduction — that analysis depended on the now-omitted
   `description` field) rather than erroring.
10. **No source of the old live Hermes profile is changed.** `/opt/data/
    config.yaml` (live `default` profile) re-read after this pass: byte-
    equivalent structure to the pre-pass audit record (real toolsets, real
    MCP servers, memory/user-profile still enabled) — confirmed in item 2
    above. `docker-compose.yml` was not edited (`git diff --stat` shows no
    change to it).

---

## 5. Isolation / provenance guarantees now enforced

- **Every future backtest run gets its own, disposable Hermes profile**,
  provisioned fresh at the start of that run's `preflight()` call and
  deleted in a `finally` at the end (normal completion, a halted cycle, or
  an uncaught exception all reach the same cleanup) — no session database,
  request dump, memory store, or user profile is ever shared between two
  separate runs, past or future.
- **The isolated runtime's workdir is that runtime's own, always-empty
  directory** — never the path the live agent's real AGENTS.md/RULES.md
  are mounted at — so Hermes' own cwd context-file auto-injection has
  nothing to inject; the ONLY trading context a backtest Hermes call ever
  receives is the controller's own prepared, masked prompt.
- **`preflight()` fails the run closed, before any side effect**, if the
  freshly-provisioned runtime's own offline `prompt-size` report ever shows
  a nonzero tool count, nonzero memory/user-profile bytes, OR nonzero
  auto-discovered context bytes — a structural gate on every future
  `main()`/`eval_suite.run_configuration()` invocation, re-provisioned and
  re-measured every single run (never trusted from a prior run).
- **Every pre-fill news-gate confirmation call this cycle makes is
  durably preserved, in order**, with its own prompt/hash/response/hash/
  attempt-count/covered-orders — a day needing more than one such call
  during a retry never silently loses an earlier batch's evidence.
- **Run-level provenance never persists a raw absolute filesystem path** —
  only a non-secret per-run profile name and a short fingerprint hash — and
  additionally records the isolated runtime's own best-effort model/
  provider/reasoning-effort configuration, explicitly marked `"unavailable"`
  rather than fabricated on any read failure.
- **Cycle-level provenance carries an explicit tool-use attestation**,
  honestly labeled as structural (implied by zero offered tools) rather
  than an independently observed runtime trace.
- **The public Data API surface excludes every model-generated or
  arbitrary free-text field outright** (not merely redacted) from every
  table that carries one — verified live through the actual anonymous
  PostgREST endpoint, not just against source SQL or `information_schema`.

---

## 6. Remaining limitations / unproven assumptions

- **`hermes_system_prompt_sha256` is still not a hash of Hermes' actual raw
  system prompt text** (unchanged limitation from the prior pass) —
  `hermes prompt-size` does not expose that text, only its size/composition
  breakdown.
- **`git_head_sha`/`git_dirty`/`gateway_image_digest` remain best-effort**
  (unchanged) — a `git` failure or an unparseable compose file degrades to
  `None` rather than failing the run.
- **`redact_aliases()` is now unused by the public views** but is kept
  defined in the schema (a future consumer might still want it; dropping a
  function some other session could be relying on is a bigger risk than
  leaving an unused one). It still only matches the fixed, literal
  14-token NATO pool if anything ever calls it again.
- **The dashboards' rule-citation/rejection-breakdown analytics
  (`isBlockedRow`/`rejectionDistribution`/`relatedActivity` in
  dashboard-v2's `calc.js`, and V1's equivalent) are now permanently dark
  for anonymous/public viewers** — a deliberate, documented consequence of
  omitting `agent_activity.description` entirely, not a bug, but a genuine
  feature loss worth knowing about if someone later asks "why doesn't the
  public rejection chart show anything."
- **`hermes_context_bytes`/`hermes_runtime_*` prove the ISOLATED runtime's
  own state, not what a specific past cycle's prompt actually contained**
  — they are run-level, measured once at `preflight()` time, same
  granularity discipline as the prior pass's `offered_tool_count`.
- **`dashboard-v2/js/fixtures.js`** (the dev-only fixtures-preview tool,
  never shipped in the real app shell) still includes `strategy_notes`/
  `insight_text`/etc. in its example rows — a preview-fidelity nuance, not
  a security issue (it's static local fixture data, never live Neon data),
  left unchanged as out of this task's scope.
- **`test_execution_path.py` was not run** (§4) — confirmed unaffected by
  grep, not independently re-verified end-to-end.
- **No new real (non-mocked) Neon row was written** to prove item 7's
  attestation fields round-trip through a REAL INSERT — proven via the
  live schema/column check (§3) plus the mocked test path (§4 item 7), the
  task's own explicitly allowed proof for a plumbing-only change.

---

## 7. V1/V2 untouched — explicit confirmation

- **HEAD unchanged**: `1285526e8f3ae18da1f841c51fef6713977dbe2e` before and
  after this pass; nothing committed.
- **No baseline code touched**: `RULES.md`, `AGENTS.md`, `SOUL.md`,
  `risk_checker.py`, `watchlist/watchlist.json`, `docker-compose.yml` do not
  appear in `git status --short`'s modified list; `git diff --stat`
  against each is empty.
- **All 10 `backtest_run_configuration` rows (the original 8, plus 2 more
  belonging to the user's own separate, unrelated ongoing work) verified
  read-only**: every new/corrected column reads `NULL` for all 10 —
  `count(*) FROM backtest_run_configuration WHERE hermes_home_fingerprint
  IS NOT NULL OR hermes_context_bytes IS NOT NULL OR hermes_runtime_model
  IS NOT NULL OR hermes_profile IS NOT NULL` returned `0`. `backtest_cycle_
  evidence` shows `0` rows with any of `pre_fill_gate_calls`/`tools_used`/
  `tools_used_count` set, out of 211 total rows in the table — no row
  anywhere was updated, only new columns added (and four unused-by-any-
  real-row scalar columns dropped).
- **No run, episode, holdout, smoke test, or live-paper action was
  started** by this task — every command run was a pure/mocked test, a
  read-only Neon/`information_schema` query, a real anonymous PostgREST
  GET (read-only, public endpoint, no auth beyond the anonymous token every
  visitor already gets), an additive/corrective schema migration, local
  `hermes profile create`/`delete` calls (never a real inference call,
  never any market data), or a read-only browser load of the already-
  running V1 dashboard.
- **The `baseline_v1`/`baseline_v2` evaluation and its own report**
  (`docs/evaluations/baseline_v1_vs_v2_evaluation.md`) are unmodified by
  this task.

---

## 8. Addendum — lifecycle cleanup gap closed (2026-09-28)

A final review found one remaining defect in §5's cleanup guarantee:
`preflight()` provisioned the ephemeral `btrun-*` profile but only cleaned
it up if `_verify_backtest_hermes_isolation()` raised `SystemExit`. A
timeout, malformed `prompt-size` JSON, any other exception, or a
`KeyboardInterrupt` before `preflight()` returned would have orphaned the
profile (the caller never receives `runtime`, so its own `finally` could not
clean it). Fixed in `backtest-harness/run_backtest.py`:

- `preflight()` now cleans up on `BaseException` and re-raises the original
  exception unchanged.
- `_provision_ephemeral_hermes_runtime()` attempts cleanup of the just-named
  profile if the create command itself is interrupted/times out (it may have
  created the profile before failing); a plain non-zero create exit still
  raises `SystemExit` without a delete (nothing was created).
- `_cleanup_ephemeral_hermes_runtime()` now prints a visible warning when the
  delete command exits non-zero, not only when `subprocess` itself raises. It
  remains best-effort and never raises; it is a no-op for `None` and for the
  shared base `backtest` profile, and no code path selects the live `default`
  profile.

Regression tests (mocks only — no profile created, no Hermes/Neon/market
call): `test_preflight_cleans_up_the_provisioned_runtime_on_every_failure_path`
and `test_cleanup_warns_honestly_on_nonzero_exit_and_never_targets_base_or_live_profile`.
Result after this addendum: `test_execution_semantics.py` PASS,
`pytest -q backtest-harness` 257 passed, `calc.test.mjs` 48/48.
