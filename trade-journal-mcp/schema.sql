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
    -- RULES.md §6b's active minimum be verified after the fact with a query,
    -- not by re-deriving it from entry/stop/target by hand.
    expected_reward_risk  NUMERIC(6, 2) CHECK (expected_reward_risk > 0),
    -- One sentence: what would prove this trade wrong. RULES.md §6d --
    -- lets a later reflection check whether the predicted failure mode
    -- matched the trade's actual exit reason.
    invalidation_condition TEXT,
    -- RULES.md §6a/§6b: the real stop/target set at decision time. Was
    -- computed and enforced by the risk checker on every BUY but never
    -- actually persisted anywhere -- an open position's stop/target only
    -- ever lived in the harness's in-memory Holding / the live risk
    -- checker's call args, both gone once the process exits. Nullable:
    -- unused on SELL rows and on pre-migration rows.
    stop_price           NUMERIC(14, 2),
    target_price         NUMERIC(14, 2),
    -- §6c: source-of-truth timestamps for whatever data justified this
    -- decision (technical/sentiment feed timestamps), not just created_at.
    data_timestamps     JSONB,
    -- Phase 1 aggressive profile (docs/PHASE1_AGGRESSIVE_DESIGN.md) --
    -- which numeric tier produced this decision, and the actual threshold
    -- used. See the "Migration -- Phase 1 aggressive profile" ALTER TABLE
    -- block below for the full rationale; listed here too so a genuinely
    -- fresh database (CREATE TABLE, not ALTER) gets the same columns.
    strategy_profile     TEXT,
    min_reward_risk_used NUMERIC(6, 2),
    -- Which backtest run this row belongs to (NULL for live -- a run is a
    -- backtest-only concept). Filtering, not identity: decision_id is
    -- already the real idempotency key. Lets the dashboard and reflection
    -- retrieval show only the CURRENT/selected run's trades instead of
    -- also surfacing a discarded prior run's rows that share the same Neon
    -- database. A fresh uuid4 per run (run_backtest.py), persisted in
    -- backtest_state.json so a resumed (not reset) run keeps writing under
    -- the same id.
    run_id              UUID,
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
    -- Same contamination risk as agent_activity.mode, fixed the same way,
    -- just later: without this, a backtest reflection and a live
    -- reflection are indistinguishable once live cron exists, so a
    -- training-run insight would silently appear in the Live dashboard
    -- view. Nullable: pre-dates this column; backfilled for existing rows
    -- (see migration below) rather than left ambiguous.
    mode          TEXT CHECK (mode IN ('backtest', 'live')),
    -- How many CLOSED trades existed when this was written. Insights are
    -- fed back into every later prompt, and without this a conclusion
    -- drawn from 2 trades reads with exactly the same authority as one
    -- drawn from 40 -- which is precisely the "learn superstition from
    -- randomness" failure SOUL.md warns against, crystallised into text
    -- and then repeated forever. Shown alongside the insight so its weight
    -- is visible rather than assumed. Nullable: pre-dates this column.
    sample_size   INTEGER,
    -- Same run-filtering purpose as trades.run_id -- NULL for live.
    run_id        UUID,
    -- Deterministic uuid5(run_id, day_index) for a backtest reflection;
    -- NULL for live (no cycle concept there). UNIQUE (Postgres allows
    -- unlimited NULLs in a UNIQUE constraint, so live rows are unaffected)
    -- so retrying an already-completed cycle can never double-write the
    -- same reflection -- at most one insight is ever written per cycle by
    -- construction (run_backtest.py's is_reflection_day branch), so this
    -- is a real invariant, not an arbitrary constraint.
    cycle_id      UUID UNIQUE,
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
    -- Run/cycle filtering, same purpose as trades.run_id/insight_log.cycle_id
    -- above. Not unique here -- many activity rows legitimately share one
    -- cycle_id (cycle_start, reasoning, order_queued, ...). Duplicate
    -- narration on a retried-but-already-succeeded cycle is prevented at
    -- the call site instead (run_backtest.py only re-narrates when the
    -- cycle's snapshot INSERT was genuinely new -- see record_snapshot_once).
    -- total_tokens/hermes_calls_used: real usage as of a FAILED cycle
    -- attempt (event_type='cycle_failed') -- a failed attempt still burns
    -- real tokens/quota and the dashboard must show that real state, not
    -- just an error string with no numbers behind it. NULL on every other
    -- event_type; not overloading trades/portfolio_snapshots' meaning with
    -- a fake equity row for something that never actually completed.
    run_id        UUID,
    cycle_id      UUID,
    total_tokens  INTEGER,
    hermes_calls_used INTEGER,
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
    -- Real per-cycle Hermes usage, from --usage-file (run_backtest.py's
    -- _call_hermes) -- previously computed and printed to the console only,
    -- never persisted, so the dashboard had no real source and resorted to
    -- estimating usage by counting snapshot rows instead. api_calls > 1
    -- means a JSON-parse retry happened this cycle (ask_hermes's one retry).
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    reasoning_tokens  INTEGER,
    total_tokens      INTEGER,
    api_calls         INTEGER,
    -- The real quota_pacer.py numbers as of this cycle (pacer.used_this_week()
    -- / pacer.budget) -- the single source of truth for quota, mirrored here
    -- so the dashboard (Neon-only, no access to the host's local
    -- quota_pacer_state.json) can display the real count instead of
    -- re-deriving its own from unrelated data.
    hermes_calls_used INTEGER,
    weekly_budget     INTEGER,
    -- Per-open-position detail (ticker, quantity, entry/current price,
    -- stop, target, days_held, unrealized P&L) as the harness itself sees
    -- it that cycle -- the harness already computes all of this for
    -- Hermes' own prompt (positions_view) but never wrote it anywhere a
    -- browser could read. JSONB rather than new position-level table/rows:
    -- one full snapshot per cycle matches how this table already works,
    -- and open positions rarely exceed RULES.md §3's cap of 8.
    open_positions_detail JSONB,
    -- Run/cycle identity -- NULL for live. cycle_id is deterministic
    -- (uuid5(run_id, day_index), run_backtest.py) and UNIQUE: exactly one
    -- successful snapshot can ever exist per (run, simulated day),
    -- enforced by the database, not just by the harness's own control
    -- flow. This is what makes "retrying a failed day cannot duplicate a
    -- successful snapshot" a real guarantee rather than a hope -- the
    -- INSERT itself is `ON CONFLICT (cycle_id) DO NOTHING`, and whether a
    -- row actually came back is what run_backtest.py uses to decide
    -- whether to also (re-)write that cycle's activity narration/insight.
    run_id          UUID,
    cycle_id        UUID UNIQUE,
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
-- 2026-08-29 correctness pass #4 hygiene: CREATE POLICY has no IF NOT
-- EXISTS in Postgres (unlike CREATE TABLE/INDEX above), so re-running
-- this file against a database that already has these policies used to
-- fail outright. Guarded via pg_policies so the whole file is safely
-- re-runnable -- NOT a signal to actually re-apply it to Neon; the
-- policies already exist there (see the module-level note above).
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'trades' AND policyname = 'anonymous_read_trades'
    ) THEN
        CREATE POLICY anonymous_read_trades ON trades FOR SELECT TO anonymous USING (true);
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'insight_log' AND policyname = 'anonymous_read_insight_log'
    ) THEN
        CREATE POLICY anonymous_read_insight_log ON insight_log FOR SELECT TO anonymous USING (true);
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'agent_activity' AND policyname = 'anonymous_read_agent_activity'
    ) THEN
        CREATE POLICY anonymous_read_agent_activity ON agent_activity FOR SELECT TO anonymous USING (true);
    END IF;
