import {
  pairTrades, realizedPnl, winRate, maxDrawdown, totalReturn,
  money, pctSigned, pctPlain, time, esc, STARTING_EQUITY,
} from "../calc.js";

// Simple population stdev of day-over-day % equity change — a real,
// derivable-from-stored-data volatility figure. Not the same methodology
// as docs/evaluations/round2_benchmark_sensitivity.md's annualized
// figure, so it is labeled separately, never presented as the same number.
function equityVolatilityPct(snapshotsAsc) {
  if (snapshotsAsc.length < 3) return null;
  const rets = [];
  for (let i = 1; i < snapshotsAsc.length; i++) {
    const prev = Number(snapshotsAsc[i - 1].equity), cur = Number(snapshotsAsc[i].equity);
    if (prev > 0) rets.push((cur / prev - 1) * 100);
  }
  if (rets.length < 2) return null;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length;
  return Math.sqrt(variance);
}

function benchmarkRow(label) {
  return `<div class="kv"><span class="k">${label}</span><span class="v" style="font-weight:400;color:var(--text-faint)">not available from Neon — see docs/evaluations/round2_benchmark_sensitivity.md</span></div>`;
}

export function render(el, data, ctx = {}) {
  const { trades, snapshots, insights, failedCount } = data;
  const { closed } = pairTrades(trades);
  const snap = snapshots[0];
  const wr = winRate(closed);
  const avgExposure = snapshots.length
    ? snapshots.reduce((a, s) => a + Number(s.exposure_pct ?? 0), 0) / snapshots.length
    : null;
  const vol = equityVolatilityPct([...snapshots].reverse());

  const withUsage = snapshots.filter(s => s.total_tokens != null);
  const totalTokens = withUsage.reduce((a, s) => a + Number(s.total_tokens), 0);
  const avgTokens = withUsage.length ? Math.round(totalTokens / withUsage.length) : null;
  const apiCalls = withUsage.reduce((a, s) => a + Number(s.api_calls ?? 1), 0);
  const retries = withUsage.reduce((a, s) => a + Math.max(0, (Number(s.api_calls) || 1) - 1), 0);

  const adequate = closed.length >= 30;

  el.innerHTML = `
    <div class="section-title">Results</div>
    <div class="stats-grid">
      ${cell("Total return", snap ? pctSigned(totalReturn(snap.equity)) : "—", snap && totalReturn(snap.equity) >= 0 ? "pos" : "neg")}
      ${cell("Closed-trade sample size", closed.length, "", `win rate ${wr.n ? Math.round(wr.pct) + "%" : "—"}${wr.n ? ` (n=${wr.n})` : ""}`)}
      ${cell("Avg. exposure", avgExposure != null ? pctPlain(avgExposure) : "—")}
      ${cell("Max drawdown", pctPlain(maxDrawdown(snapshots)), "neg")}
      ${cell("Equity volatility", vol != null ? vol.toFixed(2) + "%" : "not enough snapshots yet", "", "population stdev of period-over-period equity change — not the same methodology as the offline benchmark report's annualized figure")}
      ${cell("Failed cycles", failedCount ?? 0)}
    </div>

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

    <div class="section-title">Runtime usage</div>
    <div class="stats-grid">
      ${cell("Total tokens", totalTokens.toLocaleString())}
      ${cell("Avg tokens / cycle", avgTokens != null ? avgTokens.toLocaleString() : "—")}
      ${cell("API calls", apiCalls)}
      ${cell("Retries", retries)}
    </div>

    <div class="section-title">Reflections</div>
    ${insights.length
      ? `<div class="panel"><table class="responsive"><thead><tr><th>Cycle</th><th>Insight</th><th>Time</th></tr></thead><tbody>${insights.map(r => `<tr><td data-label="Cycle">${esc(r.cycle_type)}</td><td data-label="Insight">${esc(r.insight_text)}</td><td data-label="Time">${time(r.created_at)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="card empty" style="padding:16px">No reflection entries yet.</div>`}

    ${data.mode === "backtest" ? renderRunHistory(ctx.runs, data.run_id) : ""}
  `;
}

function cell(label, value, cls = "", sub = "") {
  return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${cls}">${value}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`;
}

function renderRunHistory(runs, currentRunId) {
  if (!runs || !runs.length) return "";
  const rows = runs.map(r => `
    <tr class="${r.run_id === currentRunId ? "" : ""}" data-label>
      <td data-label="Run">${esc(r.run_id)}${r.run_id === currentRunId ? " (current)" : ""}</td>
      <td data-label="Latest cycle">${r.latest_day_label ? esc(r.latest_day_label) : "—"}</td>
      <td data-label="Last updated">${time(r.latest_created_at)}</td>
    </tr>`).join("");
  return `
    <div class="section-title">Run history</div>
    <div class="panel"><table class="responsive">
      <thead><tr><th>Run</th><th>Latest cycle</th><th>Last updated</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <p style="font-size:11px;color:var(--text-faint);margin:8px 0 0">Runs are never merged — each row is a strictly separate run_id. Use the run selector above to switch.</p>
  `;
}
