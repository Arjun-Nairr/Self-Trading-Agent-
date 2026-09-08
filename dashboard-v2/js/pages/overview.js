import {
  pairTrades, realizedPnl, unrealizedPnl, winRate, maxDrawdown, totalReturn,
  money, pctSigned, pctPlain, esc, STARTING_EQUITY, openPositionDetail, equityCurveSeries,
} from "../calc.js";

const STALE_MS = 24 * 3600_000; // live mode only — see render() below

function stat(label, value, cls = "", sub = "") {
  return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${cls}">${value}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`;
}

export function render(el, data, ctx) {
  const { trades, snapshots, mode } = data;
  const { closed, open } = pairTrades(trades);
  const snap = snapshots[0];

  if (!snap && !trades.length) {
    el.innerHTML = `<div class="empty">No ${mode === "backtest" ? "training" : "live"} data recorded yet for this ${mode === "backtest" ? "run" : "account"}. This view populates as soon as the first cycle runs.</div>`;
    return;
  }

  const dd = snap ? Number(snap.drawdown_pct ?? 0) : 0;
  const maxDd = maxDrawdown(snapshots);
  const totRet = snap ? totalReturn(snap.equity) : null;
  const rPnl = realizedPnl(closed);
  const uPnl = snap ? unrealizedPnl(snap.open_positions_detail) : 0;
  const wr = winRate(closed);

  let staleBanner = "";
  if (mode === "live" && snap) {
    const ageMs = Date.now() - new Date(snap.created_at).getTime();
    if (ageMs > STALE_MS) {
      staleBanner = `<div class="stale-banner">Last live snapshot is ${Math.round(ageMs / 3600_000)}h old — this view may not reflect the account's current state.</div>`;
    }
  }

  el.innerHTML = `
    ${staleBanner}
    <section>
      <div class="section-title">Portfolio</div>
      <div class="stats-grid">
        ${stat("Equity", snap ? money(snap.equity) : "—", "", snap ? `Cash ${money(snap.cash)}` : "")}
        ${stat("Total return", pctSigned(totRet), totRet >= 0 ? "pos" : "neg", `vs. ${money(STARTING_EQUITY)} start`)}
        ${stat("Realized P&L", money(rPnl), rPnl >= 0 ? "pos" : "neg", `${closed.length} closed`)}
        ${stat("Unrealized P&L", money(uPnl), uPnl >= 0 ? "pos" : "neg", `${open.length} open`)}
      </div>
    </section>

    <section>
      <div class="section-title">Risk &amp; exposure</div>
      <div class="stats-grid">
        ${stat("Exposure", snap ? pctPlain(snap.exposure_pct) : "—")}
        ${stat("Drawdown", pctPlain(dd), dd < 0 ? "neg" : "")}
        ${stat("Max drawdown", pctPlain(maxDd), maxDd < 0 ? "neg" : "")}
        ${stat("Win rate", wr.n ? `${Math.round(wr.pct)}%` : "—", "", wr.n ? `n=${wr.n}` : "no closed trades yet")}
      </div>
    </section>

    <section>
      <div class="section-title">Open positions</div>
      ${renderPositions(open, snap)}
    </section>

    <section>
      <div class="section-title">Equity over time</div>
      ${renderChart(snapshots)}
    </section>
  `;
}

function renderPositions(open, snap) {
  if (!open.length) return `<div class="card empty" style="padding:20px">No open positions.</div>`;

  const rows = open.map(t => {
    // Current price and unrealized P&L come ONLY from the latest scoped
    // snapshot's own open_positions_detail — never the entry fill price
    // relabeled as "current".
    const d = openPositionDetail(t.ticker, snap);
    const current = d ? Number(d.current_price) : null;
    const upl = d ? Number(d.unrealized_pl) : null;
    const uplPct = d ? Number(d.unrealized_pl_pct) : null;
    return `<tr class="responsive-row">
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Qty">${t.fill_quantity}</td>
      <td data-label="Entry">${money(t.fill_price)}</td>
      <td data-label="Current">${money(current)}</td>
      <td data-label="P&L" class="${upl == null ? "" : upl >= 0 ? "pos" : "neg"}">${upl == null ? "—" : `${upl >= 0 ? "+" : ""}${money(upl)} (${pctSigned(uplPct)})`}</td>
      <td data-label="Stop">${d ? money(d.stop_price) : (t.stop_price != null ? money(t.stop_price) : "—")}</td>
      <td data-label="Target">${d ? money(d.target_price) : (t.target_price != null ? money(t.target_price) : "—")}</td>
    </tr>`;
  }).join("");

  return `<div class="panel"><table class="responsive">
    <thead><tr><th>Ticker</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th>Stop</th><th>Target</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

// Vanilla SVG line chart, built only from stored portfolio_snapshots (no
// market data, no per-trade path). Colors reference CSS custom properties
// so the chart stays readable in both themes without a redraw.
function renderChart(snapshots) {
  const series = equityCurveSeries(snapshots);

  if (series.length < 2) {
    const msg = series.length === 0
      ? "No snapshots yet for this run."
      : "One snapshot so far — the chart needs at least two to draw a line.";
    return `<div class="card empty" style="padding:24px">${msg}</div>`;
  }

  const vals = series.map(s => s.equity);
  const lo = Math.min(...vals, STARTING_EQUITY), hi = Math.max(...vals, STARTING_EQUITY);
  const pad = (hi - lo) * 0.15 || 500;
  const min = lo - pad, max = hi + pad, range = max - min || 1;

  const W = 600, H = 200, PAD = 6;
  const x = i => PAD + (i / (series.length - 1)) * (W - PAD * 2);
  const y = v => PAD + (1 - (v - min) / range) * (H - PAD * 2);
  const baseY = y(STARTING_EQUITY);

  const points = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1];
  const color = last >= STARTING_EQUITY ? "var(--green)" : "var(--red)";
  const first = series[0], lastPt = series[series.length - 1];
  const summary = `Equity curve from ${first.label} (${money(vals[0])}) to ${lastPt.label} (${money(last)}), ${series.length} snapshots`;

  return `
    <div class="card chart-card">
      <svg viewBox="0 0 ${W} ${H}" class="equity-chart" role="img" aria-label="${esc(summary)}">
        <line x1="${PAD}" y1="${baseY.toFixed(1)}" x2="${W - PAD}" y2="${baseY.toFixed(1)}"
              stroke="var(--text-faint)" stroke-width="1" stroke-dasharray="4 4"/>
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
      </svg>
      <div class="chart-labels">
        <span>${esc(first.label)} · ${money(vals[0])}</span>
        <span>${esc(lastPt.label)} · ${money(last)}</span>
      </div>
    </div>`;
}
