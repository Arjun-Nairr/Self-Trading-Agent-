# Project integrity & Laya-readiness audit

**Status:** internal, read-only audit. Generated 2026-09-27 against `HEAD 1285526`
("Match sizing prompt to rounded fill reserve"). No source, rules, manifest,
watchlist, Docker config, dashboard, database row, checkpoint, or runtime
configuration was modified. No Hermes call, backtest, holdout, or live-paper
action was run. Neon was queried with `SET default_transaction_read_only = on`
(SELECT only). The Hermes gateway's local data directory
(`~/.hermes-trading-agent/`) was inspected read-only (SQLite opened with
`mode=ro`; request dumps read for structure/tool names only — no headers,
credentials, or config secrets were printed). Only side-effect-free test suites
were executed (see §6).

**Evidence labels used on every finding:**
- **[code]** — self-verified from code
- **[stored]** — validated from stored run evidence (Neon rows, Hermes session DB, request dumps)
- **[inferred]** — inferred; needs a targeted check

**Scope of stored evidence:** the eight valid comparison runs —
`baseline_v1` `1c754200…`, `295c1bb9…`, `329ec8aa…`, `dd02237a…`;
`baseline_v2` `bbabc03a…`, `d4c71c98…`, `cdb5120e…`, `791f3137…` — plus the
Hermes gateway's session store for 2026-09-19 → 09-28.

---

## 1. Executive verdict

The project is **safe and internally consistent as a paper-trading research
system**: every numeric rule is enforced by a pure, fail-closed risk checker at
execution time; nothing in this audit found a path to real money, a lookahead
in price/news data, or a corrupted comparison between `baseline_v1` and
`baseline_v2`. The V1/V2 pair is genuinely comparable — same model, same Hermes
system prompt (bar a wall-clock date line), same RULES/AGENTS/SOUL hashes, same
alias maps, and only the sizing commits changed between them.

What it **cannot yet support is the project's headline claim** — that these
results measure *the trading agent's judgment, under RULES.md, on
identity-masked data*. Four findings each weaken one part of that sentence:

1. The backtest call to Hermes is **not tool-less and not memory-isolated**:
   30 tools (terminal, file read/write, session search, memory, browser, and a
   generic MCP `tool_call` that reaches the trade journal and the Alpaca paper
   broker) are offered on every cycle. Stored evidence shows the model **never
   used them** (0 tool calls across 188 baseline-window sessions) — so no result
   is contaminated — but nothing enforces it.
2. **RULES.md's text never reached the model** in any baseline run. Hermes saw
   AGENTS.md, SOUL.md, and the controller's restated sizing numbers — not the
   12% stop ceiling, the §9 prohibitions, or the time stop — while AGENTS.md
   tells it to "read RULES.md every cycle."
3. **Execution semantics filter which trades exist.** Entries are sized to
   exactly the 20% cap at the decision price, and execution rejects any BUY
   whose next-day open is above that price (§2b) or far enough below it (§6a).
   Only **87 of 223 queued BUYs (39%) filled**; the fill set is conditioned on
   the next-day open. V2's 5bps fix moved §2b rejections only 50 → 44.
4. **Identity masking leaks in stored prompts** — "Allegheny Technologies"
   (ATI's former name), "Holly Energy Partners" (DINO's affiliate, printed
   next to its alias), and "Warner Records / Warner Chappell" (WMG) — and the
   leak detector cannot see these by design.

None of these requires touching `baseline_v1`/`baseline_v2`; they are frozen,
comparable, and honest *if these four caveats are disclosed with them*.

**Verdicts (detail in §8/§9):**
- **Current project: GO** as a frozen paper-trading research baseline, **with
  mandatory disclosure** of findings H1–H4 alongside any results claim.
- **Laya scope discussion: GO.** Nothing must be fixed before *discussing* it.
- **Laya implementation: NO-GO** until the prerequisites in §7.9 are met —
  H1 (invocation isolation) and H4 (scalable masking) must be fixed first, and
  the backlog's "dynamic candidate selector — cancelled" decision must be
  explicitly reopened.

---

## 2. Confirmed strengths

Each was checked in this audit, not taken from prior documents.

