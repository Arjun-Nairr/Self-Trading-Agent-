import { listTrainingRuns, loadRunData, REFRESH_MS } from "./data.js";
import { esc, time, computeHealth } from "./calc.js";
import { render as renderOverview } from "./pages/overview.js";
import { render as renderTrades } from "./pages/trades.js";
import { render as renderDecisions } from "./pages/decisions.js";
import { render as renderRuns } from "./pages/runs.js";
import { render as renderRoadmap } from "./pages/roadmap.js";
import { initTheme } from "./theme.js";

const PAGES = {
  overview: { label: "Overview", render: renderOverview },
  trades: { label: "Trades", render: renderTrades },
  decisions: { label: "Decisions", render: renderDecisions },
  runs: { label: "Runs", render: renderRuns },
  roadmap: { label: "Roadmap", render: renderRoadmap },
};

// Roadmap is a static, mode-independent projection of the project
// backlog — the shell issues no Neon request while it is the active page.
const STATIC_PAGES = new Set(["roadmap"]);

const state = {
  // Dashboard-exposure correction (2026-09-13): fixed at "backtest",
  // never mutated after this — there is no UI control left that can
  // change it. loadRunData/page renderers still take `mode` as a plain
  // value (unchanged, live-paper query support preserved in data.js),
  // it just can never become anything but "backtest" from here.
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
    b.addEventListener("click", () => {
      const wasStatic = STATIC_PAGES.has(state.page);
      state.page = b.dataset.page;
      renderNav();
      if (STATIC_PAGES.has(state.page)) refresh(false);        // static page: swap content, no fetch
      else if (wasStatic || !state.lastData) refresh(true);    // (re)entering a data page: fetch fresh
      else renderPage(state.lastData);                          // data ↔ data: cheap swap
    });
  });
}

function renderRunSelector() {
  const wrap = el("run-select-wrap");
  if (!state.runs.length) { wrap.innerHTML = ""; return; }
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
  if (!data && !STATIC_PAGES.has(state.page)) return;
  const container = el("page-root");
  container.innerHTML = "";
  PAGES[state.page].render(container, { ...(data || {}), mode: state.mode, run_id: state.runId }, { runs: state.runs });
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
  if (STATIC_PAGES.has(state.page)) {
    // No run data needed — and no Neon/Data API request is made. The
    // Roadmap is project-level and static: it never shows a run selector
    // or any other run/mode-specific control, so both are cleared here
    // rather than left showing whatever the last data page rendered.
    // (.health-bar sets display:flex, which beats [hidden]; clear it too.)
    const bar = el("health-bar");
    bar.innerHTML = ""; bar.hidden = true;
    el("run-select-wrap").innerHTML = "";
    setStatus("Project roadmap");
    renderPage(state.lastData);
    return;
  }
  if (withSkeleton) showSkeleton();
  // The scope is already stated beside this line by the fixed
  // "Backtest research mode" note -- no need to repeat it every tick.
  setStatus("Loading…", "busy");
  try {
    if (state.mode === "backtest") {
      state.runs = await listTrainingRuns();
      if (!state.runId) state.runId = state.runs[0]?.run_id ?? null;
      renderRunSelector();
      if (!state.runId) {
        // No registry-backed training run with snapshots exists — an
        // honest empty state, never a fallback to an arbitrary (possibly
        // smoke or archived) snapshot run.
        state.lastData = { trades: [], activity: [], snapshots: [], blockedActivity: [], insights: [], failedCount: 0, lastFailed: null };
        renderPage(state.lastData);
        renderHealthBar(state.lastData);
        setStatus("No training run available");
        return;
      }
    } else {
      // Unreachable while state.mode is fixed at "backtest" (see state
      // above) -- kept alongside data.js's preserved MODES.live mapping and
      // the renderers' own live branches, not removed by this visual pass.
      el("run-select-wrap").innerHTML = "";
      state.runId = null;
    }

    const data = await loadRunData(state.mode, state.runId);
    state.lastData = data;
    renderPage(data);
    renderHealthBar(data);
    setStatus(`Updated ${new Date().toLocaleTimeString()}`);
  } catch (e) {
    setStatus("Fetch failed", "err");
    el("page-root").innerHTML = `<div class="error-box">Failed to load: ${esc(e.message)}. Old data is not shown as current — retrying on the next refresh.</div>`;
    el("health-bar").hidden = true;
    state.lastData = null;
  }
}

function boot() {
  initTheme();
  renderNav();
  refresh(true);
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => refresh(false), REFRESH_MS);
}

boot();
