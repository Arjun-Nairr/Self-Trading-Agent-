# Live tracing runbook (Langfuse, Tickets 1–3)

Tracing is **diagnostic only**: it never feeds prompts, selection, risk or execution, and Neon, `events.jsonl`,
recovery state and idempotency keys are unchanged. Off by default. Status and evidence:
`docs/research/langfuse_tracing_acceptance.md` (current status, measurements, open decisions) and
`docs/research/langfuse_ticket1_gateway_feasibility.md` (Hermes gateway detail).

## What you get

One trace per `cycle` / `dry-run` / loop tick, in Langfuse:

```
live_cycle                         (controller; metadata: mode, release = code version, policy_sha256, orders on/off)
├─ reconcile                       └─ broker.positions / broker.order_by_client_id   (observations)
├─ selection
│  ├─ selection.universe           (screen identity: universe_version = live-screen-<screen id>, hash, size)
│  ├─ selection.laya               (snapshot sha, inputs digest, `inference: ran | snapshot reuse`, full ranking)
│  │  ├─ laya.fetch_history        (only when the day's snapshot is built)
│  │  └─ laya.inference            (generation: question, checkpoint, numeric cards by ticker, scores)
│  └─ selection.exclusions         (held/pending removed in rank order; delivered shortlist)
├─ engine.shortlist / engine.*     (engine state transitions as events: order_filled, exit_settled, ...)
├─ prompt_assembly                 └─ broker.orders
├─ hermes_research                 (controller; its ids are handed to the container)
│  └─ hermes_run                   (Hermes process: plugin `hermes-trace`)
│     ├─ llm_call_1 … llm_call_n   (each model request: redacted input, output, provider-returned usage, model)
│     │   └─ <MCP tool name>       (each tool call, nested under the request that asked for it; parallel calls are siblings)
│     └─ tool_describe:<tool>      (schema lookups; counted separately from invocations)
├─ reply_validation
├─ reconcile_before_actions / holding_action (decision as input) / entry_validation (plan in, plan_id out)
│  └─ broker.submit|cancel (attempts) · broker.latest_quote|news|daily_bars · journal.decision|result|exit
└─ reconcile_after

weekly_screen                      (its own trace: `python live/trace_screen.py --write`)
├─ screen.universe / screen.facts / screen.history / screen.news   (inputs, rows, errors, request/retry stats)
└─ root output: screen id, attempt id, content/pool hash, effective date, counts, failure reasons, persisted/reused
```

A broker *attempt* (`submit`/`cancel`/`replace`, `operation: mutation`) is not a fill; seeing a filled order on every
reconciliation (`operation: observation`) is not a new fill. Fills come only from the engine's own events, emitted once
per transition. A cycle never runs a screen: it names the screen it consumed. Laya's worker keeps `--network none` and
no credentials; the controller exports its observations.

Retries appear as `llm_call_n (attempt 2)` with the earlier attempt marked WARNING/`retried`. Work with no recorded
result at exit is marked WARNING/`unfinished`. Token usage is exactly what the provider returned (`usage:
unavailable` otherwise); **no cost is estimated or exported**. Reconcile-only ticks produce a small trace (root +
reconcile) whose root says what was skipped.

### Reading a model request (`llm_call_n` → Input)

Each request lists what the model was sent, in order, with every gap named. Requests in a run repeat their whole
history, so a message is exported **once**, by the first request that carried it; later requests cite it.

