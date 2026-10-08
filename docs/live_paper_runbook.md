# Live-paper runbook (Alpaca paper account)

Operator guide for `live/`. Current project state and the next step live in
`HANDOFF.md` §10; the rules Hermes follows live in `live/LIVE_POLICY.md`
(with `RULES.md`, which still binds every numeric limit). This file holds
commands, prerequisites, verification evidence and known limits.

**Status (2026-10-08): verified for a read-only integration dry run. No paper
order has ever been placed, so order entry, fills, OCO protection, cancel and
replace are verified only against a model of Alpaca (mock tests), not against
Alpaca itself. Nothing here is evidence of profitability.**

## What it is

`live/run_live.py` is a deterministic controller around Hermes. Each cycle:
**reconcile with the broker** (no LLM) → manage holdings → research
candidates → trade plan → **execution validation** → submit/reconcile →
later reassessment.

- Hermes (isolated `live` profile) researches and returns JSON decisions. It
  has no order, journal, shell, file or write tools.
- The controller sizes in code, runs `risk_checker.check_trade` unchanged,
  submits a price-bounded IOC limit entry, then a broker-held OCO
  (stop + target) sized to the shares actually filled. A position counts as
  protected only when the broker reports that OCO open with the intended
  quantity and prices.
- State is durable and idempotent (`lv-<plan>-<kind><n>` client order ids,
  intent persisted before sending, lookup by client id after any error or
  restart). Cycles are serialized by an OS-level lock; state is loaded inside it.
- Private state and evidence: `--state-dir` (default `~/.hermes-trading-agent-live`,
  outside the repo): `live_state.json`, `events.jsonl` (includes raw Hermes
  replies), `selector/` (Laya snapshots). Journal rows go to the existing
  `trades` table with `execution_mode='alpaca'`.

## Commands

All from the repo root, Docker Desktop running (never started by the tooling).

```bash
python live/run_live.py setup-profile            # idempotent: (re)configure the isolated `live` Hermes profile
python live/run_live.py dry-run --hermes         # READ-ONLY: broker reads + Laya + one Hermes call; nothing executed
python live/run_live.py cycle                    # one real reconcile + review (places orders if Hermes plans entries)
python live/run_live.py loop                     # scheduler; NOT authorized (unattended)
```

Add `--universe watchlist` only as an explicit, temporary choice of the frozen
ten names (used for the 2026-10-08 dry run). The default is `--universe screened`.

## Prerequisites for the first supervised paper session

1. **Wider-pool snapshot (blocking).** Record the weekly screen on a Saturday
   or Sunday (UTC): `python watchlist/scalable_screener.py --write` (needs the
   maverick and alpaca containers). It takes effect the following Monday, so
   the earliest usable session is Mon 2026-10-12. Check with
   `python watchlist/scalable_screener.py --usable --on-date YYYY-MM-DD`.
   Without a usable screen the selector reports the blocker and entries stay
   blocked (exits and reconciliation still work).
2. **Verify the 20-name path on real data:** `python live/run_live.py dry-run --hermes`
   with the default universe. Confirm the shortlist is ≤ 20, the Laya worker
   scores the whole pool within its timeouts, and no orders are sent.
3. Docker up; `maverick-mcp` image built from current source; `live` profile
   configured (`setup-profile`); Laya checkpoint present (default
   `~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/55cf4c4e…`).
4. **Separate user authorization** for a supervised session. Run `cycle`
   by hand while watching the Alpaca paper dashboard; do not run `loop`.

## Verification evidence (2026-10-08)

| Level | Evidence |
|---|---|
| Mock-tested | `live/` 73 tests: delayed/partial entries, pending/partial/rejected/ambiguous exits, protective fill during cancel, ignored cancels, rejected/ambiguous/interrupted stop replacement, stale/future quotes after long research, news after the research cutoff, stale-state and lock races, 30-name pool → 20-name shortlist with rotation. `python verify.py` 13/13 suites (backtest 631 passed). The fake broker enforces Alpaca's wash-trade and over-sell refusals, but it is a model. |
| Adapter contracts | The container scripts in `live/adapters.py` are executed against a stub `httpx`/`server` (URLs, params, bodies, 404 → none, cancel on terminal order). |
| Real, read-only | Paper broker clock, account, positions, orders, calendar, IEX quote, news and daily bars; maverick bars through `uv run`; the isolated Laya worker scored the ten-name list (10 scored, 10 selected); one Hermes research call ran 10 `technical_get_full_technical_analysis` calls through its tool loop and returned a valid "no entry" reply; 0 orders and 0 positions on the paper account afterwards; no leftover containers; `backtest_state.json` unchanged. |
| Containers | `maverick-mcp` rebuilt (observed-only support/resistance); its technical tests: 147 pass, 12 fail identically on the unmodified HEAD (missing `lint-imports`; stale `StubService` signature). `alpaca-mcp` and `trade-journal` sources match their running images. Laya worker images embed `laya_selector.py` but never call `select_pool`, so they were not rebuilt (their baked copy lacks only the new `max_k` argument). |
| Hermes tools | `live` profile built-in toolsets: none. MCP whitelist: 9 maverick read tools + `fetch_headlines`; with the MCP protocol's read-only list/get-prompt/resource utilities this is 18 reachable tools, none able to place orders, write portfolios/watchlists/journals, run shell or touch files. |

Dry-run attempts: 2 launches. Attempt 1 failed before any model reasoning
(maverick runner used the wrong interpreter; the profile's `auth.json` had
been created root-owned by an inspection command) and was fixed. Attempt 2
succeeded (about 3 minutes, one Hermes session).

## Known limits and unverified items

- No real order lifecycle has been exercised: IOC entry, OCO acceptance
  (including its leg shape), cancel/replace latency, partial fills.
- A stop-replace is cancel → confirm → resubmit, so protection has a short gap;
  entries pause until it is re-verified. Broker stops do not cap loss across gaps.
- IEX quotes only (no SIP). No halt or corporate-action handling.
- One process does everything: a long selector refresh delays reconciliation
  (broker-held stops are unaffected).
- An entry the broker cannot find after 2 minutes is marked failed; if it lands
  later it appears as an unmanaged position.
- The wider-pool path (pool > 20, Laya scoring time at that size) is
  mock-tested only; real-data verification waits for the snapshot above.
- The injection monitor is not wired into live mode.
- Docker commands that run as root can create root-owned files in the `live`
  profile; use `exec -u hermes` for inspection (setup re-chowns the profile).