| Strength | Evidence | Label |
|---|---|---|
| **V1 vs V2 is a clean comparison.** Only three runtime commits separate them (`bf61502`, `e57ff95`, `1285526`, all sizing); every V2 Hermes session postdates `1285526` (17:40 UTC 09-26; first V2 session 18:42 UTC). | `git log` on all runtime paths since 09-18; Hermes `sessions.started_at` | [code] [stored] |
| **Same model and Hermes context across all 8 runs.** Every evidence row records `gpt-5.6-terra`; one RULES/AGENTS/SOUL hash triple across all 8 runs; the three Hermes system-prompt hashes seen during the window differ *only* in the line `Conversation started: <date>`. | `backtest_cycle_evidence.model/*_sha256`; `state.db` `system_prompts` diff | [stored] |
| **Same alias maps and seeds per regime pair.** `derive_episode_seed` is a pure function of manifest hash + episode identity; V1/V2 `backtest_run_configuration` rows match exactly. | `eval_suite.py:766-775`; `get_run_configuration` for all 8 runs | [code] [stored] |
| **Price basis is consistent.** Technicals (`auto_adjust=False`), daily bars, and Alpaca intraday bars (`adjustment: raw`) share one raw basis. Stored fills show no dividend-adjustment gap (BRX, a REIT, averages −0.68% fill-vs-proposal, not the ~9% an adjusted series would imply). | `maverick/market_data/fetchers.py:24-44`, `run_backtest.py:1461-1499`, `alpaca-mcp/server.py:564-566`; `trades.fill_price/proposed_entry_price` | [code] [stored] |
| **Point-in-time technicals.** `TechnicalService._prepared_frame` bounds the fetch at `as_of`; `get_price_history` reads/fetches only `[start, end]` trading days. | `maverick/technical/service.py:172-194`, `maverick/market_data/service.py:201-249` | [code] |
| **News windows are enforced on both edges, newest-first, fail-closed.** `start < created_at <= end` validated host-side before truncation; provider sort is `desc`; incomplete pagination halts the cycle. | `run_backtest.py:1587-1657, 1784-1845`; `alpaca-mcp/server.py:502` | [code] |
| **Reflection memory is run-scoped.** Insights and reflection trades are filtered by `mode='backtest'` and `run_id`. | `run_backtest.py:2971-3060` | [code] |
| **Risk checker is pure and fail-closed**, and the live `place_order` tool also calls `check_trade` against a hard-coded paper URL. | `risk-checker/risk_checker.py`; `alpaca-mcp/server.py:53,155,186`; `test_risk_checker.py` PASS | [code] |
| **Halted cycles never auto-retry**; evaluation episodes are never resumed and halted ones are excluded from aggregates. | `run_backtest.py:4861-4900`; `eval_suite._aggregate_episode_reports` | [code] |
| **Private evidence tables are outside the anonymous grant.** No grant on `backtest_cycle_evidence` or `backtest_run_configuration`. | `trade-journal-mcp/schema.sql:417-423, 713-734` | [code] (grant state not re-verified live in this audit) |
| **No gateway memory was written.** `~/.hermes-trading-agent/memories/` is empty despite `memory_enabled: true`. | directory listing | [stored] |

---

## 3. Critical / high findings

No finding is **Critical** — none produced an observed contamination, a safety
breach, or an invalid V1/V2 comparison. Four are **High** because each makes
the project's central claim unsupported as currently worded.

### H1 — The backtest Hermes call is not tool-less or memory-isolated

**What:** `_call_hermes` runs `hermes -z <prompt> -t ""` inside the long-lived
`gateway` container (`run_backtest.py:3640-3668`), whose `/opt/data` is the
persistent `~/.hermes-trading-agent` directory (`docker-compose.yml`, gateway
`volumes`). The `-t ""` argument does not remove tools. Stored request dumps
(`~/.hermes-trading-agent/sessions/request_dump_*.json`, 2026-08-30 and 09-12)
show every backtest request carrying:
- `tool_choice: "auto"` and **30 tools**, including `terminal`,
  `execute_code`, `read_file`, `write_file`, `patch`, `search_files`,
  **`session_search`**, **`memory`**, `browser_navigate`, `delegate_task`,
  `skill_manage`, and the generic MCP meta-tools `tool_search` / `tool_describe`
  / **`tool_call`**;
- a **42,112-character system prompt** assembled by Hermes: SOUL.md, AGENTS.md,
  generic agent boilerplate (`<mandatory_tool_use>`, `<act_dont_ask>`), and a
  "**Skills (mandatory)** — you MUST load it with `skill_view`" section.

`config.yaml` registers all four MCP servers — maverick, news, **trade-journal
(Neon writes)**, and **execution → alpaca-mcp (paper orders)** — and sets
`memory_enabled: true`. The gateway's `state.db` (21 MB) retains every past
backtest prompt and reply, including V1's, which used **the same alias maps**
as V2.

