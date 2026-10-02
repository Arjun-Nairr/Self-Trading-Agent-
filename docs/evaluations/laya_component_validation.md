# Laya component-only validation

> **Status (latest): Round 2 — GREEN, component-readiness only** (see
> "Round 2" at the end of this file). **Round 1 below returned RED on
> 2026-10-02 and is preserved unmodified as historical evidence**; its two
> findings (lenient worker-reply parsing, incomplete Python-only network
> block) were real and were fixed before Round 2. GREEN means only that the
> component is ready to *discuss* the screener — not that Laya is useful,
> calibrated, profitable, or ready for Hermes.

# Round 1 (2026-10-02) — historical, verdict RED

**Date:** 2026-10-02 · **Verdict: RED** (two criteria; both small, fixable, and
independent of the model itself — see §6) · **Scope:** the already-built local
Laya component (`laya_selector.py`, `laya_adapter.py`, `laya_worker.py`,
`laya_experiment.LayaWorkerScorer`) as an isolated software/model dependency.

This is **not** a screener, backtest, Hermes, trading, or selector-performance
test. Only synthetic, identity-free numeric cards in the approved eight-field
schema were used — no company data, no universe file, no prices, news,
fundamentals, screener output, Hermes, risk checker, or run command. No source
file, rule, watchlist, Docker, Neon, or dashboard was edited; this report is
the only repository change.

## 1. Result at a glance

| # | Check | Result |
|---|---|---|
| 1 | Pins: package / commit / weight hash | **Match** (all three) |
| 2 | Local load with network blocked | **Pass** (12 s, 0 network attempts) |
| 3–4 | Varied valid batch → correct, finite, in-range, prose-free output | **Pass** |
| 5 | Repeat ×5 same worker + fresh worker | **Byte-identical** at the provider's output precision |
| 6 | Batch vs individual (30 and 64 cards) | **Identical** per row |
| 7 | Shuffled order (6 permutations) | **Mapping and ranking order-independent** |
| 8 | Duplicates / incomplete / malformed / out-of-range / forbidden / malformed responses | Inputs: **all rejected, worker survives**. Responses: **3 malformed shapes accepted** ✗ |
| 9 | Timings, ties, cleanup | Measured; cleanup **clean** on normal close, forced kill, timeout, and controller hard-exit |
| 10 | No network / secrets / port / trading code | Secrets, port, trading code: **pass**. Network block **incomplete** ✗ |

## 2. Commands run

Scratch directory `<scratch>` = the session scratchpad, outside the repository;
all scripts live there and were not committed. Script SHA-256 (first 16):
`val_common.py e29281b514a0d324`, `val1_inprocess.py 37b500d43266cb3a`,
`val2_worker.py a232c7efb1cec9d1`, `val3_malformed.py 3b6fa47cb7d0d312`,
`val3b_nan.py 071977981f04d86c`, `probe_launcher.py 37a6fbcfb48f9ce1`.

```
# pinned venv (created earlier from backtest-harness/requirements-laya.txt) vs the pins
<scratch>\laya-venv\Scripts\python -m pip list --format=freeze        # matches requirements-laya.txt (+ truststore, used only to download)
# the ONLY network step: official pinned checkpoint, TLS verified via the OS trust store
PYTHONPATH=backtest-harness <scratch>\laya-venv\Scripts\python -c "import truststore; truststore.inject_into_ssl(); import laya_adapter; laya_adapter.provision_checkpoint(cache_dir=r'<scratch>\hf-ckpt')"
# 1,2,3,4,5(in-process),6,7(in-process): pinned venv, network blocked BEFORE load
<scratch>\laya-venv\Scripts\python val1_inprocess.py
# 3-10 through the real controller + real private worker (controller process has no torch/laya)
python val2_worker.py
# malformed worker responses vs the real controller code (scripted fake worker, correct handshake)
python val3_malformed.py ; python val3b_nan.py
```
`val2_worker.py` also shells out to `tasklist`, `taskkill /F`, and `netstat -ano`
for the cleanup and port checks. Nothing launched Docker, Neon, Hermes, an
order, or any run/evaluation command.

