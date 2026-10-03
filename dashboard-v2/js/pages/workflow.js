// Workflow page — the main data flow of one backtest run, day by day.
// Self-managed (not part of the shell's 15s refresh): `mount` loads the
// run once, replays recorded outcomes locally, and only polls when the
// visitor turns on "Follow current". Replay never calls a model, tool or
// execution path — it only re-renders data already fetched.
import { esc, time } from "../calc.js";
import { loadWorkflowData, pollWorkflowEvents, WORKFLOW_POLL_MS } from "../data.js";
import {
  ORDER_PATH, CANDIDATE_PATH, STATE_LABELS, buildWorkflowModel, replaySteps, currentStage,
  boxSummary, durationMs, runKindLabel, SKIP_LABELS,
} from "../workflow-model.js";
import { LAYA, LAYA_FIELDS, TOOLS, STAGE_HELP, OWNER_NOTE } from "../workflow-text.js";

const STEP_MS = 900;
const STEP_MS_REDUCED = 1600;
const MAX_BACKOFF_MS = 60_000;
const JOURNAL_REFRESH_MS = 15_000;

const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

const METRIC_LABELS = {
  orders: "Orders carried in", queued_buys: "Queued BUYs checked", confirmed: "Confirmed", cancelled: "Cancelled",
  articles: "Overnight articles", hermes_calls: "Hermes news checks", queued_orders: "Orders checked",
  filled: "Filled", rejected: "Rejected", superseded: "Superseded", monitored: "Positions monitored",
  exits: "Exits", open_after: "Open after session", universe: "Universe", excluded_held_or_pending: "Excluded (held/pending)",
  eligible: "Eligible", selector_mode: "Selector", cards: "Cards", complete: "Complete cards", scored: "Scored",
  unavailable: "Missing data", score_min: "Lowest review_probability", score_median: "Median review_probability",
  score_max: "Highest review_probability", worker_batches: "Worker batches", status: "Outcome", selected: "Selected",
  k: "Cap (k)", shortfall: "Fewer than k", names: "Names fetched", shortlisted: "Shortlisted", held: "Held",
  technicals_ok: "Technicals ok", technicals_errors: "Technical errors", news_items: "News items",
  fundamentals: "Fundamentals", candidates: "Candidates shown", positions: "Positions shown", decisions: "Decisions",
  buys: "BUY", sells: "SELL", attempts: "Model attempts", reflection: "Reflection day", total_tokens: "Tokens",
  proposals: "Proposals", queued: "Queued", rejected_recorded: "Risk-check rejections (journal)", replayed: "Replayed from checkpoint",
};

const fmt = v => (v === true ? "yes" : v === false ? "no" : typeof v === "number" && !Number.isInteger(v) ? v.toFixed(4) : String(v));
const secs = d => (d == null ? null : d < 1000 ? `${d} ms` : `${(d / 1000).toFixed(1)} s`);