END $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'portfolio_snapshots' AND policyname = 'anonymous_read_portfolio_snapshots'
    ) THEN
        CREATE POLICY anonymous_read_portfolio_snapshots ON portfolio_snapshots FOR SELECT TO anonymous USING (true);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Migration (2026-08-28) -- dashboard-correctness pass. CREATE TABLE IF NOT
-- EXISTS above only runs on a fresh database; the live Neon database already
-- has these tables, so the new columns are added here explicitly, once,
-- idempotently (ADD COLUMN IF NOT EXISTS is safe to re-run). Existing
-- columns/rows/grants are untouched -- additive only.
-- ---------------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN IF NOT EXISTS stop_price NUMERIC(14, 2);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS target_price NUMERIC(14, 2);

ALTER TABLE insight_log ADD COLUMN IF NOT EXISTS mode TEXT CHECK (mode IN ('backtest', 'live'));
-- Backfill: every insight_log row that predates this column was written by
-- the backtest harness (no live cron has ever run -- see HANDOFF.md,
-- "Current in-progress state"/PROJECT_FRESH_START.md's "not yet built" live
-- loop). Verified against real project history before running, not assumed.
UPDATE insight_log SET mode = 'backtest' WHERE mode IS NULL;

ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS total_tokens INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS api_calls INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS hermes_calls_used INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS weekly_budget INTEGER;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS open_positions_detail JSONB;

