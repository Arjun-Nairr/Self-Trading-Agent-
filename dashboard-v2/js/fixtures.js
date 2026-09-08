// Representative local fixtures — dev-only. Shaped exactly like
// data.loadRunData()'s return value so page renderers cannot tell the
// difference between a fixture bundle and a real one. Loaded only from
// dev/fixtures-preview.html, never from the shipped app shell (index.html
// never imports this file) — see the "fixtures must not remain exposed as
// a normal mode" requirement.

const RUN_ID = "fixture-run-0001";
const now = Date.now();
const hoursAgo = h => new Date(now - h * 3600_000).toISOString();

// ---- a populated backtest run: open + closed (winning and losing) trades,
// accepted and rejected decisions, a failed cycle, pending orders, and one
// row with missing optional values.
export const FIXTURE_BACKTEST = {
  run_id: RUN_ID,
  trades: [
    // Open position (BUY, filled, no matching SELL yet).
    {
      ticker: "ALPHA", action: "BUY", status: "filled", fill_price: 42.10, fill_quantity: 200,
      stop_price: 39.50, target_price: 47.80, confidence: 3, primary_signal: "technical",
      technical_basis: "breakout above resistance", expected_reward_risk: 2.2,
      invalidation_condition: "closes back below $40.50", strategy_notes: "Clean breakout, volume confirmed.",
      strategy_profile: "aggressive_v1_base", min_reward_risk_used: 1.5,
      created_at: hoursAgo(6), execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 41",
    },
    // Closed winning trade (BUY + SELL pair). data.js always queries
    // order=created_at.desc, and pairTrades relies on that — within each
    // ticker's own rows here, newest (the SELL) must come first.
    {
      ticker: "BRAVO", action: "SELL", status: "filled", fill_price: 20.55, fill_quantity: 500,
      confidence: null, primary_signal: null, expected_reward_risk: 0.0,
      strategy_notes: "Mechanical exit: target hit.", created_at: hoursAgo(20),
      execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 40",
    },
    {
      ticker: "BRAVO", action: "BUY", status: "filled", fill_price: 18.00, fill_quantity: 500,
      stop_price: 16.80, target_price: 20.60, confidence: 4, primary_signal: "technical",
      technical_basis: "MACD crossover", expected_reward_risk: 2.17,
      invalidation_condition: "closes back below $17.20", strategy_notes: "Strong trend confirmation.",
      created_at: hoursAgo(48), execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 38",
    },
    // Closed losing trade — same newest-first ordering.
    {
      ticker: "CHARLIE", action: "SELL", status: "filled", fill_price: 51.00, fill_quantity: 90,
      expected_reward_risk: 0.0, strategy_notes: "Mechanical exit: stop hit.",
      created_at: hoursAgo(72), execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 34",
    },
    {
      ticker: "CHARLIE", action: "BUY", status: "filled", fill_price: 55.30, fill_quantity: 90,
      stop_price: 51.00, target_price: 62.00, confidence: 3, primary_signal: "technical",
      technical_basis: "support bounce", expected_reward_risk: 1.56,
      invalidation_condition: "closes back below $52.00", strategy_notes: "Support held twice before.",
      created_at: hoursAgo(96), execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 33",
    },
    // Decision-time rejected BUY (never filled) — §2b concentration cap.
    {
      ticker: "DELTA", action: "BUY", status: "rejected", fill_price: null, fill_quantity: null,
      confidence: 3, primary_signal: "technical", expected_reward_risk: 1.9,
      strategy_notes: "Proposed but rejected before execution.", created_at: hoursAgo(30),
      execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 39",
    },
    // Missing optional values: no technical_basis, no invalidation_condition.
    {
      ticker: "ECHO", action: "BUY", status: "pending", fill_price: null, fill_quantity: null,
      stop_price: 12.40, target_price: 14.10, confidence: 3, primary_signal: "technical",
      expected_reward_risk: 1.6, created_at: hoursAgo(1),
      execution_mode: "backtest", run_id: RUN_ID, day_label: "Day 41",
    },
  ],
  activity: [
    { event_type: "cycle_start", description: "Cycle 41 started.", created_at: hoursAgo(6), mode: "backtest", run_id: RUN_ID },
    { event_type: "risk_check", description: "DELTA rejected: §2b concentration cap exceeded (20.06% > 20.00%).", created_at: hoursAgo(30), mode: "backtest", run_id: RUN_ID },
    { event_type: "order_rejected", description: "FOXTROT rejected at execution: §6b reward:risk below 1.5 floor.", created_at: hoursAgo(52), mode: "backtest", run_id: RUN_ID },
    { event_type: "cycle_failed", description: "Cycle 37 failed: expected_reward_risk contract mismatch.", created_at: hoursAgo(80), mode: "backtest", run_id: RUN_ID },
    { event_type: "weekly_reflection", description: "Reflection: process discipline steady, sample size still small.", created_at: hoursAgo(90), mode: "backtest", run_id: RUN_ID },
  ],
  snapshots: [
    {
      created_at: hoursAgo(6), cash: 41200.55, equity: 103696.08, drawdown_pct: -1.2,
      exposure_pct: 20.0, day_label: "Day 41", run_id: RUN_ID, mode: "backtest",
      open_positions_detail: [
        { ticker: "ALPHA", current_price: 44.90, unrealized_pl: 560.00, unrealized_pl_pct: 6.65, stop_price: 39.50, target_price: 47.80, days_held: 2 },
      ],
      total_tokens: 41608, input_tokens: 30000, output_tokens: 8000, reasoning_tokens: 3000, cache_read_tokens: 600, api_calls: 1,
    },
    { created_at: hoursAgo(30), cash: 40000, equity: 101200, drawdown_pct: -2.46, exposure_pct: 18.0, day_label: "Day 39", run_id: RUN_ID, mode: "backtest", open_positions_detail: [], total_tokens: 39210, api_calls: 1 },
    { created_at: hoursAgo(72), cash: 44900, equity: 98800, drawdown_pct: -3.10, exposure_pct: 15.0, day_label: "Day 34", run_id: RUN_ID, mode: "backtest", open_positions_detail: [], total_tokens: 40012, api_calls: 2 },
  ],
  get blockedActivity() { return this.activity.filter(a => a.event_type === "risk_check" || a.event_type === "order_rejected"); },
  insights: [
    { cycle_type: "weekly", insight_text: "Sizing discipline holding at both the risk and concentration limits.", created_at: hoursAgo(90) },
  ],
  failedCount: 1,
  lastFailed: { created_at: hoursAgo(80), description: "Cycle 37 failed: expected_reward_risk contract mismatch." },
};

