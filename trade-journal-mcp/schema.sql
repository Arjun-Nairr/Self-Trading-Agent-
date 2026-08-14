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
    -- Which execution tool actually produced this fill — 'simulated' while
    -- Upstox Sandbox signup is blocked (OTP delivery issue), 'upstox_sandbox'
    -- once swapped in. Read this before trusting any pattern reflection
    -- draws from trade history: a lesson learned under 'simulated' (e.g.
    -- "LIMIT orders always fill instantly") may be an artifact of the sim's
    -- simplified fill logic, not a real trading pattern.
    execution_mode      TEXT NOT NULL DEFAULT 'simulated'
                             CHECK (execution_mode IN ('simulated', 'alpaca')),
    strategy_notes      TEXT,
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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_insight_log_cycle_type ON insight_log(cycle_type);

-- §7: dashboard reads via Neon's Data API using the anonymous role,
-- SELECT only — no write grant, ever, from the browser side. This tool
-- (trade-journal-mcp) writes using the full-credential NEON_DATABASE_URL
-- from .env instead, never the anonymous role.
GRANT SELECT ON trades, insight_log TO anonymous;
