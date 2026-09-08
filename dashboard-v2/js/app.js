import { currentRun, listBacktestRuns, loadRunData, REFRESH_MS } from "./data.js";
import { esc, time, computeHealth } from "./calc.js";
import { render as renderOverview } from "./pages/overview.js";
import { render as renderTrades } from "./pages/trades.js";
import { render as renderDecisions } from "./pages/decisions.js";
import { render as renderRuns } from "./pages/runs.js";
import { initTheme } from "./theme.js";

const PAGES = {
  overview: { label: "Overview", render: renderOverview },
  trades: { label: "Trades", render: renderTrades },
  decisions: { label: "Decisions", render: renderDecisions },
  runs: { label: "Runs & Evaluation", render: renderRuns },
};

const state = {
  mode: "backtest",
  page: "overview",
  runId: null,
  runs: [],
  refreshTimer: null,
};

const el = id => document.getElementById(id);

function renderNav() {
  const topNav = el("page-nav");
  const bottomNav = el("bottom-nav");
  const btn = (id, extraAttrs = "") => `<button type="button" data-page="${id}" aria-current="${state.page === id ? "page" : "false"}" ${extraAttrs}>${esc(PAGES[id].label)}</button>`;
  topNav.innerHTML = Object.keys(PAGES).map(id => btn(id)).join("");
  bottomNav.innerHTML = Object.keys(PAGES).map(id => btn(id, `class="nav-item"`)).join("");
  [...topNav.querySelectorAll("button"), ...bottomNav.querySelectorAll("button")].forEach(b => {
    b.addEventListener("click", () => { state.page = b.dataset.page; renderNav(); renderPage(state.lastData); });
  });
}

function renderRunSelector() {
  const wrap = el("run-select-wrap");
  if (state.mode !== "backtest" || !state.runs.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<select class="run-select" aria-label="Select run"></select>`;
  const sel = wrap.querySelector("select");
  sel.innerHTML = state.runs.map(r =>
    `<option value="${esc(r.run_id)}" ${r.run_id === state.runId ? "selected" : ""}>${esc(r.run_id.slice(0, 8))}… ${r.latest_day_label ? "· " + esc(r.latest_day_label) : ""}</option>`
  ).join("");
  sel.addEventListener("change", () => { state.runId = sel.value; refresh(true); });
}

function setStatus(text, cls = "") {
  el("status-line").innerHTML = `<span class="dot ${cls}"></span>${esc(text)}`;
}

function showSkeleton() {
  el("page-root").innerHTML = `<div class="skel" style="height:280px"></div>`;
}

function renderPage(data) {
  if (!data) return;
  const container = el("page-root");
  container.innerHTML = "";
  PAGES[state.page].render(container, { ...data, mode: state.mode, run_id: state.runId }, { runs: state.runs });
}

function renderHealthBar(data) {
  const bar = el("health-bar");
  const snap = data.snapshots?.[0];
  if (!snap) { bar.innerHTML = ""; bar.hidden = true; return; }
  bar.hidden = false;
  const health = computeHealth(data.snapshots, data.lastFailed);
  const pending = data.trades.filter(t => t.status === "pending").length;
  bar.innerHTML = `
    <span class="dot ${health && !health.healthy ? "err" : ""}"></span>
    <strong>${esc(health ? health.label : "Unknown")}</strong>
    <span class="sep">·</span>${esc(snap.day_label || "—")}
    <span class="sep">·</span>${esc(time(snap.created_at))}
    <span class="sep">·</span>${pending} pending order${pending === 1 ? "" : "s"}
  `;
}

async function refresh(withSkeleton) {
  if (withSkeleton) showSkeleton();
  setStatus(`${state.mode.toUpperCase()} · loading…`, "busy");
  try {
    if (state.mode === "backtest") {
      state.runs = await listBacktestRuns();
      if (!state.runId) state.runId = state.runs[0]?.run_id ?? await currentRun("backtest");
      renderRunSelector();
      if (!state.runId) {
        state.lastData = { trades: [], activity: [], snapshots: [], blockedActivity: [], insights: [], failedCount: 0, lastFailed: null };
        renderPage(state.lastData);
        renderHealthBar(state.lastData);
        setStatus("BACKTEST · no run scoped yet");
        return;
      }
    } else {
      el("run-select-wrap").innerHTML = "";
      state.runId = null;
    }

    const data = await loadRunData(state.mode, state.runId);
    state.lastData = data;
    renderPage(data);
    renderHealthBar(data);
    setStatus(`${state.mode.toUpperCase()} · updated ${new Date().toLocaleTimeString()}`);
  } catch (e) {
    setStatus(`${state.mode.toUpperCase()} · fetch failed`, "err");
    el("page-root").innerHTML = `<div class="error-box">Failed to load: ${esc(e.message)}. Old data is not shown as current — retrying on the next refresh.</div>`;
    el("health-bar").hidden = true;
    state.lastData = null;
  }
}

function setMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;
  state.runId = null;
  document.querySelectorAll(".segmented button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  refresh(true);
}

function boot() {
  initTheme();
  renderNav();
  document.querySelectorAll(".segmented button").forEach(b => {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  });
  refresh(true);
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => refresh(false), REFRESH_MS);
}

boot();
