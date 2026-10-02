# Scalable screener — scope v1

**status: Stage 1 (screener only) implemented and tested with injected data;
it has not been exercised on live data. Stages 2–3 remain discussion-stage:
not approved, not built.**

Stage 1 code: `watchlist/scalable_screener.py` (tests:
`watchlist/test_scalable_screener.py`; records go to the git-ignored
`watchlist/.screen_snapshots/`). No live screen has been run, so pool size,
rejection mix, and news-API cost are still unknown. Settled for Stage 1:
carry-forward lasts **one** refresh interval and only after a recorded failed
refresh; there is **no minimum pool size** — an empty pool is a valid result.
The sector cap belongs to the Laya shortlist and is not part of Stage 1; the
screener records sector mix only.

**A live screen is always today's.** `--dry-run` and `--write` fetch *current*
membership and facts, so they are only ever recorded under the real current
UTC calendar date; any other `--screen-date` is refused before a container is
touched. Only `--usable --on-date <any date>` takes another date, because it
reads stored records and fetches nothing.

How the screener grows past the fixed 10 names. This document changes no
code, rule, watchlist, Docker service, Neon schema, or dashboard. The fixed
10-name `watchlist/watchlist.json` and the frozen `baseline_v1`/`baseline_v2`
evaluation runs stay exactly as they are; this is a separate, additive path.

Target flow (live/current-universe only):

```
current broad universe
  → deterministic basic eligibility filter
  → variable screened candidate pool        (one immutable screen snapshot per refresh)
  → Laya attention shortlist, at most 10    (one immutable selection snapshot per day)
  → existing full technical/news review → Hermes   (later, separate approval)
```

Laya is an **attention selector, never a trade signal**. It is GREEN only as
an isolated, secure, deterministic component
(`docs/evaluations/laya_component_validation.md`, Round 2). It has never
scored a real company, and nothing in this scope claims it ranks usefully.

---

## 1. Current state

**Reusable as-is (logic, not wiring)**

| What | Where |
|---|---|
| §8 thresholds: cap $2B–$50B, volume 1M–12M, price ≥ $10, no Biotechnology | `watchlist/screen_watchlist.py:34-55`, `passes()` `:92-100` |
| 3-year history check | `screen_watchlist.py:103-120` |
| Per-symbol market-data fetch | `screen_watchlist.py:73-89` |
| News-coverage criterion (≥ 6 articles over three 14-day probes, complete pagination) | `watchlist/check_news_coverage.py:28-30`; pure evaluator already takes any symbol list: `backtest-harness/laya_preflight.py:96-118` |
| Price-coverage evaluator (any symbol list) | `laya_preflight.py:48-93` |
| Identity-free 8-field card builder | `backtest-harness/laya_cards.py:90`, `:131` |
| Opaque row IDs, strict scoring contract, top-10 selection, tie-break | `backtest-harness/laya_selector.py:205`, `:305`, `:356` (`MAX_SELECTED = 10`, `:52`) |
| Immutable write-once snapshot pattern | `laya_selector.py:545-589` |
| No-network Laya worker container | `backtest-harness/laya_experiment.py` |

**Fixed to 10 (or 30) — must not be touched; the new path goes around them**

| What | Where |
|---|---|
| `TARGET_SIZE = 10` and one-name-per-sector `diversify()` | `screen_watchlist.py:48`, `:123-148` |
| Hardcoded 10-symbol list and hardcoded 2026 probe dates | `check_news_coverage.py:28`, `:32-34` |
| The baseline watchlist file and its default load | `watchlist/watchlist.json`; `run_backtest.py:1669-1674` |
| §8 text: "8–12 names", the ten-row table (also embedded in every prompt) | `RULES.md:297-340` |
| Fixed-30 loader: requires `expected_count`, `group` ∈ baseline/added | `laya_selector.py:111-142`; "all 30 symbols" `laya_preflight.py:146` |
| Experimental selector wiring exists only in the backtest harness | `run_backtest.py:1628-1731`, `:6003-6025` |

**Requires redesign for a variable pool**

| Gap | Where it shows |
|---|---|
| Universe source is a scrape of *today's* S&P 400 page — no timestamp, no constituent hash, no history | `screen_watchlist.py:30`, `:58-70` |
| One unversioned cache file, never refreshed once written | `screen_watchlist.py:31`, `:155-165` |
| A failed fetch is silently dropped (`None`), indistinguishable from "not in universe" | `screen_watchlist.py:88-89`, `:163` |
| `passes()` returns a bare bool — no rejection reason is recorded | `screen_watchlist.py:92-100` |
| Screen output is printed, not persisted; no screener version or config hash | `screen_watchlist.py:167-180` |
| News check costs 3 API calls per name; untested at hundreds of names | `check_news_coverage.py:37-51` |
| Laya universe hash, row IDs, and masking assume one list frozen for a whole run | `laya_selector.py:94-108`, `:205-217`; `run_backtest.py:6017-6025` |
| No live-path selector wiring at all; live mode has never run | `docs/PROJECT_BACKLOG.md` §5 |
| Sector-concentration rule was deferred *because* the watchlist is small and hand-spread | `RULES.md:118-120` |

