# Hardened backtest acceptance rerun — 2026-10-03

**Verdict: GREEN.** After rebuilding the stale `trade-journal` container, the
current hardened backtest pipeline completed one inspectable end-to-end
episode: all 20 trading days, no halt, with persisted provenance and per-cycle
evidence. GREEN means only that. It is **not** a claim about profitability,
agent learning, strategy quality, Laya, optimization value, or live-paper
readiness. The operational figures below show that the machinery ran; they
are not performance results and must not be compared with `baseline_v1` or
`baseline_v2`.

This follows the RED first attempt
(`docs/evaluations/hardened_backtest_acceptance_2026-10-03.md`). That run,
`25bd0ac3-0026-405d-913e-01ead86fad49`, holds only a registry row. It was
left in place and stays excluded from all analysis.

---

## Phase A — repair and parity (all passed before launch)

| Check | Result |
|---|---|
| Git | HEAD `bca59a6aacfd7ecbb1c22c1fe1822c7034c9cbbd`. Dirty only with the user-owned `HANDOFF.md`, `PROJECT_FRESH_START.md`, `docs/content/CONTENT_CONTEXT.md`, two `docs/obsidian/*.canvas`, and untracked `docs/research/`, `exports/`, `tmp_*_ppt/`. All preserved. |
| Rebuild | `docker compose build trade-journal` + `docker compose up -d --no-deps trade-journal`. No other service was rebuilt or recreated. Image `sha256:0b84cda1…` (built 2026-09-15) → `sha256:78a81a64…` (built 2026-10-03). The dependency layer came from cache, so the installed versions are unchanged: `asyncpg 0.31.0`, `fastmcp 3.4.7`, `mcp 1.29.1`, `pydantic 2.13.5`. |
| Healthy | Container running, 0 restarts. Uvicorn startup completed, and `server` imports with `_register_run`, `_record_cycle_evidence`, `_get_run_configuration`, and `get_pool` present. |
| Source parity | The container's `/app/server.py` and the repository's `trade-journal-mcp/server.py` have the same sha256, `b4469caf88ca0e80764ca955c4c70e3564d4fa58dab93256f6cf9873e38c94c0`. Re-checked after the run: still identical. |
| Signature | Inside the container, `_register_run_configuration` takes the nine configuration inputs plus `provenance` (10 parameters, `provenance=None`). |
| Neon schema | All 15 hardened `backtest_run_configuration` provenance columns and all 8 hardened `backtest_cycle_evidence` columns exist (read-only `information_schema` check). |
| Services | `gateway`, `maverick-mcp`, `alpaca-mcp`, `trade-journal`, `execution-sim` (plus `dashboard`, `google-news-rss-mcp`) running. |
| Baseline counts | Captured read-only. The snapshot was identical to the one taken after the RED attempt. |
| Manifest / episode | `require_approved` passed (approved, version 4, content hash `30f4c12e…d018fb` recomputed and matched). July 2022 / rally is a comparison episode, not a holdout. Its watchlist viability is valid with no unavailable tickers. |

## Phase B — the run

| | |
|---|---|
| Command | `python eval_suite.py run-one --manifest eval_suites/manifest_20260916T154923_30f4c12e0f7e.json --year 2022 --month 7 --regime rally --checkpoint-dir <scratch dir outside the repo>` |
| Run | `2b2b2a2d-47e2-4af3-afff-443dcd2814d4`, kind `evaluation`, window `2022-07-01`..`2022-07-31` |
| Time | 2026-10-03 10:07:58Z → 10:42:50Z, exit code 0 |
| Status | completed, `halted: false`, 20 / 20 trading days |
| Attempts | Exactly one: no retry, resume, reset, or repair |
| Selector / fundamentals | selector off (`eval_suite` never passes one); `fundamentals_enabled: false` (persisted) |

Operational outputs, which show the machinery ran and are not performance:
return −0.3029% (recomputed identically from the 20 persisted equity
snapshots, 100,000.00 → 99,697.12), max drawdown −2.6637%, 19 proposals,
9 fills, 5 closed trades, 10 blocked decisions.

## Phase C — persisted evidence

**Run configuration** (private `backtest_run_configuration`):

| Field | Value |
|---|---|
| Watchlist hash | `56a5ab412f50f08768497378ec87b654408a9625b1ab064dffe20b1dffbc6bb2`. Recomputed from the persisted snapshot: matches. |
| Alias-map hash | `d99d0b7f5aa4d6858184de5fa24753d050faa7ddee8ac2b856cc459fae0063b9`. Recomputed: matches. A mapping rebuilt from (watchlist, start date, seed) equals the persisted mapping. |
| Masking seed | `259137704` (deterministic from manifest + episode) |
| Fundamentals | `false` |
| Git | `git_head_sha bca59a6…`, `git_dirty true` (the user-owned documentation files above; no code) |
| Gateway image | `nousresearch/hermes-agent@sha256:7052fcafb84acaf0d93f78510a51c344ca2041d4ba7dda9dca67660f817309c4`. This is the reference pinned in `docker-compose.yml`, not a `docker inspect` of the running container. |
| Hermes runtime | Disposable profile `btrun-9446a57a23fd4cdc` (fingerprint `82343a69cc35b121`). Model `gpt-5.6-terra`, provider `openai-codex`, reasoning `medium`, read from the profile's own config (`isolated_profile_config`). |
| Isolation | `hermes_offered_tool_count 0`, `hermes_memory_enabled false`, `hermes_context_bytes 0`, verified at `2026-10-03T10:08:19Z`. User-profile bytes have no separate column. Zero is enforced by the same fail-closed preflight, which would have aborted the run before registration, and is covered by `hermes_system_prompt_sha256` (`e673af60…`, a hash of the full prompt-size report). The clone source's config has `user_profile_enabled: false`. |

