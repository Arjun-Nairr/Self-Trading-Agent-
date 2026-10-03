# Scalable screener — first real-data dry run, 2026-10-03

**Verdict: AMBER.** The Stage 1 screener ran once against live providers and
completed every stage with **no failures and no unavailable data**. Its
printed output is internally consistent, and the dry run wrote nothing
persistent. The verdict is AMBER rather than GREEN for one reason:
`--dry-run` prints only aggregate counts and the pool. So the required check
that *each eligible name's own facts, history, and news values* satisfy the
configured criteria could not be done from the returned evidence. The dry run
also never runs the screener's record validator, which re-derives every row.

This says nothing about whether the pool is good. It also does not show that
Laya works on it, that Hermes can use it, that historical use is valid, or
that any strategy is profitable.

---

## 1. What was run

| | |
|---|---|
| Command | `python watchlist/scalable_screener.py --dry-run` (default limits, no overrides), run once from the repository root |
| UTC | started 2026-10-03T10:59:04Z, finished 11:27:20Z (28 min 16 s). Exit code 0, empty stderr. |
| Screen | `2026-10-03_d8ede6698b35` (attempt label `.a001`, never written). Screen date 2026-10-03 = run-start UTC date, a Saturday. |
| Code | HEAD `106cf98721b10dc7059506a80dddc3e1ea446fb7`. Working tree dirty only with the user-owned `HANDOFF.md`, `PROJECT_FRESH_START.md`, `docs/content/CONTENT_CONTEXT.md`, two `docs/obsidian/*.canvas`, and untracked `docs/research/`, `exports/`, `tmp_*_ppt/`. No screener or code changes. |
| Containers | All compose services were already running; nothing was started, rebuilt, or recreated. `maverick-mcp` and `alpaca-mcp` running with 0 restarts before and after. |

**Before the run — source and parity checks:**

- **No writes in `--dry-run`:** in `main()`, `--dry-run` calls only `run_screen(...)` and `print`. `run_screen` reads the record directory, which did not exist, and never writes. Only `--write` calls `write_attempt` / `publish_result`.
- **`alpaca-mcp`:** its `server.py` matches the repository by sha256 (`d3b49bc5…`).
- **`maverick-mcp`:** every source file changed since its image was built matches the repository (the Dockerfile itself is not copied into the image). The screener uses only these third-party libraries there: `httpx 0.28.1`, `pandas 2.3.3`, `yfinance 1.4.0`, `lxml`.

## 2. Stage-by-stage results

| Stage | Requested | OK | Unavailable | Requests | Retries | Elapsed | Errors |
|---|---|---|---|---|---|---|---|
| Universe (Wikipedia S&P MidCap 400) | 1 | 1 | 0 | 1 | 0 | 4.8 s | none |
| Facts (yfinance) | 400 | 400 | 0 | 401 | 1 | 527.8 s | none |
| History (yfinance, 5y) | 205 | 205 | 0 | 205 | 0 | 265.5 s | none |
| News (Alpaca, 3 trailing 14-day probes) | 201 | 201 | 0 | 603 | 0 | 897.1 s | none |

Limits in force are the defaults:
- **Facts and history:** concurrency 4, 30 s per-request timeout, 3 tries, 0.25 s spacing between requests.
- **News:** concurrency 2, 60 s timeout, 3 tries, 0.5 s spacing.
- **Universe:** concurrency 1, 45 s timeout.

News probe windows ended at 00:00 UTC on 2026-10-03, 2026-07-05 and 2026-04-06, so only completed days were requested.

There was one transient facts failure, recovered by its single retry. There
were no timeouts, no incomplete news pagination, no stage deadline hit, and no
signs of rate limiting.

**Outcome:** 400 names → **75 eligible**, 325 rejected, **0 data_unavailable**.
Status `OK`: a complete screen, not a partial or failed one, and not an empty
pool. The pool would have taken effect on Monday 2026-10-05 had it been
written.

## 3. Rejections

| Stage | Reason code | Count |
|---|---|---|
| Facts | `avg_volume_below_min` (< 1M shares/day) | 175 |
| Facts | `avg_volume_above_max` (> 12M) | 9 |
| Facts | `industry_excluded` (Biotechnology) | 9 |
| Facts | `market_cap_below_min` (< $2B) | 3 |
| Facts | `price_below_min` (< $10) | 3 |
| History | `history_below_min_bars` (< 750 bars) | 4 |
| News | `news_coverage_below_min` (< 6 articles across the 3 probes) | 126 |