**Why it matters:** the "no tools in backtest mode" invariant (AGENTS.md "When
you are backtesting") is an instruction, not a boundary. A single tool call
could fetch live data for a real ticker, search prior sessions with identical
aliases (defeating the comparison), write a `MEMORY.md` injected into every
later prompt, write to the journal, or place a paper order. The model is also
being measured under instructions that *tell it to use tools*.

**What stored evidence shows:** across **188 Hermes sessions** in the baseline
window (all 8 runs), `tool_call_count = 0`, `api_call_count = 1`, exactly 2
messages each, and no `role='tool'` messages. Session counts reconcile exactly
with cycles plus pre-fill confirmation calls. **No baseline result is
contaminated.** The risk is latent, but nothing detects a violation — no
controller check fails closed if a tool is used.

**Labels:** [stored] (request dumps, config, `state.db` counts); applicability
of the 09-12 dumps to the 09-20/26/27 runs is [inferred] — same pinned image
and invocation, and the stored system-prompt text for those days contains the
same `## Skills (mandatory)` and tool text.

**Fix direction (not implemented):** a dedicated Hermes home/profile for
backtests with toolsets and memory disabled, no MCP servers registered, and no
session persistence; plus a controller check that fails the cycle closed if
the usage/session report shows any tool offered or used.

### H2 — RULES.md's text never reaches the model in backtest mode

**What:** the stored Hermes system prompt for the baseline runs contains
AGENTS.md and SOUL.md in full but **none of RULES.md's body** — probes for
"Stop-losses can be jumped", "12% below entry", "5 trading days", "Never
average down", and the §8 watchlist table all return absent. The controller
prompt (`build_prompt`, `run_backtest.py:3324-3516`, 5–9 KB) restates only §2a,
§2b, §3, and the §6b minimum. With zero tool calls (H1), the model never read
RULES.md. Meanwhile:
- AGENTS.md: "Read `RULES.md` … at the start of every cycle, before making any
  decision"; RULES.md header: "re-read every cycle";
- every evidence row stamps `rules_md_sha256`, which reads as "this cycle was
  governed by this file."

**Why it matters:** safety is intact — the risk checker enforces every number.
But the stop ceiling (12%), §9.4 same-day re-entry, and §6c are invisible to the
model, so some rejections (§6a, §9.4) are the harness enforcing rules the agent
was never shown, and "Hermes operated under RULES.md" is not a true statement
for any baseline run. The prompt is also internally contradictory (system:
mandatory tools and skills; AGENTS: no tools in backtest; user: reply only
JSON).

**Labels:** [stored] (`state.db` system prompt + zero tool calls); [code]
(`build_prompt`).

### H3 — Execution semantics create a next-day-open fill filter

**What:** Hermes must propose *exactly* `required_entry_quantity`
(`run_backtest.py:3941`, enforced at `4782-4793`), which almost always binds on
§2b (20%) because stops sit inside 10%. The order then executes at the next
day's 09:45 bar, and `resolve_queued_order` (`run_backtest.py:667-720`)
re-checks it against the *slipped real open* (`risk_check_price`, line 709):
- open meaningfully **above** the decision price → §2b breach → rejected;
- open far enough **below** → stop distance < 3% → §6a → rejected.

Stored outcome of every queued BUY across the 8 runs:

| | Queued BUYs | Filled | §2b rejected | §6a-only rejected | Pre-fill news | Episode ended | Other |
|---|---|---|---|---|---|---|---|
| V1 | 115 | 43 | 50 | 11 | 3 | 6 | 2 |
| V2 | 108 | 44 | 44 | 11 | 3 | 5 | 1 |

Every filled BUY across all eight runs filled between −2.5% and **+0.92%** of
the proposed entry.

**Why it matters:** the realized trade set is filtered by a post-decision
variable (the overnight gap direction and size), independent of the agent's
judgment. This is legitimate fail-closed behavior, but it means (a) any claim
about entry quality measures "entries that opened flat-to-slightly-down," and
(b) the V2 slippage fix addressed a ~5bps component of a much larger effect.
This is not lookahead; it is a selection effect that must accompany any
performance or judgment claim.

**Labels:** [stored] (`trades` status/rejection_reason/fill vs proposed);
[code] (sizing and re-check path).

### H4 — Identity masking leaks in stored prompts; the leak check cannot see it

**What:** stored `backtest_cycle_evidence.prompt_text` for the baseline runs
contains, identically in V1 and V2:
- July 2022 (5 prompts each): *"Keybanc Maintains Overweight on **Allegheny
  Technologies**, Lowers Price Target to $31"* — ATI's pre-2022 legal name;
- September 2022 (3 each): *"NOVEMBER and **Holly Energy Partners** Announce
  Management Changes"* — the alias printed next to the name of HF Sinclair's
  affiliate;
- July 2022 (8 each): *"**Warner Records** UK…"*, *"…Signs Deal With **Warner
  Chappell Music**"* — WMG's label and publishing arm.

Causes:
- `_name_variants` (`run_backtest.py:160-173`) strips suffixes from the
  *current* legal name; `_BRAND_OVERRIDES` (`:153-157`) covers only SFM, BRX,
  ALGM. No former names, affiliates, labels, or executives.
- `RunMasking.assert_clean` (`anonymize.py:251`) only searches for the
  configured names, so it passes all of these. A pure local probe confirmed
  "Allegheny Technologies", "HollyFrontier", "Holly Energy Partners", "PF Black
  Card", and executive names all pass `mask_text` and `assert_clean`
  unchanged.
- `build_prompt` prints `[sector]` per candidate (`run_backtest.py:3433`), and
  the watchlist has **exactly one company per sector**, so the sector label
  identifies the company within any run and across runs regardless of alias.

**Why it matters:** HANDOFF §6 says masking was "verified leak-free against
real headline text," and §5 says masking and lookahead prevention "guarantee a
backtest decision never sees information that wouldn't have existed yet." The
stored prompts contradict the first; the second overclaims (the price/news
gate is sound, but identity recall is not prevented). V1 and V2 received the
same leaked text, so the **comparison is unaffected**; the
**memorization-free claim is not**. At 100–150 companies (Laya), this manual
approach does not scale.

**Labels:** [stored] (`prompt_text` ILIKE counts and contexts); [code]
(`_name_variants`, `assert_clean`, `build_prompt`); pure local probe run.

---

## 4. Medium findings worth scheduling

### M1 — Stored "verbatim" prompt ≠ what the model received; Hermes-side context is not captured
- `_call_hermes` backslash-escapes `\`, `"`, `$` (`run_backtest.py:3643`) and
  passes the prompt as a **list argv** — there is no shell, so the backslashes
  arrive literally. The stored request dumps show 60–68 `\"` and 21 `\$`
  sequences with zero unescaped `$`. `backtest_cycle_evidence.prompt_text`
  stores the unescaped version, so it is not byte-identical to the model input.
- The 42 KB Hermes system prompt, tool list, and skills index are **not hashed
  or stored** by the controller. Reconstructing them (as this audit did)
  depends on `state.db` outside the repo, which is not backed up or
  retention-controlled.
- Argv transport also caps prompt size: the Windows `CreateProcess` limit is
  32,767 characters (current max is about 8.6 KB, so this blocks Laya-scale
  prompts).
- **Labels:** [code] [stored].

### M2 — No durable per-run code/runtime provenance
`backtest_run_configuration` stores window, watchlist hash, alias hash, seed,
and the fundamentals flag, but **not** the git SHA, dirty-tree flag, container
image digests, Hermes system-prompt hash, or offered tool count. "`baseline_v2`
= code `1285526`" currently rests on a chat-time preflight plus timestamp
inference (§2), not a stored record. **Labels:** [code]
(`run_backtest.py:2664-2693`, `schema.sql:713-734`).

### M3 — Pre-fill news gate: LLM verdict is neither checkpointed nor recorded
`apply_pre_fill_news_gate` (`run_backtest.py:1987-2152`) runs at the top of
`run_day` (`:4145`) **before** the cycle checkpoint is consulted (`:4530`). It
makes a separate Hermes call whose prompt and raw reply are never persisted —
only an activity line with counts and timestamps — and it writes
`resolve_trade_rejection` to Neon immediately (`:4160-4161`), before the
cycle's essential commit. On a resumed training cycle, the gate re-fetches and
re-asks, can reach a different verdict, and may contradict an already-journaled
rejection. The six trades cancelled this way (3 in V1, 3 in V2) have no
auditable record of what Hermes was shown or said, outside the gateway's local
`state.db`. **Not reachable for evaluation episodes**, which are never resumed.
**Labels:** [code]; reachability [inferred].

### M4 — Public Data API exposes each run's ticker↔alias map
The anonymous role has table-level `SELECT … USING (true)` on `trades`,
`agent_activity`, `insight_log`, `portfolio_snapshots`, registry and status
(`schema.sql:259-305, 542, 601`), which the public Dashboard V2 reads.
**107 of 306** `trades` rows in the 8 runs carry alias names in
`strategy_notes`/`invalidation_condition` beside the real `ticker`, and
**695 of 1,096** `agent_activity` rows name aliases. Because
`derive_episode_seed` is config-independent (`eval_suite.py:766-775`), every
future configuration run on these four episodes will reuse maps that are
already publicly reconstructable. Low harm today — the model does not read the
dashboard, though H1's `browser_navigate` means it structurally could — but
this is a boundary Laya must not inherit. **Labels:** [stored] [code].

### M5 — Documentation contradicts current state or overclaims (could mislead implementation)
- `HANDOFF.md` §3 still presents Round 2 as "Current verified state"; §6 calls
  the eval suite "defined but never invoked"; §10's "exact next step" predates
  both baselines. (HANDOFF has user-owned uncommitted edits; this refers to the
  working copy.)
- `docs/PROJECT_BACKLOG.md` §4 "Now": `baseline_v2` "not yet started"
  (it is complete).
- HANDOFF §6 "verified leak-free" and §5 "guarantee" — contradicted or
  overstated per H4.
- RULES.md header "re-read every cycle" and AGENTS.md "Read RULES.md" —
  false in backtest mode per H2.
- HANDOFF §7 "verbatim rendered prompt" — per M1.
- PROJECT_BACKLOG §2: "Point-in-time dynamic candidate selector — **cancelled
  for this project** … fixed 10-ticker watchlist remains the permanent scope" —
  directly conflicts with a Laya experiment unless explicitly reopened.
- HANDOFF §8's survivorship note ("screened using data from inside the exact
  backtest window") is stale for 2022–2024 episodes, where the watchlist was
  chosen in 2026 from then-current S&P 400 members — survivorship bias is
  stronger, not weaker.
- **Labels:** [code] (document text).

### M6 — Tests cover the fixed bugs, not the failure modes found here
Pure suites pass (§6), but nothing asserts: the Hermes invocation offers zero
tools; the bytes the model receives equal the stored `prompt_text`; RULES.md
content (or an explicit restatement) reaches the model; masking rejects former
names, affiliates, or brands (`test_anonymize.py`'s leak test omits ATI and
PLNT and has no historical names); or pre-fill gate verdicts replay
deterministically. **Labels:** [code].

---

## 5. Known limitations acceptable for this project

Each is acceptable **if disclosed** with results; none needs fixing before Laya
discussion.

- **Statistical power:** 4 one-month, single-seed, single-path episodes per
  config; no buy-and-hold benchmark in `eval_suite` output (computed separately
  in prior analysis). Already acknowledged in
  `docs/evaluations/baseline_v1_vs_v2_evaluation.md`.
- **Survivorship-biased fixed watchlist** (M5). Acceptable for a
  fixed-watchlist baseline; disqualifying for any universe-selection claim.
- **Real prices, volumes, and sectors unmasked** — deliberate (`anonymize.py`
  docstring). Combined with one-company-per-sector, identity is
  model-recoverable in principle (H4).
- **News veto sees at most 3 headlines**, headline only, ≤110 chars
  (`run_backtest.py:3411, 3438`); summaries fetched but not shown.
- **Final-day BUYs expire unexecuted** (11 across the 8 runs) — correct episode
  semantics.
- **Exact-share-match requirement** wastes 1–3 proposals per run on
  arithmetic misses (§2/§3 mismatch rejections).
- **`proposal_count` not persisted**; `blocked_reasons` capped at 20
  (`eval_suite.py:1020`) — documented in the V1/V2 evaluation.
- **Live mode never run**: no bracket/OCO, partial fills, or ambiguous-write
  reconciliation (HANDOFF §8).
- **Correlation-unaware caps** (RULES.md §3 deferral) — fine at 10 names.
- **Maverick price cache is first-write-wins** (`get_price_history` +
  dedupe): safe today because the raw-price pin predates every 2022–2024 fetch
  and the 2026 screener used yfinance directly, not the cache
  (`watchlist/screen_watchlist.py:117`). [inferred] — the cache volume itself
  was not inspected.

---

## 6. Non-issues — deliberately reviewed and found acceptable

| Reviewed | Result | Label |
|---|---|---|
| Model tool use during baselines | 0 tool calls in 188 sessions; 1 API call each; session count = cycles + gate calls | [stored] |
| Hermes system prompt drift V1→V2 | Only the `Conversation started:` date differs | [stored] |
| Price-basis mismatch (adjusted vs raw) | None; fill-vs-proposal gaps −1.3%…−0.2% average per ticker, no dividend skew | [stored] |
| Runtime code drift V1→V2 | Only the three sizing commits | [code] |
| Decision-time lookahead in news | Both window edges enforced at session-close cutoff; newest-first | [code] |
| Decision-time lookahead in technicals | `as_of` bounds the fetch; `current_price` = `as_of` close; execution T+1 | [code] |
| Cross-run reflection leakage | `mode` + `run_id` scoped | [code] |
| Validation retries | `attempt_count` = 1 on every evidence row in all 8 runs | [stored] |
| Paper-only broker path | Hard-coded paper URL; server-side `check_trade` | [code] |
| Private tables in public grant | Not granted per `schema.sql` | [code] |
| Gateway memory writes | `memories/` empty | [stored] |
| Pure test suites (the only ones run; Docker was up, so anything capable of I/O was deliberately not run) | `risk-checker/test_risk_checker.py` PASS; `backtest-harness/test_anonymize.py` 13 passed; `alpaca-mcp/test_historical_news_pagination.py` 5 passed (httpx mocked); `dashboard-v2/test/calc.test.mjs` 46/46 | executed |

---

## 7. Laya selector readiness

Target pipeline: **broad point-in-time universe → deterministic eligibility
screen → alias masking → local Laya review pool → Hermes judgment → risk
checker.** No Laya implementation is approved; this section is design input
only. Laya would be a **separate experiment with its own baseline**, never a
retroactive change to `baseline_v1`/`baseline_v2`.

### 7.1 Current seams, stage by stage

| Stage | Existing seam | Ready? |
|---|---|---|
| Broad point-in-time universe | **None.** `watchlist/watchlist.json` is a static 2026 snapshot; `_resolve_watchlist_snapshot` (`run_backtest.py:257-310`) and `backtest_run_configuration` can *freeze* a per-run universe, but nothing *builds* a historical one. | ✗ |
| Deterministic eligibility screen | `eval_suite.audit_episode_watchlist_viability` (`eval_suite.py:414-464`) proves per-ticker data availability via the production technicals path. `_check_candidate_coverage` (`run_backtest.py:3538`) handles outage vs quiet day as a *fraction*. Insertion point: `run_day` after `fetch_technicals`/`fetch_news` (`:4385-4387`), before candidate masking (`:4415`). | Partial — the viability audit is all-or-nothing (`"valid": len(unavailable) == 0`), so one unavailable name among 150 blocks the whole manifest. |
| Alias masking | `RunMasking` (`anonymize.py`) — stable within a run, `assert_clean` fail-loud. | ✗ at scale — see 7.3 and H4. |
| Local Laya review pool | **None.** Nearest pattern is the pre-fill gate — the pattern *not* to copy (M3: uncheckpointed, unrecorded). Copy the main-cycle checkpoint pattern instead (`_save_checkpoint`, `:1415`). | ✗ |
| Hermes judgment | `build_prompt` candidate list (`:3417-3442`). | ✓ seam exists, but H1/H2/M1 apply; argv transport caps prompt size. |
| Risk checker | `check_trade` with `Context.watchlist` (`risk_checker.py:192-195`); exits of held names remain allowed off-list. | ✓ mechanically; **semantics need a decision** — is "watchlist" the run's frozen universe or the day's selected pool? RULES.md §1/§8/§9.3 say "watchlist only". |

### 7.2 Every place that assumes a ~10-company universe

| Location | Assumption | Laya impact |
|---|---|---|
| `anonymize.py:71, 137-140` `_ALIAS_POOL` | 14 NATO aliases; **raises `ValueError` above 14 tickers** (verified with a 15-ticker probe) | Hard block |
| `anonymize.py:146` `dict(zip(sorted(tickers), pool))` | Positional zip: any membership change reshuffles every alias | Must key aliases on the frozen universe |
| `run_backtest.py:153-157` `_BRAND_OVERRIDES` | 3 hand-curated brand entries | Does not scale; needs per-company, time-versioned names |
| `run_backtest.py:4032, 4070, 4386-4387` | Fetches bars, technicals, and news for **every** watchlist ticker every cycle, serially in one container call; news paginates per ticker | 100–150× per-day I/O; Alpaca rate limits; cycle time |
| `run_backtest.py:3417-3442` `build_prompt` | Every unheld name becomes a prompt candidate | Prompt grows linearly; must show only the selected pool |
| `run_backtest.py:3650-3652` argv prompt transport | Fits ~8.6 KB today | Windows 32,767-char limit (M1) |
| `run_backtest.py:3433` `[sector]` label + one-name-per-sector watchlist | Sector identifies the company | Weaker at scale, but sector plus price remains a cue |
| `run_backtest.py:3047` `_get_trade_history(limit=200)` | Reflection pairing window | Can truncate BUY/SELL pairs in long, wide runs |
| `eval_suite.py:375, 414-464` | Manifest valid only if **all** watchlist names are usable | Needs per-ticker exclusion with a recorded reason |
| `risk_checker.py:121, 192-195`; `alpaca-mcp/server.py:49` (mounted `watchlist.json`) | Watchlist = eligibility | Must be redefined per run or per day |
| `RULES.md` §1, §3 (deferral: "small and hand-picked for sector spread"), §8 (10-row table, "Size 8–12 names"), §9.3 | Rules text | Needs an explicit, approved rules change |
| `watchlist/screen_watchlist.py` (median-liquidity pick per sector), `check_news_coverage.py` | One name per sector, live-dated screen | Needs a point-in-time rewrite |
| `dashboard-v2/js/data.js:115` activity `limit=200`; roadmap text | Display sizing | Cosmetic |
| `docs/PROJECT_BACKLOG.md` §2 ("fixed 10-ticker watchlist remains the permanent scope") | Policy | Must be reopened (decision 3) |

### 7.3 Minimum safe design: a stable seeded alias map for ~100–150 companies with only a subset shown

1. **Assign aliases over the whole frozen universe, never the daily pool.** A
   company keeps one alias for the whole run, whether or not it is selected on
   a given day, so selection never reshuffles identity.
2. **Key the map on identity, not position:** alias =
   `pool[ HMAC(seed, point-in-time company id) mod |pool| ]`, collision-resolved
   deterministically — or freeze a full permutation of the frozen universe.
   The seed stays derived from manifest hash + episode identity (so paired
   arms share maps), and the **full map lives only in the private
   `backtest_run_configuration`**.
3. **Opaque, non-ordinal aliases, pool ≥ 2× universe:** e.g. random
   pronounceable tokens. No sector, rank, selection-order, or size hint. Never
   derive the alias from the Laya rank.
4. **Hermes sees only the selected pool's aliases.** Held positions stay
   visible (for exits) even after they leave the pool.
5. **Laya must also be masked.** A local model that sees real tickers or dated
   headlines can select from memory ("this name rallied in July 2022") — the
   same memorization leak the project masks Hermes against, one step earlier.
   Laya input = the same masked, point-in-time packet (aliases, relative days,
   redacted years).
6. **Masking dictionary per company, per time:** legal name history, former
   names, tickers (old and new), brands, major subsidiaries/affiliates, and
   (ideally) top executives, versioned by date — plus a **coverage scan**
   against a broad name list (the whole universe plus known aliases) rather
   than only the configured variants. The current `assert_clean` must not be
   the only line of defense (H4).

### 7.4 Historical-universe and survivorship-bias requirements

- **Point-in-time membership** of the source index or universe (e.g. S&P MidCap
  400 constituents *as of* each episode start), including names later
  delisted, acquired, or bankrupt. yfinance history for delisted tickers is
  unreliable; the data source must be proven for delistings before design.
- **Point-in-time eligibility inputs only:** market cap, ADV, price floor,
  listing age, and news-coverage counts computed from data ≤ `as_of`, never
  from today's screen (today's `screen_watchlist.py` uses live data).
