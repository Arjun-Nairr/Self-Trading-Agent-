// Static, public-safe data for the Roadmap page -- a fast visual
// checklist, not documentation. No Neon, no fetch, no secrets/URLs/run
// IDs/endpoints -- this module is the page's ONLY data source.
//
// One short label per item, nothing else: no mission text, no per-item
// reasoning, no counts, no percentages. Detailed reasoning for any item
// here stays in docs/PROJECT_BACKLOG.md and HANDOFF.md -- this file is a
// deliberately terse summary of it, never a competing source of truth,
// and never grows a "why" field.
//
// Status meanings, fixed, never redefined per item:
//   done    completed
//   active  active now
//   next    next
//   later   later / gated

export const STATUS_MARKS = { done: "✓", active: "•", next: "○", later: "—" };
export const STATUS_LABELS = { done: "Completed", active: "Active now", next: "Next", later: "Later / gated" };

// "done" means built and tested; whether something is proven USEFUL is a
// separate item (e.g. "Laya trading benefit"), never implied by "done".
// Refreshed 2026-10-04 against docs/PROJECT_BACKLOG.md and the dated
// records in docs/evaluations/.
export const ROADMAP_SECTORS = [
  {
    name: "Context & Memory",
    items: [
      { status: "done", text: "Rules system" },
      { status: "done", text: "Decision reflections" },
      { status: "next", text: "Reflection-impact audit" },
    ],
  },
  {
    name: "Safety & Controls",
    items: [
      { status: "done", text: "Risk gates" },
      { status: "done", text: "News veto" },
      { status: "next", text: "Dashboard access review" },
      { status: "later", text: "Live-paper readiness" },
    ],
  },
  {
    name: "Market Intelligence",
    items: [
      { status: "done", text: "News analysis" },
      { status: "done", text: "Fundamentals data (SEC filings)" },
      { status: "done", text: "Laya selector (smoke-tested)" },
      { status: "active", text: "Scalable screener (live dry run)" },
      { status: "next", text: "Laya vs. control comparison" },
      { status: "later", text: "Laya trading benefit" },
      { status: "later", text: "Historical scalable screening" },
      { status: "later", text: "Reddit sentiment" },
    ],
  },
  {
    name: "Trading Strategy",
    items: [
      { status: "done", text: "Technical analysis (price/chart signals)" },
      { status: "later", text: "Advanced trading setups" },
      { status: "later", text: "Technical ML model" },
    ],
  },
  {
    name: "Testing & Optimization",
    items: [
      { status: "done", text: "Smoke-test separation" },
      { status: "done", text: "Regime-diverse evaluation (baseline v1)" },
      { status: "done", text: "Baseline v2 run + v1/v2 comparison" },
      { status: "later", text: "Fundamentals impact measurement" },
      { status: "later", text: "Variable + synergy optimization" },
      { status: "later", text: "Bayesian optimization" },
    ],
  },
  {
    name: "Dashboard & Observability",
    items: [
      { status: "done", text: "Trade viewer" },
      { status: "done", text: "Per-trade journeys" },
      { status: "done", text: "Mobile dashboard" },
      { status: "done", text: "Workflow page + live progress" },
      { status: "done", text: "Workflow agent diagram" },
      { status: "next", text: "V2 cutover decision" },
    ],
  },
];
