import { pairTrades, money, pctSigned, time, esc, openPositionDetail, relatedActivity } from "../calc.js";

let selectedKey = null; // ticker+created_at of the entry row, or null

export function render(el, data) {
  const { closed, open } = pairTrades(data.trades);
  const snap = data.snapshots?.[0] ?? null;
  const all = [
    ...open.map(t => ({ kind: "open", entry: t, exit: null })),
    ...closed.map(c => ({ kind: "closed", entry: c.entry, exit: c.exit, pnl: c.pnl, pnlPct: c.pnlPct })),
  ].sort((a, b) => new Date(b.entry.created_at) - new Date(a.entry.created_at));

  if (!all.length) {
    el.innerHTML = `<div class="empty">No trades recorded yet for this ${data.mode === "backtest" ? "run" : "account"}.</div>`;
    return;
  }

  const selected = all.find(r => key(r.entry) === selectedKey) || null;

  const rows = all.map(r => {
    const t = r.entry;
    let current, pnl, pnlPct, daysHeld, exitReason;

    if (r.kind === "closed") {
      current = r.exit.fill_price;
      pnl = r.pnl;
      pnlPct = r.pnlPct;
      daysHeld = dayDelta(t.created_at, r.exit.created_at);
      exitReason = esc((r.exit.strategy_notes || "").replace("Mechanical exit: ", ""));
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
    }

    return `<tr data-key="${esc(key(t))}" class="responsive">
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Status"><span class="status-tag ${r.kind}">${r.kind}</span></td>
      <td data-label="Entry">${money(t.fill_price)}</td>
      <td data-label="${r.kind === "closed" ? "Exit" : "Current"}">${money(current)}</td>
      <td data-label="Qty">${t.fill_quantity ?? "—"}</td>
      <td data-label="P&L" class="${pnl == null ? "" : pnl >= 0 ? "pos" : "neg"}">${pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${money(pnl)} (${pctSigned(pnlPct)})`}</td>
      <td data-label="Stop">${t.stop_price != null ? money(t.stop_price) : "—"}</td>
      <td data-label="Target">${t.target_price != null ? money(t.target_price) : "—"}</td>
      <td data-label="Days held">${daysHeld == null ? "—" : daysHeld}</td>
      <td data-label="Exit reason">${exitReason ?? "—"}</td>
    </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="section-title">Trade ledger</div>
    <div class="panel"><table class="responsive">
      <thead><tr><th>Ticker</th><th>Status</th><th>Entry</th><th>Exit/Current</th><th>Qty</th><th>P&L</th><th>Stop</th><th>Target</th><th>Days held</th><th>Exit reason</th></tr></thead>
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
        ${step("Observations", related.length > 0)}
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
      <p style="font-size:11px;color:var(--text-faint);margin-top:12px">
        ${related.length
          ? `${related.length} activity record${related.length === 1 ? "" : "s"} matched by ticker/time window (best-effort, not a real join).`
          : "No matching activity record found (best-effort ticker/time-window match)."}
      </p>
      <p style="font-size:11px;color:var(--text-faint);margin-top:6px">
        The repository does not yet store a reliable per-trade intraday price path, so no price-path chart is shown here — see HANDOFF.md §8.
      </p>
      <button class="close-detail" type="button">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => {
    selectedKey = null; el.innerHTML = "";
  });
}