---

## 2. Recommended live-only v1 architecture

1. **Universe.** Fetch the current S&P MidCap 400 constituents (the existing
   source). Record source name, URL, fetch timestamp (UTC), constituent count,
   and a hash of the sorted constituent list. Valid for the current date only.
2. **Basic eligibility filter.** The existing §8 thresholds, unchanged, applied
   in cost order: cap / volume / price / industry → 3-year history → news
   coverage (only for names that survived the cheap filters; probe dates are
   trailing from the screen date, not hardcoded). Every name gets exactly one
   status — `eligible`, `rejected` (with ordered reason codes), or
   `data_unavailable` — never a silent drop.
3. **Variable candidate pool.** Every `eligible` name. No `TARGET_SIZE`, no
   one-per-sector trim, no padding. Pool size is an output, not a setting.
4. **Laya shortlist (daily).** For pool names not held and not pending, build
   the existing 8-field cards, score them in the existing worker container,
   take at most 10 with the existing deterministic rule. Fewer than 10 scored
   cards → pass all of them, record the shortfall.
5. **Full review → Hermes (later).** Out of scope here; see §6 stage 3.

Held positions always keep independent exit management, whether or not the
name is still in the pool or the shortlist.

---

## 3. State and snapshot design

Every record is immutable and write-once (the exclusive-link pattern used by
`laya_selector.write_snapshot`). Nothing is edited or overwritten, and there
is no mutable "current pool" pointer.

**Logical screen vs attempt.** The *logical screen* is one refresh:
`screen_id` = screen date + config hash. An *attempt* is one actual run of it.

| Record | File | Meaning |
|---|---|---|
| Attempt | `attempts/<screen_id>.a<NNN>.json` | Every run, successful or failed. A same-day retry is a new numbered attempt beside the earlier ones. |
| Result | `results/<screen_id>.json` | The one successful attempt for that logical screen, as a byte-identical copy. At most one, forever: a different successful attempt for the same screen fails loudly and replaces nothing. |

Re-running a logical screen that already has a result validates and returns
it without fetching anything.

**Record fields**

| Field | Content |
|---|---|
| `schema_version`, `screener_version` | Code/version identity |
| `screen_id`, `attempt`, `attempt_id`, `screen_date` | Logical screen and which attempt this is |
| `started_at`, `finished_at` | Real UTC timestamps of the run |
| `config`, `config_hash` | Every threshold and filter order; SHA-256 of the canonical config |
| `universe` | Source name, URL, `fetched_at` (UTC), constituent count, constituents hash |
| `news_windows` | The trailing probe windows derived from the screen date |
| `telemetry` | Per stage: start time, elapsed seconds, requested / ok / unavailable counts, request and retry counts, a bounded error summary (short codes only — never messages, tracebacks, credentials, or remote payloads), and the request limits in force |
| `rows` (one per constituent) | Symbol; raw filter inputs (market cap, avg volume, price, sector, industry, history bars, news counts per probe); `status`; ordered `reasons` |
| `counts`, `pool`, `pool_hash`, `sector_mix` | Status counts; sorted eligible symbols, their hash, and their sector mix |
| `screen_status`, `failure_reasons` | `ok` or `failed`; a failed attempt carries no pool and no effective date |
| `effective_from` / `previous_ok_screen` | The Monday the pool starts to apply; the previous successful screen |
| `content_hash` | Hash of everything above |

A record is trusted only after full validation, not just a matching content
hash: required keys and types, filename / attempt ID / screen ID agreement,
config hash, sorted-unique rows, counts, pool and pool hash, sector mix,
`effective_from`, and a valid status/failure combination. A result must also
have its matching attempt record.

**Which pool is usable on a date** (`usable_pool`, read-only, fails closed):

- the latest result already in effect is `current` during its own week;
- the week after, it is `carried_forward` only if the refresh that should
  have replaced it has a recorded *failed* attempt — a refresh nobody ran is
  not a failure and authorizes nothing;
- after one carried-forward week, or with any invalid record in the
  directory, the pool is `unavailable`.

