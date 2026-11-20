# Langfuse Ticket 1 — pinned-gateway feasibility, then implementation (PARTIAL: hosted trace not yet inspected)

Date: 2026-11-02. Ticket: `.scratch/langfuse-live-tracing/issues/01-connected-hermes-tracing.md`.
**Status: partial — blocked at the gateway feasibility gate. Not GREEN. No code, config, image, Compose or
profile change was made; no Langfuse account, credential, SDK install, model call or dry run happened.**
Evidence is read-only inspection of the running, digest-pinned gateway image (files read with `cat`/`grep`,
no Hermes model call) plus public package metadata.

## Verdict

The pinned Hermes **does** have the hook points needed (per-model-request and per-tool-call), and ships a
bundled, opt-in Langfuse plugin that uses them. But that stock plugin **cannot satisfy this ticket as shipped**,
and it cannot run at all in the current image. Making it compliant is a gateway runtime change, which the
ticket says to report and stop for. Smallest proposed change is at the end.

## What the pinned gateway has (evidence)

- Image: `nousresearch/hermes-agent@sha256:7052fc…` = **Hermes Agent v0.20.0 (2026.8.3)**, Python 3.13.5,
  venv `/opt/hermes/.venv` (`docker-compose.yml:36`).
- Hook names registered by the core (`hermes_cli/plugins.py`): `pre_api_request`, `post_api_request`,
  `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`. `agent/conversation_loop.py` fires
  `pre_api_request` per model request (`api_request_id = "<turn_id>:api:<n>"`, computed once before the
  retry loop, line ~2212).
- Bundled plugin `plugins/observability/langfuse/` (1137 lines): opt-in (`plugins.enabled` in the profile
  config, currently `[]`), requires `HERMES_LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY` (`pk-lf-` / `sk-lf-`
  prefixes are validated), fails open when the SDK or keys are missing. One trace per Hermes turn, a
  `generation` child per model request, a `tool` child per tool call matched by `tool_call_id` (so parallel
  same-name calls are not cross-matched), usage from the provider response, `HERMES_LANGFUSE_MAX_CHARS`
  truncation (default 12000, labelled `... [truncated N chars]`), base64 data-URI redaction, a 256-entry cap
  on live trace state.

## Gaps against the ticket (each is a concrete reason the stock plugin is insufficient)

1. **SDK absent.** `import langfuse` fails in the gateway venv (`ModuleNotFoundError`); the plugin is inert.
   There is no `pip` in the venv (only `uv`, `/usr/local/bin/uv`). Latest PyPI `langfuse` is **5.1.0**
   (`requires_python >=3.11`, pure-Python wheel; OpenTelemetry deps). The plugin was written against the 3.x
   API (`propagate_attributes`, `start_as_current_observation`, `create_trace_id`); 5.x compatibility is
   **unverified**, so the SDK version would have to be chosen and tested, not assumed.
2. **No cross-process parentage.** The trace id is derived from Hermes's own `session_id::task_id`
   (`create_trace_id(seed=…)`); nothing reads a trace id or parent span from the environment. A controller
   root span and the Hermes trace would be two unrelated traces. The only link today would be text inside the
   prompt (the root input is the last user message). Requirement 2 (connected timeline) needs a small
   addition that accepts a controller-supplied trace/parent id.
3. **No credential redaction.** Only truncation and data-URI redaction exist. Prompts and tool results are
   exported verbatim up to the cap. Requirement 4 (redact before export) needs a masking step.
4. **Cost is estimated, not returned.** `_usage_and_cost` calls `estimate_usage_cost` and writes
   `cost_details` from a pricing table. Token counts come from the provider response, but the cost fields are
   derived — contrary to "report returned usage only; never invent". Needs to be suppressed or labelled.
5. **Synchronous flush at turn end.** `_finish_trace` calls `client.flush()` in the Hermes process. The
   controller waits for that process (`call_hermes`, 900 s timeout), so an unreachable Langfuse could delay
   the controller's critical path. Requirement 3 (no network waits on the critical path, bounded cleanup)
   needs a bounded or background flush.
6. **Retries are not distinguished.** A repeated request with the same `api_call_count` ends the earlier
   generation and starts another with the same name; a retry looks like a second call rather than a labelled
   retry.
