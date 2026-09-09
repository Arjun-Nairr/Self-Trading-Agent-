import { rejectionDistribution, relatedActivity, ruleLabel, time, esc } from "../calc.js";

let selectedKey = null;

function key(t) { return `${t.ticker}__${t.created_at}`; }

export function render(el, data) {
  const { trades, blockedActivity, activity } = data;
  if (!trades.length) {
    el.innerHTML = `<div class="empty">No decisions recorded yet for this ${data.mode === "backtest" ? "run" : "account"}.</div>`;
    return;
  }

  // Exact trade-row statuses only. Pre-journal decision-time rejections
  // never reach `trades` at all in this anonymous dashboard's data — the
  // full evaluated → proposed → accepted funnel needs cycle evidence,
  // which is intentionally out of scope here (see the note below).
  const outcomes = {
    total: trades.length,
    filled: trades.filter(t => t.status === "filled").length,
    rejected: trades.filter(t => t.status === "rejected").length,
    pending: trades.filter(t => t.status === "pending").length,
  };
  const dist = rejectionDistribution(blockedActivity);

  const selected = trades.find(t => key(t) === selectedKey) || null;

  const rows = [...trades].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).map(t => `
    <tr data-key="${esc(key(t))}" class="responsive">
      <td data-label="Time">${time(t.created_at)}</td>
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Action">${esc(t.action)}</td>
      <td data-label="Outcome"><span class="status-tag ${esc(t.status)}">${esc(t.status)}</span></td>
      <td data-label="Confidence">${t.confidence ? `${t.confidence}/5` : "—"}</td>
      <td data-label="Signal">${esc(t.primary_signal || "—")}</td>
    </tr>`).join("");

  el.innerHTML = `
    <div class="section-title">Journaled order outcomes</div>
    <div class="stats-grid">
      <div class="stat"><div class="stat-label">Total journaled</div><div class="stat-value">${outcomes.total}</div></div>
      <div class="stat"><div class="stat-label">Filled</div><div class="stat-value">${outcomes.filled}</div></div>
      <div class="stat"><div class="stat-label">Rejected</div><div class="stat-value">${outcomes.rejected}</div></div>
      <div class="stat"><div class="stat-label">Pending</div><div class="stat-value">${outcomes.pending}</div></div>
    </div>
    <p style="font-size:11px;color:var(--text-faint);margin:8px 0 0">This counts journaled trade records only. The full proposal funnel (evaluated → proposed → accepted) requires cycle evidence and is intentionally unavailable to this dashboard.</p>

    <div class="section-title">Rejection reasons</div>
    ${dist.length
      ? `<div class="card kv-list">${dist.map(([k, v]) => `<div class="kv"><span class="k">${esc(ruleLabel(k))}</span><span class="v">${v}</span></div>`).join("")}</div>`
      : `<div class="card empty" style="padding:16px">No blocked decisions recorded.</div>`}
    <p style="font-size:11px;color:var(--text-faint);margin:8px 0 0">News is never a positive BUY trigger in this system — it can only veto an otherwise technically-justified entry (RULES.md §5a).</p>

    <div class="section-title">Decisions</div>
    <div class="panel"><table class="responsive">
      <thead><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Outcome</th><th>Confidence</th><th>Signal</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div id="decision-detail"></div>
  `;

  el.querySelectorAll("tbody tr").forEach(tr => {
    tr.addEventListener("click", () => {
      selectedKey = selectedKey === tr.dataset.key ? null : tr.dataset.key;
      render(el, data);
    });
  });

  if (selected) renderDetail(el.querySelector("#decision-detail"), selected, activity);
}

function evidenceRow(label, value, muted = false) {
  return `<div class="evidence-step"><div class="k">${label}</div><div class="v ${muted ? "muted" : ""}">${value}</div></div>`;
}

const NO_MATCH = "no matching activity record found (best-effort ticker/time-window match)";

function renderDetail(el, t, activity) {
  const related = relatedActivity(t, activity);
  const newsRow = related.find(a => /§5a/.test(a.description || ""));
  const validation = related.find(a => a.event_type === "risk_check" || a.event_type === "order_rejected");

  el.innerHTML = `
    <div class="detail-panel">
      <h3>${esc(t.action)} ${esc(t.ticker)} — ${time(t.created_at)}</h3>
      ${evidenceRow("1. Technical evidence", esc(t.technical_basis || "not individually recorded for this decision"), !t.technical_basis)}
      ${evidenceRow("2. Proposed stop", t.stop_price != null ? `$${t.stop_price}` : "—", t.stop_price == null)}
      ${evidenceRow("3. Proposed target &amp; reward:risk", t.target_price != null ? `$${t.target_price} · R:R ${t.expected_reward_risk ?? "—"}` : "—", t.target_price == null)}
      ${evidenceRow("4. News-veto result", newsRow ? esc(newsRow.description) : NO_MATCH, !newsRow)}
      ${evidenceRow("5. Risk-based sizing (§2a)", "final quantity only — per-decision sizing inputs are not individually stored; see filled qty below", true)}
      ${evidenceRow("6. Concentration-cap sizing (§2b)", "final quantity only — see note above", true)}
      ${evidenceRow("7. Available-cash check (§3)", "not individually stored per decision", true)}
      ${evidenceRow("8. Deterministic validation result", validation ? esc(validation.description) : NO_MATCH, !validation)}
      ${evidenceRow("Filled quantity", t.fill_quantity != null ? String(t.fill_quantity) : "not filled")}
      <button class="close-detail" type="button" style="margin-top:12px">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => { selectedKey = null; el.innerHTML = ""; });
}
