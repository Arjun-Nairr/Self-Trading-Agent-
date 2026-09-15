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
    --
    -- Action-aware (contract-mismatch fix, 2026-09-01): a BUY is a new
    -- entry, so its ratio must be a real positive number. A SELL is a
    -- closing decision -- prospective entry reward doesn't apply to it, so
    -- 0.0 is the HONEST value (e.g. a discretionary time-stop close,
    -- RULES.md §6c), not a missing or fabricated one. NULL still passes
    -- (Postgres's own 3-valued CHECK semantics) for pre-migration rows
    -- that predate this column entirely. Mirrors server.py's
    -- `_valid_expected_reward_risk` / run_backtest.py's own copy exactly.
    expected_reward_risk  NUMERIC(6, 2) CHECK (
        (action = 'BUY' AND expected_reward_risk > 0)
        OR (action <> 'BUY' AND expected_reward_risk >= 0)
    ),
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

-- ---------------------------------------------------------------------------
-- Migration (2026-09-12) -- fundamentals V1 backtest-integration correction
-- pass, requirement 2: a durable, non-agent-facing per-cycle audit payload
-- linking the exact masked fundamentals block each candidate was shown to
-- its FULL raw SEC provenance (source observations, filed dates/forms/
-- accession numbers/URLs/CIK where present, source as_of date) -- NULL for
-- every cycle where the fundamentals profile is off (unaffected, every
-- prior row keeps this column NULL forever) or fundamentals-disabled runs
-- going forward. Lives on backtest_cycle_evidence specifically because that
-- table is ALREADY not granted to the `anonymous` role and NOT RLS-enabled
-- (see the table's own comment above) -- the exact protection boundary this
-- raw, unmasked data needs, reused rather than duplicated into a new table.
-- ---------------------------------------------------------------------------
ALTER TABLE backtest_cycle_evidence ADD COLUMN IF NOT EXISTS fundamentals_provenance JSONB;

-- ---------------------------------------------------------------------------
-- Migration (2026-09-01) -- action-aware expected_reward_risk constraint.
--
-- Contract mismatch found live on Round 2 tranche 2, cycle 9 (day_index=49):
-- Hermes returned legitimate discretionary time-stop SELL decisions with
-- expected_reward_risk=0.0 -- 0.0 is the honest value for a closing
-- decision (prospective entry reward doesn't apply to an exit), but the old
-- blanket `expected_reward_risk > 0` constraint rejected every one of them,
-- halting the run. Replaces the old constraint with the same action-aware
-- rule now enforced in server.py's `_valid_expected_reward_risk` and
-- run_backtest.py's own copy: BUY requires > 0, SELL (or any non-BUY
-- action) permits >= 0. Idempotent -- DROP CONSTRAINT IF EXISTS is a safe
-- no-op on rerun, and the guarded ADD CONSTRAINT below mirrors the existing
-- DO $$ ... EXCEPTION WHEN duplicate_object pattern used elsewhere in this
-- file. Preserves every existing row -- purely a constraint swap, no data
-- touched.
-- ---------------------------------------------------------------------------
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_expected_reward_risk_check;
DO $$ BEGIN
    ALTER TABLE trades ADD CONSTRAINT trades_expected_reward_risk_check CHECK (
        (action = 'BUY' AND expected_reward_risk > 0)
        OR (action <> 'BUY' AND expected_reward_risk >= 0)
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Migration (2026-09-09) -- backtest run registry.
--
-- Durable classification of each backtest run_id as 'training' (real,
-- evaluation-ready), 'smoke' (end-to-end infra test), or 'archived'
-- (preserved historical run) -- at the database/harness level, not a
-- dashboard-only hidden list. Additive only: no historical trades/
-- portfolio_snapshots/agent_activity/insight_log/backtest_cycle_evidence
-- row is touched, deleted, or relabeled by this migration -- it only adds
-- one new table and classifies existing run_ids into it.
--
-- run_id is the unique key (a run has exactly one classification, ever --
-- see _register_run's collision check in server.py, which fails loudly on
-- any attempt to re-register an existing run_id under a different
-- run_kind rather than silently overwriting it). run_kind is intentionally
-- a locked three-value CHECK, not free text like agent_activity.event_type
-- -- unlike that field, there is no reason for this vocabulary to grow, and
-- a typo'd fourth value here would silently break run discovery.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_run_registry (
    run_id     UUID PRIMARY KEY,
    run_kind   TEXT NOT NULL CHECK (run_kind IN ('training', 'smoke', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dashboard V2 reads this the same read-only way it reads every other
-- table -- SELECT only, RLS-gated, never writable from the browser. This
-- grant is unrelated to (and does not change) backtest_cycle_evidence's
-- own protection boundary, which stays deliberately un-granted above.
GRANT SELECT ON backtest_run_registry TO anonymous;
ALTER TABLE backtest_run_registry ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'backtest_run_registry' AND policyname = 'anonymous_read_backtest_run_registry'
    ) THEN
        CREATE POLICY anonymous_read_backtest_run_registry ON backtest_run_registry FOR SELECT TO anonymous USING (true);
    END IF;
END $$;

-- Historical classification, seeded once. Every distinct non-null run_id
-- across trades/portfolio_snapshots/insight_log/agent_activity/
-- backtest_cycle_evidence was inventoried before writing this block (six
-- total, all accounted for below -- see HANDOFF.md §3/§9 and
-- docs/history/HANDOFF_FULL_2026-09-01_PRE_COMPACTION.md for the
-- provenance of each smoke-test id). ON CONFLICT DO NOTHING keeps this
-- block safe to re-run without overwriting a real classification.
INSERT INTO backtest_run_registry (run_id, run_kind) VALUES
    ('e68525f9-ce89-4588-a57e-866f28cd99fd', 'training'), -- active Round 2 tranche, 50 cycles
    ('26f1d371-08db-4885-ab24-b42200792fb7', 'archived'), -- Round 1, conservative profile, 125 cycles
    ('97d112e8-57be-4632-a503-0417993cdf51', 'smoke'),    -- 3-cycle Round 2 pilot (one-share sizing defect found)
    ('c55f0fc2-5073-4cf8-bc5c-17800e139bfc', 'smoke'),    -- --reset attempt, crashed cycle 1 (usage-accumulator null bug)
    ('1bf9af3d-8375-45b2-8cdb-313fd56832d5', 'smoke'),    -- 3-cycle --reset smoke test (stale 1% risk-budget bug found)
    ('81db75e5-f9e9-48df-8bf1-0a2f581d3db8', 'smoke')     -- 3-cycle --reset smoke test confirming the fix
ON CONFLICT (run_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Migration (dashboard run-status indicator) -- an explicit lifecycle status
-- record for a training run, so Dashboard V2's run-status banner never has
-- to infer "is this run actively training" from a portfolio_snapshots
-- timestamp. One row per run_id, upserted by run_backtest.py at three
-- points: the start of a training invocation, after every completed cycle
-- (progress), and once at the end (see run_backtest.py's own
-- record_run_status/main() for exactly when each write happens -- a cycle
-- failure writes 'failed' too, best-effort, never blocking the run itself).
-- 'running' with a stale updated_at is how the dashboard derives "Paused"
-- -- there is deliberately no separate 'paused' status value written here.
--
-- Never written for a 'smoke' or 'archived' run -- run_backtest.py checks
-- run_kind == 'training' before calling record_run_status at all, so a
-- smoke run can never even acquire a row here. The dashboard also
-- independently requires the run to appear in backtest_run_registry as
-- 'training' (the same registry-membership check every other training-only
-- view already uses) before it will render anything from this table --
-- belt and suspenders against a smoke run ever reading as active training.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_run_status (
    run_id      UUID PRIMARY KEY REFERENCES backtest_run_registry(run_id),
    status      TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    current_day INTEGER NOT NULL DEFAULT 0,
    total_days  INTEGER,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same read-only contract as every other dashboard-facing table: SELECT
-- only, RLS-gated, never writable from the browser. No new write access is
-- granted to the anonymous role by this migration.
GRANT SELECT ON backtest_run_status TO anonymous;
ALTER TABLE backtest_run_status ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'backtest_run_status' AND policyname = 'anonymous_read_backtest_run_status'
    ) THEN
        CREATE POLICY anonymous_read_backtest_run_status ON backtest_run_status FOR SELECT TO anonymous USING (true);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Migration (exit provenance) -- answers exactly one objective fact for a
-- SELL row going forward: was this exit generated by a deterministic
-- harness path, or requested by the trading agent? Bookkeeping only --
-- does not change which trades can be entered or exited, does not require
-- a new LLM response field (server.py/run_backtest.py set it themselves,
-- never asking Hermes for it), and carries no semantic reason on its own
-- ('agent_requested' never says WHY, only WHO/WHAT decided -- see
-- backtest-harness/trade_journey_report.py's own separate, note-derived
-- "rationale" label for that).
--
-- A locked five-value CHECK, same discipline as backtest_run_registry.run_kind
-- -- this vocabulary answers one narrow question and has no reason to grow:
--   harness_stop / harness_target / harness_time_stop -- written by
--     run_backtest.py's record_exit (deterministic, derived from the SAME
--     fixed `reason` string simulate_trading_day produces, never re-read
--     from strategy_notes after the fact).
--   agent_requested -- written whenever record_trade/record_queued_decision
--     journal an action='SELL' Hermes actually chose to make (backtest) or
--     record_decision's own SELL path (live) -- set server-side from the
--     row's own action, never inferred from prose.
--   legacy_unknown -- explicit fallback for a pre-migration row where
--     provenance was never captured (defaulted below); NEVER assigned to
--     a new row, and NULL is also accepted (Postgres's standard CHECK
--     semantics) for the same reason -- a BUY row has no exit to classify
--     at all, and NULL, not a fabricated value, is the honest state there.
-- Idempotent: ADD COLUMN IF NOT EXISTS is a safe no-op on rerun, and the
-- guarded ADD CONSTRAINT below mirrors the existing DO $$ ... EXCEPTION
-- WHEN duplicate_object pattern used elsewhere in this file. No existing
-- row's ticker/action/fill/price/decision_id/etc. is touched -- purely
-- additive, and every pre-migration SELL is explicitly backfilled to
-- 'legacy_unknown' rather than left silently NULL and indistinguishable
-- from "not applicable."
-- ---------------------------------------------------------------------------
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_provenance TEXT;
DO $$ BEGIN
    ALTER TABLE trades ADD CONSTRAINT trades_exit_provenance_check CHECK (
        exit_provenance IS NULL OR exit_provenance IN (
            'harness_stop', 'harness_target', 'harness_time_stop',
            'agent_requested', 'legacy_unknown'
        )
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
UPDATE trades SET exit_provenance = 'legacy_unknown'
WHERE action = 'SELL' AND status = 'filled' AND exit_provenance IS NULL;

-- ---------------------------------------------------------------------------
-- Migration (2026-09-15, correction pass item E, prior audit M2) --
-- backtest_run_registry.run_kind gains a fourth value, 'evaluation'.
--
-- eval_suite.py's regime-diverse comparison suite (backtest-harness/
-- eval_suite.py) had no correct run_kind to register its episodes under:
-- 'training' would make every eval episode indistinguishable from a real
-- Round 2-style run in this registry AND in dashboard-v2's training-run
-- selector (js/data.js filters on run_kind=eq.training); 'smoke' mislabels
-- genuine evaluation data as an infrastructure test, the exact thing
-- 'smoke' exists to exclude. eval_suite.py's own run_configuration()
-- now refuses to register an episode under anything but 'evaluation'
-- (EVAL_RUN_KIND) -- this widened CHECK is the second, independent layer
-- of that same guarantee, at the database boundary rather than only in
-- the Python caller.
--
-- Purely additive: no existing backtest_run_registry row is touched, and
-- no eval_suite.py episode has ever registered a row (see HANDOFF.md §13
-- / docs/PROJECT_BACKLOG.md §4 -- "defined but never invoked"), so this
-- migration relabels nothing.
-- ---------------------------------------------------------------------------
ALTER TABLE backtest_run_registry DROP CONSTRAINT IF EXISTS backtest_run_registry_run_kind_check;
DO $$ BEGIN
    ALTER TABLE backtest_run_registry ADD CONSTRAINT backtest_run_registry_run_kind_check
        CHECK (run_kind IN ('training', 'smoke', 'archived', 'evaluation'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Migration (2026-09-15, correction pass item D2 / freeform red-team R2
-- durability) -- backtest_run_configuration: the private, Neon-durable long
-- term home for a run's frozen watchlist snapshot and full ticker<->alias
-- mapping.
--
-- backtest_state.json (run_backtest.py's local runtime checkpoint) already
-- freezes this identity per run (2026-09-15 correction pass item D), but it
-- is only a LOCAL file: a later --reset (even the confirmation-gated one,
-- item F) or a lost/replaced checkout takes it with it, and with it the
-- only record of which real ticker each alias in that run's already-
-- journaled trades/activity/cycle evidence stood for. This table is that
-- durable copy -- reachable only through the same trusted trade-journal-mcp
-- path as backtest_cycle_evidence, never the anonymous Data API, dashboard,
-- or Cloudflare surface.
--
-- One row per run_id, written once (idempotent retry only -- see
-- _register_run_configuration in server.py) BEFORE that run's first
-- market-data fetch, Hermes call, decision write, or cycle-evidence write.
-- A resumed run whose local backtest_state.json already carries a frozen
-- watchlist_snapshot reads this record back and fails closed before any
-- further work if it disagrees -- see run_backtest.py's main(),
-- get_run_configuration, _check_run_configuration_matches. A legacy resume
-- with no stored watchlist_snapshot at all (the preserved Round 2 run)
-- predates this table entirely and never touches it -- its historical
-- mapping cannot be reconstructed retroactively, and this migration does
-- not attempt to.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_run_configuration (
    run_id                   UUID PRIMARY KEY REFERENCES backtest_run_registry(run_id),
    -- Full ordered watchlist snapshot and ticker<->alias mapping this run's
    -- aliases were built from -- the actual reproducibility/de-masking
    -- record, not just a fingerprint of it.
    watchlist_snapshot       JSONB NOT NULL,
    watchlist_snapshot_hash  TEXT NOT NULL,
    alias_mapping            JSONB NOT NULL,
    alias_mapping_hash       TEXT NOT NULL,
    masking_seed             INTEGER NOT NULL,
    -- Plain ISO text, not DATE: nothing here sorts or range-filters on the
    -- simulated window, only compares it for exact equality on resume.
    start_date               TEXT NOT NULL,
    end_date                 TEXT NOT NULL,
    fundamentals_enabled     BOOLEAN NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Deliberately NOT granted to the `anonymous` role and NOT RLS-enabled
-- (same boundary as backtest_cycle_evidence above) -- this table carries
-- the full real ticker<->alias mapping, which must never reach the
-- anonymous Data API, the dashboard, or a public user. No GRANT here means
-- the anonymous role cannot see this table at all, by Postgres default.
