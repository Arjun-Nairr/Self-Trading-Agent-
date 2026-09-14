import {
  pairTrades, realizedPnl, unrealizedPnl, winRate, maxDrawdown, totalReturn,
  money, pctSigned, pctPlain, esc, STARTING_EQUITY, openPositionDetail, equityCurveSeries,
  runStatusView, isRunStatusDismissed, dismissRunStatus,
} from "../calc.js";

const STALE_MS = 24 * 3600_000; // live mode only — see render() below

function stat(label, value, cls = "", sub = "") {
  return `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${cls}">${value}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`;
}

// The two figures this page exists to report, directly on the page ground.
function hero(label, value, cls = "", sub = "") {
  return `<div><div class="hero-label">${label}</div><div class="hero-value ${cls}">${value}</div>${sub ? `<div class="hero-sub">${sub}</div>` : ""}</div>`;
}

function trainingRunLabel(runId, runs) {
  const short = `${runId.slice(0, 8)}…`;
  const meta = (runs || []).find(r => r.run_id === runId);
  return meta && meta.latest_day_label ? `${short} · ${meta.latest_day_label}` : short;
}

// Compact, closeable, near the top of Overview -- not a modal. Hidden
// entirely when there's nothing to show (no run_id, no classified
// training run, or dismissed this session) -- see runStatusView's own
// contract in calc.js for exactly what "classified training run" means.
function renderRunStatus(data, ctx) {
  if (data.mode !== "backtest" || !data.run_id) return "";
  if (isRunStatusDismissed(window.sessionStorage, data.run_id)) return "";
  const isTrainingRun = (ctx.runs || []).some(r => r.run_id === data.run_id);
  const view = runStatusView({
    statusRow: data.runStatus,
    isTrainingRun,
    runLabel: trainingRunLabel(data.run_id, ctx.runs),
  });
  if (!view) return "";
  return `
    <div class="run-status-banner ${view.kind}" title="Marked paused after 10 minutes without a progress update from the backtest harness">
      <span>${esc(view.label)}</span>
      <button type="button" class="close-run-status" data-run-id="${esc(data.run_id)}" aria-label="Dismiss for this session">×</button>
    </div>`;
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
    ${renderRunStatus(data, ctx || {})}
    ${staleBanner}
    <section>
      <div class="hero-pair">
        ${hero("Equity", snap ? money(snap.equity) : "—", "", snap ? `Cash ${money(snap.cash)}` : "")}
        ${hero("Total return", pctSigned(totRet), totRet >= 0 ? "pos" : "neg", `vs. ${money(STARTING_EQUITY)} start`)}
      </div>
      <div class="stats-grid six">
        ${stat("Realized P&L", money(rPnl), rPnl >= 0 ? "pos" : "neg", `${closed.length} closed`)}
        ${stat("Unrealized P&L", money(uPnl), uPnl >= 0 ? "pos" : "neg", `${open.length} open`)}
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

  const closeBtn = el.querySelector(".close-run-status");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      dismissRunStatus(window.sessionStorage, closeBtn.dataset.runId);
      render(el, data, ctx); // re-render immediately -- no need to wait for the next poll
    });
  }
}

// The detail each position shows is identical either way — only the
// layout differs. A handful of positions read as lines; a real book of
// them still needs the scannable columns of a table.
const COMPACT_POSITION_LIMIT = 3;

function renderPositions(open, snap) {
  if (!open.length) return `<div class="empty">No open positions.</div>`;
  return open.length <= COMPACT_POSITION_LIMIT
    ? renderPositionLines(open, snap)
    : renderPositionTable(open, snap);
}

