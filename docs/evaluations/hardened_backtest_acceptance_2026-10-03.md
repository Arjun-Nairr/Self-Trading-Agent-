# Hardened backtest acceptance run — 2026-10-03

**Verdict: RED.** The single acceptance run halted before its first simulated
day: the running `trade-journal` container is built from source that predates
the provenance-hardening commit, so the run could not persist its
run-configuration record. It halted cleanly. It made no Hermes inference call
and no market-data fetch for trading, and wrote nothing except one registry
row. Neither verdict would say anything about profitability, learning, or
live-paper readiness; this one says only that the hardened pipeline has **not
yet** been shown to run end to end.

`baseline_v1` and `baseline_v2` were not rerun, altered, or compared against.

---

## 1. What was run

| | |
|---|---|
| Command | `python eval_suite.py run-one --manifest eval_suites/manifest_20260916T154923_30f4c12e0f7e.json --year 2022 --month 7 --regime rally --checkpoint-dir <scratch dir outside the repo>` (from `backtest-harness/`) |
| Why this episode | The smallest valid fresh run of the full evaluation stack: `run-one` is the only CLI entry to one approved episode, and July 2022 (rally) has the fewest trading days of the four comparison episodes. No manifest was created or edited. |
| Launched / exited | 2026-10-03 09:56:28Z / 09:56:59Z (31 s), exit code 1 |
| Attempts | Exactly one. Not retried, resumed, reset, or repaired. |

## 2. Preflight (all passed, before any side effect)

| Check | Result |
|---|---|
| Manifest approval, hash, version | `require_approved` passed: status `approved`, content hash `30f4c12e0f7ed7bb75780b9d84f6f38cf69c84a149c58b3ab66f4d4a97d018fb` recomputed and matched, `manifest_version` 4 equals code `MANIFEST_VERSION` 4. File sha256 `7b478c4a…603392` unchanged before and after. |
| Comparison, not holdout | 2022-07 / rally (`2022-07-01`..`2022-07-31`) is in `episodes`; no holdout episode matches it. `run-one` refuses holdouts structurally. |
| Fixed-watchlist viability | The manifest's stored audit for the episode is valid with no unavailable tickers, and `_require_valid_watchlist_viability` passed. A fresh live read-only re-audit (`audit_episode_watchlist_viability`, checked 2022-07-01 and 2022-07-29) also passed: 10/10 usable. The current `watchlist.json` matches the manifest's `watchlist_tickers`; watchlist hash `56a5ab41…dffbc6bb2`. |
| Required services | `gateway`, `maverick-mcp`, `alpaca-mcp`, `trade-journal`, `execution-sim` (plus `dashboard`, `google-news-rss-mcp`) all running. |
| Git | HEAD `04ae2d42f5acedeb6676f9cec4e2f962bff03afa`. Dirty: only the user-owned `HANDOFF.md`, `PROJECT_FRESH_START.md`, `docs/content/CONTENT_CONTEXT.md`, two `docs/obsidian/*.canvas`, and untracked `docs/research/`, `exports/`, `tmp_*_ppt/` — no code. Provenance would therefore have recorded `git_dirty = true`. |
| Selector / fundamentals | `eval_suite.run_configuration` never passes a selector (selector is `off` by construction); `--fundamentals` was not given (`EpisodeConfig("baseline", fundamentals_enabled=False)`). |
| Hardened Hermes runtime | The code path is `run_configuration` → `preflight()` → `_provision_ephemeral_hermes_runtime` (fresh `btrun-*` clone of the base `backtest` profile) → `_verify_backtest_hermes_isolation` (fails closed unless tools, memory, user-profile, and workspace-context bytes are all 0). The base profile read-only: `platform_toolsets.cli: []`, `mcp_servers: {}`, `memory_enabled: false`, `user_profile_enabled: false`, model `gpt-5.6-terra` / provider `openai-codex` / reasoning `medium`. Only the `backtest` profile existed beforehand. |