**Bounded live workload.** Each stage runs with a small fixed concurrency, a
minimum spacing between request starts, a per-request timeout, and at most
three tries with capped exponential backoff; the container call for a whole
stage has its own computed ceiling. Incomplete news pagination is terminal
for that probe (fail closed, not retried). News is still requested only for
names that passed the cheap checks and the history check.

**Laya handoff — one selection snapshot per trading day.** The existing
selection snapshot, with `screen_id` + `pool_hash` recorded in place of the
fixed-universe hash, plus the count excluded as held/pending. Row IDs are
derived from (seed, symbol) so a name keeps a stable private ID across
screens; cards stay identity-free. Laya scores, ranks, and row IDs never
reach Hermes.

**Why rotation is unambiguous.** Membership on a date is never read from a
current list — it is "the pool of the latest successful result with
`effective_from` ≤ that date". A name that leaves and returns appears as
`rejected` (with the reason and the numbers that caused it) in one snapshot
and `eligible` in a later one; both records persist. So "was X eligible on
day D, and why not" always has exactly one answer, and a daily selection
snapshot names the exact screen it drew from. The opaque alias scheme is
already per-symbol and stable when membership changes.

---

## 4. Decisions that need Arjun

| Decision | Recommendation | Alternative |
|---|---|---|
| **Pool bounds / policy** | **Settled:** no target count, no ceiling, and no minimum pool size — the pool is everything that passes, and an empty pool is a valid result. Revisit a ceiling after stage 1 shows the real pool size. | Bound the pool (e.g. 30–100); when over, trim deterministically by distance from median liquidity (the existing narrative-avoidance tiebreak). |
| **Refresh cadence** | Weekly, on the weekend; the snapshot applies to the following week's trading days. Laya runs daily on that frozen pool. | Monthly — less churn and fewer news-API calls, but slower to drop a name that stops qualifying. |
| **Sector diversification** | No cap at the screener; apply it at the shortlist: at most 3 of the 10 from one sector, filled deterministically by next-ranked name. Record the sector mix either way. | No cap anywhere in v1; record the mix and decide from observed concentration. |
| **Incomplete coverage/data** | Fail closed. A name with missing data is `data_unavailable`, never eligible. If the universe fetch fails, the news check is incomplete, or more than 10% of constituents are `data_unavailable`, the screen is `failed`; the last successful result stays in effect for one further refresh interval only, and only when that refresh has a recorded failed attempt (settled); then new entries stop. Exits are never affected. | Strict: no carry-forward — any failed refresh means no new entries until a clean screen. |

Not a screener decision, but it follows from the third row: once the pool is
broad, the reason `RULES.md:118-120` gives for deferring a sector-exposure
rule no longer holds. Any such rule is a separate rules change needing its
own approval.

---

## 5. Deferred: historical / backtest requirement

**Point-in-time universe membership is mandatory before this screener enters
any historical evaluation.** Today's S&P 400 scrape answers "who is in the
index now"; replayed over past dates it would be survivorship-biased and
would leak future knowledge of which companies lasted. The same applies to
the filter inputs — yfinance `.info` market cap, volume, sector, and industry
are current values, not as-of values — and to prices for names since
delisted.

Until a point-in-time membership source (and as-of filter inputs) exists:

- this screener is live/current-universe only;
- it is not wired into `run_backtest.py` or `eval_suite.py`;
- no result from it may be compared with `baseline_v1`/`baseline_v2`;
- the fixed-30 prototype (`docs/scopes/LAYA_SELECTOR_V1.md`) remains the only
  selector experiment permitted in backtests.

---

## 6. Staged build order

Each stage needs its own explicit go-ahead; none is approved by this document.

1. **Screener-only validation — code implemented, live validation not yet run.** A new screener module beside the existing
   one (the existing script and `watchlist.json` untouched) that writes screen
   records. Pure-function tests for filters, reason codes, config hash, and
   snapshot immutability. Run at least two refreshes on current data and
   review pool size, rejection-reason distribution, churn between screens,
   sector mix, and news-API cost. No Laya, no Hermes.
2. **Laya on real screened cards (shadow).** Build cards for the pool, score
   them in the worker container, write daily selection snapshots. Nothing
   reaches Hermes and nothing trades. This is the first time Laya would see
   real-company cards; compare against seeded-random and simple deterministic
   shortlists on attention metrics only.
3. **Hermes integration (later).** Needs, at minimum: a live-path design (live
   mode has never run), the risk checker's watchlist membership defined
   against the active screen snapshot, an approved `RULES.md` §8 change, the
   live news-verification gate (`docs/PROJECT_BACKLOG.md` §3), and private
   selector evidence in Neon.
