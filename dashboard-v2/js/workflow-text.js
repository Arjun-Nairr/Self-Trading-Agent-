// Static, public-safe reference text for the Workflow page's expandable
// details. No fetch, no DOM, no identities. The Laya strings are copied
// EXACTLY from backtest-harness/laya_adapter.py and laya_cards.py;
// test/workflow.test.mjs fails if they drift.

export const LAYA = {
  questionFormat: "noul-criteria-v2",
  package: "laya 0.3.23",
  checkpoint: "convaiinnovations/laya @ 55cf4c4e",
  question: "Is this identity-free numeric card worth spending the existing full technical/news review on today?",
  criteriaTrue: "yes: the numbers show a notable technical situation today -- a large recent move, price stretched well away from its 20-day average or range, an extreme RSI, a strong MACD swing, or unusual volume -- worth a full technical and news review",
  criteriaFalse: "no: the numbers look ordinary today -- a small recent move, price near its 20-day average and mid-range, RSI near 50, a flat MACD, normal volume -- not worth a full review",
  selection: "Rank by review_probability (descending), break exact ties with a seeded hash of the opaque row ID, select up to 10. No threshold; answer_confidence is logged only and never used to select.",
  meaning: "review_probability is the model's P(true) for the versioned question below — a review-priority signal from an uncalibrated model. It is not a probability of profit, not a buy signal, and not how well Hermes will trade the name.",
};

// The seven numeric inputs (laya_cards.compute_card_inputs). The model sees
// only these numbers under an opaque row ID — never a ticker, name, date or text.
export const LAYA_FIELDS = [
  ["five_day_return", "Close today ÷ close 5 sessions ago − 1"],
  ["sma20_distance", "Close ÷ 20-day simple moving average − 1"],
  ["rsi14", "Wilder RSI over 14 sessions (0–100)"],
  ["macd_direction", "tanh(MACD(12,26,9) histogram ÷ close ÷ 0.005), −1 to 1"],
  ["relative_volume", "Today's volume ÷ mean of the 20 prior sessions"],
  ["realized_volatility", "Stdev of the last 20 daily log returns × √252"],
  ["range_position_20d", "Close's position inside the 20-day low–high range (0–1)"],
];

export const TOOLS = {
  technicals: "maverick-mcp — daily technicals as of the simulated close (RSI, MACD, support/resistance, trend)",
  news: "alpaca-mcp — historical headlines published before the decision cutoff",
  prefill: "alpaca-mcp — headlines between the decision cutoff and the 09:45 ET fill; a Hermes check runs only if any exist",
};

// Stage explanations (who does what, in the order the controller runs it).
export const STAGE_HELP = {
  execution: "At the simulated open the controller re-runs the risk checker at the real fill price (position cap, reward:risk, cash, timing).",
  fill_outcome: "The execution simulator fills a passing order at the real bar; a failing one is rejected. Each outcome is journaled.",
  positions: "Held positions are monitored through the simulated session — independently of Laya's shortlist.",
  exit: "Mechanical exits (stop, target, time stop) and filled SELL orders close positions; each exit is journaled.",
  population: "After the close: the run's frozen universe minus names already held or with a pending order.",
  shortlist: "The controller keeps up to 10 names by review_probability. Missing data never pads the list.",
  hermes: "Hermes receives the masked rules, account, positions and candidates — no tools, no Laya scores or ranks — and returns structured decisions.",
  decision_risk: "Each proposal is checked against the rules (watchlist, sizing, reward:risk, one order per name). Only passing proposals are queued.",
};

export const OWNER_NOTE = "In backtests the controller fetches all data, runs every check and journals results; Hermes only receives masked context and returns decisions.";
