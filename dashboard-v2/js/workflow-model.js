// Workflow page model: pure functions only (no fetch, no DOM), shared by the
// page and test/workflow.test.mjs.
//
// Two sources, never mixed up:
//   * "event"   -- a row of public_workflow_events, written by the backtest
//                  controller when the stage actually happened (real wall
//                  clock, allowlisted counts only);
//   * "journal" -- an existing public record (public_trades,
//                  public_agent_activity, portfolio_snapshots) that proves a
//                  stage produced an outcome, but carries no stage timing.
// Anything with neither is "not_recorded" -- never inferred, never timed.

export const STALE_MS = 15 * 60_000;

// Major stages in the controller's real order within one simulated day:
// the ORDER path runs at the simulated open, BEFORE the CANDIDATE path
// (after the close). `owner` is who performs the stage.
export const ORDER_PATH = [
  { key: "queued_in", label: "Queued order", owner: "Journal · prior day", journal: false },
  { key: "prefill_news", label: "Pre-fill news", owner: "Controller" },
  { key: "execution", label: "Execution risk", owner: "Risk checker" },
  { key: "fill_outcome", label: "Filled / cancelled", owner: "Simulator", journal: true },
  { key: "positions", label: "Positions", owner: "Controller" },
  { key: "exit", label: "Exit", owner: "Controller", journal: true },
];
export const CANDIDATE_PATH = [
  { key: "population", label: "Eligible pool", owner: "Controller" },
  { key: "laya", label: "Laya scoring", owner: "Laya worker" },
  { key: "shortlist", label: "Shortlist", owner: "Controller" },
  { key: "evidence", label: "Technicals & news", owner: "Controller · tools" },
  { key: "hermes", label: "Hermes", owner: "Hermes · masked" },
  { key: "decision_risk", label: "Decision risk", owner: "Risk checker" },
  { key: "queue_outcome", label: "Queued / rejected", owner: "Controller", journal: true },
];
export const STEP_ORDER = [...ORDER_PATH, ...CANDIDATE_PATH].map(s => s.key);

export const STATE_LABELS = {
  completed: "Completed", failed: "Failed", skipped: "Skipped", running: "In progress",
  interrupted: "Interrupted", not_recorded: "Not recorded", pending: "Waiting",
};

const isSubject = e => e.decision_id || e.proposal_key || (e.metrics && e.metrics.outcome) ||
  (e.stage === "positions" && e.metrics && e.metrics.exit_kind);
const ms = iso => (iso ? Date.parse(iso) : NaN);

function emptyStages() {
  const s = {};
  for (const k of STEP_ORDER) s[k] = { key: k, state: "not_recorded", source: "none", metrics: {}, startedAt: null, endedAt: null };
  return s;
}

// -------------------------------------------------------------- events

function invocationsFor(events) {
  const by = new Map();
  for (const e of events) {
    if (!by.has(e.invocation_id)) by.set(e.invocation_id, []);
    by.get(e.invocation_id).push(e);
  }
  for (const list of by.values()) list.sort((a, b) => a.seq - b.seq);
  return [...by.values()].sort((a, b) => ms(a[0].occurred_at) - ms(b[0].occurred_at));
}

function applyEvents(day, cycleEvents, runActive) {
  const invs = invocationsFor(cycleEvents);
  const last = invs[invs.length - 1];
  day.attempts = invs.map((list, i) => {
    const end = list.filter(e => e.stage === "cycle" && e.status !== "started").pop();
    return {
      invocationId: list[0].invocation_id, startedAt: list[0].occurred_at,
      state: end ? end.status : (i === invs.length - 1 && runActive ? "running" : "interrupted"),
    };
  });
  const cur = day.attempts[day.attempts.length - 1];
  day.state = cur.state;
  day.source = "event";
  for (const e of last) {
    if (isSubject(e)) continue;
    if (e.stage === "cycle" || e.stage === "run") continue;
    if (e.stage === "journal") {
      day.journal = { state: e.status, metrics: e.metrics || {}, at: e.occurred_at, source: "event" };
      continue;
    }
    const st = day.stages[e.stage];
    if (!st) continue;
    st.source = "event";
    if (e.status === "started") {
      st.startedAt = e.occurred_at;
      st.state = "running";
    } else {
      st.state = e.status;
      st.endedAt = e.occurred_at;
      if (e.status === "skipped") st.startedAt = st.startedAt || null;
    }
    st.metrics = { ...st.metrics, ...(e.metrics || {}) };
  }
  for (const st of Object.values(day.stages)) {
    if (st.state === "running" && cur.state !== "running") st.state = cur.state === "failed" ? "failed" : "interrupted";
  }
  for (const e of last) {
    if (!isSubject(e)) continue;
    const m = e.metrics || {};
    if (e.stage === "decision_risk") {
      day.proposals.push({ key: e.proposal_key, decisionId: e.decision_id, action: m.action || null,
        outcome: m.outcome, ruleCodes: m.rule_codes || [], at: e.occurred_at, source: "event" });
    } else if (e.stage === "execution") {
      day.orders.push({ decisionId: e.decision_id, action: m.action || null, outcome: m.outcome,
        ruleCodes: m.rule_codes || [], at: e.occurred_at, source: "event" });
    } else if (e.stage === "positions") {
      day.exits.push({ decisionId: e.decision_id, exitKind: m.exit_kind, exitReason: m.exit_reason,
        at: e.occurred_at, source: "event" });
    }
  }
}

