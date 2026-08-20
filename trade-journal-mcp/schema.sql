-- trade-journal schema — Neon (eu-central-1)
--
-- decision_id is the idempotency key (§6b of the plan): every intended
-- trade gets one, persisted durably (not in-process memory, which a
-- crash/restart erases at exactly the moment it's needed). A retry of the
-- same decision_id must return the original outcome, never place a
-- second order.

CREATE TABLE IF NOT EXISTS trades (
    decision_id         UUID PRIMARY KEY,
    ticker              TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    intended_quantity   INTEGER NOT NULL CHECK (intended_quantity > 0),
    intended_notional   NUMERIC(14, 2) NOT NULL CHECK (intended_notional > 0),
    order_type          TEXT NOT NULL,
    limit_price         NUMERIC(14, 2),
    status              TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'filled', 'failed', 'rejected')),
    broker_order_id     TEXT,
    fill_price          NUMERIC(14, 2),
    fill_quantity       INTEGER,
    rejection_reason    TEXT,
    -- Which execution tool actually produced this fill. 'alpaca' = real
    -- paper-trading API. 'simulated' = execution-sim-mcp used ad hoc for
    -- manual dev testing (pre-dates Alpaca; kept for local testing without
    -- burning real API calls). 'backtest' = execution-sim-mcp's historical-
    -- fill mode, driving real backtest volume. Distinguishing 'simulated'
    -- from 'backtest' matters: without it, a handful of leftover manual
    -- dev-test rows would be indistinguishable from the thousands of real
    -- backtest decisions once that runs, corrupting any query over "what
    -- did the backtest actually produce." Read this field before trusting
    -- any pattern reflection draws from trade history: a lesson learned
    -- under 'simulated' or 'backtest' (e.g. "LIMIT orders always fill
    -- instantly") may be an artifact of the sim's simplified fill logic,
    -- not a real trading pattern.
    execution_mode      TEXT NOT NULL DEFAULT 'simulated'
                             CHECK (execution_mode IN ('simulated', 'alpaca', 'backtest')),
    strategy_notes      TEXT,
    -- Structured "report card" fields, added on top of strategy_notes so
    -- eval questions like "did high-confidence trades actually win more?"
    -- are a single query instead of reading every trade's prose by hand.
    -- Nullable at the DB level (existing rows predate these columns, and
    -- an ALTER TABLE ADD COLUMN ... NOT NULL would fail against them) --
    -- "required" is enforced in server.py's record_decision instead, so
    -- every NEW decision must supply them without needing a data backfill.
    confidence           SMALLINT CHECK (confidence BETWEEN 1 AND 5),
    -- What actually drove the decision. 'other' is a legitimate, trackable
    -- answer -- never force a bad fit into technical/sentiment/both; see
    -- server.py's validation for the requirement that 'other' must come
    -- with an explanation in strategy_notes.
    primary_signal        TEXT CHECK (primary_signal IN ('technical', 'sentiment', 'both', 'other')),
    -- Which specific maverick-mcp signal, when primary_signal touches
    -- technical -- ties eval directly to a real tool output, not vague
    -- prose. Same 'other' escape hatch as primary_signal.
    technical_basis       TEXT CHECK (technical_basis IN ('rsi', 'macd', 'support_resistance', 'trend', 'multiple', 'other')),
    -- The reward:risk ratio actually calculated for this trade -- lets
    -- RULES.md §6b's 2:1 minimum be verified after the fact with a query,
    -- not by re-deriving it from entry/stop/target by hand.
    expected_reward_risk  NUMERIC(6, 2) CHECK (expected_reward_risk > 0),
    -- One sentence: what would prove this trade wrong. RULES.md §6d --
    -- lets a later reflection check whether the predicted failure mode
    -- matched the trade's actual exit reason.
    invalidation_condition TEXT,
    -- §6c: source-of-truth timestamps for whatever data justified this
    -- decision (technical/sentiment feed timestamps), not just created_at.
    data_timestamps     JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- insight_log — reflective prose, separate from trades (facts) and
-- proposed_skills/ (procedural rules). Written during weekly reflection,
-- before any proposed_skills/ change is drafted, so the change is
-- traceable back to the reasoning that justified it (§4).
CREATE TABLE IF NOT EXISTS insight_log (
    id            BIGSERIAL PRIMARY KEY,
    cycle_type    TEXT NOT NULL CHECK (cycle_type IN ('decision', 'weekly_reflection')),
    insight_text  TEXT NOT NULL,
    -- How many CLOSED trades existed when this was written. Insights are
    -- fed back into every later prompt, and without this a conclusion
    -- drawn from 2 trades reads with exactly the same authority as one
    -- drawn from 40 -- which is precisely the "learn superstition from
    -- randomness" failure SOUL.md warns against, crystallised into text
    -- and then repeated forever. Shown alongside the insight so its weight
    -- is visible rather than assumed. Nullable: pre-dates this column.
    sample_size   INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- agent_activity — step-by-step narration of what Hermes is doing right
-- now (fetching data, reasoning, checking risk, placing an order, etc.),
-- separate from trades (outcomes) and insight_log (weekly reflection
-- prose). This is what a live "what is Hermes up to" dashboard view reads
-- from. event_type is free text, not a locked CHECK enum — the real
-- vocabulary of steps doesn't exist yet (depends on how SOUL.md is
-- designed to narrate itself), so this stays flexible rather than
-- guessing a fixed list now. decision_id is nullable and only set when
-- an event is tied to a specific trade decision (most early-cycle events,
-- like "fetched quote," aren't tied to one yet).
CREATE TABLE IF NOT EXISTS agent_activity (
    id            BIGSERIAL PRIMARY KEY,
    decision_id   UUID REFERENCES trades(decision_id),
    event_type    TEXT NOT NULL,
    description   TEXT NOT NULL,
    -- Same contamination risk already fixed once on trades.execution_mode,
    -- applied here before it becomes a real bug rather than after: most
    -- activity rows (reasoning, risk_check) carry no decision_id at all, so
    -- there is no other way to separate backtest narration from live
    -- narration once live cron exists and both start writing here.
    mode          TEXT CHECK (mode IN ('backtest', 'live')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- portfolio_snapshots — equity/exposure at the end of one cycle. Written
-- once per cycle by whatever is running it (the backtest harness now,
-- live cron later). Exists specifically because the harness only ever
-- tracked its portfolio in a LOCAL file (backtest_state.json) -- a
-- dashboard is a static page reading Neon, so it structurally cannot show
-- an equity curve or current position value from data that only ever
-- lived on one machine's disk. This table is what makes that possible,
-- and the SAME table (filtered by `mode`) is what lets the dashboard
-- switch between training and live views -- there was never a real
-- TEST/PROD Neon split, everything lives in one database tagged by mode,
-- matching how `execution_mode` already works on `trades`.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    mode            TEXT NOT NULL CHECK (mode IN ('backtest', 'live')),
    equity          NUMERIC(14, 2) NOT NULL,
    cash            NUMERIC(14, 2) NOT NULL,
    exposure_pct    NUMERIC(6, 2),
    drawdown_pct    NUMERIC(6, 2),
    -- Backtest: the masked "Day N" label (matches what Hermes itself saw,
    -- so a snapshot can be cross-referenced against its own reasoning).
    -- Live: null -- created_at alone is the real timeline there.
    day_label       TEXT,
    open_positions  INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_insight_log_cycle_type ON insight_log(cycle_type);
CREATE INDEX IF NOT EXISTS idx_agent_activity_created_at ON agent_activity(created_at);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_mode_created ON portfolio_snapshots(mode, created_at);

-- §7: dashboard reads via Neon's Data API using the anonymous role,
-- SELECT only — no write grant, ever, from the browser side. This tool
-- (trade-journal-mcp) writes using the full-credential NEON_DATABASE_URL
-- from .env instead, never the anonymous role.
-- SELECT on the tables isn't enough by itself — Postgres also needs
-- schema-level USAGE granted separately, or the anonymous role can't
-- even see into the schema to run the SELECT it was granted.
GRANT USAGE ON SCHEMA public TO anonymous;
GRANT SELECT ON trades, insight_log, agent_activity, portfolio_snapshots TO anonymous;

-- Neon's Data API refuses anonymous access to any table without RLS
-- enabled, as a safety-by-default measure — GRANT alone isn't enough.
-- USING (true) means "no row filtering, expose every row" since we
-- want the whole journal visible, not a per-user filtered view.
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE insight_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY anonymous_read_trades ON trades FOR SELECT TO anonymous USING (true);
CREATE POLICY anonymous_read_insight_log ON insight_log FOR SELECT TO anonymous USING (true);
CREATE POLICY anonymous_read_agent_activity ON agent_activity FOR SELECT TO anonymous USING (true);
CREATE POLICY anonymous_read_portfolio_snapshots ON portfolio_snapshots FOR SELECT TO anonymous USING (true);