| field | meaning |
|---|---|
| `message_count` | messages in this request |
| `messages` | only the messages exported *by this span* (each with its `index`, role/type, ids, tool calls and call ids, so call ↔ result pairs stay linked) |
| `inherited` | `[{indexes:[a,b], recorded_in:"llm_call_1", span_id}]` — unchanged earlier messages, recorded in that span |
| `omitted` | `[{indexes, roles, reason}]` — messages **not** exported anywhere (per-request budget: 18 KB / 60 messages) |
| `clipped` | indexes whose text was cut (the text itself ends `...[truncated N chars]`, 4,000 chars kept per field) |
| `history_changed_at_index` | present when Hermes rewrote earlier history (e.g. compaction) between two requests |
| `equality_unverified` | indexes too large to compare exactly (> 1 M chars, or 8 M per request): never cited, `complete: false`; `history_unverified_from_index` then replaces `history_changed_at_index` |
| `system` | the request's instructions/policy (20,000 chars kept), then `{same_as:"llm_call_1"}` only while the observed text is identical; a shorter copy Hermes clipped is `{prefix_of, observed_chars, equality: unverified}` |
| `tools` | tool count and names only; schemas are never exported |
| `request_fields_not_exported` | other request parameters that exist but are not exported |
| `complete` | `true` only if nothing is omitted or clipped (messages and instructions) |

The first request carries the opening task and policy and is the one to read; the newest messages are always kept
when a budget forces a choice. `complete: false` is the explicit "do not assume this is everything" flag.

## Enable