7. **Unverified at runtime:** that the plugin loads in `hermes -z` one-shot mode (the controller's call path;
   `oneshot.py` only calls `discover_plugins()` on one branch I read), and how tool calls are named: the live
   profile reaches MCP tools through `tool_search` / `tool_describe` / `tool_call` meta-tools (observed in the
   2026-10-08 dry run), so a tool observation may be named `tool_call` with the real tool name only in its
   arguments.

None of 1–7 can be settled without installing the SDK and running Hermes, which needs approval.

## Smallest proposed change (needs your approval; nothing done)

1. **Derived gateway image** (replaces `image:` with `build:`): `FROM nousresearch/hermes-agent@sha256:7052fc…`
   (same pinned bytes below one added layer) + `uv pip install langfuse==<tested 3.x or 5.x pin>`. Native
   builds on ARM later work (the SDK is pure Python; OpenTelemetry wheels exist).
   *Alternative with no image change:* install the SDK into a read-only bind-mounted directory and set
   `PYTHONPATH` only for the live invocation; smaller to review but per-architecture wheels are awkward.
2. **One project plugin** (~150–250 lines, `live/tracing/`) that wraps or replaces the stock hook handlers:
   (a) reads `HERMES_TRACE_ID` / parent span id from the environment so Hermes observations nest under the
   controller's cycle span; (b) masks secrets/hosts/account ids before export; (c) drops estimated cost;
   (d) labels retries; (e) background export with a bounded queue and a short, bounded shutdown instead of a
   per-turn synchronous flush; (f) swallows every exception.
3. **Profile-only enablement:** `plugins.enabled: [observability/langfuse-live]` and the two keys in the
   `live` profile's `.env` only (the profile has no terminal/file tools, and MCP tools run in other
   containers, so keys are not model-visible or in research-tool environments). The backtest and default
   profiles stay untouched.
4. **Controller side (`live/`):** a small tracing module (no-op when unconfigured) that creates the cycle
   root, passes the trace/parent ids to `call_hermes` through `docker compose exec -e`, and exports its own
   spans on a background thread; everything wrapped so an exception or outage cannot reach trading code.
5. **Tests** per the ticket (off/on/failing parity over `run_cycle`, redaction, queue/payload bounds,
   cross-process correlation with a fake hook caller), then one real orders-disabled `dry-run --hermes`
   once you provide hosted credentials through the profile `.env` (never in chat or Git).

If you would rather avoid a patched plugin, the alternative is to accept two linked-but-separate traces
(controller trace + stock Hermes trace joined by a cycle id in metadata/prompt text) and live with gaps 3–6.
That would not meet the ticket's redaction, estimated-cost and critical-path requirements, so I do not
recommend it.

## Baseline (before any edit)

- `pytest live/`: **73 passed** (4.6 s).
- `python verify.py`: **13/13 suites passed** (backtest 631 passed, live 73 passed).
- Docker engine 29.6.2 reachable (checked with a bounded `docker info`; not started by me).
- Lint/type checks: the repository has no configured linter or type checker for `live/` (none run).

## Limits of this inspection

Read-only. Not shown: that hooks fire in `hermes -z`, which SDK version works, real export, real
parentage, redaction or overhead numbers, RAM. Package metadata is not proof of execution.

---

# Implementation and evidence (2026-11-02, after approval)

**Status: PARTIAL, not GREEN.** Everything offline passes and the plugin has been shown to load and fire inside
the real pinned gateway's one-shot call, but **no trace has been sent to or inspected in hosted Langfuse**: that
needs your credentials in `<state-dir>/tracing.env` (see `docs/live_tracing_runbook.md`). The orders-disabled
`dry-run --hermes` has therefore not been run with tracing.

## What was built (and one deliberate deviation)

You approved a derived gateway image with a pinned Langfuse SDK. I did **not** use it. The Langfuse SDK's own
queue/atexit shutdown is the hardest part to bound, the plugin API of SDK 3.x/4.x/5.x was unverified against the
stock plugin, and an image change was avoidable. Instead: a standard-library OTLP/HTTP-JSON exporter (Langfuse
documents JSON ingestion at `/api/public/otel/v1/traces`) shared by the controller and a profile-only plugin
installed in `/opt/data/profiles/live/plugins/hermes-trace/`. **The pinned gateway image, digest, Compose file and
the default/backtest profiles are unchanged.** If hosted ingestion rejects this encoding, the approved
derived-image + SDK route remains the fallback.