// ------------------------------------------------------------- journal

function applyJournal(day, dayActivity, snapshot, prevQueuedCount, tradesById, seenQueued) {
  const count = t => dayActivity.filter(a => a.event_type === t).length;
  const ids = t => dayActivity.filter(a => a.event_type === t && a.decision_id).map(a => a.decision_id);
  if (snapshot) day.snapshot = snapshot;
  if (day.source === "event") return;          // telemetry is authoritative for this cycle
  day.source = dayActivity.length || snapshot ? "journal" : "none";
  day.state = snapshot ? "completed" : (dayActivity.length ? "not_recorded" : "not_recorded");
  const s = day.stages;
  const placed = ids("order_placed"), rejected = ids("order_rejected");
  // The harness logs a superseded order as a second "order_queued" row on its
  // resolution day; only the FIRST "order_queued" per decision is the proposal.
  const queuedRows = ids("order_queued");
  const superseded = queuedRows.filter(id => seenQueued.has(id));
  const queued = queuedRows.filter(id => !seenQueued.has(id));
  queued.forEach(id => seenQueued.add(id));
  const resolved = placed.length + rejected.length + superseded.length;
  if (resolved) {
    s.execution = { ...s.execution, state: "completed", source: "journal",
      metrics: { filled: placed.length, rejected: rejected.length, superseded: superseded.length } };
    for (const id of placed) day.orders.push({ decisionId: id, outcome: "filled", source: "journal",
      action: tradesById.get(id)?.action ?? null, ruleCodes: [] });
    for (const id of rejected) {
      const t = tradesById.get(id);
      const pre = (t?.rejection_reason || "").startsWith("pre_fill_news");
      day.orders.push({ decisionId: id, outcome: pre ? "cancelled" : "rejected", source: "journal",
        action: t?.action ?? null, ruleCodes: [] });
      if (pre) s.prefill_news = { ...s.prefill_news, state: "completed", source: "journal", metrics: {} };
    }
    for (const id of superseded) day.orders.push({ decisionId: id, outcome: "superseded", source: "journal",
      action: tradesById.get(id)?.action ?? null, ruleCodes: ["superseded"] });
  } else if (prevQueuedCount === 0 && dayActivity.length) {
    s.execution = { ...s.execution, state: "skipped", source: "journal", metrics: { queued_orders: 0 } };
    s.prefill_news = { ...s.prefill_news, state: "skipped", source: "journal", metrics: { queued_buys: 0 } };
  }
  const exits = dayActivity.filter(a => a.event_type === "exit" && a.decision_id);
  for (const a of exits) {
    const t = tradesById.get(a.decision_id);
    if (placed.includes(a.decision_id)) continue;      // a queued SELL fill: already an order
    day.exits.push({ decisionId: a.decision_id, exitKind: t?.exit_provenance || null, exitReason: null,
      source: "journal" });
  }
  if (snapshot) {
    s.positions = { ...s.positions, state: "completed", source: "journal",
      metrics: { open_after: snapshot.open_positions ?? null, exits: day.exits.length } };
  }
  if (count("reasoning")) s.hermes = { ...s.hermes, state: "completed", source: "journal", metrics: {} };
  const riskChecks = count("risk_check");
  if (queued.length || riskChecks || count("reasoning")) {
    s.decision_risk = { ...s.decision_risk, state: "completed", source: "journal",
      metrics: { queued: queued.length, rejected_recorded: riskChecks } };
    for (const id of queued) day.proposals.push({ key: null, decisionId: id, outcome: "queued", source: "journal",
      action: tradesById.get(id)?.action ?? null, ruleCodes: [] });
  }
  if (dayActivity.length || snapshot) day.journal = { state: "completed", source: "journal",
    metrics: { activity_rows: dayActivity.length, snapshot: !!snapshot } };
}