export function mount(root, { runId, runs = [] }) {
  const st = {
    runId, run: runs.find(r => r.run_id === runId) || null, data: null, model: null,
    dayIdx: 0, cursor: -1, open: null, playing: false, timer: null,
    following: false, pollTimer: null, inflight: false, backoff: WORKFLOW_POLL_MS, conn: "idle",
    lastJournalAt: 0, destroyed: false,
  };

  const onVisibility = () => { if (st.following && !document.hidden) schedulePoll(0); renderConn(); };
  document.addEventListener("visibilitychange", onVisibility);

  root.innerHTML = `<div class="skel skel-page"></div>`;
  load();

  async function load() {
    try {
      st.data = await loadWorkflowData(st.runId);
      if (st.destroyed) return;
      rebuild();
      st.dayIdx = Math.max(0, st.model.days.length - 1);
      render();
    } catch (e) {
      if (st.destroyed) return;
      root.innerHTML = `<div class="error-box">Workflow data unavailable: ${esc(e.message)}. Nothing is shown as current.</div>`;
    }
  }

  function rebuild() {
    st.model = buildWorkflowModel({ runId: st.runId, runKind: st.run?.run_kind ?? null, ...st.data });
  }

  // ------------------------------------------------------------ replay
  function day() { return st.model.days[st.dayIdx] || null; }
  function steps() { const d = day(); return d ? replaySteps(d) : []; }

  function stop() { st.playing = false; clearTimeout(st.timer); st.timer = null; }
  function play() {
    if (!st.model.days.length) return;
    st.following && setFollow(false);
    st.playing = true;
    if (st.cursor < 0 || st.cursor >= steps().length - 1) st.cursor = -1;
    tick();
  }
  function tick() {
    if (!st.playing || st.destroyed) return;
    const n = steps().length;
    if (st.cursor < n - 1) st.cursor += 1;
    else if (st.dayIdx < st.model.days.length - 1) { st.dayIdx += 1; st.cursor = 0; }
    else { stop(); renderControls(); return; }
    renderDiagrams();
    renderControls();
    st.timer = setTimeout(tick, reducedMotion() ? STEP_MS_REDUCED : STEP_MS);
  }
  function goDay(i) {
    stop();
    st.dayIdx = Math.min(Math.max(i, 0), st.model.days.length - 1);
    st.cursor = -1;
    render();
  }

  // ------------------------------------------------------------ follow
  function setFollow(on) {
    st.following = on;
    clearTimeout(st.pollTimer);
    if (on) {
      stop();
      st.dayIdx = Math.max(0, st.model.days.length - 1);
      highlightCurrent();
      st.backoff = WORKFLOW_POLL_MS;
      schedulePoll(0);
    } else {
      st.conn = "idle";
    }
    render();
  }
  function schedulePoll(delay) {
    clearTimeout(st.pollTimer);
    if (!st.following || st.destroyed) return;
    st.pollTimer = setTimeout(poll, delay);
  }
  async function poll() {
    if (!st.following || st.destroyed) return;
    if (document.hidden) { st.conn = "paused"; renderConn(); return; }     // resumes on visibilitychange
    if (st.inflight) return;
    st.inflight = true;
    try {
      const fresh = await pollWorkflowEvents(st.runId, st.model.lastEventId);
      if (st.destroyed) return;
      st.conn = "live";
      st.lastPollAt = Date.now();
      st.backoff = WORKFLOW_POLL_MS;
      const needJournal = fresh.length && Date.now() - st.lastJournalAt > JOURNAL_REFRESH_MS;
      if (needJournal) {
        st.data = await loadWorkflowData(st.runId);
        st.lastJournalAt = Date.now();
      } else if (fresh.length) {
        st.data.events = st.data.events.concat(fresh);
      }
      rebuild();
      if (fresh.length) { st.dayIdx = Math.max(0, st.model.days.length - 1); highlightCurrent(); render(); }
      else { renderHeader(); renderConn(); }
      if (st.model.status.state === "completed" || st.model.status.state === "failed") {
        st.data = await loadWorkflowData(st.runId);               // final journal state, once
        rebuild(); render();
        st.following = false; st.conn = "ended"; render();
        return;
      }
      schedulePoll(WORKFLOW_POLL_MS);
    } catch (e) {
      if (st.destroyed) return;
      st.conn = "disconnected";
      st.backoff = Math.min(st.backoff * 2, MAX_BACKOFF_MS);
      renderConn();
      schedulePoll(st.backoff);
    } finally {
      st.inflight = false;
    }
  }
  function highlightCurrent() {
    const d = day();
    const k = d ? currentStage(d) : null;
    st.cursor = k ? steps().findIndex(s => s.key === k) : -1;
  }

  // ------------------------------------------------------------ render
  function render() {
    if (!st.model) return;
    root.innerHTML = `
      <div class="wf-head" id="wf-head"></div>
      <div class="wf-controls" id="wf-controls"></div>
      <p class="note wf-note">Replay steps through recorded outcomes in the controller's real order. Animation speed is illustrative, not historical duration. Nothing is re-run — no model, tool or execution call.</p>
      <div id="wf-diagrams"></div>
      <div id="wf-detail"></div>
      <div id="wf-end"></div>`;
    renderHeader(); renderControls(); renderDiagrams(); renderEnd();
  }

  function renderHeader() {
    const m = st.model, s = m.status;
    const info = m.runInfo;
    const tags = [
      `<span class="badge wf-kind">${esc(runKindLabel(m.runKind))}</span>`,
      info.selectorMode ? `<span class="badge">Selector: ${esc(info.selectorMode === "laya" ? "Laya" : "off")}</span>` : "",
      info.fundamentals ? `<span class="badge">Fundamentals: ${esc(info.fundamentals)}</span>` : "",
    ].join("");
    const el = root.querySelector("#wf-head");
    if (!el) return;
    el.innerHTML = `
      <div class="wf-title-row">
        <div class="section-title">Workflow · ${esc(String(m.runId).slice(0, 8))}…</div>
        <div class="wf-tags">${tags}</div>
      </div>
      <div class="wf-status wf-status-${esc(s.state)}" role="status">
        <strong>${esc(s.label)}</strong>
        <span>${esc(s.detail || "")}</span>
        ${s.lastEventAt ? `<span class="wf-last">Last progress event ${esc(time(s.lastEventAt))}</span>` : ""}
      </div>
      <p class="note">${esc(OWNER_NOTE)}</p>`;
  }

  function renderConn() {
    const el = root.querySelector("#wf-conn");
    if (!el) return;
    const text = !st.following ? (st.conn === "ended" ? "Run reported its end — follow stopped" : "")
      : document.hidden ? "Paused while this tab is hidden"
      : st.conn === "disconnected" ? `Disconnected — retrying in ${Math.round(st.backoff / 1000)} s`
      : st.conn === "live" ? `Live · checked ${esc(time(new Date(st.lastPollAt).toISOString()))}`
      : "Connecting…";
    el.textContent = text;
    el.className = `wf-conn ${st.conn}`;
  }

  function renderControls() {
    const el = root.querySelector("#wf-controls");
    const days = st.model.days;
    if (!el) return;
    if (!days.length) {
      el.innerHTML = `<div class="empty">No recorded cycles for this run yet.</div>
        <button type="button" id="wf-follow" class="wf-btn" aria-pressed="${st.following}">Follow current</button>
        <span id="wf-conn" class="wf-conn"></span>`;
      el.querySelector("#wf-follow").addEventListener("click", () => setFollow(!st.following));
      renderConn();
      return;
    }
    el.innerHTML = `
      <label class="wf-day-label" for="wf-day">Day</label>
      <select id="wf-day" class="run-select wf-day">${days.map((d, i) =>
        `<option value="${i}" ${i === st.dayIdx ? "selected" : ""}>${esc(d.dayLabel)}${d.marketDate ? ` · ${esc(d.marketDate)}` : ""} · ${esc(STATE_LABELS[d.state] || d.state)}</option>`).join("")}</select>
      <button type="button" class="wf-btn" id="wf-prev" ${st.dayIdx === 0 ? "disabled" : ""}>Previous</button>
      <button type="button" class="wf-btn primary" id="wf-play" aria-pressed="${st.playing}">${st.playing ? "Pause" : "Play"}</button>
      <button type="button" class="wf-btn" id="wf-next" ${st.dayIdx >= days.length - 1 ? "disabled" : ""}>Next</button>
      <button type="button" class="wf-btn" id="wf-follow" aria-pressed="${st.following}">Follow current</button>
      <span id="wf-conn" class="wf-conn" aria-live="polite"></span>`;
    el.querySelector("#wf-day").addEventListener("change", e => goDay(Number(e.target.value)));
    el.querySelector("#wf-prev").addEventListener("click", () => goDay(st.dayIdx - 1));
    el.querySelector("#wf-next").addEventListener("click", () => goDay(st.dayIdx + 1));
    el.querySelector("#wf-play").addEventListener("click", () => { st.playing ? stop() : play(); renderControls(); });
    el.querySelector("#wf-follow").addEventListener("click", () => setFollow(!st.following));
    renderConn();
  }

  function box(def, d, cursorKey) {
    const s = d.stages[def.key];
    const dur = durationMs(s);
    const state = s.state;
    const stateText = state === "skipped" ? SKIP_LABELS[def.key] || "Skipped"
      : `${STATE_LABELS[state] || state}${dur != null ? ` · ${secs(dur)}` : ""}`;
    const journal = def.journal && d.journal ? `<span class="wf-journal ${esc(d.journal.state)}" title="Journal write ${esc(d.journal.state)}">journal</span>` : "";
    const count = boxSummary(def.key, s);
    return `<button type="button" class="wf-box s-${esc(state)} ${cursorKey === def.key ? "is-current" : ""} ${st.open === def.key ? "is-open" : ""}"
        data-key="${esc(def.key)}" aria-expanded="${st.open === def.key}" aria-controls="wf-detail"
        aria-label="${esc(def.label)}: ${esc(STATE_LABELS[state] || state)}${count ? `, ${esc(count)}` : ""}">
      <span class="wf-box-label">${esc(def.label)}</span>
      <span class="wf-box-owner">${esc(def.owner)}</span>
      <span class="wf-box-count">${esc(count || "—")}</span>
      <span class="wf-box-state"><i class="wf-dot" aria-hidden="true"></i>${esc(stateText)}</span>
      ${journal}
    </button>`;
  }

  const ARROW = `<span class="wf-arrow" aria-hidden="true"><svg viewBox="0 0 24 12" width="24" height="12"><path d="M0 6h20M15 1l6 5-6 5" fill="none" stroke="currentColor" stroke-width="1.6"/></svg></span>`;

  function path(title, defs, d, cursorKey) {
    return `<section class="wf-path-block">
      <div class="wf-path-title">${esc(title)}</div>
      <div class="wf-path" role="group" aria-label="${esc(title)}">
        ${defs.map(def => box(def, d, cursorKey)).join(ARROW)}
      </div>
    </section>`;
  }

  function renderDiagrams() {
    const el = root.querySelector("#wf-diagrams");
    const d = day();
    if (!el) return;
    if (!d) { el.innerHTML = ""; return; }
    const cursorKey = st.cursor >= 0 ? steps()[st.cursor]?.key : null;
    const attempts = d.attempts.length > 1
      ? `<p class="note">${d.attempts.length} attempts recorded for this day: ${d.attempts.map((a, i) =>
          `#${i + 1} ${esc(STATE_LABELS[a.state] || a.state)} (${esc(time(a.startedAt))})`).join(", ")}. Shown: the latest.</p>` : "";
    const source = d.source === "event" ? "Recorded progress events (real wall-clock times)."
      : d.source === "journal" ? "Reconstructed from journal records — stage timing and counts without a record show “not recorded”."
      : "No records for this day.";
    el.className = reducedMotion() ? "wf-reduced" : "";
    el.innerHTML = `
      <div class="wf-day-meta">${esc(d.dayLabel)}${d.marketDate ? ` · simulated session ${esc(d.marketDate)}` : ""} · ${esc(STATE_LABELS[d.state] || d.state)} · <span class="muted-cell">${esc(source)}</span></div>
      ${attempts}
      ${path("1 · Order & position path — at the simulated open", ORDER_PATH, d, cursorKey)}
      ${path("2 · Candidate path — after the close", CANDIDATE_PATH, d, cursorKey)}`;
    el.querySelectorAll(".wf-box").forEach(b => {
      b.addEventListener("click", () => toggleOpen(b.dataset.key));
      b.addEventListener("keydown", onBoxKey);
    });
    renderDetail();
  }

  function onBoxKey(e) {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (!(e.key in keys)) return;
    e.preventDefault();
    const all = [...root.querySelectorAll(".wf-box")];
    const i = all.indexOf(e.currentTarget);
    all[(i + keys[e.key] + all.length) % all.length]?.focus();
  }

  function toggleOpen(key) {
    st.open = st.open === key ? null : key;
    renderDiagrams();
    root.querySelector(`.wf-box[data-key="${key}"]`)?.focus();
  }

  function kvs(metrics) {
    const rows = Object.entries(metrics || {}).filter(([k, v]) => v != null && METRIC_LABELS[k] && typeof v !== "object");
    if (!rows.length) return `<p class="note">No counts recorded for this stage.</p>`;
    return `<div class="kv-list">${rows.map(([k, v]) =>
      `<div class="kv"><span class="k">${esc(METRIC_LABELS[k])}</span><span class="v">${esc(fmt(v))}</span></div>`).join("")}</div>`;
  }

  function orderRows(items, kind) {
    if (!items.length) return "";
    const m = st.model;
    const rows = items.map(it => {
      const link = it.decisionId ? m.links.get(it.decisionId) : null;
      const t = link?.trade;
      const id = it.decisionId ? `${it.decisionId.slice(0, 8)}…` : esc(it.key || "—");
      const outcome = it.outcome || (kind === "exit" ? "exit" : "—");
      const tagCls = { filled: "filled", queued: "pending", rejected: "rejected", cancelled: "rejected", superseded: "rejected", exit: "open" }[outcome] || "open";
      const extra = kind === "exit" ? esc([it.exitKind, it.exitReason].filter(Boolean).join(" · ") || t?.exit_provenance || "—")
        : esc((it.ruleCodes || []).join(", ") || (t?.rejection_reason ? "see journal" : "—"));
      const days = link ? esc([link.queuedDay && `queued ${link.queuedDay}`, link.resolvedDay && `resolved ${link.resolvedDay}`].filter(Boolean).join(" → ") || "—") : (it.decisionId ? "—" : "not journaled");
      return `<tr><td class="mono">${id}</td><td>${esc(t?.ticker || "—")}</td><td>${esc(it.action || t?.action || "—")}</td>
        <td><span class="status-tag ${tagCls}">${esc(outcome)}</span></td><td>${extra}</td><td class="mono">${days}</td></tr>`;
    }).join("");
    const col = kind === "exit" ? "Exit type" : "Rule codes";
    return `<div class="panel"><table class="wf-table"><thead><tr><th>Link</th><th>Ticker</th><th>Action</th><th>Outcome</th><th>${col}</th><th>Days</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">Tickers come from the public trade journal. A proposal rejected before journaling has no trade row — it is shown by its position in that day's response (<code>p&lt;day&gt;-&lt;n&gt;</code>).</p>`;
  }

  function renderDetail() {
    const el = root.querySelector("#wf-detail");
    const d = day();
    if (!el) return;
    if (!st.open || !d) { el.innerHTML = ""; return; }
    const def = [...ORDER_PATH, ...CANDIDATE_PATH].find(x => x.key === st.open);
    const s = d.stages[st.open];
    const timing = s.source === "event"
      ? `Progress event${s.startedAt ? ` · started ${esc(time(s.startedAt))}` : ""}${s.endedAt ? ` · ended ${esc(time(s.endedAt))}` : ""}${durationMs(s) != null ? ` · ${esc(secs(durationMs(s)))}` : ""}`
      : s.source === "journal" ? "Journal record — timing not recorded" : "Not recorded";
    let extra = "";
    if (st.open === "laya") {
      extra = `<details class="tech-details" open><summary>Laya inputs and selection rule (${esc(LAYA.questionFormat)})</summary>
        <div class="kv-list">${LAYA_FIELDS.map(([k, v]) => `<div class="kv"><span class="k"><code>${esc(k)}</code></span><span class="v">${esc(v)}</span></div>`).join("")}</div>
        <p class="tech-note"><strong>Question:</strong> ${esc(LAYA.question)}</p>
        <p class="tech-note"><strong>“Yes” option:</strong> ${esc(LAYA.criteriaTrue)}</p>
        <p class="tech-note"><strong>“No” option:</strong> ${esc(LAYA.criteriaFalse)}</p>
        <p class="tech-note"><strong>Selection:</strong> ${esc(LAYA.selection)}</p>
        <p class="tech-note">${esc(LAYA.meaning)}</p>
        <p class="tech-note">${esc(LAYA.package)} · ${esc(LAYA.checkpoint)}. Only aggregate scores are published — never a company-linked score or rank.</p>
      </details>`;
    } else if (st.open === "evidence") {
      extra = `<p class="tech-note">${esc(TOOLS.technicals)}</p><p class="tech-note">${esc(TOOLS.news)}</p>`;
    } else if (st.open === "prefill_news") {
      extra = `<p class="tech-note">${esc(TOOLS.prefill)}</p>`;
    }
    const items = st.open === "fill_outcome" || st.open === "execution" ? orderRows(d.orders, "order")
      : st.open === "exit" || st.open === "positions" ? orderRows(d.exits, "exit")
      : st.open === "queue_outcome" || st.open === "decision_risk" ? orderRows(d.proposals, "proposal") : "";
    el.innerHTML = `<div class="detail-panel" role="region" aria-label="${esc(def.label)} details">
      <h3>${esc(def.label)} <span class="wf-owner-inline">${esc(def.owner)}</span></h3>
      <p class="note">${esc(STAGE_HELP[st.open] || "")}</p>
      <div class="kv-list"><div class="kv"><span class="k">State</span><span class="v">${esc(STATE_LABELS[s.state] || s.state)}</span></div>
        <div class="kv"><span class="k">Source</span><span class="v">${timing}</span></div></div>
      ${kvs(s.metrics)}
      ${items}
      ${extra}
      <button class="close-detail" type="button">Close</button>
    </div>`;
    el.querySelector(".close-detail").addEventListener("click", () => toggleOpen(st.open));
  }

  function renderEnd() {
    const el = root.querySelector("#wf-end");
    const e = st.model.end;
    if (!el) return;
    const pend = e.pending.map(t => `${esc(t.ticker)} ${esc(t.action)} ×${esc(t.intended_quantity ?? "—")}`).join(", ");
    el.innerHTML = `<section class="band"><div class="section-title">State at the latest record</div>
      <div class="kv-list">
        <div class="kv"><span class="k">Open positions</span><span class="v">${esc(e.openPositions ?? "not recorded")}</span></div>
        <div class="kv"><span class="k">Pending orders</span><span class="v">${e.pending.length ? `${e.pending.length} · ${pend}` : "none"}</span></div>
      </div>
      <p class="note">Open positions and queued orders are shown as recorded. A finished invocation can leave both for a later resume; the dashboard never closes or resolves them.</p>
    </section>`;
  }

  return {
    destroy() {
      st.destroyed = true;
      stop();
      clearTimeout(st.pollTimer);
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