## 3. Pinned runtime and model evidence

| Item | Project pin | Observed | Match |
|---|---|---|---|
| `laya` package | `0.3.23` | `0.3.23` | ✓ |
| Checkpoint commit | `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` | snapshot dir = same SHA; also equals `laya.PINNED_REVISIONS["convaiinnovations/laya"]` | ✓ |
| `model.safetensors` SHA-256 | `891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c` | independently computed over the 842,609,210-byte file: identical | ✓ |
| Other pins | torch 2.14.1, transformers 5.18.0, tokenizers 0.23.2, safetensors 0.8.0, huggingface_hub 1.33.0, numpy 2.5.3, pydantic 2.13.5 | installed versions identical (torch reports `2.14.1+cpu`) | ✓ |
| Runtime | CPU, Python 3.12.5, Windows 11 | | |

Provider warning observed (7× in the worker log): *"this checkpoint ships
invalid temperatures or values outside [0.5, 5]; using `choice:11+`=0.1006 →
0.5. Treat confidence from the affected entries as uncalibrated."* It names a
`choice`-type entry; the project asks a `noul` question, and whether it affects
that path was not determined. Consistent with the existing scope position,
`answer_confidence` is uncalibrated and is not used for ranking.

## 4. Synthetic test design

- **Cards:** 30 deliberately varied cards — 15 corner cases (each field at its
  minimum/maximum with the others neutral: RSI 0/100, MACD ±1, range 0/1,
  relative volume 0/50, volatility 0/3, 5-day return −40%/+60%, SMA distance
  ±30%) plus 15 seeded random draws across every valid range (relative volume
  log-uniform). Plus a 64-card set, a 150-card random set (tie study), and
  seven duplicate/near-duplicate cards. Row IDs are the project's opaque
  `r_<12 hex>` hashes of synthetic labels (`synthetic-0`…); a literal salt
  string was used — the experimental universe was never loaded.
