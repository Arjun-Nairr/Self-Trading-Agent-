import { rejectionDistribution, relatedActivity, ruleLabel, time, esc } from "../calc.js";

let selectedKey = null;

function key(t) { return `${t.ticker}__${t.created_at}`; }

// The most frequent non-null value in a column, or null when the column is
// empty. Display-only: decides what to render quietly, never what to show.
function modeValue(values) {
  const counts = new Map();
  for (const v of values) {
    if (v == null) continue;
    counts.set(v, (counts.get(v) || 0) + 1);
  }
  let best = null, bestN = 0;
  for (const [v, n] of counts) if (n > bestN) { best = v; bestN = n; }
  return best;
}

// Five bars filled to the value. The textual "N/5" stays in the accessible
// name so the number itself is never lost, only its visual weight changes.
function confidenceMeter(confidence) {
  if (!confidence) return `<span class="muted-cell">—</span>`;
  const n = Number(confidence);
  const dots = Array.from({ length: 5 }, (_, i) =>
    `<span class="conf-dot ${i < n ? "on" : ""}"></span>`).join("");
  return `<span class="conf-meter" role="img" aria-label="Confidence ${n} of 5">${dots}</span>`
    + `<span class="sr-only">${n}/5</span>`;
}

export function render(el, data) {
  const { decisionRows, decisionCounts, blockedActivity, activity } = data;
  const rowsData = decisionRows || [];
  const counts = decisionCounts || { total: 0, filled: 0, rejected: 0, pending: 0 };

  if (!rowsData.length) {
    el.innerHTML = `<div class="empty">No decisions recorded yet for this ${data.mode === "backtest" ? "run" : "account"}.</div>`;
    return;
  }

  // Run-wide status totals come from bounded count-only queries in
  // data.js; the table below is the newest DECISIONS_LIMIT rows from a
  // bounded list query — the whole decision list is never loaded here.
  const dist = rejectionDistribution(blockedActivity);
  const selected = rowsData.find(t => key(t) === selectedKey) || null;

  // `technical` is the dominant value on every row today; keeping it quiet
  // means a genuinely different signal would stand out on sight later.
  const dominantSignal = modeValue(rowsData.map(t => t.primary_signal));

  const rows = rowsData.map(t => `
    <tr data-key="${esc(key(t))}" class="responsive ${selectedKey === key(t) ? "is-selected" : ""}" tabindex="0" aria-label="View ${esc(t.ticker)} decision details">
      <td data-label="Time" class="mono">${time(t.created_at)}</td>
      <td data-label="Ticker" class="ticker">${esc(t.ticker)}</td>
      <td data-label="Action">${esc(t.action)}</td>
      <td data-label="Outcome"><span class="status-tag ${esc(t.status)}">${esc(t.status)}</span></td>
      <td data-label="Confidence">${confidenceMeter(t.confidence)}</td>
      <td data-label="Signal" class="${t.primary_signal === dominantSignal ? "muted-cell" : ""}">${esc(t.primary_signal || "—")}</td>
    </tr>`).join("");

  el.innerHTML = `
    <div class="section-title">Journaled order outcomes</div>
    <div class="stats-grid">
      <div class="stat"><div class="stat-label">Total journaled</div><div class="stat-value">${counts.total}</div></div>
      <div class="stat"><div class="stat-label">Filled</div><div class="stat-value">${counts.filled}</div></div>
      <div class="stat"><div class="stat-label">Rejected</div><div class="stat-value">${counts.rejected}</div></div>
      <div class="stat"><div class="stat-label">Pending</div><div class="stat-value">${counts.pending}</div></div>
    </div>
    <p class="note">Run-wide counts of journaled trade records. The full proposal funnel (evaluated → proposed → accepted) requires cycle evidence and is intentionally unavailable to this dashboard.</p>

    <section class="band">
      <div class="section-title">Rejection reasons</div>
      ${renderRejections(dist)}
      <p class="note">News is never a positive BUY trigger in this system — it can only veto an otherwise technically-justified entry (RULES.md §5a).</p>
    </section>

    <section class="band">
      <div class="section-title">Recent decisions</div>
      <div class="panel"><table class="responsive">
        <thead><tr><th class="mono">Time</th><th>Ticker</th><th>Action</th><th>Outcome</th><th>Confidence</th><th>Signal</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="note">Showing the ${rowsData.length} most recent decision${rowsData.length === 1 ? "" : "s"} for this ${data.mode === "backtest" ? "run" : "account"}.</p>
    </section>
    <div id="decision-detail"></div>
  `;

  el.querySelectorAll("tbody tr").forEach(tr => {
    tr.addEventListener("keydown", e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      tr.click();
      const nextFocus = el.querySelector(".close-detail") ||
        Array.from(el.querySelectorAll("tr[data-key]")).find(row => row.dataset.key === tr.dataset.key);
      nextFocus?.focus();
    });
    tr.addEventListener("click", () => {
      selectedKey = selectedKey === tr.dataset.key ? null : tr.dataset.key;
      render(el, data);
    });
  });

  if (selected) renderDetail(el.querySelector("#decision-detail"), selected, activity);
}

// Each rule's bar is scaled against the largest count already in `dist`
// (rejectionDistribution returns it sorted descending). No new request, no
// invented score -- the bar length is the same number printed beside it.
function renderRejections(dist) {
  if (!dist.length) return `<div class="empty">No blocked decisions recorded.</div>`;
  const max = dist[0][1] || 1;
  const rows = dist.map(([rule, count]) => {
    const pct = Math.max(2, Math.round((count / max) * 100));
    return `<div class="rej-row">
      <span class="rej-label">${esc(ruleLabel(rule))}</span>
      <span class="rej-count">${count}</span>
      <span class="rej-bar" style="width:${pct}%" aria-hidden="true"></span>
    </div>`;
  }).join("");
  return `<div class="rej-list">${rows}</div>`;
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
      <button class="close-detail" type="button">Close</button>
    </div>
  `;
  el.querySelector(".close-detail").addEventListener("click", () => {
    const row = el.parentElement.querySelector("tr.is-selected");
    if (row) { row.classList.remove("is-selected"); row.focus(); }
    selectedKey = null; el.innerHTML = "";
  });
}