**Per-cycle evidence** (`backtest_cycle_evidence`, 20 rows, one per trading
day, 20 distinct day labels):
- **Masked rules:** all 20 prompts contain the full identity-masked `RULES.md` block. Each one contains exactly `RunMasking.mask_text(RULES.md)` for this run's mapping, between its start and end markers.
- **Hashes:** `prompt_sha256` and `response_sha256` recompute correctly for all 20 rows.
- **Leak check:** `RunMasking.assert_clean` found no real ticker, company name, or year in any prompt, input payload, candidate status, or pre-fill gate record. It flagged one match in the model's **own** Day 11 response: the number `2000` in its sizing arithmetic (`floor(2000 / (65.49 − 62.19))`, the $2,000 risk budget). That is a dollar amount, not a year, and does not appear in the prompt. It is a false positive of the year regex, not a leak.
- **Tool use:** on every row `offered_tool_count 0`, `tools_used_count 0`, `tools_used []`, and attestation basis `structural_zero_offered_tools`.
- **Model metadata:** per-cycle `model gpt-5.6-terra` and `provider openai-codex` on every row. Per-cycle `reasoning_effort` is `NULL`, because Hermes' usage report does not return it; the run-level config records `medium`.
- **Attempts:** every row has `attempt_count 1` and no retry diagnostics. Two days recorded one pre-fill news-gate call each.

**Reconciliation**:

| Record | Count | Reconciles with |
|---|---|---|
| Evidence rows | 20 | days completed (20) |
| Portfolio snapshots | 20 | days completed; final equity matches the reported return |
| Trades | 21 | 16 BUY + 3 agent-requested SELL (the 19 proposals) + 2 harness-stop SELLs |
| Trades by state | — | BUY 6 filled / 10 rejected; SELL 5 filled. Fills 9 = 6 BUY + 3 agent-requested SELL; closed trades 5 = all filled SELLs (3 agent-requested + 2 `harness_stop`). |
| Agent activity | 104 | `order_queued` 19 = proposals; `order_rejected` 8 + `risk_check` 2 = 10 blocked; `order_placed` 6; `exit` 5; plus `cycle_start` 20, `reasoning` 40, `weekly_reflection` 4; no `cycle_failed` |
| Insights | 4 | the 4 weekly reflections |
| Run configuration | 1 | — |
| Pending at end | **0** | The 2 orders queued on the final day were resolved as rejected (`episode window ended before execution`), as reported in `dangling_pending_orders_resolved: 2`. |

## Scoped audit

| Check | Result |
|---|---|
| Neon deltas | Only the new run's rows were added: trades +21, insight_log +4, agent_activity +104, portfolio_snapshots +20, cycle_evidence +20, registry +1, run_configuration +1. **No other run's per-table counts changed.** That covers all eight `baseline_v1`/`baseline_v2` runs, Round 2 (`e68525f9…`), and the prior RED run (still exactly one registry row). |
| No other runs | The only new registry row is this run. No holdout, live-paper, Laya, scalable-screener, or fundamentals-enabled run (zero `fundamentals_enabled = true` configuration rows). Pre-existing pending trades on three older smoke runs are unchanged. |
| Files | `RULES.md`, `AGENTS.md`, `SOUL.md`, `watchlist/watchlist.json`, `docker-compose.yml`, `trade-journal-mcp/schema.sql`, the manifest, and `backtest_state.json` are byte-identical before and after. No `cycle_checkpoint.json`; the scratch checkpoint directory is empty. |
| Hermes profiles | Only `backtest` remains; the disposable `btrun-9446a57a23fd4cdc` was cleaned up. |
| Commit | Only this report is committed. No runtime output, checkpoint, log, secret, or image artifact. |

## Known limits of this evidence

- One one-month episode. The operational figures are not performance evidence of any kind.
- The gateway image reference is the compose-file pin, not an inspection of the running container.
- Model, provider, and reasoning are recorded from the isolated profile's configuration. The per-cycle model and provider come from Hermes' usage report; per-cycle reasoning effort is not reported.
- The year-pattern leak check cannot tell a 4-digit dollar figure from a year. It flagged one such figure in the model's own output, as described above.
- `trade-journal-mcp/requirements.txt` is unpinned (`>=`). This rebuild reused the cached dependency layer; a future no-cache rebuild could pull newer `fastmcp`/`asyncpg`.

## Next step

Proceed to the first real-data scalable-screener validation:
`python watchlist/scalable_screener.py --dry-run`. It is read-only and writes
nothing. No backtest-path blocker remains.
