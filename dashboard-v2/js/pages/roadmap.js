import { esc } from "../calc.js";
import { ROADMAP_SECTORS, STATUS_MARKS, STATUS_LABELS } from "../roadmap-data.js";

// Fast visual checklist -- not documentation. Detailed reasoning for any
// item here stays in docs/PROJECT_BACKLOG.md / HANDOFF.md, never
// duplicated on this page. Makes ZERO Neon / Data API calls -- its only
// import is the static roadmap module. No counts, percentages, tooltips,
// or accordions; every item is one line, always visible.

const STATUSES = ["done", "active", "next", "later"];

function legend() {
  return `<div class="rm-legend" role="note" aria-label="What each mark means">
    ${STATUSES.map(s => `<span class="rm-legend-item"><span class="rm-mark ${s}" aria-hidden="true">${STATUS_MARKS[s]}</span>${esc(STATUS_LABELS[s])}</span>`).join("")}
  </div>`;
}

function item(it) {
  return `<li class="rm-item">
    <span class="rm-mark ${it.status}" aria-hidden="true">${STATUS_MARKS[it.status]}</span>
    <span class="rm-text"><span class="sr-only">${esc(STATUS_LABELS[it.status])}: </span>${esc(it.text)}</span>
  </li>`;
}

function sector(s) {
  return `<section class="rm-sector">
    <h3 class="rm-sector-title">${esc(s.name)}</h3>
    <ul class="rm-list">${s.items.map(item).join("")}</ul>
  </section>`;
}

// No page heading here: the active nav tab already says "Roadmap", and
// repeating it directly beneath was the same label twice.
export function render(el) {
  el.innerHTML = `
    ${legend()}
    <div class="rm-sectors">${ROADMAP_SECTORS.map(sector).join("")}</div>
  `;
}