New: `live/tracing/otlp.py` (redaction, OTLP encoding, bounded exporter), `live/tracing/controller.py` (tracer,
credential file loader), `live/tracing/hermes_plugin/{plugin.yaml,__init__.py}`, `live/test_tracing.py`,
`docs/live_tracing_runbook.md`. Edited: `live/run_live.py` (`run_cycle` gains an optional `tracer`; spans around
existing calls; `setup-profile` installs/enables/removes the plugin and manages `HERMES_TRACE_*` in the live
profile `.env`), `live/agent.py` (`call_hermes(prompt, trace_env=None)`: only three allowlisted correlation ids reach
the command line). `engine.py`, `adapters.py`, risk, strategy and every historical path are untouched.

## Results

| Check | Result |
|---|---|
| Baseline before edits | `pytest live/` 73 passed; `verify.py` 13/13 (backtest 631) |
| `live/test_tracing.py` (offline transport evidence) | 34 passed (~34 s) |
| Parity: off / null (missing credentials) / healthy / throwing exporter / hung exporter / full queue / unreachable, over 8 scenarios (reconcile-only, HOLD, REVISE, EXIT, entry rejected, invalid reply, selector failure, model failure) | broker submits, cancels, journal calls, durable state and event names identical after normalising random plan ids and wall-clock stamps; each cycle < 4 s; shutdown < 0.25 s (see §Review corrections: shutdown no longer waits) |
| Trading exception under tracing | identical exception and journal; ERROR span recorded |
| Trace ids in durable state / idempotency keys | none; client order ids identical |
| Redaction | dummy secrets in prompts, tool args/results, errors and exceptions never appear in any exported byte; 5 MB value clipped in microseconds |
| ~~Process exit with hung / dead service (host py3.12 subprocess)~~ | superseded: exit no longer waits at all (§Review corrections) |
| ~~Process exit in the real gateway runtime, 1.5 s bounded flush~~ | superseded: that wait was itself the defect (finding 2); see §Review corrections for the no-wait measurements |
| Plugin in the real gateway, one-shot `hermes -z` (1 tiny model session, dummy keys to a dead local address, local debug file) | `hermes plugins list`: `hermes-trace` enabled, stock `langfuse` not enabled. The run fired 3 model requests (provider-returned usage incl. cache and reasoning tokens, no cost), 1 `tool_describe`, 2 parallel quote calls as siblings (4.1 s and 0.6 s) with correct per-call inputs/results; every span carried the injected trace id and the root's parent was the injected controller span id |
| Regression after the changes | `verify.py` 13/13 (backtest 631 passed, live 106 passed at that run; final live total 107 with one more test); `backtest_state.json` prefix `4b428d35` unchanged; no historical file modified |
| Instrumentation cost | ~130 µs per controller span; plugin ~0.2 ms per span; worst-case queue memory ~22 MB with a stuck endpoint (now ~52 MB at the 64 KB attribute cap, see below) |

Defects found by running it and fixed: (1) the email regex backtracked quadratically on a 2 MB value and would have
blocked a Hermes hook — now clip-before-redact, prefilters, bounded quantifiers; (2) Hermes reports the *resolved*
MCP name to the hooks, with `tool_describe` separate — handled both forms, lookups no longer counted as
invocations; (3) `null` metadata was exported as the string `"null"`; (4) leftover tracing settings were not removed
on disable. The generation's `tool_calls` field came back empty in the real run (opaque payload); the plugin now
reads the raw message object first. **That specific fix is covered by a unit test but not re-run in the real
gateway.**

## Not verified

Hosted Langfuse ingestion and rendering; a full `dry-run --hermes` with tracing (zero broker mutations still to be
confirmed under tracing; the earlier dry run 2026-10-08 did confirm zero without it); retry labels and tool errors
in the real runtime (unit-tested only); `loop` mode; ARM/cloud; measured peak RAM including Laya (Ticket 2 scope).
No lint or type checker is configured for `live/`, so none was run.

