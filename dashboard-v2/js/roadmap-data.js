// Static, public-safe projection of docs/PROJECT_BACKLOG.md for the
// Roadmap page. No Neon, no fetch, no secrets/URLs/run IDs/endpoints —
// this module is the page's ONLY data source. docs/PROJECT_BACKLOG.md
// remains the detailed, canonical roadmap; this is a deliberately
// concise summary of it, never a competing editable source of truth.
//
// Wording rule: understandable to a recruiter, and it must not claim the
// trading agent is profitable, live, autonomous with real money, or
// "learning" where that has not been shown. No invented metrics or
// completion percentages.
//
// Every `deferred` ("Later / gated") item ends with " — <reason>" —
// pages/roadmap.js splits on that separator to show a short, stated
// pause reason next to the item, per this page's own design rule that
// nothing sits in that lane without saying why.

export const ROADMAP_INTRO =
  "A concise snapshot of the six engineering areas of this project. It is a " +
  "paper-trading research system: the agent has never traded real money, and " +
  "how good its decisions are is still being measured.";

export const ROADMAP_NOTE =
  "Several areas are deliberately paused while the current results baseline is " +
  "frozen for measurement. The detailed roadmap is maintained in the project repository.";

export const ROADMAP_SECTORS = [
  {
    n: 1,
    name: "Orchestration, Context & Memory",
    mission:
      "Keep the agent's rule and context packaging and its reflection mechanism honest — never mistake “received a note” for “learned something”.",
    completed: [
      "Full rule set re-read on every decision cycle — no partial-rules shortcut",
      "Sentiment can only veto a trade, never trigger one (this is also the prompt-injection defense)",
      "Activity-escalation is deterministic and controlled by the harness, never the agent's own choice",
      "Recent reflection notes are shown to the agent each cycle; a fresh one is required periodically",
    ],
    active: [],
    next: [
      "Audit whether past reflections measurably changed later decisions, not just that they were seen",
    ],
    deferred: [
      "Split the rules document into always-on core rules plus optional reference material, chosen by the harness (never by the agent) — Needs the baseline to unfreeze first.",
    ],
  },
  {
    n: 2,
    name: "Market Intelligence & Data",
    mission:
      "Widen the evidence the agent can see without letting any new source become a hidden buy signal.",
    completed: [
      "Technical indicators (RSI, MACD, trend, support and resistance) as entry evidence",
      "News used only as a veto or exit signal, never a standalone reason to buy",
      "A fixed, known trading universe",
    ],
    active: [],
    next: [],
    deferred: [
      "Company fundamentals from public filings (statements, liabilities, debt) with point-in-time filing dates — Needs the frozen-baseline evaluation decision first.",
      "Cache-first fundamentals lookup: check stored data first, fetch only when missing or stale — Needs the frozen-baseline evaluation decision first.",
      "Optional multi-day observation of a company before entry when there is no urgency — Still under discussion, not yet scoped.",
      "Defined rules for combining any future evidence source, so positive sentiment alone can never create a buy — Waiting on a concrete proposal to add a new source.",
      "Broader, controlled opportunity screening beyond the fixed universe — Still under discussion, not yet scoped.",
      "Pre-fetching and caching news ahead of the decision window — Still under discussion, not yet scoped.",
      "RAG-backed retrieval over a large, date-gated corpus of raw filings, earnings materials and later news: conditional document retrieval, not a replacement for normalized financial metrics — Needs fundamentals ingestion to exist first.",
      "A point-in-time dynamic candidate selector to replace the fixed watchlist for live trading, narrowing attention only and never judging a trade itself — Needs the frozen-baseline evaluation decision first.",
    ],
  },
  {
    n: 3,
    name: "Execution, Risk & Security",
    mission:
      "Every money-affecting decision passes a deterministic, fail-closed gate before it can execute.",
    completed: [
      "A deterministic risk checker enforces every numeric rule in code, independent of the model",
      "Every proposal is validated twice: once at decision time, once against real next-day prices",
      "Whole-position exit model; hard prohibitions enforced (no averaging down, no widening stops, no off-universe trades, and more)",
      "The public dashboard preview is read-only; sensitive per-cycle evidence is excluded from what it can read",
    ],
    active: [
      "Reviewing whether the read-only public dashboard needs access controls before any wider hosting",
    ],
    next: [],
    deferred: [
      "Broker-side protective (bracket / OCO) exit orders for live paper trading — Required before live-paper activation.",
      "Safe retry handling after an ambiguous order result, so an order is never duplicated — Required before live-paper activation.",
      "Partial-fill handling for live paper trading — Required before live-paper activation.",
      "A deterministic news-verification gate for new live entries, so execution fails closed on a missing or stale news check — Required before live-paper activation.",
    ],
  },
  {
    n: 4,
    name: "Evidence, Evaluation & Optimization",
    mission:
      "Never treat a small or ambiguous sample as proof, and never optimize before measurement is trustworthy.",
    completed: [
      "Durable run classification — every run is tagged training or smoke before it can start",
      "Full per-cycle evidence capture for the most recent run segment",
      "Offline benchmark and sensitivity comparison against passive baselines",
    ],
    active: [
      "Deciding the next measurement step: current evidence is enough, run another unchanged batch, or improve measurement first (current recommendation: improve measurement first)",
    ],
    next: [],
    deferred: [
      "Review the current aggressive rule settings against real completed-trade evidence — Needs the frozen-baseline evaluation decision first.",
      "Richer per-trade measurement capture during a future run — Needs the frozen-baseline evaluation decision first.",
      "Reduce survivorship bias with point-in-time universe construction — Needs the frozen-baseline evaluation decision first.",
      "A separate evaluation agent that reviews completed cycles and proposes improvements, advisory only — Still under discussion, not yet scoped.",
      "A regime-diverse sampled evaluation suite across rallies, selloffs, and sideways markets, as a prerequisite for any future optimization — Needs the frozen-baseline evaluation decision first.",
    ],
  },
  {
    n: 5,
    name: "Platform, Reliability & Operations",
    mission:
      "Keep the containerized stack and its database durable and observable without expanding scope during a healthy run.",
    completed: [
      "Isolated tool containers (no container can reach another), pinned images",
      "One durable database of record, with idempotent writes and per-cycle checkpoints",
    ],
    active: [],
    next: [
      "Confirm a finite outer timeout on external data fetches",
      "Preserve failure details when a run cycle fails, for diagnosis",
    ],
    deferred: [
      "A scheduler for continuous paper trading after the training phase — Live mode has never run; needs its own design first.",
      "Hosting the trading stack on an always-on machine — Needs a hosting, credential-handling, and persistence plan first.",
      "Hosting the dashboard separately from the host machine — Needs the access-control review above first.",
    ],
  },
  {
    n: 6,
    name: "Interface, Learning & Communication",
    mission:
      "Make the project's real state legible — on a phone, in the architecture vault, and to people outside the project — without overstating what the data shows.",
    completed: [
      "Dashboard V2: four data pages plus this roadmap, responsive from desktop to small phones, light and dark themes",
      "The Decisions page shows the ten most recent decisions for a run",
      "Full-width equity chart built only from stored snapshots, with honest axes",
      "Plain-English rejection labels, a compact recent-trade ledger, and a rules-synced strategy summary",
      "An architecture vault for engineering ground truth, plus a public-safe architecture diagram",
    ],
    active: [
      "Collecting real mobile feedback on Dashboard V2 from a public preview",
    ],
    next: [
      "Decide whether Dashboard V2 replaces the original dashboard",
    ],
    deferred: [
      "A single-trade detail view: entry to current to exit, reasoning, and invalidation condition in one place — Needs the V2 cutover decision first.",
      "A per-trade progress chart toward stop or target — Needs richer per-trade price data first.",
      "Additional recruiter and presentation material beyond the current architecture diagram — Still under discussion, not yet scoped.",
    ],
  },
];
