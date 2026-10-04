// Workflow page — an agent-centred architecture diagram of one simulated
// day, with recorded activity flowing through it. Hermes sits inside the
// backtest controller; the controller calls every tool, check and journal
// write. Self-managed (not part of the shell's 15s refresh): `mount` loads
// one run once, replays recorded steps locally, and only polls while the
// visitor turns on "Follow current" (inside "Explore other days"). Replay
// and browsing never call a model, tool or execution path — they re-render
// data already fetched.
import { esc, time } from "../calc.js";
import { loadWorkflowData, pollWorkflowEvents, WORKFLOW_POLL_MS } from "../data.js";
import {
  STATE_LABELS, buildWorkflowModel, durationMs, runKindLabel, workflowRunLabel,
  EXEMPLAR, DIAGRAM_NODES, DIAGRAM_EDGES, NODE_STATE_TEXT, nodeStates, diagramSteps, dayIndexByLabel,
} from "../workflow-model.js";
import { LAYA, LAYA_FIELDS, TOOLS, STAGE_HELP, OWNER_NOTE } from "../workflow-text.js";

const STEP_MS = 1100;
const STEP_MS_REDUCED = 2000;
const MAX_BACKOFF_MS = 60_000;
const JOURNAL_REFRESH_MS = 15_000;
const WIDE_MIN = 780;                    // diagram width at which the side-by-side layout applies (CSS @container too)

const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const fmt = v => (v === true ? "yes" : v === false ? "no" : typeof v === "number" && !Number.isInteger(v) ? v.toFixed(4) : String(v));
const secs = d => (d == null ? null : d < 1000 ? `${d} ms` : `${(d / 1000).toFixed(1)} s`);
const PHASE = { open: "At the open", close: "After the close" };
const LABEL = Object.fromEntries(DIAGRAM_NODES.map(n => [n.key, n.label]));

// ------------------------------------------------------------ edge routes
// Points in diagram-local px, from the rendered node boxes (R). One table per
// layout; the CSS grid places the boxes, these only connect them.
const WIDE = {
  pool_elig: R => [[R.pool.cx, R.pool.b], [R.pool.cx, R.eligibility.t]],
  elig_laya: R => [[R.eligibility.r, R.laya.cy], [R.laya.l, R.laya.cy]],
  laya_ctrl: R => [[R.x, R.laya.b], [R.x, R.ctrl.t]],
  ctrl_tech: R => [[R.ctrl.l, R.technicals.cy], [R.technicals.r, R.technicals.cy]],
  ctrl_news: R => [[R.ctrl.l, R.news.cy], [R.news.r, R.news.cy]],
  ctrl_fund: R => [[R.ctrl.r, R.fundamentals.cy], [R.fundamentals.l, R.fundamentals.cy]],
  ctrl_journal: R => [[R.ctrl.r, R.journal.cy - 7], [R.journal.l, R.journal.cy - 7]],
  journal_ctrl: R => [[R.journal.l, R.journal.cy + 7], [R.ctrl.r, R.journal.cy + 7]],
  ctx: R => [[R.x, R.ctxTop], [R.x, R.hermes.t]],
  prop: R => [[R.x, R.hermes.b], [R.x, R.ctrl.b - 14]],
  ctrl_risk: R => [[R.x, R.ctrl.b], [R.x, R.risk.t]],
  risk_queued: R => [[R.risk.l, R.risk.cy - 8], [R.queued.r, R.risk.cy - 8]],
  queued_risk: R => [[R.queued.r, R.risk.cy + 8], [R.risk.l, R.risk.cy + 8]],
  risk_rej: R => [[R.risk.r, R.risk.cy], [R.rejected.l, R.risk.cy]],
  risk_exec: R => [[R.x, R.risk.b], [R.x, R.execution.t]],
  exec_pos: R => [[R.execution.l, R.execution.cy], [R.positions.r, R.execution.cy]],
  pos_ctrl: R => [[R.positions.l, R.positions.cy], [10, R.positions.cy], [10, R.ctrl.b - 22], [R.ctrl.l, R.ctrl.b - 22]],
};
const NARROW = {
  pool_elig: R => [[R.x, R.pool.b], [R.x, R.eligibility.t]],
  elig_laya: R => [[R.x, R.eligibility.b], [R.x, R.laya.t]],
  laya_ctrl: R => [[R.x, R.laya.b], [R.x, R.ctrl.t]],
  ctrl_tech: R => [[R.technicals.cx, R.ctrl.t], [R.technicals.cx, R.technicals.b]],
  ctrl_news: R => [[R.news.cx, R.ctrl.t], [R.news.cx, R.news.b]],
  ctrl_fund: R => [[R.fundamentals.cx, R.ctrl.b], [R.fundamentals.cx, R.fundamentals.t]],
  ctrl_journal: R => [[R.journal.cx - 8, R.ctrl.b], [R.journal.cx - 8, R.journal.t]],
  journal_ctrl: R => [[R.journal.cx + 8, R.journal.t], [R.journal.cx + 8, R.ctrl.b]],
  ctx: R => [[R.x, R.ctxTop], [R.x, R.hermes.t]],
  prop: R => [[R.x, R.hermes.b], [R.x, R.ctrl.b - 14]],
  ctrl_risk: R => [[R.x, R.ctrl.b], [R.x, R.risk.t - 16], [R.risk.cx, R.risk.t - 16], [R.risk.cx, R.risk.t]],
  risk_queued: R => [[R.risk.cx + 14, R.risk.b], [R.risk.cx + 14, R.queued.t]],
  queued_risk: R => [[R.risk.cx - 14, R.queued.t], [R.risk.cx - 14, R.risk.b]],
  risk_rej: R => [[R.risk.r, R.risk.cy - 9], [R.rejected.l, R.risk.cy - 9]],
  risk_exec: R => { const g = (R.risk.r + R.rejected.l) / 2; return [[R.risk.r, R.risk.cy + 9], [g, R.risk.cy + 9], [g, R.execution.cy], [R.execution.l, R.execution.cy]]; },
  exec_pos: R => [[R.execution.cx, R.execution.b], [R.execution.cx, R.positions.t]],
  pos_ctrl: R => [[R.positions.l, R.positions.cy], [8, R.positions.cy], [8, R.ctrl.b - 22], [R.ctrl.l, R.ctrl.b - 22]],
};
// Label placement overrides: "below" a horizontal run, "left" of a vertical
// one, or "first" = near the start of the edge instead of the longest run.
const LABEL_AT = {
  wide: { queued_risk: "below", journal_ctrl: "below" },
  narrow: { queued_risk: "left", ctrl_journal: "left", laya_ctrl: "first", pos_ctrl: "first" },
};
const SHORT_LABEL = { narrow: { queued_risk: "re-check", risk_exec: "at fill price", ctrl_journal: "writes", journal_ctrl: "reads" },
  wide: { journal_ctrl: "reads" } };