## Self-audit (this ticket's seams)

Privacy: secrets never reach `os.environ` of the controller or the Hermes command line; prompts and tool results
are exported (including paper-account balances) — by design, flagged in the runbook. Blocking: no network call on
the trading path. (The first pass left a bounded 1.5 s + 2 s wait at Hermes/controller exit; removed in §Review corrections.)
Isolation: plugin and keys exist only in the live profile; the stock plugin is off, so no duplicate observations.
Accidental trading change: none observed by the parity matrix; `run_cycle`'s body is unchanged apart from `with`
wrappers around single calls.

## Review corrections (2026-11-02, second pass) — four findings

Status: **all four fixed and verified offline and in the real gateway; hosted acceptance still pending** (no
`tracing.env`, so no hosted trace and no traced orders-disabled `dry-run --hermes`). Not GREEN.

Method: regression tests were written first (`live/test_tracing_hardening.py`) and run against the unfixed code:
**45 failed / 7 passed** (the 7 were the "ordinary content stays readable" and "still delivers" guards). After the
fixes the same file passes (64 tests).

| # | Finding | Status | What changed | Evidence |
|---|---|---|---|---|
| 1 | Secrets inside JSON-formatted strings survived (`{"password":"x"}`) | **Fixed** | Key-value redaction is now a scanner that handles quoted/escaped/double-escaped/nested JSON, repr, YAML, env, query strings, `Basic`/`Bearer` schemes, bracket values and `password is …` prose; sensitive keys are camelCase/kebab/UPPER_SNAKE aware (`apiKey`, `x-api-key`, `AWS_SECRET_ACCESS_KEY`, `clientSecret`); strings that are JSON are redacted by structure (keys at any depth, JSON-in-JSON); every exported string is redacted again at the exporter; names, status messages, metadata and exceptions go through the same path; every backtracking pattern has bounded quantifiers | 36 payload shapes × 5 entry points; every export path inspected on the complete wire payload (plugin and controller); 10 hostile inputs (3 MB unterminated value, 500k open brackets, 2 MB backslashes, …) each < 1.5 s, within one attribute cap, no fragment; worst 32 KB adversarial final pass 8–12 ms |
| 2 | Hermes completion waited up to 1.5 s (controller up to 2 s) for the export | **Fixed** | No production path drains or waits: `_finish` queues the last spans and returns; `close()` only stops intake; the exit-timeout setting is gone; a test forbids any `.flush(` / timed `close` in production code | host: finish < 0.25 s and process exit ≈ tracing-off with a stalled **or** dead service; controller `close()` 0.01 ms; real gateway (below) |
| 3 | Queue overflow did file I/O under the exporter lock | **Fixed** | `submit` only bumps an in-memory counter and appends to a 20-entry deque; one separate writer thread persists warnings (≤ 1 write/s, 16 KB file cap, 20-line backlog) | 300 overflowing submits < 0.3 s while every file call takes 0.4 s and none happens on the caller's thread; with persistence sleeping 2 s or raising, the exporter still flushes and accepts work; 20,000 overflows add ≤ 3 threads and ≤ 16 KB of log |
| 4 | Model-request context was reduced to the last 3 messages, losing arguments, ids and policy | **Fixed** (one upstream limit, below) | Each request exports its messages whole (role/type, ids, tool calls, call ids). Messages already exported by an earlier request of the trace are **cited, not repeated** (`inherited`: index range → span id); gaps are listed (`omitted` with roles and reason; `clipped`; `history_changed_at_index`); `complete` says whether the request is fully inspectable; instructions and tool names are recorded once, then `same_as` | codex-style and chat-style conversations rebuilt **from exported spans only** equal the original messages, in order, with call ↔ result ids intact; a 40-message oversized request is fully accounted for (exported + inherited + omitted = count); newest and opening messages always kept |

### Real gateway (pinned Hermes v0.20.0, py3.13.5; throwaway collector inside the container, dummy keys; 3 tiny model sessions + 1 re-check)