// ----------------------------------------------------------- derived boxes

function deriveBoxes(day, prevDay) {
  const s = day.stages;
  const exe = s.execution;
  const incoming = prevDay ? prevDay.proposals.filter(p => p.outcome === "queued").length : null;
  const nIn = exe.metrics.queued_orders ?? (day.orders.length || incoming);
  s.queued_in = { ...s.queued_in, source: exe.source === "none" ? (prevDay ? "journal" : "none") : exe.source,
    state: nIn == null ? "not_recorded" : nIn > 0 ? "completed" : "skipped",
    metrics: { orders: nIn ?? null } };
  if (s.prefill_news.state === "completed" || s.prefill_news.state === "skipped") {
    s.queued_in.metrics.orders = Math.max(s.queued_in.metrics.orders ?? 0, s.prefill_news.metrics.queued_buys ?? 0);
    if (s.queued_in.metrics.orders > 0) s.queued_in.state = "completed";
  }
  const outcomes = day.orders.reduce((m, o) => ({ ...m, [o.outcome]: (m[o.outcome] || 0) + 1 }), {});
  s.fill_outcome = { ...s.fill_outcome, source: day.orders.length ? day.orders[0].source : exe.source,
    state: day.orders.length ? "completed" : (exe.state === "skipped" ? "skipped" : exe.state === "completed" ? "completed" : exe.state),
    metrics: outcomes, startedAt: null, endedAt: day.orders.length ? day.orders[day.orders.length - 1].at ?? null : null };
  s.exit = { ...s.exit, source: day.exits.length ? day.exits[0].source : s.positions.source,
    state: day.exits.length ? "completed" : (s.positions.state === "completed" ? "skipped" : s.positions.state),
    metrics: { exits: day.exits.length }, endedAt: day.exits.length ? day.exits[day.exits.length - 1].at ?? null : null };
  const q = day.proposals.filter(p => p.outcome === "queued").length;
  const r = day.proposals.filter(p => p.outcome === "rejected").length;
  const dr = s.decision_risk;
  s.queue_outcome = { ...s.queue_outcome, source: day.proposals.length ? day.proposals[0].source : dr.source,
    state: dr.state === "skipped" ? "skipped" : day.proposals.length ? "completed" : dr.state,
    // Pre-journal rejections are only fully known from progress events; the
    // journal records some (risk_check rows) but not all of them.
    metrics: { queued: q, rejected: dr.source === "event" ? r : null }, endedAt: dr.endedAt };
}

// ------------------------------------------------------------------ run

function runStatus(runEvents, allEvents, nowMs, hasRecords) {
  if (!allEvents.length) {
    return { state: "not_recorded", label: "Progress not recorded",
      detail: hasRecords
        ? "This run predates workflow telemetry: outcomes below come from journal records; stage timing was not recorded."
        : "No progress events or journal records for this run yet.", lastEventAt: null };
  }
  const lastAt = allEvents.reduce((m, e) => Math.max(m, ms(e.occurred_at)), 0);
  const invs = invocationsFor(runEvents);
  const latest = invs[invs.length - 1] || [];
  const end = latest.filter(e => e.status !== "started").pop();
  const lastEventAt = new Date(lastAt).toISOString();
  if (end && end.status === "completed") {
    return { state: "completed", label: "Invocation completed", lastEventAt,
      detail: "The harness reported this invocation finished. Open positions or queued orders may remain for a resume.",
      metrics: end.metrics || {} };
  }
  if (end && end.status === "failed") {
    return { state: "failed", label: "Halted", lastEventAt, metrics: end.metrics || {},
      detail: "A cycle failed and the run stopped without advancing the simulated date." };
  }
  const age = nowMs - lastAt;
  if (age > STALE_MS) {
    return { state: "interrupted", label: "Interrupted or stalled", lastEventAt,
      detail: `No progress for ${Math.round(age / 60000)} min and no completion was reported — completion is not confirmed.` };
  }
  return { state: "running", label: "Running", lastEventAt, detail: "Progress events are arriving." };
}

/**
 * Build the page model for one run. Inputs are the public rows for exactly
 * that run (already run-filtered by data.js). Pure.
 */