// ---- an empty live-paper account: no snapshots, no trades, nothing to
// scope a run to (live has no run_id concept at all).
export const FIXTURE_LIVE_EMPTY = {
  run_id: null,
  trades: [],
  activity: [],
  snapshots: [],
  blockedActivity: [],
  insights: [],
  failedCount: 0,
  lastFailed: null,
};

// ---- stale data: one old snapshot, nothing recent — exercises the
// staleness warning path.
export const FIXTURE_STALE = {
  run_id: null,
  trades: [
    { ticker: "GOLF", action: "BUY", status: "filled", fill_price: 10.0, fill_quantity: 100, confidence: 3, primary_signal: "technical", expected_reward_risk: 1.8, strategy_notes: "Older live position.", created_at: hoursAgo(400), execution_mode: "alpaca", run_id: null },
  ],
  activity: [
    { event_type: "cycle_start", description: "Cycle started.", created_at: hoursAgo(400), mode: "live", run_id: null },
  ],
  snapshots: [
    { created_at: hoursAgo(400), cash: 90000, equity: 100000, drawdown_pct: 0, exposure_pct: 10, run_id: null, mode: "live", open_positions_detail: [] },
  ],
  blockedActivity: [],
  insights: [],
  failedCount: 0,
  lastFailed: null,
};
