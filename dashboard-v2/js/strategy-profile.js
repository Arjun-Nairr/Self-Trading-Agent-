// Display-only reflection of RULES.md's currently-locked Phase 1
// aggressive profile numbers. This module does NOT parse RULES.md at
// runtime — it is a small, hand-maintained mirror, exactly like
// risk_checker.py's own RiskLimits class (see RULES.md's own "Changing a
// number here? Change it in the risk checker too" section). Verified
// against the real file text by test/calc.test.mjs's dedicated check —
// change a number here only together with the same number in RULES.md.

export const STRATEGY_PROFILE = [
  { label: "Risk per trade", value: "2% of equity" },
  { label: "Maximum position", value: "20% of equity" },
  { label: "Portfolio exposure", value: "cash only · no margin" },
  { label: "Stop distance", value: "3%–12% below entry" },
  { label: "Reward:risk floor", value: "1.5:1 base · 1.25:1 if escalated" },
  { label: "Time stop", value: "5 trading days" },
];

export const CIRCUIT_BREAKER_NOTE =
  "Circuit breaker: new entries pause at −30% drawdown from peak equity.";
