import { rejectionDistribution, time, esc } from "../calc.js";

let selectedKey = null;

function key(t) { return `${t.ticker}__${t.created_at}`; }

// Best-effort correlation only — trades rows carry no foreign key into
// agent_activity, so this matches by ticker symbol appearing in the
// description text within a loose time window of the decision. Never
// presented as an exact join; the detail panel says so explicitly.
function relatedActivity(t, activity) {
  const tCreated = new Date(t.created_at).getTime();
  return activity.filter(a => {
    if (!a.description || !a.description.includes(t.ticker)) return false;
    const dt = Math.abs(new Date(a.created_at).getTime() - tCreated);
    return dt < 48 * 3600_000;
  });
}

export function render(el, data) {
  const { trades, blockedActivity, activity } = data;
  if (!trades.length) {
    el.innerHTML = `<div class="empty">No decisions recorded yet for this ${data.mode === "backtest" ? "run" : "account"}.</div>`;
    return;
  }

  const funnel = {
    evaluated: trades.length,
    proposed: trades.filter(t => t.status !== "rejected" || t.stop_price != null).length,
    accepted: trades.filter(t => t.status === "filled" || t.status === "pending").length,
    filled: trades.filter(t => t.status === "filled").length,
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
    <div class="section-title">Funnel</div>
    <div class="stats-grid" style="grid-template-columns:repeat(4,1fr)">
      <div class="stat"><div class="stat-label">Evaluated</div><div class="stat-value">${funnel.evaluated}</div></div>
      <div class="stat"><div class="stat-label">Proposed</div><div class="stat-value">${funnel.proposed}</div></div>
      <div class="stat"><div class="stat-label">Accepted</div><div class="stat-value">${funnel.accepted}</div></div>
      <div class="stat"><div class="stat-label">Filled</div><div class="stat-value">${funnel.filled}</div></div>
    </div>

    <div class="section-title">Rejection reasons (by RULES.md section)</div>
    ${dist.length
      ? `<div class="card kv-list">${dist.map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`).join("")}</div>`
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
      ${evidenceRow("4. News-veto result", newsRow ? esc(newsRow.description) : "no news-veto activity matched to this decision", !newsRow)}
      ${evidenceRow("5. Risk-based sizing (§2a)", "final quantity only — per-decision sizing inputs are not individually stored; see filled qty below", true)}
      ${evidenceRow("6. Concentration-cap sizing (§2b)", "final quantity only — see note above", true)}
      ${evidenceRow("7. Available-cash check (§3)", "not individually stored per decision", true)}
      ${evidenceRow("8. Deterministic validation result", validation ? esc(validation.description) : (t.status === "filled" ? "passed — no risk-checker rejection logged for this decision" : "no matching risk-checker activity found"), !validation)}
      ${evidenceRow("Filled quantity", t.fill_quantity != null ? String(t.fill_quantity) : "not filled")}
      <button class="close-detail" type="button" style="margin-top:12px">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => { selectedKey = null; el.innerHTML = ""; });
}