- **Probes** ran against (a) the pinned model in-process, (b) the real private
  worker through the real controller, (c) the real worker started via a
  test-only launcher that records facts and then runs the unmodified
  `laya_worker.main`, with dummy secrets (`ALPACA_API_KEY`, `DATABASE_URL`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_TOKEN`, …) set in the controller's
  environment, and (d) a scripted fake worker emitting malformed replies.
  Network probes targeted `127.0.0.1`/`localhost` only — no external traffic.

## 5. Measured results

**Output contract (3–4).** `DecisionResult` per card; the single answer carries
only `type`, `noul`, `confidence`, `answer_confidence`, and `action`
(`{act_probability}` — a provider field, ignored by the adapter, not a trade
action). The only string anywhere in the raw output is the literal
`type = "noul"`: no generated prose. The model input was the seven numeric
fields only (all `float`, canonical order, 4-decimal rounding; no row ID, no
text). `state_tokens_dropped = 0`, `truncated = false`. All 30 outputs mapped
back to the correct opaque row, in input order, with finite `review_probability`
and `answer_confidence` in [0, 1]; the record type has exactly three fields.

**Determinism (5–7).** Compared as exact float reprs:

| Comparison | Rows differing | Max drift |
|---|---|---|
| 5 repeats, same worker, vs first batch | 0 | 0.0 |
| Fresh worker (new process), vs first batch | 0 | 0.0 |
| In-process: 3 repeats, 30 singles, 5 shuffles | 0 | 0.0 |
| Worker: 30 individual calls vs the batch | 0 | 0.0 |
| Worker: 64 cards (two internal passes of 32) vs 64 individual calls | 0 | 0.0 |
| Worker: 6 shuffled orders — mapping and `select_pool` ranking | identical in all 6 | — |

Caveat: Laya itself rounds `noul` and `answer_confidence` to **4 decimals**, so
"identical" means identical at that resolution; sub-1e-4 numerical drift is not
observable from this interface. Records follow the *input* order in every case
(shuffled input → shuffled records); order independence therefore holds for the
row→value mapping and the downstream ranking, not for the list order.

**Timings (CPU).** Cold worker start → ready: **30.1 s** (in-process load with
network blocked: 12.1 s). First 30-card batch: 16.1 s. Warm 30-card batch
(×5): 15.8–16.0 s, median **15.9 s** (≈0.53 s/card). Earlier sessions on this
machine measured ≈11 s per 29-card batch and 12–18 s loads, so treat these as
±40% run-to-run on this host.

**Scores and ties (9).** On the 30 varied cards: 30 distinct values (range
0.0006–0.2786, median 0.098), no ties. On 150 random cards: **134 distinct, 15
tied groups covering 31 rows, largest tie 3**; values near a hypothetical
top-10 boundary (ranks 8–12) were 0.2538, 0.2322, 0.2183, 0.1912, 0.1870 —
separated by far more than the 0.0001 resolution in this sample. Ties are
real, modest, and resolved by the seeded tie-break; they are synthetic-card
statistics, not a prediction for real cards. Sensitivity note: moving one RSI
input by only 1e-5…1e-3 (the 4-decimal input rounding collapses sub-5e-5
changes) changed the score from 0.0447 to 0.0464 and then 0.0456 — small,
non-monotonic input-to-score noise at about the 0.002 level.

**Duplicates and edge cases (8).**

| Case | Outcome |
|---|---|
| Exact duplicate cards (distinct opaque IDs) | identical outputs |
| Near-duplicates below the 4-dp input resolution | collapse to identical input and output |
| Same `row_id`, different content, one batch | `score_complete_cards` returned **1** entry (silent overwrite, no error); `select_pool` then raised `duplicate row_id` — fails closed downstream, not at the scorer |
| Incomplete card (supported path) | skipped, never scored; sent raw to the worker: rejected |
| Forbidden fields (`ticker`, `name`, `company_name`, `symbol`, `date`, `as_of`, `headline`, `news`, `sector`, `pnl`, `reasoning`, `data_complete`) and a name-like `row_id` | all rejected (`ForbiddenCardField`) |
| Malformed numerics (string, bool, list, dict, 10^400, NaN, ±inf, `1e999`) | all rejected or treated as incomplete-and-rejected |
| Out-of-range (RSI 101/−0.01, MACD 1.0001, range 1.01/−0.01, negative volume/volatility) | all rejected (`InvalidCardValue`) |
| Malformed protocol lines (non-JSON, `null`, `[]`, missing/non-list `cards`) | each answered with an error reply; empty `cards` → `ok []` |
| After every edge case | worker alive; the original batch re-scored **byte-identical** |

**Malformed worker responses vs the real controller (26 malformed scripted replies plus an unmodified control, and a NaN/Infinity re-test with full-length replies).**
Rejected (fail closed): garbage and truncated JSON, wrong request id, missing
`records`, explicit error, too few/too many records, reversed order, duplicate
record, unknown row id, probability >1 or <0, confidence >1, **NaN/Infinity**
(`NaN`/`Infinity` tokens in full-length replies), `null`, prose
(`"strong buy"`), list, missing field, non-object record, non-list records,
empty object, list reply. Several rejected with a raw `KeyError`/`TypeError`
rather than `ScorerError` — still fail closed, because `select_for_day`
catches every exception. **Accepted:**

1. `review_probability: "0.5"` (numeric **string**) → coerced to `0.5`.
2. `review_probability: true` (a **boolean**) → coerced to `1.0`.
3. A record carrying an extra prose field (`"explanation": "BUY this…"`) → the
   record was accepted and the extra field silently dropped (the prose cannot
   propagate: the record type has no field for it).

**Cleanup (9, 10).** Normal `close()`: worker gone in 0.56 s. Worker force-killed
(`taskkill /F`) mid-run: next call restarted a fresh worker with identical
output; closed cleanly. Request timeout: controller closed and killed the
worker (gone in 0.46 s; the error text reads "within 0s" — a cosmetic
formatting of a 0.001 s test timeout). Controller hard-exit (`os._exit(1)`):
the orphaned worker exited by itself in **1.8 s** (stdin EOF).

**Isolation (10)** — measured inside the real worker process:

| Probe (localhost targets only) | Result in the real worker |
|---|---|
| Secrets in worker environment | **none** — keys present: `APPDATA, COMSPEC, HF_HUB_DISABLE_TELEMETRY, HF_HUB_OFFLINE, LOCALAPPDATA, PATH, PATHEXT, PYTHONIOENCODING, PYTHONUNBUFFERED, SYSTEMDRIVE, SYSTEMROOT, TEMP, TMP, TRANSFORMERS_OFFLINE, USERPROFILE, WINDIR` (dummy secrets were set in the controller) |
| Listening port / connections (`netstat -ano`, by PID, after scoring) | **none** |
| Project/trading modules imported | only `laya_adapter`, `laya_selector`, `laya_worker` (no `run_backtest`, risk checker, `anonymize`, Hermes, Alpaca, DB driver) |
| `socket.connect` / `getaddrinfo` | blocked ✓ |
| `socket.connect_ex` to a local listener | **succeeded — not blocked** ✗ |
| `socket.gethostbyname("localhost")` | **succeeded — not blocked** ✗ |
| UDP `sendto` | **succeeded — not blocked** ✗ |
| Open a listening socket (`create_server`) | **succeeded — not blocked** ✗ |
| Network attempts during model load (blocked first; instrumented: `connect`, `connect_ex`, `getaddrinfo`, `gethostbyname`) | **0** |

The worker's working directory is the harness directory and its `sys.path`
includes it, so it is process-isolated, not sandboxed: it does not import
trading code or hold credentials, but the filesystem is not restricted.

## 6. Verdict — RED

Applying the stated standard exactly:

- **RED criterion — "malformed output accepted":** the controller accepts three
  malformed worker replies (numeric string, boolean, extra prose field). The
  boolean case is the sharpest: `true` is turned into a valid-looking `1.0`
  probability. All other malformed replies fail closed. Impact is bounded —
  the real model always returned proper floats, and a defective/compromised
  worker is the only source — but the standard names this as RED.
- **RED criterion — "network exposure while scoring":** the worker's
  Python-level block covers only `connect` and `getaddrinfo`. In the scoring
  process, `connect_ex`, `gethostbyname`, UDP `sendto`, and listening sockets
  still work (confirmed on localhost). Nothing used them — zero attempts during
  load, no connection or listening port observed by `netstat` — so this is a
  control weaker than documented rather than observed traffic, but it is
  exposure by capability and the "network blocked" claim in the scope doc is
  not accurate as written. Python-level patches also do not constrain native
  code.
- **Not a blocker, noted:** duplicate `row_id`s are not detected at the scorer
  stage (they fail closed one stage later); some rejections use raw exception
  types.

What is sound: the pinned model, checkpoint and runtime match exactly;
offline load works; output is correct, numeric, prose-free and row-mapped;
results are reproducible at the provider's precision across repeats, fresh
workers, batch/individual scoring and input order; bad inputs are rejected
without harming the worker; cleanup is clean in every tested failure mode; no
secrets, ports, or trading code in the worker. Had only the cosmetic items
above been found, this would be GREEN.

## 7. What this validates — and what it does not

**Validates (component reliability only):** that the pinned Laya package,
commit and weights load and run locally and offline on CPU; that the card →
worker → record contract behaves deterministically at the provider's 4-decimal
resolution; the input-rejection and worker-lifecycle behavior; the measured
isolation properties above.

**Does not validate:** anything about real cards or real markets; whether
`review_probability` or `answer_confidence` is calibrated, meaningful, or
useful for any question (the model's own shipped temperatures raise a
provider warning); ranking quality or tie behavior on real data (the tie
numbers are synthetic); the screener, selector design, eligibility, masking,
Hermes, risk checking, or any trading outcome; that Laya selects good stocks or
predicts profitable trades — **no such claim is made.** It also does not test
GPU/other hardware, other OSes, other batch sizes beyond 30/64/150, or
adversarial model files.

## 8. Recommended next action

One change set, then re-run this validation: **make the controller's response
parsing strict and the worker's network block complete** — in
`LayaWorkerScorer.score` / `score_complete_cards`, require every record to have
exactly the three expected keys, `row_id` as a string, and the two values as
real floats (reject `bool`, strings, extra keys; raise `ScorerError`; also
reject duplicate `row_id`s there), and in `laya_worker`, deny *all* socket
egress and listening (not just `connect`/`getaddrinfo`) or enforce it at the OS
level (e.g. a firewall rule or no-network job object), then correct the scope
doc's "network blocked" wording to match what is enforced.


---

---

# Round 2 — hardening and revalidation (2026-10-02)

**Verdict: GREEN — the Laya component is ready to discuss the screener, and
nothing more.** Round 1 above is preserved unmodified as historical evidence:
it returned RED, and its two findings were real. Round 2 fixes exactly those
two boundaries and repeats the same component-only validation.

## R2.1 What changed

1. **Strict worker-reply contract** (`laya_experiment.parse_score_reply`,
   `parse_handshake`): the success envelope must be exactly `{id, records}` (an
   error reply exactly `{id, error}`); each record exactly
   `{row_id, review_probability, answer_confidence}`; `row_id` a string equal to
   the request's id at the same position (so duplicate, reordered, unknown, and
   count-mismatched ids are all rejected); both numbers genuine JSON floats —
   `bool`, `int`, `str`, `null`, lists/dicts rejected — finite and in [0, 1].
   Every failure raises `ScorerError` (no raw `KeyError`/`TypeError`). Duplicate
   `row_id`s are now rejected *before* scoring, in both `score_complete_cards`
   and `LayaWorkerScorer.score`. A worker that sends a malformed reply is
   dropped; a malformed reply selects nobody that day.
2. **Real network isolation: a dedicated worker container** instead of a host
   process. `docker run --rm -i --init --network none --read-only --tmpfs
   /tmp:rw,noexec,nosuid,size=256m --cap-drop ALL --security-opt
   no-new-privileges --pids-limit 512 --user 10001:10001`, offline-flag `-e`
   values only, one read-only checkpoint mount, no `-p`, no host environment.
   Image: `backtest-harness/laya_worker_image/` (base image pinned by digest,
   CPU-only torch, the pinned Laya packages, exactly three project modules, a
   default-deny build context). The controller verifies the **live** container
   with `docker inspect` after every worker start and refuses to score if the
   configuration differs. JSON-lines over stdin/stdout is unchanged; the model
   is still loaded once per run (lazily); the Python-level socket deny is now
   complete (`connect`, `connect_ex`, `bind`, `listen`, `accept`, `sendto`,
   `sendmsg`, `sendfile`, `getaddrinfo`, `gethostbyname[_ex]`, `gethostbyaddr`,
   `getnameinfo`, `create_connection`, `create_server`, `fromfd`) but is
   documented as a second line of defence only.
3. **No host-worker fallback.** `--laya-python` was removed. In experimental
   mode, before any other work, the controller requires a reachable Docker
   daemon, the worker image, and the pinned-commit checkpoint snapshot, else
   `SystemExit` ("there is no host-worker fallback") — verified with tests that
   fail if any process is spawned or any run side effect occurs.
   `--selector-mode` off is untouched.

## R2.2 Commands run

`<scratch>` = the session scratchpad (outside the repository). Scripts were not
committed. Round 2 added `val4_container.py`, `val5_raw.py`, `xplat.py`, and
re-ran the Round 1 response scripts against the new parser.

```
docker pull python:3.12-slim                                   # base image; digest pinned in the Dockerfile
docker build -f backtest-harness/laya_worker_image/Dockerfile -t hermes-laya-worker:1 backtest-harness
PYTHONPATH=backtest-harness <scratch>\laya-venv\Scripts\python -c "...provision_checkpoint(cache_dir=...)"   # only network step besides the build
python val4_container.py                      # real controller + real container worker: determinism, edge cases, docker-level isolation, cleanup
docker run --rm --network none --read-only ... --entrypoint python hermes-laya-worker:1 /val/val5_raw.py       # raw output / no prose / offline load
python val3_malformed.py ; python val3b_nan.py                # 26 malformed replies + NaN/Infinity vs the new strict parser
<scratch>\laya-venv\Scripts\python xplat.py                   # Windows host vs Linux container, same 30 cards
python -m pytest -q backtest-harness ; python verify.py       # selector-off behavior
```
The validation also used `docker exec`, `docker inspect`, `docker port`,
`docker kill`, `docker ps -a`, and `docker rm -f` on the worker containers it
created. No trading, evaluation, Hermes, backtest, or live-paper command ran;
only synthetic identity-free cards were scored.

## R2.3 Results

| # | Requirement | Round 2 result |
|---|---|---|
| 1 | Package / checkpoint / weight pins | **Match, measured inside the container.** `laya 0.3.23`; mount dir = commit `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` (equals the provider's reviewed pin); `model.safetensors` SHA-256 `891102d3…cbc8d86c` computed in the container over 842,609,210 bytes = the project pin. torch `2.14.1+cpu`, transformers 5.18.0, tokenizers 0.23.2, safetensors 0.8.0, huggingface_hub 1.33.0, numpy 2.5.3, pydantic 2.13.5 — all equal to `requirements-laya.txt`. Image `sha256:797acd3c…725e`, 364 MB, linux/amd64, Python 3.12.15. |
| 2 | Offline local load | **Pass** — the model loaded (24.6 s in a one-off container; 30.2 s start→ready as the production worker) under `--network none` with the checkpoint on a read-only mount. |
| 3 | Determinism | **Byte-identical at the provider's 4-decimal output precision**: 5 repeats on one worker; a fresh container; 30 individual calls vs the batch; 64 cards (two internal passes) vs individual; 6 shuffled orders (mapping and ranking). Also identical between the Windows host venv and the Linux container on the same 30 cards (0 rows differ). |
| 4 | Strict rejection | **All rejected.** Inputs (via the real worker protocol): 12 forbidden fields, name-like row id, incomplete cards, string/bool/list/dict/10^400/NaN/±inf/`1e999`, 7 out-of-range cases, 6 malformed protocol lines (an empty `cards` list returns an empty result, not an error) — worker alive after each and re-scoring byte-identical. Responses: **all 26 malformed replies raise `ScorerError`** — including the three Round 1 accepted (numeric string, boolean, extra prose field) — plus duplicate/reordered/unknown ids and NaN/Infinity with full-length replies. Duplicate `row_id`: `ScorerError` at both `score_complete_cards` and `LayaWorkerScorer.score`, before any scoring. |
| 5 | No generated prose | **Pass** — the only string in the raw provider output is the literal `type = "noul"`; records have exactly three fields; model input is the seven numeric floats (no row id); `state_tokens_dropped = 0`. |
| 6 | Cleanup | **Pass** — normal close: container gone in 0.3 s; `docker kill` mid-run: next call restarted a fresh container with identical output; request timeout: closed and removed in 0.3 s; **controller hard-exit (`os._exit`): container gone in 1.9 s**; `docker ps -a --filter label=hermes.laya.worker=1` empty before, between, and after. |
| 7 | Docker-level boundary | **Pass** — see below. |
| 8 | Selector-off unchanged | **Pass** — harness suite 447 passed (392 before; net +55 tests — only worker/CLI tests of the experimental mode were edited, no selector-off test was modified), `verify.py` 12/12; the off-mode `run_day` equivalence test, the lazy-import isolation test, and the baseline-mapping pins all pass unmodified; `run_backtest.py`'s diff touches only experimental-mode code and flags. |

**Docker-level isolation, probed inside the live production worker container**
(native Python sockets via `docker exec`, so the Python-level deny layer is
*not* in play):

| Probe | Result |
|---|---|
| TCP to `1.1.1.1:443`, `8.8.8.8:53`, `172.17.0.1:80` (Docker host gateway) | `Network is unreachable` (errno 101) |
| `connect_ex` to `1.1.1.1:443` | returns 101 |
| UDP `sendto` `8.8.8.8:53` | `Network is unreachable` |
| DNS `getaddrinfo` / `gethostbyname("example.com")` | `Temporary failure in name resolution` |
| Network interfaces (`/proc/net/dev`) | `lo` only |
| IPv4 routes / non-loopback IPv6 routes | none |
| Listening TCP sockets (`/proc/net/tcp`, `tcp6`) | none |
| `docker port`, `NetworkSettings.Ports`, `PortBindings`, `ExposedPorts` | empty / null |
| `docker inspect` | `NetworkMode none`; networks `[none]`; `ReadonlyRootfs true`; not privileged; `CapDrop [ALL]` (`CapEff 0`); `no-new-privileges`; user `10001:10001`; `PidsLimit 512`; one mount: the checkpoint, `RW false` |
| Writes: `/`, `/app`, checkpoint mount | refused; `/tmp` writable (tmpfs, `noexec`, 256 MB) |
| Environment (dummy `ALPACA_API_KEY`, `DATABASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_TOKEN`, `HERMES_HOME`, … set in the controller) | **none present**; container keys are only the offline flags, `HOME`, `PATH`, Python/pip settings, and the base image's public build metadata (`GPG_KEY`, `PYTHON_SHA256`, `PYTHON_VERSION`) |
| `/app` contents; trading/Hermes/secret files anywhere in the filesystem | only `laya_worker.py`, `laya_adapter.py`, `laya_selector.py`; none of `run_backtest`, `risk_checker`, `anonymize`, `watchlist.json`, `RULES.md`, `SOUL.md`, `.env`, `docker-compose.yml`, `schema.sql` exist; none of the trading/Hermes/DB modules import |

Honest notes: opening a *loopback* listening socket inside the container still
succeeds (a network namespace with only `lo` — nothing outside can reach it, and
no listener exists); the Python-level deny remains defence in depth only; the
boundary is the container runtime (here Docker Desktop's Linux VM on Windows),
not a proof against container escapes; the image tag `hermes-laya-worker:1` is
a local mutable tag — the validated image id is recorded above; the model still
shows the provider's invalid-temperature warning (§3), unchanged.

**Measurements (CPU, Docker Desktop):** cold container start→ready 30.2 s;
first 30-card batch 17.9 s; warm batches 12.5–17.5 s, median **13.6 s**
(≈0.45 s/card). Ties: 30 varied cards → 30 distinct values; 150 random cards →
133 distinct, 16 tied groups covering 33 rows, largest tie 3 (the Windows host
gave 134/15 earlier; synthetic statistics, resolved by the seeded tie-break).

**Environment note.** Docker Desktop was not running; starting it for this work
also restarted the existing compose services under their `restart:
unless-stopped` policy. `docker-compose.yml` and those services were not
changed.

## R2.4 Verdict — GREEN

Every stated component boundary passes: pins exact; offline local load;
deterministic repeated, fresh-worker, batch-vs-single, and shuffled results;
every malformed input and response shape rejected with a clear `ScorerError`;
no model-generated prose; cleanup on normal close, failure, timeout, and
controller exit; a Docker-level no-network, no-port, no-secret, no-trading-code
boundary; and unchanged selector-off behavior. Both Round 1 RED conditions
("malformed output accepted", "network exposure while scoring") are resolved
with direct evidence.

**This GREEN means only: the Laya component is component-ready to discuss the
screener.** It does **not** show that Laya is useful for ranking companies, that
its probabilities or confidences are calibrated, that it predicts anything
profitable, or that it is ready for Hermes. Round 1's §7 limits still apply
unchanged (synthetic data only; one OS/hardware/runtime; batch sizes 1–150).