- **Identity continuity:** ticker changes and renames (e.g. HFC→DINO,
  Allegheny→ATI) mapped by date, feeding both data fetches and masking.
- **Corporate actions and price basis:** yfinance with `auto_adjust=False` is,
  to my knowledge, still split-adjusted while Alpaca `adjustment: raw` is
  not — harmless for the current 10 names (no splits in 2022–2026 found in
  stored fills) but it **will** mis-size stops and entries around any split in
  a 150-name universe. [inferred — needs a targeted check against a known
  split ticker]
- **Frozen universe snapshot + hash per run**, persisted privately before any
  fetch — same discipline as `_resolve_watchlist_snapshot`.
- **Disclosure:** until the above exist, any Laya result is a
  survivorship-biased selector result and must say so.

### 7.5 The correct local boundary for Laya

**Recommendation: an isolated local service, invoked only by the backtest
controller.**

- **Not inside the Hermes gateway** — that is exactly the tool-, memory-, and
  session-bearing environment H1 identifies.
- **Not in-process in the controller** — model weights/runtime dependencies
  and nondeterminism should not share a process with the code that owns state,
  commits, and the alias map; separate pinning and digests are needed.
- **Isolated service properties:** own container or process; **no network
  egress**; no Neon credentials; no tool or file access beyond its input;
  pinned model weights digest, quantization, runtime version, temperature 0,
  and fixed seed; input by stdin or file, not argv.
