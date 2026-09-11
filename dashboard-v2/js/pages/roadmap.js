import { esc } from "../calc.js";
import { ROADMAP_INTRO, ROADMAP_NOTE, ROADMAP_SECTORS } from "../roadmap-data.js";

// Read-only visual projection of docs/PROJECT_BACKLOG.md. Makes ZERO
// Neon / Data API calls — its only import is the static roadmap module.
// Status marks are decorative <span>s (never form inputs) and nothing
// here is collapsed by default — this is a showcase, so the actual
// implementation story (what's done, not just what's planned) stays
// visible without a click.

// data key -> display lane. Renamed for the page only; roadmap-data.js
// keeps the same four keys (completed/active/next/deferred) so its
// shape — and the test that checks it — is unaffected.
const LANES = [
  { key: "completed", label: "Completed", cls: "done", mark: "✓" },
  { key: "active", label: "In discussion now", cls: "active", mark: "●" },
  { key: "next", label: "Queued next", cls: "next", mark: "○" },
  { key: "deferred", label: "Later / gated", cls: "later", mark: "–" },
];

const LEGEND = [
  { cls: "done", mark: "✓", label: "Completed", text: "shipped and verified" },
  { cls: "active", mark: "●", label: "In discussion now", text: "being actively decided" },
  { cls: "next", mark: "○", label: "Queued next", text: "ready to start, not yet begun" },
  { cls: "later", mark: "–", label: "Later / gated", text: "paused on a stated prerequisite" },
];

// "Later / gated" items are authored as "<what it is> — <why it's paused>"
// (see roadmap-data.js's own comment) — split that off to show the
// reason as its own short line rather than burying it in one sentence.
function splitReason(text) {
  const i = text.indexOf(" — ");
  return i === -1 ? { text, reason: null } : { text: text.slice(0, i), reason: text.slice(i + 3) };
}

function item(l, text) {
  if (l.cls !== "later") {
    return `<li class="rm-item"><span class="rm-mark ${l.cls}" aria-hidden="true">${l.mark}</span><span class="rm-text">${esc(text)}</span></li>`;
  }
  const { text: main, reason } = splitReason(text);
  return `<li class="rm-item">
    <span class="rm-mark ${l.cls}" aria-hidden="true">${l.mark}</span>
    <span class="rm-text">${esc(main)}${reason ? `<span class="rm-reason">${esc(reason)}</span>` : ""}</span>
  </li>`;
}

function lane(l, items) {
  const body = items.length
    ? `<ul class="rm-list">${items.map(it => item(l, it)).join("")}</ul>`
    : `<p class="rm-none">Nothing in this lane right now.</p>`;
  return `
    <div class="rm-lane" data-state="${l.cls}">
      <div class="rm-lane-head"><span class="rm-mark ${l.cls}" aria-hidden="true">${l.mark}</span>${l.label}<span class="rm-count">${items.length}</span></div>
      ${body}
    </div>`;
}

function legend() {
  return `
    <div class="rm-legend" role="note" aria-label="What the four roadmap states mean">
      ${LEGEND.map(g => `
        <span class="rm-legend-item">
          <span class="rm-mark ${g.cls}" aria-hidden="true">${g.mark}</span>
          <strong>${esc(g.label)}</strong> — ${esc(g.text)}
        </span>`).join("")}
    </div>`;
}

export function render(el) {
  const chapters = ROADMAP_SECTORS.map(s => `
    <section class="rm-chapter">
      <h3 class="rm-chapter-title"><span class="rm-num">${s.n}</span>${esc(s.name)}</h3>
      <p class="rm-mission">${esc(s.mission)}</p>
      <div class="rm-lanes">${LANES.map(l => lane(l, s[l.key])).join("")}</div>
    </section>`).join("");

  el.innerHTML = `
    <div class="section-title">Project roadmap</div>
    <p class="rm-intro">${esc(ROADMAP_INTRO)}</p>
    <p class="rm-note">${esc(ROADMAP_NOTE)}</p>
    ${legend()}
    <div class="rm-chapters">${chapters}</div>
  `;
}