// Ticker and P&L carry the line; entry/stop/target sit quietly beside them.
function renderPositionLines(open, snap) {
  const lines = open.map(t => {
    const d = openPositionDetail(t.ticker, snap);
    const current = d ? Number(d.current_price) : null;
    const upl = d ? Number(d.unrealized_pl) : null;
    const uplPct = d ? Number(d.unrealized_pl_pct) : null;
    const stop = d ? d.stop_price : t.stop_price;
    const target = d ? d.target_price : t.target_price;
    return `<div class="pos-line">
      <div class="pos-head">
        <span class="pos-ticker">${esc(t.ticker)}</span>
        <span class="pos-pnl ${upl == null ? "" : upl >= 0 ? "pos" : "neg"}">${
          upl == null ? "—" : `${upl >= 0 ? "+" : ""}${money(upl)} (${pctSigned(uplPct)})`
        }</span>
      </div>
      <div class="pos-meta">
        <span>${t.fill_quantity} sh</span>
        <span>Entry <b>${money(t.fill_price)}</b></span>
        <span>Current <b>${money(current)}</b></span>
        <span>Stop <b>${stop != null ? money(stop) : "—"}</b></span>
        <span>Target <b>${target != null ? money(target) : "—"}</b></span>
      </div>
    </div>`;
  }).join("");
  return `<div class="pos-lines">${lines}</div>`;
}