1. Create a free Langfuse project (hosted), then create the file **outside the repo** (Windows:
   `%USERPROFILE%\.hermes-trading-agent-live\tracing.env`) with exactly:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_BASE_URL=https://cloud.langfuse.com
   ```
   Use your project's region URL if it is not the EU cloud. Never paste keys into chat, a command line or Git
   (`*.env` is git-ignored). Check the current free allowance at signup (the research note records 50,000 units/month
   and 30-day access; unverified until you see your account).
2. Docker running, then: `python live/run_live.py setup-profile`. It installs the `hermes-trace` plugin into the
   **live profile only**, enables it in that profile's `plugins.enabled`, and copies the three keys into that
   profile's `.env` over stdin (nothing printed). The stock `observability/langfuse` plugin stays disabled; the two
   must never be enabled together.
3. The controller reads the same file for its own spans. Run `python live/run_live.py dry-run --hermes` (read-only,
   no orders) and open the trace in Langfuse.

### Weekly screen

Run the refresh through the traced wrapper (same arguments as `watchlist/scalable_screener.py`; it keeps every rule of
that CLI): `python live/trace_screen.py --write` on Saturday/Sunday, `--dry-run` any day. Running the screener directly
still works and is simply untraced.

### Diagnostic drain (verification only)

`python live/run_live.py dry-run --hermes --universe watchlist --trace-drain 5` waits up to 5 s (max 10) at the end for
the last spans of this process and of that one Hermes call. **Diagnostic, orders-disabled only:** refused for `cycle`,
`loop` and `setup-profile`. It does not make normal one-shot delivery reliable (see Limits).

## Disable

Delete `tracing.env` and run `python live/run_live.py setup-profile` again: the plugin directory and every
`HERMES_TRACE_*` line are removed and the plugin is no longer enabled. No trading code path changes.

## Safety properties (and the tests that cover them: `live/test_tracing.py`)

- **Nothing waits for export, anywhere.** Not at a Hermes decision return, not at turn completion, not at process
  exit, not at controller shutdown. Export is a daemon thread with a bounded queue (512 spans), 64-span batches,
  2 s socket timeouts, one retry for 5xx/timeouts, none for 4xx; two consecutive failures open a 60 s circuit during
  which nothing is sent (no network wait).
- **Exit spool (approved 2026-11-06).** At exit (`close()`), everything not yet acknowledged is written to one new
  local file: queued spans, the batch in flight, failed batches and batches held back by the open circuit. They are
  already redacted and OTLP-encoded, with no credentials.
  - Controller: `<state-dir>/trace_spool/`. Hermes: `/opt/data/profiles/live/trace_spool/` in the container,
    i.e. `%USERPROFILE%\.hermes-trading-agent\profiles\live\trace_spool` on the host. Both are outside Git.
  - Owner-only permissions where the OS supports them (the Hermes profile directory is `drwx------ hermes`).
  - Bounds: ≤ 2 MB per exit (the rest is dropped and counted), ≤ 16 MB per directory (an exit that would exceed it
    writes nothing). Drops are counted in `stats` and written as one line to `tracing_warnings.log`.
  - Measured: 6 ms for a 38-span cycle, 25 ms for a full 512-span queue.
  - **The next invocation** (its first span starts the background sender) sends spool files oldest first. It claims a
    file by an atomic rename, so two processes never replay the same file, and deletes it only after every line was
    accepted. On a failure the unsent rest goes back to the spool. Lines that are not valid OTLP bodies are dropped
    and counted. A claim untouched for 15 min (a dead process) is recovered.
  - Original trace and span ids are kept. A batch that was in flight at exit can be sent twice; Langfuse upserts by
    id, so it is displayed once (hosted probe, acceptance report §4).
- **What it does not do:** delivery needs another invocation of the same component. The controller's spool is sent by
  the next `cycle`/`dry-run`/`loop` start, and Hermes' spool by the next Hermes call. The last invocation's records
  can therefore stay local indefinitely. A process killed outright (no `close()`), a full or unwritable disk, or a
  disk failure loses what was unsent. No crash-proof or exactly-once delivery is claimed. In `loop`, batches that fail
  during an outage are kept in memory (≤ 2 MB) and spooled only at shutdown, not retried in-process.
- Producers do no I/O: an overflow only increments an in-memory counter. Warnings are written by one separate
  writer thread (≤ 1 write/second, `<state-dir>/tracing_warnings.log`, 16 KB cap, 20-line backlog), so a slow or
  failing disk cannot delay a caller or the exporter.
- Redaction runs before encoding **and again on every exported string at the exporter** (last line of defence).
  It covers secret-named keys in any spelling (`apiKey`, `x-api-key`, `AWS_SECRET_ACCESS_KEY`, `clientSecret`…),
  including inside JSON-formatted text, escaped or nested JSON, repr/YAML/env/query-string forms and
  `password is …` prose; bearer/basic tokens, Langfuse/OpenAI/AWS/broker keys, JWTs, credentialed URLs and
  connection strings, account numbers, emails and user directories — in messages, tool args/results, metadata,
  names, instructions and exception text. Usage counters (`input_tokens`, `max_tokens`, `token_count`) stay readable.
  Fields are clipped to 8,000 characters *before* any pattern runs, with an in-band `...[truncated N chars]`
  marker; an attribute is capped at 64 KB. **Limit:** a secret with no key name, no recognisable prefix and no
  context (a bare random string in free prose) cannot be recognised by any redactor. Research content (prompts,
  tool results) is exported; paper-account balances in the prompt are therefore visible in Langfuse.
- Credentials: host file outside the repo; profile `.env` of the live profile only (no terminal/file tools, MCP tools
  run in other containers); the Hermes command line carries only the three correlation ids.
- Measured (not guaranteed elsewhere): ~130 µs per controller span; worst-case queue memory ~52 MB with a stuck
  endpoint and 512 maximum-size spans (typical spans are 1–10 KB); plugin hooks ~0.2 ms per span in the gateway's Python 3.13.

## Limits

- Hosted ingestion of this hand-written OTLP/JSON path is verified only once a real trace has been inspected
  (see the evidence report); the offline tests use a local capture server.
- Only the live profile is traced. The default profile, the backtest profile and every historical path are not.
- Spans are exported when they end. A one-shot `cycle`/`dry-run` used to lose its last spans and its root at exit.
  They now go to the exit spool and arrive with the **next** invocation (see Safety properties). `--trace-drain`
  remains a diagnostic for immediate delivery in orders-disabled dry runs.
- Reading traces back by API: use `GET /api/public/v2/observations` (`fields=core,basic,io,metadata,usage,model`).
  The v1 `traces` read API answers 410 on Langfuse Cloud and its notice gives a 2026-11-16 removal date. Export is
  OTLP and is unaffected. Langfuse prices model calls itself from model + usage; tracing exports no cost.
- A review cycle with Hermes research measured 40 units (run 2, acceptance report §5).
- The 50,000-unit free allowance counts traces plus observations; reconcile-only ticks every 5 minutes are the
  largest contributor. Watch usage after the first day.