-- ---------------------------------------------------------------------------
-- Migration (2026-08-28, second pass) -- final pre-training correctness
-- pass: run/cycle identity + idempotency, queue-time journaling, decision-
-- time risk visibility. Additive only, same idempotent ADD COLUMN pattern
-- as above.
-- ---------------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN IF NOT EXISTS run_id UUID;

ALTER TABLE insight_log ADD COLUMN IF NOT EXISTS run_id UUID;
ALTER TABLE insight_log ADD COLUMN IF NOT EXISTS cycle_id UUID;
DO $$ BEGIN
    ALTER TABLE insight_log ADD CONSTRAINT insight_log_cycle_id_key UNIQUE (cycle_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE agent_activity ADD COLUMN IF NOT EXISTS run_id UUID;
ALTER TABLE agent_activity ADD COLUMN IF NOT EXISTS cycle_id UUID;
ALTER TABLE agent_activity ADD COLUMN IF NOT EXISTS total_tokens INTEGER;
ALTER TABLE agent_activity ADD COLUMN IF NOT EXISTS hermes_calls_used INTEGER;

ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS run_id UUID;
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS cycle_id UUID;
DO $$ BEGIN
    ALTER TABLE portfolio_snapshots ADD CONSTRAINT portfolio_snapshots_cycle_id_key UNIQUE (cycle_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_trades_run_id ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_activity_run_cycle ON agent_activity(run_id, cycle_id);

-- ---------------------------------------------------------------------------
-- Migration -- Phase 1 aggressive profile (docs/PHASE1_AGGRESSIVE_DESIGN.md).
-- Additive only, same idempotent ADD COLUMN pattern as above. Nullable: every
-- pre-existing row (Round 1 and earlier) genuinely has no profile/threshold
-- to report -- NULL is the honest value, not backfilled to a guess.
-- ---------------------------------------------------------------------------
-- Which numeric tier produced this decision -- 'aggressive_v1_base' (1.5:1
-- min reward:risk) or 'aggressive_v1_escalated' (1.25:1, after a slow
-- 20-trading-day activity block -- see run_backtest.py's deterministic,
-- harness-controlled escalation mechanic, RULES.md §6b). Free text, not a
-- CHECK enum -- a future profile version (v2, v3, ...) should be able to
-- write its own label here without a schema migration every time.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_profile TEXT;
-- The ACTUAL minimum reward:risk this decision was validated against --
-- lets later analysis distinguish base- from escalated-tier decisions by a
-- single query instead of parsing Hermes' prose or cross-referencing which
-- block a decision's date fell into.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS min_reward_risk_used NUMERIC(6, 2);

-- ---------------------------------------------------------------------------
-- Migration (2026-08-31) -- per-cycle backtest evidence + price precision.
-- Additive only, same idempotent pattern as above.
-- ---------------------------------------------------------------------------

-- The exact entry_price Hermes proposed for a BUY, persisted directly --
-- previously not stored at all (only intended_notional/intended_quantity
-- existed, and dividing them back apart to reconstruct "what price did
-- Hermes actually propose" is a derived guess, not a fact; see
-- docs/evaluations/round2_tranche1_evaluation_extract.md §6 item 5). NULL
-- for mechanical SELLs (nothing was "proposed", the exit is deterministic)
-- and for every pre-existing row -- honestly absent, not backfilled.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS proposed_entry_price NUMERIC(18, 6);

-- Widened from NUMERIC(14,2): a real intraday slipped price can land on a
-- third decimal (e.g. 78.125) and silently round to 78.13 or 78.12
-- depending on which code path formats it -- see the same evaluation doc's
-- documented $1.31 reconciliation gap on exactly this. Widening precision
-- is data-preserving for every existing 2-decimal value; nothing here
-- changes fill mechanics or historical figures, only how precisely a
-- future one is stored.
ALTER TABLE trades ALTER COLUMN fill_price TYPE NUMERIC(18, 6);

-- backtest_cycle_evidence -- one exact record per successful backtest
-- cycle: the precise masked inputs Hermes was shown, the exact rendered
-- prompt, and the final validated response. Round 2 tranche 1 could
-- measure outcomes but not audit whether Hermes actually read the
-- technicals/headlines/reflections it was given -- this table exists so a
-- future tranche can be audited, not just scored. cycle_id is the same
-- deterministic key portfolio_snapshots/insight_log already use for this
-- exact cycle, so ON CONFLICT DO NOTHING makes a retried write a true
-- no-op, matching every other essential write's idempotency discipline in
-- this schema (see _record_decision's own docstring).
--
-- Deliberately NOT granted to the `anonymous` role and NOT RLS-enabled below
-- (unlike trades/insight_log/agent_activity/portfolio_snapshots) -- this
-- table can carry the full rendered prompt and masked payload, a finer
-- granularity of raw model input/output than the public read-only
-- dashboard was ever meant to expose. No GRANT here means the anonymous
-- Data API role cannot see this table at all, by Postgres default.
CREATE TABLE IF NOT EXISTS backtest_cycle_evidence (
    cycle_id              UUID PRIMARY KEY,
    run_id                UUID NOT NULL,
    -- The masked "Day N" label, matching portfolio_snapshots.day_label /
    -- what Hermes itself saw this cycle.
    day_label             TEXT NOT NULL,
    -- Exact rendered prompt text (build_prompt's own output) -- the same
    -- masked object, not a reconstruction after the fact.
    prompt_text           TEXT NOT NULL,
    -- The already-masked structured objects build_prompt was called with
    -- (account/positions/candidates/recent_insights/reflection_trades) --
    -- passed straight through from the same call site, never rebuilt
    -- later from a different source.
    input_payload         JSONB NOT NULL,
    -- The final, schema-validated Hermes response for this cycle.
    validated_response    JSONB NOT NULL,
    -- Every eligible candidate alias this cycle, each marked ONLY
    -- 'proposed' or 'no_proposal' -- a purely structural classification
    -- (whether that alias appears in validated_response's own decisions
    -- list), never an inference parsed out of Hermes' free-text reasoning.
    candidate_status       JSONB NOT NULL,
    -- Real attempt count from ask_hermes (1 = succeeded first try, 2 = one
    -- retry) -- makes the "10 extra calls" pattern seen in tranche 1
    -- explainable per-cycle instead of only visible as a run-wide total.
    attempt_count          INTEGER NOT NULL,
    -- Ordered [{"attempt": N, "error_category": "...", "error_message":
    -- "..."}] for every UNSUCCESSFUL attempt this cycle -- error_message is
    -- caller-bounded (never raw/unbounded), never a credential, env value,
    -- or container command.
    retry_diagnostics       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Which build_prompt schema version rendered prompt_text -- lets a
    -- future prompt-format change be told apart from an older cycle's
    -- evidence without guessing from the text itself.
    prompt_schema_version  INTEGER NOT NULL,
    -- SHA-256 of RULES.md/AGENTS.md/SOUL.md AT THE TIME this cycle ran --
    -- proves (or disproves) which exact instructions Hermes was actually
    -- operating under, without persisting the files themselves.
    rules_md_sha256        TEXT NOT NULL,
    agents_md_sha256       TEXT NOT NULL,
    soul_md_sha256         TEXT NOT NULL,
    -- Real runtime metadata, only when the Hermes usage report itself
    -- actually included it -- NULL rather than a hard-coded assumption
    -- when it didn't (see run_backtest.py's ask_hermes/HermesResult).
    model                  TEXT,
    provider               TEXT,
    reasoning_effort       TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_backtest_cycle_evidence_run_id ON backtest_cycle_evidence(run_id);