export function buildWorkflowModel({ runId, runKind = null, events = [], trades = [], activity = [],
  snapshots = [] } = {}, nowMs = Date.now()) {
  const tradesById = new Map(trades.map(t => [t.decision_id, t]));
  const cycleEvents = events.filter(e => e.stage !== "run" && e.cycle_id);
  const runEvents = events.filter(e => e.stage === "run");
  const status = runStatus(runEvents, events, nowMs, activity.length > 0 || snapshots.length > 0);
  const runActive = status.state === "running";

  const days = new Map();
  const dayFor = (cycleId, init = {}) => {
    if (!days.has(cycleId)) days.set(cycleId, {
      cycleId, dayIndex: null, dayLabel: null, marketDate: null, state: "not_recorded", source: "none",
      attempts: [], stages: emptyStages(), orders: [], proposals: [], exits: [], journal: null, snapshot: null,
      firstAt: null, ...init });
    const d = days.get(cycleId);
    for (const [k, v] of Object.entries(init)) if (d[k] == null && v != null) d[k] = v;
    return d;
  };
  for (const e of cycleEvents) {
    const d = dayFor(e.cycle_id, { dayIndex: e.day_index, dayLabel: e.day_label, marketDate: e.market_date });
    d.firstAt = d.firstAt && ms(d.firstAt) < ms(e.occurred_at) ? d.firstAt : e.occurred_at;
  }
  const snapByCycle = new Map(snapshots.filter(s => s.cycle_id).map(s => [s.cycle_id, s]));
  for (const s of snapshots) if (s.cycle_id) {
    const d = dayFor(s.cycle_id, { dayLabel: s.day_label });
    d.firstAt = d.firstAt || s.created_at;
  }
  const actByCycle = new Map();
  for (const a of activity) {
    if (!a.cycle_id) continue;
    if (!actByCycle.has(a.cycle_id)) actByCycle.set(a.cycle_id, []);
    actByCycle.get(a.cycle_id).push(a);
    const d = dayFor(a.cycle_id);
    if (!d.firstAt || ms(a.created_at) < ms(d.firstAt)) d.firstAt = a.created_at;
  }
  const ordered = [...days.values()].sort((a, b) =>
    (a.dayIndex ?? Infinity) - (b.dayIndex ?? Infinity) || ms(a.firstAt) - ms(b.firstAt));
  // day labels for journal-only cycles come from snapshots; fall back to position
  ordered.forEach((d, i) => { if (!d.dayLabel) d.dayLabel = `Day ${i + 1}`; });

  let prev = null;
  const seenQueued = new Set();
  for (const d of ordered) {
    const ev = cycleEvents.filter(e => e.cycle_id === d.cycleId);
    if (ev.length) {
      applyEvents(d, ev, runActive);
      d.proposals.forEach(p => p.decisionId && seenQueued.add(p.decisionId));
    }
    const prevQueued = prev ? prev.proposals.filter(p => p.outcome === "queued").length : 0;
    applyJournal(d, actByCycle.get(d.cycleId) || [], snapByCycle.get(d.cycleId) || null,
      prev ? prevQueued : null, tradesById, seenQueued);
    deriveBoxes(d, prev);
    prev = d;
  }

  // Cross-day order links: proposal -> next-day resolution -> exit.
  const links = new Map();
  const link = id => {
    if (!links.has(id)) {
      const t = tradesById.get(id) || null;
      links.set(id, { decisionId: id, ticker: t?.ticker ?? null, action: t?.action ?? null, trade: t,
        queuedDay: null, resolvedDay: null, outcome: t?.status ?? null, ruleCodes: [] });
    }
    return links.get(id);
  };
  for (const d of ordered) {
    for (const p of d.proposals) if (p.decisionId) link(p.decisionId).queuedDay = d.dayLabel;
    for (const o of d.orders) if (o.decisionId) {
      const l = link(o.decisionId);
      l.resolvedDay = d.dayLabel; l.outcome = o.outcome; l.ruleCodes = o.ruleCodes;
    }
  }

  const latestSnap = [...snapshots].sort((a, b) => ms(b.created_at) - ms(a.created_at))[0] || null;
  const runMetrics = (runEvents.find(e => e.status === "started") || {}).metrics || {};
  return {
    runId, runKind, status, days: ordered, links,
    hasEvents: events.length > 0,
    runInfo: {
      selectorMode: runMetrics.selector_mode ?? null, fundamentals: runMetrics.fundamentals ?? null,
      sessionsRequested: runMetrics.sessions_requested ?? null,
      sessionsCompleted: status.metrics?.sessions_completed ?? null,
    },
    end: {
      openPositions: latestSnap ? (latestSnap.open_positions ?? null) : null,
      openDetail: Array.isArray(latestSnap?.open_positions_detail) ? latestSnap.open_positions_detail : [],
      pending: trades.filter(t => t.status === "pending"),
    },
    lastEventId: events.reduce((m, e) => Math.max(m, e.id || 0), 0),
  };
}