- **The controller remains sole owner** of the universe snapshot, eligibility
  screen, alias map, pool-size/constraint enforcement, checkpointing (Laya's
  raw output checkpointed *before* use, replayed verbatim on retry — unlike
  M3), and all journal writes.
- Laya returns **only ranked aliases from the eligible set plus bounded
  reason codes**; the controller validates membership and size and fails
  closed on any off-set alias, duplicate, or malformed reply.

### 7.6 Selector inputs: eligibility/context vs an indirect trade signal

**Non-predictive eligibility/context** (a deterministic rule on these can
define a *universe* without its own paired experiment):
listing age / data sufficiency; price ≥ floor; ADV and market-cap bands
(trailing, ≤ `as_of`); exchange/security type; sector (for diversity quotas);
trailing news-coverage *count* (so the veto is live); known binary-event
exclusion *only* if the event calendar is itself point-in-time; halted/delisting
flags.

**Indirect trade signals** (require a paired experiment against a control arm
that uses the same eligibility screen, pool size, seeds, and episodes but a
non-informative selection — e.g. seeded random or fixed-rule — so selection
value is measured, not assumed):
any use of returns, momentum, relative strength, volatility regime, RSI/MACD or
"setup quality"; news sentiment or news-*volume spikes*; earnings or event
proximity; analyst actions; fundamentals; and **any Laya judgment that reads
technicals or news content at all.** If Laya reads content, it is a signal.

