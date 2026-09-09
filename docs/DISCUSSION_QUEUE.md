# Discussion queue — unresolved architecture/product decisions

Tracks open decisions this project hasn't settled, not a chronological
journal, not an implementation spec, and not a duplicate of `HANDOFF.md`
(current verified state) or `docs/PROJECT_BACKLOG.md` (approved/candidate
work items). An item lives here only while it's genuinely unresolved.

**When a discussion becomes an approved implementation:** move its
distilled requirement to `docs/PROJECT_BACKLOG.md` or `HANDOFF.md` (as
appropriate) and remove it from this file. Never duplicate completed
history in both places.

Each item: current decision · why it matters · current conclusion or
options · prerequisite / when to revisit · status (`discussing` /
`deferred` / `locked`).

---

## 1. Dashboard V2 release path

**Current decision:** how Dashboard V2 (`dashboard-v2/`) moves from
candidate to active, and what happens to V1 (`dashboard/`) along the way.

**Why it matters:** V2 is a full IA + visual rebuild with its own read
path against Neon; cutting it over carelessly could leave the project
without a working dashboard, or silently diverge from V1's verified
metrics.

**Current conclusion / options:** a fixed sequence, in order —
responsive safety check (done, `86b3569`) → Cloudflare deployment →
manual phone testing → an explicit V2 cutover decision. V1 stays
untouched and active at every step until cutover is explicitly approved.

**Prerequisite / when to revisit:** once Cloudflare deployment and real
manual phone testing are both complete, revisit to make the cutover
call.

**Status:** `discussing`.

---

## 2. Training / experiment discipline

**Current decision:** whether to start any new backtest run right now,
and under what classification.

**Why it matters:** the active 50-cycle Round 2 baseline
(`e68525f9-ce89-4588-a57e-866f28cd99fd`, `training`) is the only
evaluation-ready dataset this project has; running something casually
on top of it (e.g. "just to demonstrate the dashboard") would blur that
baseline's meaning or waste evaluation-track attention before the
measurement decision in `HANDOFF.md` §10 is made.

**Current conclusion / options:** do not start a casual small run merely
to demonstrate the dashboard. The active baseline stays frozen until
the measurement-improvement work and an explicit next-experiment
decision (`HANDOFF.md` §10) happen. Any future run — including a
demo/smoke run — must go through the durable
`training`/`smoke` classification in `backtest_run_registry`
(`--run-kind`, `trade-journal-mcp/schema.sql`); `archived` stays
migration-only.

**Prerequisite / when to revisit:** the evaluation-track decision in
`HANDOFF.md` §10 (existing evidence sufficient / run another tranche /
improve measurement first) is made.

**Status:** `locked` (the classification requirement itself is
implemented and non-negotiable; *when* to run anything new stays
`discussing` until §10 is decided).

---

## 3. Reflection memory and actual learning

**Current decision:** whether the trading agent's reflection/insight
mechanism, as currently wired, is evidence of real judgment improvement
over time.

**Why it matters:** `SOUL.md`/`AGENTS.md`'s temperament explicitly warns
against "learning superstition from randomness" — if reflection is
treated as proof of learning without checking whether it actually
changed anything, that's exactly the failure mode being guarded against.

**Current conclusion / options:** the backtest harness injects the
latest two run-scoped Neon reflections into every cycle's prompt, and
every fifth cycle requires writing a fresh reflection informed by recent
closed-trade outcomes. This proves the trading agent *receives* prior
notes each cycle — it does **not** prove judgment *improved* because of
them. That's a separate, unaudited question.

**Prerequisite / when to revisit:** audit the cycles 26–50 evidence
(`backtest_cycle_evidence` — exact masked prompts/inputs/responses
already captured for that range) for whether reflections measurably
changed later decisions, before changing reflection retention, cadence,
or memory design in any direction. Do not introduce `MEMORY.md`/
`USER.md` or any other uncontrolled, unscoped backtest memory source in
the meantime.

**Status:** `discussing`.

---

## 4. Evidence-source convergence

**Current decision:** how additional evidence sources (Reddit/social
sentiment, fundamentals) would combine with existing technical/news
evidence if ever added.

**Why it matters:** RULES.md §5a's sentiment-is-a-veto-never-a-trigger
design is the project's deliberate prompt-injection defense as well as
an evidence-quality stance; a naive multi-source scoring system could
quietly reopen the exact hole that design closes.

**Current conclusion / options:** do not build a simplistic global
percentage-weight system across sources. The current design stays the
reference: technicals are entry evidence, news is a negative veto/exit
signal only, never a standalone buy trigger. Any future source
(Reddit/social, fundamentals) should be given a defined role and require
independent corroboration — agreement between trustworthy sources can
strengthen a warning/veto, but positive sentiment alone can never create
a buy, from any source, alone or combined. Not implemented; no source
beyond current technicals/news exists in the pipeline today.

**Prerequisite / when to revisit:** a concrete proposal to add a new
evidence source (Reddit/social or fundamentals) triggers this — no open
prerequisite otherwise.

**Status:** `deferred`.

---

## 5. Rules/context packaging

**Current decision:** whether and how `RULES.md` could be restructured
as it grows, without letting any hard limit become skippable.

**Why it matters:** `RULES.md`'s own closing section is explicit that
`risk_checker.py` mirrors every numeric limit specifically so a limit is
enforced in code, not just prose — any packaging change that let the
trading agent see only a subset of rules per cycle would undermine that
guarantee structurally, not just as an oversight.

**Current conclusion / options:** do not build a model-chosen "easy
trade" rules router — every hard rule must stay available every single
cycle, no exceptions. A possible future direction, not adopted: split
the (currently long) rules document into concise, always-on binding
rules plus optional rationale/reference material, with the *harness*
selecting deterministically what reference material to include (never
the trading agent choosing what rules it sees). Do not change current
`RULES.md` while the baseline (item 2) is frozen.

**Prerequisite / when to revisit:** the baseline unfreezes (item 2's
prerequisite) and `RULES.md`'s real length/complexity is judged to
actually be causing a problem worth solving.

**Status:** `deferred`.
