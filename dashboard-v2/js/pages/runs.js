import { pairTrades, time, esc } from "../calc.js";
import { STRATEGY_PROFILE, CIRCUIT_BREAKER_NOTE } from "../strategy-profile.js";

function benchmarkRow(label) {
  return `<div class="kv"><span class="k">${label}</span><span class="v" style="font-weight:400;color:var(--text-faint)">not available from Neon — see docs/evaluations/round2_benchmark_sensitivity.md</span></div>`;
}

export function render(el, data) {
  const { trades, snapshots, insights, failedCount, lastFailed } = data;
  const { closed } = pairTrades(trades);
  const adequate = closed.length >= 30;

  el.innerHTML = `
    <div class="section-title">Current strategy profile</div>
    <div class="strategy-grid">
      ${STRATEGY_PROFILE.map(p => `
        <div class="stat">
          <div class="stat-label">${esc(p.label)}</div>
          <div class="stat-value" style="font-size:15px">${esc(p.value)}</div>
        </div>`).join("")}
    </div>
    <p class="safety-line">${esc(CIRCUIT_BREAKER_NOTE)}</p>

    <div class="section-title">Benchmark comparison</div>
    <div class="card kv-list">
      ${benchmarkRow("SPY buy-and-hold")}
      ${benchmarkRow("Equal-weight watchlist buy-and-hold")}
      ${benchmarkRow("SMA(10) technical baseline")}
    </div>
    <p style="font-size:11px;color:var(--text-faint);margin:8px 0 0">HANDOFF.md §8: no benchmark series is wired into this dashboard yet — the comparison lives only in the offline report above.</p>

    <div class="section-title">Data adequacy</div>
    <div class="warning-box">
      ${adequate
        ? `${closed.length} closed trades meets RULES.md §6b's own 30–60-trade six-month target.`
        : `${closed.length} closed trade${closed.length === 1 ? "" : "s"} is below RULES.md §6b's own stated 30–60-trade six-month target — treat any win-rate or profit-factor figure above as preliminary, not a durable performance claim.`}
    </div>

    <div class="section-title">Reflections</div>
    ${insights.length
      ? `<div class="panel"><table class="responsive"><thead><tr><th>Cycle</th><th>Insight</th><th>Time</th></tr></thead><tbody>${insights.map(r => `<tr><td data-label="Cycle">${esc(r.cycle_type)}</td><td data-label="Insight">${esc(r.insight_text)}</td><td data-label="Time">${time(r.created_at)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty" style="padding:16px">No reflection entries yet.</div>`}

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