### 7.7 Durable records needed to audit one selector run later

All private (same boundary as `backtest_run_configuration`), written before
use:

1. Code and runtime provenance — git SHA and dirty flag, image digests, Hermes
   system-prompt hash and offered tool count (M2), and Laya model id / weights
   digest / runtime / params / prompt-template hash.
2. Universe — source and version, as-of date, frozen snapshot and hash, rename
   map version.
3. Eligibility — rule version, parameters, and hash; per-day eligible set and
   its hash; per-ticker exclusion reasons.
4. Alias map — full universe map and hash; masking-dictionary version.
5. Laya, per day — exact input packet (masked) and hash, raw output, parsed
   ranking, validation result, fallback or failure events, `attempt_count`.
6. Selected pool per day — aliases plus private real tickers, pool size, and
   turnover versus the prior day.
7. Linkage — every Hermes cycle's evidence row references the day's pool hash,
   so "what Hermes could have chosen from" is reconstructable.
8. Control arm — the same records for the paired non-informative arm.

### 7.8 What a public dashboard may safely display

**Safe:** per-day counts (universe, eligible, selected, turnover %); coarse
sector mix of the selected pool; performance, drawdown, and fill/rejection
distributions; `Day N` labels; run-level provenance hashes (not contents).

**Never:** real tickers together with aliases (today's M4 pattern); any
free-text generated from a masked prompt (`strategy_notes`, reasoning,
invalidation conditions, reflections) next to a real ticker; Laya inputs,
raw outputs, or per-alias rankings; headlines or exact price series that
re-identify pool members; the alias map or seeds' derivation inputs for
episodes not yet run.

**Mechanism:** replace table-level anonymous grants with **allowlisted public
views** for Laya-era tables. Choose one public identity space per run — real
tickers *or* aliases, never both — and strip model-generated text from the
public side.

### 7.9 Laya readiness verdict

**Discussion: GO. Implementation: NO-GO** until:
1. **H1 fixed** (tool-less, memoryless, session-less backtest invocation with a
   fail-closed check) — a Laya experiment would multiply prompt volume and
   context while relying on the same latent boundary;
2. **H4's masking redesigned** for scale (7.3), including masking Laya's own
   input;
3. **point-in-time universe source proven** (7.4) — delistings, renames, split
   basis;
4. **M1/M2 provenance** in place (stdin/file transport, byte-exact evidence,
   per-run code/runtime record);
5. **M4 public-view allowlist** designed for the new tables;
6. **policy reopened:** PROJECT_BACKLOG §2 "cancelled" reversed for a
   separately scoped experiment, and RULES.md §1/§8/§9.3's "watchlist"
   semantics redefined.

---

## 8. Three user decisions genuinely required next

1. **Backtest invocation boundary and what counts as a new baseline.** Fixing
   H1 (tool-less, memoryless profile) and H2 (whether RULES.md's text, or an
   explicit restatement of every rule, goes into the backtest prompt) changes
   the model's context. Decide: (a) apply these only to *future*
   experiments, keeping `baseline_v1`/`baseline_v2` frozen with H1–H4
   disclosed; or (b) treat the fix as a new `baseline_v3` that reruns the same
   four episodes under the corrected context. Either is defensible; they cannot
   be mixed silently.
2. **Execution semantics behind H3.** Keep "size exactly at cap, reject on any
   gap past it" as permanent, disclosed semantics — or approve a change (a
   fixed sizing buffer below the cap, or resizing down to the cap at
   execution). This touches RULES.md §2b interpretation and the "never
   silently resize" principle (`resolve_queued_order` docstring), so it is a
   rules decision, not an implementation detail.
