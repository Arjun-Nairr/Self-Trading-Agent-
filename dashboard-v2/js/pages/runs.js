import { pairTrades, time, esc } from "../calc.js";
import { STRATEGY_PROFILE, CIRCUIT_BREAKER_NOTE } from "../strategy-profile.js";

const BENCHMARKS = [
  "SPY buy-and-hold",
  "Equal-weight watchlist buy-and-hold",
  "SMA(10) technical baseline",
];

// Display-only presentation of a stored cycle_type value (e.g.
// "weekly_reflection" -> "Weekly reflection"). Never written back, never
// used in a query -- the stored value is untouched.
function cycleTypeLabel(cycleType) {
  const words = String(cycleType || "").replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Reflection";
}

export function render(el, data) {
  const { trades, insights, failedCount, lastFailed } = data;
  const { closed } = pairTrades(trades);
  const adequate = closed.length >= 30;

  // Every reflection in this run shares one cycle_type today, so it reads
  // once as the section heading instead of repeating down a column. If a
  // run ever mixes types, each block states its own.
  const cycleTypes = [...new Set((insights || []).map(r => r.cycle_type))];
  const singleType = cycleTypes.length === 1 ? cycleTypes[0] : null;
  const reflectionsHeading = singleType ? `${cycleTypeLabel(singleType)}s` : "Reflections";

  el.innerHTML = `
    <div class="section-title">Current strategy profile</div>
    <div class="strategy-grid">
      ${STRATEGY_PROFILE.map(p => `
        <div class="stat">
          <div class="stat-label">${esc(p.label)}</div>
          <div class="stat-value text">${esc(p.value)}</div>
        </div>`).join("")}
    </div>
    <p class="safety-line">${esc(CIRCUIT_BREAKER_NOTE)}</p>

    <div class="section-title">Benchmark comparison</div>
    <div class="bench-list">
      ${BENCHMARKS.map(b => `<div class="bench-row">${esc(b)}</div>`).join("")}
    </div>
    <p class="note">None of the three is available from Neon — see <code>docs/evaluations/round2_benchmark_sensitivity.md</code>. HANDOFF.md §8: no benchmark series is wired into this dashboard yet, so the comparison lives only in that offline report.</p>

    <div class="section-title">Data adequacy</div>
    <div class="warning-box">
      ${adequate
        ? `${closed.length} closed trades meets RULES.md §6b's own 30–60-trade six-month target.`
        : `${closed.length} closed trade${closed.length === 1 ? "" : "s"} is below RULES.md §6b's own stated 30–60-trade six-month target — treat any win-rate or profit-factor figure above as preliminary, not a durable performance claim.`}
    </div>

    <div class="section-title">${esc(reflectionsHeading)}</div>
    ${insights.length
      ? `<div class="reflections">${insights.map(r => `
          <blockquote class="reflection">
            <p>${esc(r.insight_text)}</p>
            <span class="meta">${singleType ? "" : `${esc(cycleTypeLabel(r.cycle_type))} · `}${time(r.created_at)}</span>
          </blockquote>`).join("")}</div>`
      : `<div class="empty">No reflection entries yet.</div>`}

    ${renderTechnicalDetails(failedCount, lastFailed)}
  `;
}

// Historical failure info, collapsed by default and styled neutrally (not
// as a live alarm) — the compact header status is the current-state
// signal; this is background for someone who wants it.
function renderTechnicalDetails(failedCount, lastFailed) {
  if (!failedCount && !lastFailed) return "";
  return `
    <details class="tech-details">
      <summary>Technical: failure history</summary>
      <div class="kv-list" style="margin-top:8px">
        <div class="kv"><span class="k">Failed cycles in this run</span><span class="v">${failedCount ?? 0}</span></div>
        <div class="kv"><span class="k">Last failed attempt</span><span class="v">${lastFailed ? time(lastFailed.created_at) : "none recorded"}</span></div>
      </div>
      ${lastFailed ? `<p class="tech-note">${esc(lastFailed.description || "A cycle failed.")}</p>` : ""}
    </details>`;
}
