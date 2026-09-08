import { pairTrades, money, pctSigned, time, esc } from "../calc.js";

let selectedKey = null; // ticker+created_at of the entry row, or null

export function render(el, data) {
  const { closed, open } = pairTrades(data.trades);
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
    const current = r.kind === "closed" ? r.exit.fill_price : t.fill_price;
    const pnl = r.kind === "closed" ? r.pnl : null;
    const daysHeld = r.kind === "closed"
      ? dayDelta(t.created_at, r.exit.created_at)
      : dayDelta(t.created_at, new Date().toISOString());
    return `<tr data-key="${esc(key(t))}" class="responsive">
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Status"><span class="status-tag ${r.kind}">${r.kind}</span></td>
      <td data-label="Entry">${money(t.fill_price)}</td>
      <td data-label="${r.kind === "closed" ? "Exit" : "Current"}">${money(current)}</td>
      <td data-label="Qty">${t.fill_quantity ?? "—"}</td>
      <td data-label="P&L" class="${pnl == null ? "" : pnl >= 0 ? "pos" : "neg"}">${pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${money(pnl)} (${pctSigned(r.pnlPct)})`}</td>
      <td data-label="Stop">${t.stop_price != null ? money(t.stop_price) : "—"}</td>
      <td data-label="Target">${t.target_price != null ? money(t.target_price) : "—"}</td>
      <td data-label="Days held">${daysHeld}</td>
      <td data-label="Exit reason">${r.kind === "closed" ? esc((r.exit.strategy_notes || "").replace("Mechanical exit: ", "")) : "—"}</td>
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

  if (selected) renderDetail(el.querySelector("#trade-detail"), selected);
}

function key(t) { return `${t.ticker}__${t.created_at}`; }

function dayDelta(fromIso, toIso) {
  const ms = new Date(toIso) - new Date(fromIso);
  return Math.max(0, Math.round(ms / (24 * 3600_000)));
}

function renderDetail(el, r) {
  const t = r.entry;
  const step = (label, done) => `<span class="step ${done ? "done" : ""}">${label}</span>`;
  el.innerHTML = `
    <div class="detail-panel">
      <h3>${esc(t.ticker)} — ${r.kind === "closed" ? "closed position" : "open position"}</h3>
      <div class="lifecycle">
        ${step("Decision", true)}
        ${step("Risk validation", t.status !== "rejected")}
        ${step("Fill", t.status === "filled")}
        ${step("Observations", true)}
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
        The repository does not yet store a reliable per-trade intraday price path, so no price-path chart is shown here — see HANDOFF.md §8.
      </p>
      <button class="close-detail" type="button">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => {
    selectedKey = null; el.innerHTML = "";
  });
}
