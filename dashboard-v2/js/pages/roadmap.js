import { esc } from "../calc.js";
import { ROADMAP_INTRO, ROADMAP_NOTE, ROADMAP_SECTORS } from "../roadmap-data.js";

// Read-only visual projection of docs/PROJECT_BACKLOG.md. Makes ZERO
// Neon / Data API calls — its only import is the static roadmap module.
// The status marks are decorative <span>s (never form inputs), so
// nothing on this page is clickable or editable.

const GROUPS = [
  { key: "completed", label: "Completed", cls: "done", mark: "✓", collapsible: true },
  { key: "active", label: "Active", cls: "active", mark: "●", collapsible: false },
  { key: "next", label: "Next", cls: "next", mark: "○", collapsible: false },
  { key: "deferred", label: "Deferred", cls: "deferred", mark: "–", collapsible: true },
];

function list(g, items) {
  if (!items.length) return `<p class="rm-none">None right now</p>`;
  return `<ul class="rm-list">${items.map(it => `
    <li class="rm-item">
      <span class="rm-mark ${g.cls}" aria-hidden="true">${g.mark}</span>
      <span class="rm-text">${esc(it)}</span>
    </li>`).join("")}</ul>`;
}

function group(g, items) {
  const head = `<span class="rm-mark ${g.cls}" aria-hidden="true">${g.mark}</span>${g.label}<span class="rm-count">${items.length}</span>`;
  return g.collapsible
    ? `<details class="rm-group"><summary class="rm-group-head">${head}</summary>${list(g, items)}</details>`
    : `<div class="rm-group"><div class="rm-group-head">${head}</div>${list(g, items)}</div>`;
}

export function render(el) {
  const cards = ROADMAP_SECTORS.map(s => `
    <section class="rm-card">
      <h3 class="rm-card-title"><span class="rm-num">${s.n}</span>${esc(s.name)}</h3>
      <p class="rm-mission">${esc(s.mission)}</p>
      <div class="rm-groups">${GROUPS.map(g => group(g, s[g.key])).join("")}</div>
    </section>`).join("");

  el.innerHTML = `
    <div class="section-title">Project roadmap</div>
    <p class="rm-intro">${esc(ROADMAP_INTRO)}</p>
    <p class="rm-note">${esc(ROADMAP_NOTE)}</p>
    <div class="rm-grid">${cards}</div>
  `;
}