export function mount(root, { runId, runs = [], onSelectRun = null }) {
  const st = {
    runId, run: runs.find(r => r.run_id === runId) || null, data: null, model: null,
    dayIdx: 0, cursor: -1, playing: false, timer: null, open: null, exploreOpen: false,
    following: false, pollTimer: null, inflight: false, backoff: WORKFLOW_POLL_MS, conn: "idle",
    lastJournalAt: 0, lastPollAt: 0, destroyed: false, ro: null,
  };

  const onVisibility = () => { if (st.following && !document.hidden) schedulePoll(0); renderConn(); };
  const onKey = e => { if (e.key === "Escape" && st.open) { e.preventDefault(); closeDetail(); } };
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("keydown", onKey);

  root.innerHTML = `<div class="skel skel-page"></div>`;
  load();

  async function load() {
    try {
      st.data = await loadWorkflowData(st.runId);
      if (st.destroyed) return;
      rebuild();
      const ex = st.runId === EXEMPLAR.runId ? dayIndexByLabel(st.model, EXEMPLAR.dayLabel) : -1;
      st.dayIdx = ex >= 0 ? ex : Math.max(0, st.model.days.length - 1);
      render();
    } catch (e) {
      if (st.destroyed) return;
      root.innerHTML = `<div class="error-box">Workflow data unavailable: ${esc(e.message)}. Nothing is shown as current.</div>`;
    }
  }

  function rebuild() {
    st.model = buildWorkflowModel({ runId: st.runId, runKind: st.run?.run_kind ?? null, ...st.data });
  }

  const day = () => st.model.days[st.dayIdx] || null;
  const steps = () => { const d = day(); return d ? diagramSteps(d, st.model) : []; };
  const isExemplar = () => st.runId === EXEMPLAR.runId && day()?.dayLabel === EXEMPLAR.dayLabel;

  // ------------------------------------------------------------ replay
  function stop() { st.playing = false; clearTimeout(st.timer); st.timer = null; }
  function play() {
    if (!steps().length) return;
    if (st.following) setFollow(false);
    st.playing = true;
    if (st.cursor < 0 || st.cursor >= steps().length - 1) st.cursor = -1;
    tick();
  }
  function tick() {
    if (!st.playing || st.destroyed) return;
    if (st.cursor < steps().length - 1) {
      st.cursor += 1;
      highlight();
      st.timer = setTimeout(tick, reducedMotion() ? STEP_MS_REDUCED : STEP_MS);
    } else {
      stop();
      st.cursor = -1;          // back to the whole recorded day
      highlight();
    }
  }
  function step(delta) {
    stop();
    if (st.following) setFollow(false);
    const n = steps().length;
    if (!n) return;
    st.cursor = st.cursor < 0 ? (delta > 0 ? 0 : n - 1) : Math.min(Math.max(st.cursor + delta, 0), n - 1);
    highlight();
  }
  function goDay(i) {
    stop();
    st.dayIdx = Math.min(Math.max(i, 0), st.model.days.length - 1);
    st.cursor = -1;
    st.open = null;
    render();
  }

  // ------------------------------------------------------------ follow
  function setFollow(on) {
    st.following = on;
    clearTimeout(st.pollTimer);
    if (on) {
      stop();
      st.dayIdx = Math.max(0, st.model.days.length - 1);
      st.cursor = steps().length - 1;
      st.backoff = WORKFLOW_POLL_MS;
      schedulePoll(0);
    } else {
      st.conn = st.conn === "ended" ? "ended" : "idle";
      st.cursor = -1;
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
      if (fresh.length && Date.now() - st.lastJournalAt > JOURNAL_REFRESH_MS) {
        st.data = await loadWorkflowData(st.runId);
        st.lastJournalAt = Date.now();
      } else if (fresh.length) {
        st.data.events = st.data.events.concat(fresh);
      }
      rebuild();
      if (fresh.length) {
        st.dayIdx = Math.max(0, st.model.days.length - 1);
        st.cursor = steps().length - 1;
        render();
      } else { renderConn(); }
      if (st.model.status.state === "completed" || st.model.status.state === "failed") {
        st.data = await loadWorkflowData(st.runId);               // final journal state, once
        if (st.destroyed) return;
        rebuild();
        st.following = false; st.conn = "ended"; st.cursor = -1;
        render();
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

  // ------------------------------------------------------------ render
  function render() {
    if (!st.model || st.destroyed) return;
    st.ro?.disconnect();
    const d = day();
    if (!d) {
      root.innerHTML = `${headHtml(null)}<div class="empty">No recorded cycles for this run yet.</div>${exploreHtml()}`;
      bindExplore();
      return;
    }
    root.innerHTML = `
      ${headHtml(d)}
      <div class="wfd-stage">
        <div class="wfd-main">
          <div class="wfd-replay">
            <button type="button" class="wf-btn primary" id="wfd-play" aria-pressed="${st.playing}">${st.playing ? "Pause" : "Replay"}</button>
            <button type="button" class="wf-btn wfd-stepbtn" id="wfd-prev" aria-label="Previous step">‹</button>
            <button type="button" class="wf-btn wfd-stepbtn" id="wfd-next" aria-label="Next step">›</button>
            <p class="wfd-caption" id="wfd-caption" aria-live="polite"></p>
          </div>
          <div class="wfd-wrap" id="wfd-wrap">${diagramHtml()}</div>
          ${legendHtml()}
        </div>
        <aside class="wfd-detail" id="wfd-detail" aria-label="Diagram details"></aside>
      </div>
      ${exploreHtml()}`;
    bindDiagram();
    bindExplore();
    highlight();
    renderDetail(false);
    const wrap = root.querySelector("#wfd-wrap");
    st.ro = new ResizeObserver(() => drawEdges());
    st.ro.observe(wrap);
    document.fonts?.ready?.then(() => drawEdges());
  }

  function headHtml(d) {
    const m = st.model, s = m.status;
    const live = ["running", "interrupted", "failed"].includes(s.state)
      ? `<span class="wfd-chip wfd-chip-${esc(s.state)}">${esc(s.label)}</span>` : "";
    const ex = d && isExemplar();
    return `<header class="wfd-head">
      <p class="wfd-kicker">${ex ? `<span class="wfd-chip wfd-chip-ex">Exemplar day</span>` : ""}${live}
        <span>${esc(runKindLabel(m.runKind))} · ${esc(String(m.runId).slice(0, 8))}…</span></p>
      ${d ? `<h2 class="wfd-title">${esc(d.dayLabel)}${d.marketDate ? ` <span class="wfd-date">simulated session ${esc(d.marketDate)}</span>` : ""}</h2>` : ""}
      ${ex ? `<p class="wfd-why">${esc(EXEMPLAR.why)}</p>` : ""}
    </header>`;
  }

  function nodeBtn(key, extraCls = "") {
    return `<button type="button" class="wfd-node ${extraCls}" data-node="${key}" aria-controls="wfd-detail" aria-expanded="false">
      <i class="wfd-glyph" aria-hidden="true"></i><span class="wfd-name">${esc(LABEL[key])}</span></button>`;
  }

  function diagramHtml() {
    return `<div class="wfd-diagram" id="wfd-diagram" role="group" aria-label="Workflow diagram: select a part for details">
      <svg class="wfd-edges" id="wfd-edges" aria-hidden="true"></svg>
      <div class="wfd-top">
        ${nodeBtn("pool", "wfd-a-pool")}${nodeBtn("eligibility", "wfd-a-elig")}${nodeBtn("laya", "wfd-a-laya")}
      </div>
      <div class="wfd-tools wfd-tools-l">${nodeBtn("technicals", "wfd-a-tech")}${nodeBtn("news", "wfd-a-news")}</div>
      <div class="wfd-ctrl" id="wfd-ctrl">
        <button type="button" class="wfd-ctrl-title" data-node="controller" aria-controls="wfd-detail" aria-expanded="false">
          <i class="wfd-glyph" aria-hidden="true"></i><span class="wfd-name">${esc(LABEL.controller)}</span></button>
        ${nodeBtn("hermes", "wfd-hermes")}
      </div>
      <div class="wfd-tools wfd-tools-r">${nodeBtn("fundamentals", "wfd-a-fund")}${nodeBtn("journal", "wfd-a-journal")}</div>
      ${nodeBtn("queued", "wfd-a-queued")}${nodeBtn("risk", "wfd-a-risk")}${nodeBtn("rejected", "wfd-a-rej wfd-stop")}
      ${nodeBtn("positions", "wfd-a-pos")}${nodeBtn("execution", "wfd-a-exec")}
    </div>`;
  }

  function legendHtml() {
    const g = (cls, text) => `<span class="wfd-leg"><i class="wfd-glyph st-${cls}" aria-hidden="true"></i>${esc(text)}</span>`;
    const l = (cls, text) => `<span class="wfd-leg"><svg class="wfd-legline ${cls}" viewBox="0 0 28 8" width="28" height="8" aria-hidden="true"><path d="M1 4H27"/></svg>${esc(text)}</span>`;
    return `<div class="wfd-legend">
      ${g("done", "Recorded activity")}${g("idle", "Ran — nothing to do")}${g("off", "Off for this run")}${g("unrecorded", "Not recorded")}${g("failed", "Failed")}
      ${l("k-flow", "Flow")}${l("k-tool", "Controller fetch")}${l("k-feedback", "Feedback")}
    </div>
    <p class="note wfd-note">Replay speed is illustrative, not execution time. Nothing is re-run — no model, tool or execution call. Branches with no recorded activity stay grey.</p>`;
  }

  function bindDiagram() {
    root.querySelector("#wfd-play").addEventListener("click", () => { st.playing ? (stop(), highlight()) : play(); });
    root.querySelector("#wfd-prev").addEventListener("click", () => step(-1));
    root.querySelector("#wfd-next").addEventListener("click", () => step(1));
    const nodes = [...root.querySelectorAll("#wfd-diagram [data-node]")];
    for (const b of nodes) {
      b.addEventListener("click", () => toggleDetail(b.dataset.node));
      b.addEventListener("keydown", e => {
        const k = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!k) return;
        e.preventDefault();
        nodes[(nodes.indexOf(b) + k + nodes.length) % nodes.length].focus();
      });
    }
  }

  // Classes only — the DOM (and focus) survives every replay step.
  function highlight() {
    const d = day();
    if (!d) return;
    const all = steps();
    const states = nodeStates(d, st.model);
    const activeNodes = new Set(all.flatMap(s => s.nodes)), activeEdges = new Set(all.flatMap(s => s.edges));
    if (all.length) activeNodes.add("controller");
    const cur = st.cursor >= 0 ? all[st.cursor] : null;
    const seenNodes = new Set(st.cursor < 0 ? activeNodes : all.slice(0, st.cursor).flatMap(s => s.nodes));
    root.querySelectorAll("#wfd-diagram [data-node]").forEach(b => {
      const k = b.dataset.node, state = states[k];
      b.classList.forEach(c => { if (c.startsWith("st-")) b.classList.remove(c); });
      b.classList.add(`st-${state}`);
      b.classList.toggle("is-inactive", !activeNodes.has(k) && !["failed", "running", "interrupted"].includes(state));
      b.classList.toggle("is-current", !!cur && cur.nodes.includes(k));
      b.classList.toggle("is-seen", seenNodes.has(k));
      b.classList.toggle("is-open", st.open === k);
      b.setAttribute("aria-expanded", String(st.open === k));
      b.setAttribute("aria-label", `${LABEL[k]} — ${NODE_STATE_TEXT[state]}${cur?.nodes.includes(k) ? ", current step" : ""}. Show details.`);
    });
    st.edgeState = key => (!activeEdges.has(key) ? "inactive" : !cur ? "seen"
      : cur.edges.includes(key) ? "current" : all.slice(0, st.cursor).some(s => s.edges.includes(key)) ? "seen" : "pending");
    root.querySelector("#wfd-diagram")?.classList.toggle("is-replaying", !!cur);
    const cap = root.querySelector("#wfd-caption");
    if (cap) {
      cap.innerHTML = cur
        ? `<span class="wfd-phase">Step ${st.cursor + 1} of ${all.length} · ${esc(PHASE[cur.phase])}</span> ${esc(cur.caption)}`
        : all.length
          ? `<span class="wfd-phase">${all.length} recorded steps</span> All of this day's recorded activity is shown. Replay walks through it in the controller's order.`
          : `<span class="wfd-phase">No recorded steps</span> Nothing was recorded for this day.`;
    }
    const playBtn = root.querySelector("#wfd-play");
    if (playBtn) { playBtn.textContent = st.playing ? "Pause" : "Replay"; playBtn.setAttribute("aria-pressed", String(st.playing)); }
    drawEdges();
  }

  // ------------------------------------------------------------ edges
  function drawEdges() {
    const dia = root.querySelector("#wfd-diagram"), svg = root.querySelector("#wfd-edges");
    if (!dia || !svg || !st.edgeState) return;
    const box = dia.getBoundingClientRect();
    if (!box.width) return;
    const rect = el => { const r = el.getBoundingClientRect(); const l = r.left - box.left, t = r.top - box.top;
      return { l, t, r: l + r.width, b: t + r.height, cx: l + r.width / 2, cy: t + r.height / 2 }; };
    const R = {};
    dia.querySelectorAll("[data-node]").forEach(n => { R[n.dataset.node] = rect(n); });
    R.ctrl = rect(dia.querySelector("#wfd-ctrl"));
    R.x = R.hermes.cx;
    // the context arrow starts below the controller's title when the title spans the spine
    R.ctxTop = R.controller.l < R.x && R.controller.r > R.x ? R.controller.b + 8 : R.ctrl.t + 14;
    const wide = box.width >= WIDE_MIN;
    const routes = wide ? WIDE : NARROW, lay = wide ? "wide" : "narrow";
    let paths = "", labels = "";
    for (const [key, def] of Object.entries(DIAGRAM_EDGES)) {
      const pts = routes[key](R);
      const s = st.edgeState(key);
      const cls = `wfd-edge k-${def.kind} e-${s}`;
      const both = def.kind === "tool" || def.kind === "optional";
      const trimmed = pts.map(p => p.slice());
      const head = (a, b) => {             // arrowhead at b, pointing from a to b; shortens the line under it
        const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
        const bx = b[0] - ux * 8, by = b[1] - uy * 8;
        return `<path class="${cls} wfd-head" d="M${b[0]},${b[1]}L${bx - uy * 4.5},${by + ux * 4.5}L${bx + uy * 4.5},${by - ux * 4.5}Z"/>`;
      };
      const n = pts.length;
      let heads = head(pts[n - 2], pts[n - 1]);
      { const [a, b] = [trimmed[n - 2], trimmed[n - 1]]; const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1; b[0] -= dx / len * 6; b[1] -= dy / len * 6; }
      if (both) {
        heads += head(pts[1], pts[0]);
        const [a, b] = [trimmed[0], trimmed[1]]; const dx = a[0] - b[0], dy = a[1] - b[1], len = Math.hypot(dx, dy) || 1; a[0] -= dx / len * 6; a[1] -= dy / len * 6;
      }
      paths += `<path class="${cls}" d="M${trimmed.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("L")}"/>${heads}`;
      const text = SHORT_LABEL[lay][key] ?? def.label;
      if (text) labels += edgeLabel(text, pts, LABEL_AT[lay][key], s, R.ctrl);
    }
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    svg.innerHTML = paths + labels;
  }

  function edgeLabel(text, pts, at, s, ctrl) {
    let seg = [pts[0], pts[1]];
    if (at !== "first") {
      let best = -1;
      for (let i = 0; i < pts.length - 1; i++) {
        const len = Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);
        if (len > best) { best = len; seg = [pts[i], pts[i + 1]]; }
      }
    }
    const [a, b] = seg, vertical = Math.abs(a[0] - b[0]) < 1;
    let x, y, anchor;
    if (vertical) {
      y = at === "first" ? Math.min(a[1], b[1]) + 16 : (a[1] + b[1]) / 2 + 4;
      x = at === "left" ? a[0] - 7 : a[0] + 7;
      anchor = at === "left" ? "end" : "start";
    } else {
      x = (a[0] + b[0]) / 2; anchor = "middle";
      y = at === "below" ? a[1] + 16 : a[1] - 7;
    }
    const inside = x > ctrl.l && x < ctrl.r && y > ctrl.t && y < ctrl.b ? " inside" : "";
    return `<text class="wfd-elabel e-${s}${inside}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}">${esc(text)}</text>`;
  }

  // ------------------------------------------------------------ detail
  function toggleDetail(key) {
    if (st.open === key) { closeDetail(); return; }
    st.open = key;
    highlight();
    renderDetail(true);
  }
  function closeDetail() {
    const key = st.open;
    st.open = null;
    highlight();
    renderDetail(false);
    root.querySelector(`#wfd-diagram [data-node="${key}"]`)?.focus();
  }

  function renderDetail(focus) {
    const el = root.querySelector("#wfd-detail");
    const d = day();
    if (!el || !d) return;
    el.classList.toggle("is-empty", !st.open);
    if (!st.open) {
      el.innerHTML = `<p class="wfd-detail-hint">Select any part of the diagram to see what it did on this day.</p>`;
      return;
    }
    const state = nodeStates(d, st.model)[st.open];
    el.innerHTML = `<div class="wfd-dhead">
        <h3 tabindex="-1" id="wfd-dtitle">${esc(LABEL[st.open])}</h3>
        <button type="button" class="close-detail wfd-close" aria-label="Close details">Close</button>
      </div>
      <p class="wfd-dstate"><i class="wfd-glyph st-${esc(state)}" aria-hidden="true"></i>${esc(NODE_STATE_TEXT[state])}</p>
      ${DETAIL[st.open](d)}`;
    el.querySelector(".wfd-close").addEventListener("click", closeDetail);
    if (focus) {
      const h = el.querySelector("#wfd-dtitle");
      h.focus({ preventScroll: true });
      if (el.getBoundingClientRect().top > window.innerHeight - 80 || el.getBoundingClientRect().bottom < 0) {
        el.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
      }
    }
  }

  // --- detail building blocks (structured, allowlisted facts only)
  const kv = rows => {
    const r = rows.filter(([, v]) => v != null && v !== "");
    return r.length ? `<div class="kv-list">${r.map(([k, v]) =>
      `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(fmt(v))}</span></div>`).join("")}</div>`
      : `<p class="note">No counts recorded.</p>`;
  };
  const sec = (title, body) => `<section class="wfd-dsec"><h4>${esc(title)}</h4>${body}</section>`;
  const p = text => `<p class="wfd-dtext">${esc(text)}</p>`;
  const source = stg => {
    if (stg.source === "event") {
      const dur = durationMs(stg);
      return `Recorded progress event${stg.endedAt ? ` · ${time(stg.endedAt)}` : ""}${dur != null ? ` · took ${secs(dur)}` : ""}`;
    }
    return stg.source === "journal" ? "Journal record — timing not recorded" : "Not recorded";
  };
  const stageRow = key => { const stg = day().stages[key]; return [["State", STATE_LABELS[stg.state] || stg.state], ["Source", source(stg)]]; };

  function itemRows(items, kind) {
    if (!items.length) return "";
    const m = st.model;
    const rows = items.map(it => {
      const link = it.decisionId ? m.links.get(it.decisionId) : null;
      const t = link?.trade;
      const name = t?.ticker || it.key || "—";
      const out = it.outcome || (kind === "exit" ? "exit" : "—");
      const tag = { filled: "filled", queued: "pending", rejected: "rejected", cancelled: "rejected", superseded: "rejected", exit: "open" }[out] || "open";
      const extra = kind === "exit" ? [it.exitKind, it.exitReason].filter(Boolean).join(" · ") || t?.exit_provenance || "—"
        : out === "filled" && t?.fill_quantity != null ? `${t.fill_quantity} @ ${t.fill_price}`
        : (it.ruleCodes || []).join(", ") || (out === "queued" && t?.intended_quantity != null ? `${t.intended_quantity} shares` : "—");
      const days = link ? [link.queuedDay && `queued ${link.queuedDay}`, link.resolvedDay && `resolved ${link.resolvedDay}`].filter(Boolean).join(" → ") || "—"
        : it.decisionId ? "—" : "not journaled";
      return `<li class="wfd-item"><span class="wfd-item-main"><strong>${esc(name)}</strong> ${esc(it.action || t?.action || "")}
          <span class="status-tag ${tag}">${esc(out)}</span></span>
        <span class="wfd-item-sub">${esc([extra !== "—" && extra, days !== "—" && days].filter(Boolean).join(" · ") || "—")}</span></li>`;
    }).join("");
    return `<ul class="wfd-items">${rows}</ul>`;
  }

  const DETAIL = {
    pool: d => {
      const m = d.stages.population.metrics;
      return p("The run's frozen, coverage-qualified company pool. The controller masks every company name and date before anything reaches Hermes.")
        + kv([["Companies in the pool", m.universe], ...stageRow("population")]);
    },
    eligibility: d => {
      const m = d.stages.population.metrics;
      return p(STAGE_HELP.population)
        + kv([["Eligible", m.eligible], ["Excluded — held or pending", m.excluded_held_or_pending],
          ["Selector", m.selector_mode === "laya" ? "Laya" : m.selector_mode], ...stageRow("population")]);
    },
    laya: d => {
      const l = d.stages.laya, s = d.stages.shortlist, lm = l.metrics, sm = s.metrics;
      return p("Laya's output only prioritises which companies get the full technical and news review. It does not authorise a trade, and it does not predict profit.")
        + p(LAYA.meaning)
        + sec("Scoring (Laya worker)", kv([["Cards", lm.cards], ["Scored", lm.scored], ["Missing data", lm.unavailable],
          ["Lowest review_probability", lm.score_min], ["Median review_probability", lm.score_median], ["Highest review_probability", lm.score_max],
          ["Worker batches", lm.worker_batches], ["Replayed from checkpoint", lm.replayed], ...stageRow("laya")]))
        + sec("Shortlist (controller)", p(STAGE_HELP.shortlist) + kv([["Selected", sm.selected], ["Cap", sm.k], ["Fewer than the cap", sm.shortfall], ...stageRow("shortlist")]))
        + sec(`Inputs — ${LAYA_FIELDS.length} numbers per card`, `<div class="kv-list">${LAYA_FIELDS.map(([k, v]) =>
          `<div class="kv"><span class="k"><code>${esc(k)}</code></span><span class="v wfd-plain">${esc(v)}</span></div>`).join("")}</div>`
          + p("The model sees only these numbers under an opaque row ID — never a ticker, name, date or text."))
        + sec(`Question and criteria (${LAYA.questionFormat})`, p(LAYA.question)
          + `<p class="wfd-dtext"><strong>Yes:</strong> ${esc(LAYA.criteriaTrue.replace(/^yes:\s*/, ""))}</p>`
          + `<p class="wfd-dtext"><strong>No:</strong> ${esc(LAYA.criteriaFalse.replace(/^no:\s*/, ""))}</p>`
          + p(LAYA.selection))
        + p(`${LAYA.package} · ${LAYA.checkpoint}. Only aggregate scores are published — never a company-linked score or rank.`);
    },
    controller: d => {
      const attempts = d.attempts.length > 1
        ? p(`${d.attempts.length} attempts recorded for this day: ${d.attempts.map((a, i) => `#${i + 1} ${STATE_LABELS[a.state] || a.state} (${time(a.startedAt)})`).join(", ")}. The diagram shows the latest.`) : "";
      return p(OWNER_NOTE)
        + sec("Order of work each simulated session", `<ol class="wfd-order">
            <li><strong>At the open</strong> — orders queued on an earlier day: pre-fill news check, execution-time risk re-check, fill, position monitoring and exits.</li>
            <li><strong>After the close</strong> — eligibility, Laya scoring and shortlist, technicals and news, Hermes, decision-time risk check, then queue or reject. Outcomes are journaled last.</li></ol>`
          + p("An order proposed after the close can fill no earlier than the next session's open."))
        + kv([["This day", STATE_LABELS[d.state] || d.state],
          ["Source", d.source === "event" ? "Recorded progress events (real wall-clock times)" : d.source === "journal" ? "Reconstructed from journal records — stage timing not recorded" : "No records"]])
        + attempts;
    },
    hermes: d => {
      const h = d.stages.hermes.metrics, pf = d.stages.prefill_news.metrics;
      return p(STAGE_HELP.hermes)
        + p("In backtests Hermes calls no tools: the controller fetches all data first and passes a masked summary.")
        + sec("After the close — trading decisions", kv([["Candidates shown", h.candidates], ["Positions shown", h.positions], ["Decisions", h.decisions],
          ["BUY", h.buys], ["SELL", h.sells], ["Model attempts", h.attempts], ["Reflection due this day", h.reflection], ["Tokens", h.total_tokens], ...stageRow("hermes")]))
        + sec("At the open — pre-fill news classification", kv([["Headline checks asked of Hermes", pf.hermes_calls ?? (d.stages.prefill_news.state === "not_recorded" ? "not recorded" : 0)]])
          + p("Hermes is asked only when overnight headlines exist for a queued BUY."));
    },
    technicals: d => {
      const e = d.stages.evidence.metrics;
      return p(TOOLS.technicals) + p("Called by the controller for the shortlist plus held positions — never by Hermes.")
        + kv([["Names fetched", e.names], ["Shortlisted", e.shortlisted], ["Held", e.held], ["Technicals ok", e.technicals_ok], ["Technical errors", e.technicals_errors], ...stageRow("evidence")]);
    },
    news: d => {
      const e = d.stages.evidence.metrics, pf = d.stages.prefill_news.metrics;
      return p("News can veto an entry or trigger an exit. It never justifies a buy.")
        + sec("At the open — pre-fill check of queued BUYs", p(TOOLS.prefill) + kv([["Queued BUYs checked", pf.queued_buys], ["Overnight articles", pf.articles],
          ["Hermes headline checks", pf.hermes_calls], ["Confirmed", pf.confirmed], ["Cancelled", pf.cancelled], ...stageRow("prefill_news")]))
        + sec("After the close — headlines for review", p(TOOLS.news) + kv([["News items", e.news_items], ["Names", e.names], ...stageRow("evidence")]));
    },
    fundamentals: d => {
      const f = d.stages.evidence.metrics.fundamentals ?? st.model.runInfo.fundamentals;
      return p("Optional, backtest-only context for candidates. It can temper or support a setup but is never a gate and never creates a buy.")
        + kv([["This run", f === "on" ? "On" : f === "off" ? "Off — not fetched" : "Not recorded"]]);
    },
    journal: d => {
      const j = d.journal?.metrics || {};
      return sec("Writes — by the controller", kv([["Trade rows written", j.trade_rows], ["Order resolutions", j.resolutions], ["Activity rows", j.activity_rows],
        ["Daily snapshot", j.snapshot], ["State", d.journal ? STATE_LABELS[d.journal.state] || d.journal.state : "Not recorded"],
        ["Source", d.journal?.source === "event" ? `Recorded progress event${d.journal.at ? ` · ${time(d.journal.at)}` : ""}` : d.journal ? "Journal records" : "Not recorded"]]))
        + sec("Reads — by the controller", p("Each session the controller reads the latest two reflection notes for this run and adds them to Hermes's masked context. A new reflection is due every fifth session.")
          + kv([["Reflection due this day", d.stages.hermes.metrics.reflection]])
          + p("Reads are not recorded as progress events. A read or a write is not evidence that anything was learned."));
    },
    risk: d => {
      const dr = d.stages.decision_risk.metrics, ex = d.stages.execution.metrics;
      return p("The same deterministic, fail-closed risk checker runs twice for every new order.")
        + sec("1 · Decision-time validation — after the close", p(STAGE_HELP.decision_risk)
          + kv([["Proposals", dr.proposals], ["Queued", dr.queued], ["Rejected", d.stages.decision_risk.source === "event" ? dr.rejected : null],
            ["Risk-check rejections (journal)", dr.rejected_recorded], ...stageRow("decision_risk")]) + itemRows(d.proposals, "proposal"))
        + sec("2 · Execution-time validation — next session's open", p(STAGE_HELP.execution)
          + kv([["Orders checked", ex.queued_orders], ["Filled", ex.filled], ["Rejected", ex.rejected], ["Superseded", ex.superseded], ...stageRow("execution")])
          + itemRows(d.orders.filter(o => o.outcome !== "cancelled"), "order"));
    },
    rejected: d => {
      const pre = d.proposals.filter(x => x.outcome === "rejected");
      const exe = d.orders.filter(o => ["rejected", "superseded"].includes(o.outcome));
      const can = d.orders.filter(o => o.outcome === "cancelled");
      const none = !pre.length && !exe.length && !can.length;
      const unknown = d.stages.decision_risk.source !== "event" && d.stages.decision_risk.state !== "skipped";
      return p("A rejected or cancelled order stops here: it never reaches execution.")
        + (none ? p(unknown ? "No rejection is in this day's journal records. Rejections made before journaling were not recorded for this run." : "No rejection recorded on this day.") : "")
        + (can.length ? sec("Cancelled by the pre-fill news check", itemRows(can, "order")) : "")
        + (exe.length ? sec("Not filled at the execution-time check", itemRows(exe, "order")) : "")
        + (pre.length ? sec("Rejected at decision time", itemRows(pre, "proposal")
          + p("A proposal rejected before journaling has no trade row; it is shown by its position in that day's response (p<day>-<n>).")) : "");
    },
    queued: d => {
      const carried = d.orders;
      const now = d.proposals.filter(x => x.outcome === "queued");
      const nIn = d.stages.queued_in.metrics.orders;
      return p("A proposal that passes the decision-time check is journaled as a pending order and waits for the next session's open.")
        + sec("Carried in — queued on an earlier day", carried.length ? itemRows(carried, "order") : p(nIn ? `${nIn} carried in.` : "Nothing carried into this session."))
        + sec("Queued today — for the next session's open", now.length ? itemRows(now, "proposal") : p("Nothing queued today."))
        + p("Nothing fills on the day it is proposed. An order still queued at the latest record stays pending — the dashboard never resolves it.");
    },
    execution: d => {
      const fills = d.orders.filter(o => o.outcome === "filled");
      return p(STAGE_HELP.fill_outcome)
        + kv([["Filled", fills.length], ...stageRow("fill_outcome")])
        + itemRows(fills, "order");
    },
    positions: d => {
      const pm = d.stages.positions.metrics;
      return p(STAGE_HELP.positions) + p(STAGE_HELP.exit)
        + kv([["Held at the open (monitored)", pm.monitored], ["Exits", d.exits.length],
          ["Open after the session", d.snapshot?.open_positions ?? pm.open_after],
          ["Held names re-evaluated after the close", d.stages.evidence.metrics.held], ...stageRow("positions")])
        + itemRows(d.exits, "exit")
        + p("Held positions feed back into the controller: each session their fresh technicals and news go to Hermes with the candidates.");
    },
  };

  // ------------------------------------------------------------ explore
  function exploreHtml() {
    const m = st.model, days = m.days, s = m.status, e = m.end;
    const pend = e.pending.map(t => `${t.ticker} ${t.action} ×${t.intended_quantity ?? "—"}`).join(", ");
    return `<details class="wfd-explore" id="wfd-explore" ${st.exploreOpen ? "open" : ""}>
      <summary>Explore other days</summary>
      <div class="wfd-explore-body">
        <div class="wf-controls">
          <label class="wf-day-label" for="wfd-run">Run</label>
          <select id="wfd-run" class="run-select">${runs.map(r =>
            `<option value="${esc(r.run_id)}" ${r.run_id === st.runId ? "selected" : ""}>${esc(workflowRunLabel(r))}</option>`).join("")}</select>
          ${days.length ? `<label class="wf-day-label" for="wfd-day">Day</label>
          <select id="wfd-day" class="run-select wf-day">${days.map((d, i) =>
            `<option value="${i}" ${i === st.dayIdx ? "selected" : ""}>${esc(d.dayLabel)}${d.marketDate ? ` · ${esc(d.marketDate)}` : ""} · ${esc(STATE_LABELS[d.state] || d.state)}</option>`).join("")}</select>
          <button type="button" class="wf-btn" id="wfd-dprev" ${st.dayIdx === 0 ? "disabled" : ""}>Previous day</button>
          <button type="button" class="wf-btn" id="wfd-dnext" ${st.dayIdx >= days.length - 1 ? "disabled" : ""}>Next day</button>` : ""}
          <button type="button" class="wf-btn" id="wfd-follow" aria-pressed="${st.following}">Follow current</button>
          <span id="wfd-conn" class="wf-conn" aria-live="polite"></span>
        </div>
        <div class="wf-status wf-status-${esc(s.state)}" role="status">
          <strong>${esc(s.label)}</strong><span>${esc(s.detail || "")}</span>
          ${s.lastEventAt ? `<span class="wf-last">Last progress event ${esc(time(s.lastEventAt))}</span>` : ""}
        </div>
        <div class="kv-list">
          <div class="kv"><span class="k">Open positions at the latest record</span><span class="v">${esc(e.openPositions ?? "not recorded")}</span></div>
          <div class="kv"><span class="k">Pending orders</span><span class="v">${e.pending.length ? `${e.pending.length} · ${esc(pend)}` : "none"}</span></div>
        </div>
        <p class="note">A finished invocation can leave open positions and queued orders for a later resume; the dashboard never closes or resolves them.</p>
      </div>
    </details>`;
  }

  function bindExplore() {
    const det = root.querySelector("#wfd-explore");
    det?.addEventListener("toggle", () => { st.exploreOpen = det.open; });
    root.querySelector("#wfd-run")?.addEventListener("change", e => onSelectRun?.(e.target.value));
    root.querySelector("#wfd-day")?.addEventListener("change", e => { if (st.following) setFollow(false); goDay(Number(e.target.value)); });
    root.querySelector("#wfd-dprev")?.addEventListener("click", () => { if (st.following) setFollow(false); goDay(st.dayIdx - 1); });
    root.querySelector("#wfd-dnext")?.addEventListener("click", () => { if (st.following) setFollow(false); goDay(st.dayIdx + 1); });
    root.querySelector("#wfd-follow")?.addEventListener("click", () => setFollow(!st.following));
    renderConn();
  }

  function renderConn() {
    const el = root.querySelector("#wfd-conn");
    if (!el) return;
    el.textContent = !st.following ? (st.conn === "ended" ? "Run reported its end — follow stopped" : "")
      : document.hidden ? "Paused while this tab is hidden"
      : st.conn === "disconnected" ? `Disconnected — retrying in ${Math.round(st.backoff / 1000)} s`
      : st.conn === "live" ? `Live · checked ${time(new Date(st.lastPollAt).toISOString())}`
      : "Connecting…";
    el.className = `wf-conn ${st.conn}`;
  }

  return {
    destroy() {
      st.destroyed = true;
      stop();
      clearTimeout(st.pollTimer);
      st.ro?.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      document.removeEventListener("keydown", onKey);
    },
  };
}