| Collector | Finish hook | Finish → process exit | Delivered |
|---|---|---|---|
| healthy | 0.9 ms (re-check 1.0 ms) | 0.28 s (re-check 0.32 s) | 7/7 spans including the `hermes_run` root; last batch arrived 0.28–0.31 s **before** exit |
| stalled (accepts, never answers) | 0.6 ms | 0.37 s | 4 batches reached it (retries included); root and last `llm_call` never delivered; circuit opened |
| dead (refused) | 0.6 ms | 0.26 s | none; nothing waited |

So with a stalled or dead service the completed invocation is held open **0 s** by telemetry (the ~0.3 s is Hermes's
own shutdown, identical in all three). In the healthy run Hermes's real hook payloads produced: `llm_call_2` citing
message 0 from `llm_call_1` and exporting messages 1–4; `llm_call_3` citing 0 and 1–4 and exporting 5–8; two
parallel tool calls as siblings under `llm_call_2`; the root parented to the injected controller span; no dummy key
on the wire. (This session made one lookup and two quote calls; retries/errors in the real runtime remain
unit-tested only.)

### Trade-off you need to decide on (reported, not built)

Removing every wait means **the tail of a one-shot process is delivered only if the background thread reaches it
before the process ends.** Measured on the host with a local service (6 child spans + root, process exits right
after the controller's `close()`):

| service latency | spans delivered | root `live_cycle` delivered |
|---|---|---|
| 0 ms | 5/7 | no |
| 150 ms | 4/7 | no |
| 400 ms | 1/7 | no |

`loop` mode is unaffected (the process lives on). One-shot `dry-run` / `cycle` traces will often lack the controller's
root and last spans. The gateway side fared better only because Hermes lingers ~0.3 s after finishing; a cloud
service with TLS setup per batch may still lose the final one or two. Options, none built: (a) accept it and verify
with `loop`-style long-lived runs; (b) a keep-alive connection in the exporter (small, cuts per-batch latency,
unmeasured against the real service); (c) a detached sender process or on-disk spool for the tail (reliable, but a new
moving part on the trading host — needs your approval); (d) an explicit operator-only drain for verification runs
(a wait you would be choosing, not a default).

### Limits found

- **Upstream clip (finding 4):** Hermes clips every string in its sanitised `request` hook payload to 8,000 characters
  (1,000 for very large requests, or a preview only) before any plugin sees it, so the instructions are visible only
  up to that cut. The trace says so (`system.clipped`, a note, `complete: false`); the raw messages (including the
  research prompt) are not subject to it. Seeing the whole instructions needs a Hermes patch — out of scope.
- A secret with no key name, no recognisable prefix and no context in free prose cannot be recognised by any redactor.
  A key literally named `tokens` is not treated as secret (it would hide usage counters).
- Worst-case memory with 512 maximum-size queued spans is ~52 MB (measured) at the 64 KB attribute cap; typical spans
  are 1–10 KB. Request encoding costs ~1.3 ms per large message, ~14 ms for ten.
- Tool schemas are not exported (names only, flagged `not_exported`).

### Regression

`pytest live/` 170 passed (was 107: −1 superseded exit test, +64 hardening); `verify.py` 13/13 (backtest 631);
`backtest_state.json` prefix `4b428d35`; RULES/AGENTS/SOUL, `risk_checker.py`, `watchlist.json`, `docker-compose.yml`
and the pinned image untouched; live profile restored to "tracing plugin: off" (no `HERMES_TRACE_*`, no root-owned
files). No lint or type checker is configured for `live/`, so none was run.

### Audit of the changed seams

Privacy: redaction runs at sanitising and again at the exporter; warnings contain no payload; the debug file (opt-in)
holds already-redacted attributes; only correlation ids cross the command line. Blocking: no network, file or wait on
any producer, hook, completion or exit path (tested, and a source-level test forbids reintroducing a drain). Accidental
trading change: parity matrix (8 scenarios × 7 tracer variants incl. stalled and full queue) identical. Duplicate
instrumentation: stock `langfuse` plugin not enabled; one project plugin. In-scope defects found by the audit and
fixed: the double 8,000-char clip of the instructions (found in the real gateway), a prose rule that swallowed its own
keyword, and the per-key redaction cost (80 ms → 14 ms for ten large messages).