/** Ordered replay steps for one day: [{key, path}] in controller order. */
export function replaySteps(day) {
  return STEP_ORDER.filter(k => day.stages[k]).map(k => ({ key: k, path: ORDER_PATH.some(s => s.key === k) ? "order" : "candidate" }));
}

/** The stage a "follow current" view should highlight: the last one that
 *  started or finished in the latest attempt. */
export function currentStage(day) {
  let best = null, bestAt = -Infinity;
  for (const k of STEP_ORDER) {
    const st = day.stages[k];
    const at = ms(st.endedAt) || ms(st.startedAt);
    if (Number.isFinite(at) && at >= bestAt) { best = k; bestAt = at; }
    if (st.state === "running") return k;
  }
  return best;
}

/** One short count line for a box. Only real numbers; "—" otherwise. */
export function boxSummary(key, st) {
  const m = st.metrics || {};
  const n = v => (v == null ? "—" : String(v));
  switch (key) {
    case "queued_in": return `${n(m.orders)} order${m.orders === 1 ? "" : "s"}`;
    case "prefill_news": return m.queued_buys != null ? `${n(m.confirmed ?? (st.state === "skipped" ? 0 : null))} ok · ${n(m.cancelled ?? 0)} cancelled` : "";
    case "execution": return m.queued_orders != null ? `${n(m.queued_orders)} checked` : (m.filled != null ? `${m.filled + (m.rejected || 0)} resolved` : "");
    case "fill_outcome": return Object.keys(m).length ? Object.entries(m).map(([k, v]) => `${v} ${k}`).join(" · ") : "";
    case "positions": return m.monitored != null ? `${m.monitored} monitored` : (m.open_after != null ? `${m.open_after} open` : "");
    case "exit": return `${n(m.exits)} exit${m.exits === 1 ? "" : "s"}`;
    case "population": return m.eligible != null ? `${m.eligible} of ${n(m.universe)} eligible` : "";
    case "laya": return m.scored != null ? `${m.scored} scored · ${n(m.unavailable)} missing` : "";
    case "shortlist": return m.selected != null ? `${m.selected} of k=${n(m.k)}` : "";
    case "evidence": return m.names != null ? `${m.names} names · ${n(m.news_items)} news` : "";
    case "hermes": return m.decisions != null ? `${m.decisions} decision${m.decisions === 1 ? "" : "s"}` : "";
    case "decision_risk": return m.proposals != null ? `${m.proposals} proposal${m.proposals === 1 ? "" : "s"}` : (m.queued != null ? `${m.queued} queued` : "");
    case "queue_outcome": return `${n(m.queued)} queued · ${m.rejected == null ? "rejections not recorded" : `${m.rejected} rejected`}`;
    default: return "";
  }
}

// What "skipped" means for each box (nothing to do), so a quiet stage never
// reads as an unexplained skip.
export const SKIP_LABELS = {
  queued_in: "Nothing queued", prefill_news: "No queued BUYs", execution: "Nothing to check",
  fill_outcome: "Nothing queued", positions: "No positions", exit: "No exit", population: "Skipped",
  laya: "Selector off", shortlist: "Selector off", evidence: "Skipped", hermes: "Skipped",
  decision_risk: "No proposals", queue_outcome: "No proposals",
};

export function durationMs(st) {
  const a = ms(st.startedAt), b = ms(st.endedAt);
  return Number.isFinite(a) && Number.isFinite(b) && b >= a ? b - a : null;
}

const WF_KIND_LABELS = { training: "Training", evaluation: "Evaluation", smoke: "Smoke test" };
export const runKindLabel = kind => WF_KIND_LABELS[kind] || "Run";

/** Registry rows -> Workflow run list (newest first). Archived runs are
 *  never listed; smoke runs are included and labelled. Pure. */
export function workflowRunsFromRows(registryRows, snapshotRows) {
  const latest = new Map();
  for (const r of (snapshotRows || [])) if (r.run_id && !latest.has(r.run_id)) latest.set(r.run_id, r);
  return (registryRows || [])
    .filter(r => r.run_id && r.run_kind && r.run_kind !== "archived")
    .map(r => ({ run_id: r.run_id, run_kind: r.run_kind, created_at: r.created_at ?? null,
      latest_day_label: latest.get(r.run_id)?.day_label ?? null }))
    .sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
}

export function workflowRunLabel(run) {
  return `${runKindLabel(run.run_kind)} · ${String(run.run_id).slice(0, 8)}…${run.latest_day_label ? ` · ${run.latest_day_label}` : ""}`;
}