Pool sector mix: Technology 18, Financial Services 13, Industrials 12,
Consumer Cyclical 11, Energy 8, Consumer Defensive 4, Basic Materials 2,
Healthcare 3, Real Estate 3, Communication Services 1. Four fixed-10 baseline
names are in the pool (ALGM, BRKR, BRX, DINO). Why the other six were
excluded is not visible in dry-run output.

## 4. Consistency checks (all passed)

- **Statuses add up:** 400 = 75 + 325 + 0.
- **Rejections by stage add up:** facts 400 − 205 = 195 rows carrying 199 reason codes, so 4 rows had two facts reasons. Then 195 + 4 + 126 = 325 rejected.
- **Cost order held:** history was requested only for the 205 facts survivors, and news only for the 205 − 4 = 201 history survivors. 201 − 126 = 75 eligible, which equals the pool size.
- **Pool and request counts match:** the pool has 75 unique symbols in sorted order, and the sector mix sums to 75. News requests 603 = 201 × 3, and history requests equal names requested.
- **Inside the bounds:** no stage exceeded its concurrency, attempt, or deadline settings.

## 5. Non-persistence (held)

| Check | Before | After |
|---|---|---|
| `watchlist/.screen_snapshots/` | absent | absent: no attempt or result file |
| `git status --porcelain` fingerprint | `35d32392…` | `35d32392…` |
| git index fingerprint | `a948f7c3…` | `a948f7c3…` |
| Neon (every table, every run's row counts) | — | identical; the screener has no database path |
| `maverick-mcp` `/app/data` (`maverick.db`, cache, universe snapshot) | unchanged timestamps and sizes | unchanged |
| Lingering screener child processes | — | none in either container |

The one state change outside the repository and database: yfinance's own
ticker-timezone cache inside the `maverick-mcp` container
(`~/.cache/py-yfinance/tkr-tz.db`, about 8 KB) was updated, as on any
yfinance call. It is container-internal library cache, not screener output.

## 6. Anomalies and limits of this evidence

- **The per-name criteria check could not be done; this is why the verdict is AMBER.** `--dry-run` prints counts, reasons, telemetry, and the pool, but not each row's market cap, volume, price, industry, bars, or news counts. It also never calls `validate_snapshot`, which re-derives every row's status from its recorded inputs. That happens only on `--write`. Correctness of eligible names currently rests on the unit-tested pure filters plus the stage-flow arithmetic above, not on inspected live values. For the same reason, malformed values, stale responses, or duplicate identities cannot be ruled out per row. Duplicates within the pool and the universe were ruled out (400 unique constituents, 75 unique pool symbols).
- **High news-coverage rejection rate:** 126 of the 201 names that reached the news stage (63%) failed. That is consistent with RULES.md §8's coverage rule, but it is the dominant filter and its per-probe counts are not visible here. Worth examining in the first written record.
- **Policy observation, not a defect:** the pool includes well-known, high-attention names (for example GME, CELH, CHWY, ROKU). The 12M-share volume cap is the screener's only guard against narrative-heavy names, and these pass it.
- **Current data only:** this is today's membership and facts. It is not valid for any historical date.

## 7. Verification after the run

- `python -m pytest -q watchlist`: 156 passed.
- `python verify.py`: 12/12 suites passed.
- `git diff --check`: clean.

## 8. What this proves, and what it does not

**Proves:**
- The live Stage 1 path works end to end on real providers: the universe scrape, the per-request child-process fetches in both containers, and bounded retries.
- Its output is internally consistent.
- `--dry-run` left no persistent trace in the repository or the database.

**Does not prove:**
- That each eligible name truly meets the thresholds (not inspected per row).
- That a written record validates.
- Anything about pool quality, Laya, Hermes, historical validity, or performance.

**Next step:** one `--write` run on a Saturday or Sunday UTC, which is the only
time it is permitted, followed by an audit of the persisted attempt's rows.
`--write` runs the full record validator, which re-derives every row's status
from its recorded numbers, and persists the per-name evidence this dry run
could not show. It needs its own explicit approval.