3. **Reopen the dynamic-selector decision for Laya, and bound it.** If yes:
   confirm it is a separate research project with its own universe and
   baseline; approve the isolated-local-service boundary (7.5); and decide
   whether Laya may read **content** (technicals/news → a signal, paired
   experiment required) or **only eligibility metadata** (7.6).

---

## 9. Smallest safe next milestone

**"Backtest invocation hardening + provenance" — infrastructure only, no
trading-rule change, no backtest run, no change to frozen baselines.**

Scope:
1. Prompt transport via stdin or file instead of argv, with byte-exact
   evidence: the stored `prompt_text` hash equals the hash of the bytes Hermes
   received (fixes M1's escaping and the size ceiling).
2. A dedicated Hermes home/profile for backtests — no toolsets, no MCP
   servers, memory off, no session persistence — plus a controller check that
   **fails the cycle closed** if any tool is offered or used (H1).
3. Per-run and per-cycle provenance: git SHA and dirty flag, image digests,
   Hermes system-prompt hash, offered-tool count (M2).
4. Tests for each (M6): a tool-less-invocation assertion, prompt byte-fidelity,
   and a provenance round-trip.

Exit criteria: pure tests green; a single *dry-run inspection* of the new
invocation (no market data, no journal writes) showing zero tools offered;
documentation corrections from M5 proposed for user review.

It is the smallest step because it touches no rule, sizing, or data, yet it is
a prerequisite for every path in §8: a `baseline_v3`, the H3 decision's
follow-up run, and any Laya experiment.

---

## Final verdicts

- **Current project: GO** — as a frozen, safe paper-trading research baseline.
  `baseline_v1` and `baseline_v2` stand as valid, comparable references,
  **provided H1–H4 are disclosed with any claim about them.**
- **Laya scope discussion: GO.**
- **Must anything be fixed before discussion?** **No.**
- **Before Laya implementation?** **Yes** — H1 and H4 (masking at scale) must
  be fixed, M1/M2 provenance added, a point-in-time universe source proven, a
  public-view allowlist designed, and the backlog/RULES "watchlist-only,
  selector cancelled" policy explicitly reopened (§7.9). H2 and H3 need user
  decisions (§8), not necessarily fixes, before implementation.