function renderPositionTable(open, snap) {
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
      <td data-label="Qty" class="num">${t.fill_quantity}</td>
      <td data-label="Entry" class="num">${money(t.fill_price)}</td>
      <td data-label="Current" class="num">${money(current)}</td>
      <td data-label="P&L" class="num cell-primary ${upl == null ? "" : upl >= 0 ? "pos" : "neg"}">${upl == null ? "—" : `${upl >= 0 ? "+" : ""}${money(upl)} (${pctSigned(uplPct)})`}</td>
      <td data-label="Stop" class="num">${d ? money(d.stop_price) : (t.stop_price != null ? money(t.stop_price) : "—")}</td>
      <td data-label="Target" class="num">${d ? money(d.target_price) : (t.target_price != null ? money(t.target_price) : "—")}</td>
    </tr>`;
  }).join("");

  return `<div class="panel"><table class="responsive">
    <thead><tr><th>Ticker</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Current</th><th class="num">P&L</th><th class="num">Stop</th><th class="num">Target</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

// Vanilla SVG line chart, built only from stored portfolio_snapshots (no
// market data, no per-trade path). Colors reference CSS custom properties
// so the chart stays readable in both themes without a redraw.
//
// preserveAspectRatio="none" lets the SVG's geometry (line/area/gridlines)
// stretch to the card's full width at every breakpoint instead of the old
// fixed-aspect-ratio letterboxing. That non-uniform
// scaling would distort SVG <text>, so every human-readable label lives
// outside the SVG as plain HTML, absolutely positioned over a CSS grid
// using the exact same x()/y() fractions the SVG geometry uses — the
// labels stay pixel-aligned with their chart level without ever passing
// through the SVG's own coordinate transform.
function renderChart(snapshots) {
  const series = equityCurveSeries(snapshots);

  if (series.length < 2) {
    const msg = series.length === 0
      ? "No snapshots yet for this run."
      : "One snapshot so far — the chart needs at least two to draw a line.";
    return `<div class="empty">${msg}</div>`;
  }

  const vals = series.map(s => s.equity);
  const dataLo = Math.min(...vals), dataHi = Math.max(...vals);
  const lo = Math.min(dataLo, STARTING_EQUITY), hi = Math.max(dataHi, STARTING_EQUITY);
  const pad = (hi - lo) * 0.15 || 500;
  const min = lo - pad, max = hi + pad, range = max - min || 1;

  // Plain 0-100 coordinate space — with no text inside the SVG any more,
  // the exact viewBox size no longer matters, only the fractions below do.
  const W = 100, H = 100, PADX = 1.5, PADY = 6;
  const plotW = W - PADX * 2, plotH = H - PADY * 2;
  const x = i => PADX + (i / (series.length - 1)) * plotW;
  const y = v => PADY + (1 - (v - min) / range) * plotH;
  const plotBottom = H - PADY;

  const points = vals.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const last = vals[vals.length - 1];
  const positive = last >= STARTING_EQUITY;
  const lineColor = positive ? "var(--green)" : "var(--red)";
  // A wash tuned per theme, not a *-dim surface token — the curve has to
  // carry the same visual weight in light and dark.
  const fillColor = positive ? "var(--chart-pos-fill)" : "var(--chart-neg-fill)";
  const areaPoints = `${x(0).toFixed(2)},${plotBottom.toFixed(2)} ${points} ${x(series.length - 1).toFixed(2)},${plotBottom.toFixed(2)}`;
  // Dotted starting-equity baseline: the one reference every point on this
  // curve is read against. Drawn from the same y() the geometry uses.
  const baselineY = y(STARTING_EQUITY).toFixed(2);
  const baselineSvg = `<line x1="0" y1="${baselineY}" x2="${W}" y2="${baselineY}" stroke="var(--text-faint)" stroke-width="0.3" stroke-dasharray="1.5 1.5" vector-effect="non-scaling-stroke"/>`;

  const first = series[0], lastPt = series[series.length - 1];
  const midIdx = Math.floor((series.length - 1) / 2);
  const xTicks = [
    { pct: x(0), label: first.label, anchor: "start" },
    { pct: x(midIdx), label: series[midIdx].label, anchor: "middle" },
    { pct: x(series.length - 1), label: lastPt.label, anchor: "end" },
  ];

  // Y-axis: the actual lowest and highest equity this series reached, plus
  // the starting-equity benchmark — deduplicated by plotted pixel position
  // (not raw value) so near-identical levels never render as overlapping
  // labels.
  const yWhole = v => "$" + Math.round(v).toLocaleString();
  const yCandidates = [...new Set([dataLo, STARTING_EQUITY, dataHi])].sort((a, b) => a - b);
  const yTicks = [];
  for (const v of yCandidates) {
    if (yTicks.length && Math.abs(y(v) - y(yTicks[yTicks.length - 1])) < 8) continue;
    yTicks.push(v);
  }

  const summary = `Equity curve from ${first.label} (${money(vals[0])}) to ${lastPt.label} (${money(last)}), ` +
    `${series.length} snapshots. Low ${money(dataLo)}, high ${money(dataHi)}, starting equity ${money(STARTING_EQUITY)}.`;

  const gridSvg = yTicks.map(v =>
    `<line x1="0" y1="${y(v).toFixed(2)}" x2="${W}" y2="${y(v).toFixed(2)}" stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke"/>`).join("");

  const yAxisHtml = yTicks.map(v =>
    `<span class="chart-y-tick" style="top:${y(v).toFixed(2)}%">${esc(yWhole(v))}</span>`).join("");

  const xAxisHtml = xTicks.map(t =>
    `<span class="chart-x-tick" data-anchor="${t.anchor}" style="left:${t.pct.toFixed(2)}%">${esc(t.label)}</span>`).join("");

  // Final point, emphasised and labelled with the value it reaches.
  // Positioned in HTML on the same fractions the SVG geometry uses --
  // preserveAspectRatio="none" would squash a real <circle>.
  const endX = x(series.length - 1).toFixed(2);
  const endY = y(last).toFixed(2);
  const sign = positive ? "pos" : "neg";
  const endpointHtml =
    `<span class="chart-endpoint-dot ${sign}" style="left:${endX}%;top:${endY}%"></span>` +
    `<span class="chart-endpoint ${sign}" style="left:${endX}%;top:${endY}%">${esc(money(last))}</span>`;

  return `
    <div class="chart-block">
      <div class="chart-grid">
        <div class="chart-y-axis">${yAxisHtml}</div>
        <div class="chart-plot">
          <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="equity-chart" role="img" aria-label="${esc(summary)}">
            ${gridSvg}
            <polygon points="${areaPoints}" fill="${fillColor}" stroke="none"/>
            ${baselineSvg}
            <polyline points="${points}" fill="none" stroke="${lineColor}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
          </svg>
          ${endpointHtml}
        </div>
        <div class="chart-x-axis">${xAxisHtml}</div>
      </div>
    </div>`;
}