## 3. What happened

1. `preflight()` passed. It provisioned a disposable runtime and verified isolation: it would have raised `SystemExit` on any non-zero tool, memory, profile, or context measurement, and it did not.
2. `register_run` wrote the registry row: run `25bd0ac3-0026-405d-913e-01ead86fad49`, kind `evaluation`.
3. `register_run_configuration` failed **before** any market-data fetch, Hermes call, journal write, or checkpoint:

   ```
   TypeError: _register_run_configuration() takes 9 positional arguments but 10 were given
   ```

4. `run_configuration`'s `finally` cleaned up the ephemeral profile (afterwards `/opt/data/profiles/` lists only `backtest`, and no cleanup warning was printed).

**Root cause.** The `trade-journal` container runs an image built
2026-09-15. The `provenance` parameter of `_register_run_configuration` was
added in commit `e5ec811` (2026-09-28), so the running container's
`server.py` has the old 9-argument signature while the host code passes 10.
Host and container `server.py` sha256 differ (`95bcc366…` vs `4908e9c4…`).
`alpaca-mcp` and `execution-sim` match their source; only `trade-journal` is
stale. This also means no run has yet persisted the hardened provenance
fields: both baselines were registered before `e5ec811`.

## 4. Requested evidence that could not be produced

The run never reached day 1, so none of these exist for it:
- days completed, return, drawdown, fills, proposals, closed trades, blocked decisions;
- persisted watchlist hash, alias-map hash, masking seed, fundamentals flag. The masking seed would have been the deterministic `259137704`, but it was not persisted;
- persisted git SHA/dirty state, gateway image reference, isolated model/provider/reasoning metadata;
- persisted offered-tool, memory, and context counts. The runtime did pass the live zero check, but its measurement was never written;
- prompt evidence with the masked `RULES.md` block, cycle evidence, snapshots, activity, trades;
- end-of-episode pending-order resolution (no order was ever created).

## 5. Post-run scoped audit

| Check | Result |
|---|---|
| Neon writes | A read-only before/after snapshot of every table changed only `backtest_run_registry` (20 → 21: the one row above). `trades` 420, `insight_log` 69, `agent_activity` 2023, `portfolio_snapshots` 370, `backtest_cycle_evidence` 211, `backtest_run_configuration` 10, `backtest_run_status` 0 — all unchanged. |
| V1/V2 and Round 2 | Per-table row counts for all eight baseline runs and Round 2 (`e68525f9…`) are identical before and after. |
| No other run started | No holdout, live-paper, Laya, scalable-screener, or fundamentals-enabled run. No `backtest_run_configuration` row has `fundamentals_enabled = true`. The pre-existing pending trades on three older smoke runs are unchanged. |
| Local state | `backtest_state.json` sha256 `4b428d35…ba6d` unchanged. No `cycle_checkpoint.json`, and the scratch checkpoint directory was never even created. |
| Files | `RULES.md`, `AGENTS.md`, `SOUL.md`, `watchlist/watchlist.json`, `docker-compose.yml`, `trade-journal-mcp/schema.sql`, and the manifest are byte-identical before and after. The only new tracked file is this report. |
| Hermes profiles | Only `backtest` remains; the disposable profile was removed. |

Run `25bd0ac3-0026-405d-913e-01ead86fad49` holds just a registry row
(`evaluation`, no configuration, no cycles). It was **left in place**, not
deleted, and must be **excluded from all analysis**.

## 6. Blocker and next step

**Blocker:** the `trade-journal` image must be rebuilt from current source
(`docker compose build trade-journal && docker compose up -d trade-journal`)
before any hardened backtest can persist its provenance. This is the same
class of failure as the 2026-09-12 stale-image incident. Rebuilding and
running a fresh acceptance attempt each need explicit approval; neither was
done here.

The first real-data scalable-screener validation is **not** the next logical
task. The next step is the image rebuild above followed by one fresh
acceptance run, so the hardened backtest path is proven end to end first.
