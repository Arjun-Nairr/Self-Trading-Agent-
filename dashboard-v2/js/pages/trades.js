import {
  money, pctSigned, time, esc, openPositionDetail, relatedActivity, latestPositions,
  pairTrades, winRate, realizedPnl,
} from "../calc.js";

let selectedKey = null; // ticker+created_at of the entry row, or null
const LEDGER_LIMIT = 20;

export function render(el, data) {
  const snap = data.snapshots?.[0] ?? null;
  const { positions, total } = latestPositions(data.trades, LEDGER_LIMIT);

  if (!positions.length) {
    el.innerHTML = `<div class="empty">No trades recorded yet for this ${data.mode === "backtest" ? "run" : "account"}.</div>`;
    return;
  }

  const selected = positions.find(r => key(r.entry) === selectedKey) || null;
  const title = total > LEDGER_LIMIT ? `Latest ${LEDGER_LIMIT} positions` : "Trade ledger";

  const rows = positions.map(r => {
    const t = r.entry;
    let current, pnl, pnlPct, daysHeld, exitReason, exitReasonText;

    if (r.kind === "closed") {
      current = r.exit.fill_price;
      pnl = r.pnl;
      pnlPct = r.pnlPct;
      daysHeld = dayDelta(t.created_at, r.exit.created_at);
      // Clamped to one line on desktop (see td.reason); the full text stays
      // here as the cell's own tooltip and, in full, in the detail panel.
      exitReasonText = (r.exit.strategy_notes || "").replace("Mechanical exit: ", "");
      exitReason = esc(exitReasonText);
    } else {
      // Open position: current price and unrealized P&L come ONLY from the
      // latest scoped snapshot's own open_positions_detail — never the
      // entry fill price relabeled as "current".
      const d = openPositionDetail(t.ticker, snap);
      current = d ? Number(d.current_price) : null;
      pnl = d ? Number(d.unrealized_pl) : null;
      pnlPct = d ? Number(d.unrealized_pl_pct) : null;
      // Simulated days_held, when the harness stored one. A backtest's
      // created_at is real wall-clock time for a simulated trading day, so
      // "today minus entry" is meaningless there — never computed that
      // way. Live mode's created_at is a real timestamp, so a real-date
      // fallback is legitimate only there.
      if (d && d.days_held != null) daysHeld = d.days_held;
      else if (data.mode === "live") daysHeld = dayDelta(t.created_at, new Date().toISOString());
      else daysHeld = null;
      exitReason = null;
      exitReasonText = null;
    }

    // CLOSED is the ledger's default state and carries no emphasis; OPEN is
    // the exception worth a pill.
    const statusCell = r.kind === "open"
      ? `<span class="status-tag open">open</span>`
      : `<span class="status-quiet">closed</span>`;
    // A real 0 stays a real 0 (a same-session close genuinely held zero
    // simulated days) -- it is only de-emphasised. The em-dash stays what
    // it has always meant here: no stored value at all.
    const daysCell = daysHeld == null
      ? `<span class="muted-cell">—</span>`
      : (daysHeld === 0 ? `<span class="muted-cell">0</span>` : String(daysHeld));

    return `<tr data-key="${esc(key(t))}" class="responsive">
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Status">${statusCell}</td>
      <td data-label="P&L" class="num cell-primary ${pnl == null ? "" : pnl >= 0 ? "pos" : "neg"}">${pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${money(pnl)} (${pctSigned(pnlPct)})`}</td>
      <td data-label="Entry" class="num">${money(t.fill_price)}</td>
      <td data-label="${r.kind === "closed" ? "Exit" : "Current"}" class="num">${money(current)}</td>
      <td data-label="Qty" class="num">${t.fill_quantity ?? "—"}</td>
      <td data-label="Stop" class="num">${t.stop_price != null ? money(t.stop_price) : "—"}</td>
      <td data-label="Target" class="num">${t.target_price != null ? money(t.target_price) : "—"}</td>
      <td data-label="Days held" class="num">${daysCell}</td>
      <td data-label="Exit reason" class="reason" title="${esc(exitReasonText ?? "")}">${exitReason ?? "—"}</td>
    </tr>`;
  }).join("");

  // Conclusion first, from figures already computed for this run -- no new
  // query, no new metric definition.
  const { closed: closedAll } = pairTrades(data.trades);
  const wr = winRate(closedAll);
  const rPnl = realizedPnl(closedAll);
  const summary = `
    <div class="summary-line">
      <span>Closed positions <b>${closedAll.length}</b></span>
      <span>Win rate <b>${wr.n ? `${Math.round(wr.pct)}%` : "—"}</b>${wr.n ? ` (n=${wr.n})` : ""}</span>
      <span>Realized P&L <b class="${rPnl >= 0 ? "pos" : "neg"}">${money(rPnl)}</b></span>
    </div>`;

  el.innerHTML = `
    <div class="section-title">${esc(title)}</div>
    ${summary}
    <div class="panel"><table class="responsive">
      <thead><tr><th>Ticker</th><th>Status</th><th class="num">P&L</th><th class="num">Entry</th><th class="num">Exit/Current</th><th class="num">Qty</th><th class="num">Stop</th><th class="num">Target</th><th class="num">Days held</th><th>Exit reason</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div id="trade-detail"></div>
  `;

  el.querySelectorAll("tbody tr").forEach(tr => {
    tr.addEventListener("click", () => {
      selectedKey = selectedKey === tr.dataset.key ? null : tr.dataset.key;
      render(el, data);
    });
  });

  if (selected) renderDetail(el.querySelector("#trade-detail"), selected, data.activity);
}

function key(t) { return `${t.ticker}__${t.created_at}`; }

function dayDelta(fromIso, toIso) {
  const ms = new Date(toIso) - new Date(fromIso);
  return Math.max(0, Math.round(ms / (24 * 3600_000)));
}

function renderDetail(el, r, activity) {
  const t = r.entry;
  const related = relatedActivity(t, activity);
  const step = (label, done) => `<span class="step ${done ? "done" : ""}">${label}</span>`;
  el.innerHTML = `
    <div class="detail-panel">
      <h3>${esc(t.ticker)} — ${r.kind === "closed" ? "closed position" : "open position"}</h3>
      <div class="lifecycle">
        ${step("Decision", true)}
        ${step("Risk validation", t.status !== "rejected")}
        ${step("Fill", t.status === "filled")}
        ${step("Logged activity (best-effort)", related.length > 0)}
        ${step("Exit", r.kind === "closed")}
      </div>
      <div class="kv-list">
        <div class="kv"><span class="k">Primary signal</span><span class="v">${esc(t.primary_signal || "—")}</span></div>
        <div class="kv"><span class="k">Confidence</span><span class="v">${t.confidence ? `${t.confidence}/5` : "—"}</span></div>
        <div class="kv"><span class="k">Reward:risk (proposed)</span><span class="v">${t.expected_reward_risk ?? "—"}</span></div>
        <div class="kv"><span class="k">Invalidation condition</span><span class="v">${esc(t.invalidation_condition || "—")}</span></div>
        <div class="kv"><span class="k">Strategy profile</span><span class="v">${esc(t.strategy_profile || "—")}${t.min_reward_risk_used != null ? ` (min R:R ${t.min_reward_risk_used})` : ""}</span></div>
        <div class="kv"><span class="k">Proposed stop / target</span><span class="v">${t.stop_price != null ? money(t.stop_price) : "—"} / ${t.target_price != null ? money(t.target_price) : "—"}</span></div>
        <div class="kv"><span class="k">Filled price / qty</span><span class="v">${t.fill_price != null ? money(t.fill_price) : "not filled"} / ${t.fill_quantity ?? "—"}</span></div>
        <div class="kv"><span class="k">Reasoning</span><span class="v">${esc(t.strategy_notes || "—")}</span></div>
      </div>
      <p class="note">
        ${related.length
          ? `${related.length} activity record${related.length === 1 ? "" : "s"} matched by ticker/time window (best-effort, not a real join).`
          : "No matching activity record found (best-effort ticker/time-window match)."}
      </p>
      <p class="note">
        The repository does not yet store a reliable per-trade intraday price path, so no price-path chart is shown here — see HANDOFF.md §8.
      </p>
      <button class="close-detail" type="button">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => {
    selectedKey = null; el.innerHTML = "";
  });
}
