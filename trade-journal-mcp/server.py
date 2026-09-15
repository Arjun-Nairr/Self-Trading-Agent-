"""
trade-journal-mcp — Neon-backed trade journal + insight log + activity feed.

Seven tools: record_decision, record_execution_result, get_trade_history,
log_insight, get_insight_history, log_activity, get_recent_activity.

log_activity/get_recent_activity are step-by-step narration (fetching
data, reasoning, checking risk, placing an order) — separate from trades
(outcomes) and insight_log (weekly reflection prose). This is what a live
"what is Hermes up to" dashboard view reads from. Stays idle/empty until
SOUL.md is written to actually call log_activity.

Idempotency (§6b of the plan): a cron cycle can die between placing an
order and recording the result. record_execution_result is written so a
retry with the same decision_id never corrupts state — it only updates a
row that's still 'pending'; if the row was already resolved by an earlier
attempt, it just returns that existing result instead of a second write.
On restart, Hermes is expected to call get_trade_history(status="pending")
and reconcile each one against real broker state before doing anything
else (§6b) — decision_id already exists in that row, nothing needs to be
regenerated or remembered across the crash.
"""
import json
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastmcp import FastMCP

mcp = FastMCP("trade-journal")

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # statement_cache_size=0: NEON_DATABASE_URL is a Neon POOLED
        # (PgBouncer transaction-mode) endpoint (`-pooler` in the hostname).
        # asyncpg's default per-connection prepared-statement cache is
        # incompatible with transaction pooling -- a statement prepared on
        # one pooled backend can be reused against a different backend
        # after any DDL (e.g. this project's own migrations), surfacing as
        # asyncpg.exceptions.InvalidCachedStatementError on a query that
        # never itself changed. Found live 2026-08-28 running this
        # project's own idempotency test right after a schema migration.
        # See https://github.com/MagicStack/asyncpg/issues/750 -- the
        # documented fix is disabling the client-side cache, not retrying.
        _pool = await asyncpg.create_pool(
            os.environ["NEON_DATABASE_URL"], min_size=1, max_size=5,
            statement_cache_size=0,
        )
    return _pool


_VALID_PRIMARY_SIGNALS = {"technical", "sentiment", "both", "other"}
_VALID_TECHNICAL_BASES = {"rsi", "macd", "support_resistance", "trend", "multiple", "other"}


def _valid_expected_reward_risk(action: str, value: Any) -> bool:
    """Action-aware expected_reward_risk shape check (contract-mismatch
    fix, 2026-09-01): a BUY is a new entry, so its expected_reward_risk
    must be a real, finite, strictly positive number. A SELL is a closing
    decision -- "prospective entry reward" simply does not apply to it, so
    0.0 is the HONEST value for e.g. a discretionary time-stop close
    (RULES.md §6c), not a missing or fabricated one. SELL therefore
    permits finite >= 0. Booleans, NaN, infinities, and missing values
    (None) are invalid for EITHER action.

    Duplicated (not imported) in backtest-harness/run_backtest.py's own
    `_valid_expected_reward_risk` -- same reasoning as that file's
    BUY_SLIPPAGE_BPS duplication: the two are isolated processes (hub-and-
    spoke, RULES.md/AGENTS.md §12B), so if this contract ever changes,
    both copies must change together."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return False
    return value > 0 if action == "BUY" else value >= 0


def _validate_decision_fields(
    action: str,
    confidence: int | None,
    primary_signal: str,
    expected_reward_risk: float,
    invalidation_condition: str,
    strategy_notes: str | None,
    technical_basis: str | None,
) -> None:
    """Pure validation, no DB -- the "report card" fields are required for
    every real JUDGMENT decision (per RULES.md), and the 'other' escape
    hatch must never be a free pass: choosing it requires actually
    explaining what it was in strategy_notes, or 'other' just becomes a
    way to skip the whole point of structured logging. Raises ValueError
    naming exactly which rule failed; callers never get a silently-
    accepted bad row.

    confidence=None (2026-08-29 correctness pass #4, item 9): a
    MECHANICAL exit (stop/target/time-stop -- see run_backtest.py's
    record_exit) is not a judgment call at all, so none of the report-
    card checks below apply to it -- skipped entirely, rather than
    forcing a fabricated confidence value just to pass validation (which
    is exactly what was polluting the confidence-calibration dataset
    with non-decisions before this fix).
    """
    if confidence is None:
        return
    if not 1 <= confidence <= 5:
        raise ValueError(f"confidence must be 1-5, got {confidence}")
    if primary_signal not in _VALID_PRIMARY_SIGNALS:
        raise ValueError(
            f"primary_signal must be one of {sorted(_VALID_PRIMARY_SIGNALS)}, "
            f"got {primary_signal!r}"
        )
    if not _valid_expected_reward_risk(action, expected_reward_risk):
        if action == "BUY":
            raise ValueError(f"expected_reward_risk must be a finite number > 0 for a BUY, got {expected_reward_risk!r}")
        raise ValueError(
            f"expected_reward_risk must be a finite number >= 0 for a {action} "
            f"(0.0 is valid -- prospective entry reward does not apply to a "
            f"close), got {expected_reward_risk!r}"
        )
    if not invalidation_condition or not invalidation_condition.strip():
        raise ValueError("invalidation_condition is required and cannot be blank")

    # RULES.md §5a: sentiment can only VETO an entry, never trigger one --
    # a BUY driven purely by sentiment would violate that rule structurally.
    # (SELL is exempt: §6d's thesis-invalidation exit is legitimately
    # sentiment-only -- adverse news alone is a valid reason to close.)
    if action == "BUY" and primary_signal == "sentiment":
        raise ValueError(
            "primary_signal='sentiment' is not valid on a BUY -- RULES.md "
            "§5a: sentiment may only veto an entry, never trigger one"
        )

    needs_technical_basis = primary_signal in ("technical", "both")
    if needs_technical_basis and technical_basis is None:
        raise ValueError(
            f"technical_basis is required when primary_signal={primary_signal!r}"
        )
    if technical_basis is not None and technical_basis not in _VALID_TECHNICAL_BASES:
        raise ValueError(
            f"technical_basis must be one of {sorted(_VALID_TECHNICAL_BASES)}, "
            f"got {technical_basis!r}"
        )

    # The escape hatch is only legitimate if it's actually explained --
    # otherwise 'other' becomes a way to write nothing at all.
    chose_other = primary_signal == "other" or technical_basis == "other"
    if chose_other and (not strategy_notes or not strategy_notes.strip()):
        raise ValueError(
            "strategy_notes must explain the choice whenever primary_signal "
            "or technical_basis is 'other'"
        )


def _num_close(a: Any, b: Any, tol: float = 0.01) -> bool:
    """None-safe, tolerance-based numeric comparison -- a genuine retry
    round-trips a float through Postgres's numeric type and back, which can
    shift the last decimal place; that must never itself read as a
    'materially different payload.'"""
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def _normalize_data_timestamps(data_timestamps: dict[str, Any] | None) -> dict[str, Any] | None:
    """Empty and None are the same "nothing to record" state for this
    field -- collapsed identically wherever data_timestamps is written or
    compared (2026-08-29 correctness pass #4, item 7). Previously the
    WRITE path alone made this collapse (`json.dumps(...) if
    data_timestamps else None`); the compare path in _json_equal did not,
    so an identical retry carrying {} (stored as NULL on the first write)
    false-failed as a payload mismatch against that same NULL."""
    return data_timestamps or None


def _json_equal(a: Any, b: Any) -> bool:
    """Normalizes a DB-stored JSON value (asyncpg may hand back a str,
    depending on codec setup) against the incoming Python value before
    comparing -- key-order or str-vs-dict representation differences must
    never register as a mismatch on their own. Also collapses an empty
    dict to None on both sides -- see _normalize_data_timestamps."""
    def _norm(x: Any) -> Any:
        if isinstance(x, str):
            try:
                x = json.loads(x)
            except (TypeError, ValueError):
                return x
        return _normalize_data_timestamps(x) if x is None or isinstance(x, dict) else x
    return _norm(a) == _norm(b)


def _uuid_equal(a: Any, b: Any) -> bool:
    """None-safe UUID comparison. 2026-08-28 correctness pass (third
    pass, item 1): Neon/asyncpg returns a UUID column (e.g. `run_id`) as
    a `uuid.UUID` object, while the backtest harness always supplies a
    plain string -- comparing them with `==` directly is always False
    even for the identical value, which made every genuine identical
    retry that carried a real run_id fail with a false 'run_id' mismatch.
    Both sides are normalized to their string form before comparing."""
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


def _int_equal(a: Any, b: Any) -> bool:
    """None-safe integer comparison. 2026-08-29 correctness pass #4 (item
    8): confidence can be NULL for a mechanical exit (see item 9,
    record_exit) -- the prior direct `int(row["confidence"])` cast
    crashed with TypeError on a NULL row instead of reporting a clean,
    named mismatch, the same way every other comparator here already
    handles None."""
    if a is None or b is None:
        return a is None and b is None
    return int(a) == int(b)


def _decision_field_mismatches(
    row: "asyncpg.Record",
    *, ticker: str, action: str, intended_quantity: int, intended_notional: float,
    order_type: str, confidence: int | None, primary_signal: str, technical_basis: str | None,
    expected_reward_risk: float, invalidation_condition: str, strategy_notes: str | None,
    data_timestamps: dict[str, Any] | None, stop_price: float | None,
    target_price: float | None, limit_price: float | None, run_id: str | None,
    execution_mode: str, strategy_profile: str | None = None,
    min_reward_risk_used: float | None = None,
    proposed_entry_price: float | None = None,
    exit_provenance: str | None = None,
) -> list[str]:
    """Every field that DEFINES the decision -- not only the four
    (ticker/action/quantity/order_type) the prior check compared. A genuine
    retry of the exact same event reproduces every one of these identically
    (all derived from the same real-world decision); returns the names of
    any that don't, so the caller can fail loudly and name exactly what
    differed instead of silently keeping the stale row."""
    checks: list[tuple[str, bool]] = [
        ("ticker", row["ticker"] == ticker),
        ("action", row["action"] == action),
        ("intended_quantity", int(row["intended_quantity"]) == int(intended_quantity)),
        ("order_type", row["order_type"] == order_type),
        ("intended_notional", _num_close(row["intended_notional"], intended_notional)),
        ("confidence", _int_equal(row["confidence"], confidence)),
        ("primary_signal", row["primary_signal"] == primary_signal),
        ("technical_basis", row["technical_basis"] == technical_basis),
        ("expected_reward_risk", _num_close(row["expected_reward_risk"], expected_reward_risk)),
        ("invalidation_condition",
         (row["invalidation_condition"] or "").strip() == (invalidation_condition or "").strip()),
        ("strategy_notes", (row["strategy_notes"] or "").strip() == (strategy_notes or "").strip()),
        ("stop_price", _num_close(row["stop_price"], stop_price)),
        ("target_price", _num_close(row["target_price"], target_price)),
        ("limit_price", _num_close(row["limit_price"], limit_price)),
        ("data_timestamps", _json_equal(row["data_timestamps"], data_timestamps)),
        ("run_id", _uuid_equal(row["run_id"], run_id)),
        ("execution_mode", row["execution_mode"] == execution_mode),
        # Phase 1 aggressive profile (docs/PHASE1_AGGRESSIVE_DESIGN.md):
        # additive, nullable -- a genuine retry of a pre-Phase-1 row (both
        # NULL) must compare equal, same as every other nullable field here.
        ("strategy_profile", row["strategy_profile"] == strategy_profile),
        ("min_reward_risk_used", _num_close(row["min_reward_risk_used"], min_reward_risk_used)),
        ("proposed_entry_price", _num_close(row["proposed_entry_price"], proposed_entry_price)),
        ("exit_provenance", row["exit_provenance"] == exit_provenance),
    ]
    return [name for name, ok in checks if not ok]


async def _record_decision(
    ticker: str,
    action: str,
    intended_quantity: int,
    intended_notional: float,
    order_type: str,
    confidence: int | None,
    primary_signal: str,
    expected_reward_risk: float,
    invalidation_condition: str,
    limit_price: float | None = None,
    technical_basis: str | None = None,
    strategy_notes: str | None = None,
    data_timestamps: dict[str, Any] | None = None,
    decision_id: str | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    run_id: str | None = None,
    execution_mode: str = "simulated",
    strategy_profile: str | None = None,
    min_reward_risk_used: float | None = None,
    proposed_entry_price: float | None = None,
    exit_provenance: str | None = None,
) -> dict[str, Any]:
    """decision_id: optional caller-supplied id. Live Hermes never passes
    one (always gets a fresh uuid4, unchanged behavior). The backtest
    harness passes a DETERMINISTIC id derived from (run, day, ticker,
    action, kind) -- Phase 0 repair -- so that retrying a failed backtest
    day regenerates the SAME id for the SAME real-world event instead of a
    fresh random one. ON CONFLICT DO NOTHING + the fallback SELECT below
    make a retried write a safe no-op, mirroring
    _record_execution_result's own idempotency pattern (only resolves a
    row that's still 'pending').

    A decision_id COLLISION with a DIFFERENT payload fails loudly instead
    of silently returning the stale existing row -- see the mismatch check
    below. This should never happen for a genuine retry of the same event
    (deterministic ids are derived from the event's own identity); if it
    does, something is wrong with id derivation itself and silently
    returning the wrong trade's data would be far worse than an error.

    strategy_profile/min_reward_risk_used (Phase 1 aggressive profile,
    docs/PHASE1_AGGRESSIVE_DESIGN.md): additive, nullable -- which tier
    ('aggressive_v1_base'/'aggressive_v1_escalated') and threshold this
    decision was actually validated against. NULL for every pre-Phase-1
    row and for any caller that doesn't pass them (e.g. live Hermes' own
    public record_decision tool, unaffected by this profile).

    proposed_entry_price: the exact entry_price Hermes proposed (backtest
    BUY decisions only) -- persisted directly, never derived later from
    intended_notional / intended_quantity (that division is a reconstructed
    guess, not the real proposed figure). NULL for mechanical SELLs and
    every pre-existing row.

    exit_provenance (trades.exit_provenance, schema.sql migration): on a
    SELL row, WHO/WHAT caused the exit -- 'harness_stop'/'harness_target'/
    'harness_time_stop' (record_exit, deterministic from the harness's own
    fixed reason string) or 'agent_requested' (record_trade/
    record_queued_decision/this module's own record_decision tool,
    whenever action='SELL' -- set from the row's own action, never
    inferred from strategy_notes prose). NULL on a BUY (not applicable)
    and on any pre-migration row (see trade_journey_report.py's
    fingerprint-based historical reconciliation for those)."""
    _validate_decision_fields(
        action, confidence, primary_signal, expected_reward_risk,
        invalidation_condition, strategy_notes, technical_basis,
    )
    decision_id = decision_id or str(uuid.uuid4())
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trades (decision_id, ticker, action, intended_quantity,
                             intended_notional, order_type, limit_price,
                             confidence, primary_signal, technical_basis,
                             expected_reward_risk, invalidation_condition,
                             strategy_notes, data_timestamps, stop_price,
                             target_price, run_id, execution_mode,
                             strategy_profile, min_reward_risk_used,
                             proposed_entry_price, exit_provenance)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)
        ON CONFLICT (decision_id) DO NOTHING
        RETURNING *
        """,
        decision_id, ticker, action, intended_quantity, intended_notional,
        order_type, limit_price, confidence, primary_signal, technical_basis,
        expected_reward_risk, invalidation_condition, strategy_notes,
        json.dumps(_normalize_data_timestamps(data_timestamps)) if data_timestamps else None,
        stop_price, target_price, run_id, execution_mode,
        strategy_profile, min_reward_risk_used, proposed_entry_price, exit_provenance,
    )
    if row is None:
        # A caller-supplied decision_id that already exists -- a retry of
        # an already-journaled event. Return the existing row instead of
        # a duplicate insert or an error -- UNLESS the incoming payload
        # describes a genuinely different real-world event under the same
        # id, which fails loudly (see docstring).
        row = await pool.fetchrow("SELECT * FROM trades WHERE decision_id = $1", decision_id)
        if row is None:
            raise ValueError(f"decision_id {decision_id} conflicted on insert but no row exists -- unexpected state")
        # 2026-08-28 correctness pass (item 3): every decision-defining
        # field, not only ticker/action/quantity/order_type -- see
        # _decision_field_mismatches.
        mismatches = _decision_field_mismatches(
            row, ticker=ticker, action=action, intended_quantity=intended_quantity,
            intended_notional=intended_notional, order_type=order_type,
            confidence=confidence, primary_signal=primary_signal,
            technical_basis=technical_basis, expected_reward_risk=expected_reward_risk,
            invalidation_condition=invalidation_condition, strategy_notes=strategy_notes,
            data_timestamps=data_timestamps, stop_price=stop_price, target_price=target_price,
            limit_price=limit_price, run_id=run_id, execution_mode=execution_mode,
            strategy_profile=strategy_profile, min_reward_risk_used=min_reward_risk_used,
            proposed_entry_price=proposed_entry_price, exit_provenance=exit_provenance,
        )
        if mismatches:
            raise ValueError(
                f"decision_id {decision_id} collision with a DIFFERENT payload -- "
                f"mismatched field(s): {', '.join(mismatches)}. This should never "
                "happen for a genuine retry of the same event -- failing loudly "
                "instead of silently returning a different trade's data."
            )
    return dict(row)


def _cycle_evidence_mismatches(
    row: "asyncpg.Record", *, run_id: str, day_label: str, prompt_text: str,
    input_payload: dict[str, Any], validated_response: dict[str, Any],
    candidate_status: list[Any], attempt_count: int, retry_diagnostics: list[Any],
    prompt_schema_version: int, rules_md_sha256: str, agents_md_sha256: str,
    soul_md_sha256: str, model: str | None, provider: str | None,
    reasoning_effort: str | None, fundamentals_provenance: Any | None,
) -> list[str]:
    """Same discipline as _decision_field_mismatches -- every field that
    defines this cycle's evidence, so a genuine retry (identical payload)
    stays a no-op while a collision under the same cycle_id but DIFFERENT
    evidence fails loudly, naming exactly what differed."""
    checks: list[tuple[str, bool]] = [
        ("run_id", _uuid_equal(row["run_id"], run_id)),
        ("day_label", row["day_label"] == day_label),
        ("prompt_text", row["prompt_text"] == prompt_text),
        ("input_payload", _json_equal(row["input_payload"], input_payload)),
        ("validated_response", _json_equal(row["validated_response"], validated_response)),
        ("candidate_status", _json_equal(row["candidate_status"], candidate_status)),
        ("attempt_count", _int_equal(row["attempt_count"], attempt_count)),
        ("retry_diagnostics", _json_equal(row["retry_diagnostics"], retry_diagnostics)),
        ("prompt_schema_version", _int_equal(row["prompt_schema_version"], prompt_schema_version)),
        ("rules_md_sha256", row["rules_md_sha256"] == rules_md_sha256),
        ("agents_md_sha256", row["agents_md_sha256"] == agents_md_sha256),
        ("soul_md_sha256", row["soul_md_sha256"] == soul_md_sha256),
        ("model", row["model"] == model),
        ("provider", row["provider"] == provider),
        ("reasoning_effort", row["reasoning_effort"] == reasoning_effort),
        ("fundamentals_provenance", _json_equal(row["fundamentals_provenance"], fundamentals_provenance)),
    ]
    return [name for name, ok in checks if not ok]


async def _record_cycle_evidence(
    cycle_id: str, run_id: str, day_label: str, prompt_text: str,
    input_payload: dict[str, Any], validated_response: dict[str, Any],
    candidate_status: list[Any], attempt_count: int, retry_diagnostics: list[Any],
    prompt_schema_version: int, rules_md_sha256: str, agents_md_sha256: str,
    soul_md_sha256: str, model: str | None = None, provider: str | None = None,
    reasoning_effort: str | None = None, fundamentals_provenance: Any | None = None,
) -> dict[str, Any]:
    """One exact evidence record per successful backtest cycle -- the
    precise masked inputs Hermes was shown, the exact rendered prompt, and
    the final validated response, so a tranche can be audited later, not
    just scored (docs/evaluations/round2_tranche1_evaluation_extract.md's
    §6 coverage gaps are exactly what this closes).

    cycle_id is the SAME deterministic key portfolio_snapshots/insight_log
    already use for this exact cycle -- ON CONFLICT DO NOTHING makes a
    retried write (evidence write failed, cycle halted, harness reruns and
    reuses the checkpointed prompt/payload/response) a true no-op. A
    collision under the same cycle_id carrying genuinely DIFFERENT evidence
    fails loudly instead of silently keeping the stale row -- same
    discipline as _record_decision.

    Never call this with anything that still carries a real ticker-to-alias
    mapping -- prompt_text/input_payload/validated_response must already be
    masked by the caller (run_backtest.py's masking.assert_clean gate runs
    BEFORE this is ever reached); this function does not itself know or
    check the real mapping.

    fundamentals_provenance is the ONE deliberate exception to that rule
    (correction pass, requirement 2): it MAY carry the real ticker/CIK/
    filing URL/accession number -- it is the private, non-agent-facing raw
    SEC audit trail, not agent input, and this table is already un-granted
    to the `anonymous` role and not RLS-enabled (see schema.sql), exactly
    the protection this data needs. None whenever the fundamentals profile
    is off, matching every prior row."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO backtest_cycle_evidence (
            cycle_id, run_id, day_label, prompt_text, input_payload,
            validated_response, candidate_status, attempt_count, retry_diagnostics,
            prompt_schema_version, rules_md_sha256, agents_md_sha256, soul_md_sha256,
            model, provider, reasoning_effort, fundamentals_provenance
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
        ON CONFLICT (cycle_id) DO NOTHING
        RETURNING *
        """,
        cycle_id, run_id, day_label, prompt_text, json.dumps(input_payload),
        json.dumps(validated_response), json.dumps(candidate_status), attempt_count,
        json.dumps(retry_diagnostics), prompt_schema_version,
        rules_md_sha256, agents_md_sha256, soul_md_sha256, model, provider, reasoning_effort,
        json.dumps(fundamentals_provenance) if fundamentals_provenance is not None else None,
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT * FROM backtest_cycle_evidence WHERE cycle_id = $1", cycle_id,
        )
        if row is None:
            raise ValueError(
                f"cycle_id {cycle_id} conflicted on insert but no row exists -- unexpected state"
            )
        mismatches = _cycle_evidence_mismatches(
            row, run_id=run_id, day_label=day_label, prompt_text=prompt_text,
            input_payload=input_payload, validated_response=validated_response,
            candidate_status=candidate_status, attempt_count=attempt_count,
            retry_diagnostics=retry_diagnostics, prompt_schema_version=prompt_schema_version,
            rules_md_sha256=rules_md_sha256, agents_md_sha256=agents_md_sha256,
            soul_md_sha256=soul_md_sha256, model=model, provider=provider,
            reasoning_effort=reasoning_effort, fundamentals_provenance=fundamentals_provenance,
        )
        if mismatches:
            raise ValueError(
                f"cycle_id {cycle_id} collision with DIFFERENT evidence -- "
                f"mismatched field(s): {', '.join(mismatches)}. This should never "
                "happen for a genuine retry of the same cycle -- failing loudly "
                "instead of silently returning stale evidence."
            )
    return dict(row)


async def _record_portfolio_snapshot(
    mode: str,
    equity: float,
    cash: float,
    exposure_pct: float | None = None,
    drawdown_pct: float | None = None,
    day_label: str | None = None,
    open_positions: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    api_calls: int | None = None,
    hermes_calls_used: int | None = None,
    weekly_budget: int | None = None,
    open_positions_detail: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    """Returns the inserted row, or None if `cycle_id` already had a
    snapshot (ON CONFLICT DO NOTHING) -- the caller (run_backtest.py) uses
    that distinction to decide whether this cycle is genuinely new (and its
    activity narration/reflection should also be written) or already fully
    recorded by an earlier attempt (and must NOT be re-narrated/duplicated).
    A NULL cycle_id (always true for live) never conflicts -- Postgres
    treats every NULL as distinct in a UNIQUE constraint -- so live callers
    always get a real row back, unaffected by this mechanism."""
    if mode not in ("backtest", "live"):
        raise ValueError(f"mode must be 'backtest' or 'live', got {mode!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO portfolio_snapshots (mode, equity, cash, exposure_pct,
                                          drawdown_pct, day_label, open_positions,
                                          input_tokens, output_tokens, cache_read_tokens,
                                          reasoning_tokens, total_tokens, api_calls,
                                          hermes_calls_used, weekly_budget, open_positions_detail,
                                          run_id, cycle_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
        ON CONFLICT (cycle_id) DO NOTHING
        RETURNING *
        """,
        mode, equity, cash, exposure_pct, drawdown_pct, day_label, open_positions,
        input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, total_tokens,
        api_calls, hermes_calls_used, weekly_budget,
        json.dumps(open_positions_detail) if open_positions_detail is not None else None,
        run_id, cycle_id,
    )
    return dict(row) if row is not None else None


async def _record_execution_result(
    decision_id: str,
    status: str,
    broker_order_id: str | None = None,
    fill_price: float | None = None,
    fill_quantity: int | None = None,
    rejection_reason: str | None = None,
    execution_mode: str = "simulated",
) -> dict[str, Any]:
    pool = await get_pool()
    # Only resolves a row that's still 'pending' — a retry with the same
    # decision_id after the first attempt already succeeded is a no-op
    # here, not a second write.
    row = await pool.fetchrow(
        """
        UPDATE trades
        SET status = $2, broker_order_id = $3, fill_price = $4,
            fill_quantity = $5, rejection_reason = $6, execution_mode = $7, updated_at = now()
        WHERE decision_id = $1 AND status = 'pending'
        RETURNING *
        """,
        decision_id, status, broker_order_id, fill_price, fill_quantity, rejection_reason, execution_mode,
    )
    if row is None:
        # Already resolved by an earlier attempt (or unknown decision_id) —
        # return the current state instead of silently doing nothing.
        row = await pool.fetchrow("SELECT * FROM trades WHERE decision_id = $1", decision_id)
        if row is None:
            raise ValueError(f"unknown decision_id: {decision_id}")
    return dict(row)


async def _get_trade_history(
    ticker: str | None = None, status: str | None = None, limit: int = 20,
    execution_mode: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """execution_mode/run_id: pushed into SQL (not filtered in Python by the
    caller) so a caller asking for "this run's backtest trades" gets
    exactly that from the query itself, not an incidentally-correct
    Python-side filter applied after the fact."""
    pool = await get_pool()
    conditions, params = [], []
    if ticker:
        params.append(ticker)
        conditions.append(f"ticker = ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    if execution_mode:
        params.append(execution_mode)
        conditions.append(f"execution_mode = ${len(params)}")
    if run_id:
        params.append(run_id)
        conditions.append(f"run_id = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ${len(params)}", *params
    )
    return [dict(r) for r in rows]


async def _log_insight(
    cycle_type: str, insight_text: str, mode: str,
    sample_size: int | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    """Returns the inserted row, or None if `cycle_id` already has an
    insight (ON CONFLICT DO NOTHING) -- same duplicate-prevention purpose
    as _record_portfolio_snapshot's cycle_id handling. A NULL cycle_id
    (always true for live) never conflicts."""
    if mode not in ("backtest", "live"):
        raise ValueError(f"mode must be 'backtest' or 'live', got {mode!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO insight_log (cycle_type, insight_text, sample_size, mode, run_id, cycle_id) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (cycle_id) DO NOTHING RETURNING *",
        cycle_type, insight_text, sample_size, mode, run_id, cycle_id,
    )
    return dict(row) if row is not None else None


async def _get_insight_history(
    cycle_type: str | None = None, limit: int = 20,
    mode: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """mode/run_id: without these, a reflection prompt or the dashboard
    could surface an insight from a DIFFERENT (discarded/reset) backtest
    run, or a live insight leaking into a training view -- see RULES.md's
    own "never let old state masquerade as current" concern, applied here."""
    pool = await get_pool()
    conditions, params = [], []
    if cycle_type:
        params.append(cycle_type)
        conditions.append(f"cycle_type = ${len(params)}")
    if mode:
        params.append(mode)
        conditions.append(f"mode = ${len(params)}")
    if run_id:
        params.append(run_id)
        conditions.append(f"run_id = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM insight_log {where} ORDER BY created_at DESC LIMIT ${len(params)}", *params
    )
    return [dict(r) for r in rows]


async def _log_activity(
    event_type: str, description: str, decision_id: str | None = None,
    mode: str | None = None, run_id: str | None = None, cycle_id: str | None = None,
    total_tokens: int | None = None, hermes_calls_used: int | None = None,
) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO agent_activity (decision_id, event_type, description, mode, "
        "run_id, cycle_id, total_tokens, hermes_calls_used) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
        decision_id, event_type, description, mode, run_id, cycle_id,
        total_tokens, hermes_calls_used,
    )
    return dict(row)


async def _get_recent_activity(
    decision_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    pool = await get_pool()
    if decision_id:
        rows = await pool.fetch(
            "SELECT * FROM agent_activity WHERE decision_id = $1 ORDER BY created_at DESC LIMIT $2",
            decision_id, limit,
        )
    else:
        rows = await pool.fetch("SELECT * FROM agent_activity ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


_VALID_RUN_KINDS = {"training", "smoke", "archived", "evaluation"}


async def _register_run(run_id: str, run_kind: str) -> dict[str, Any]:
    """Idempotently register a backtest run's durable classification in
    backtest_run_registry -- a run is 'training' (real, evaluation-ready),
    'smoke' (end-to-end infra test, never shown in normal dashboard views),
    'archived' (preserved historical run, never shown in normal dashboard
    views), or 'evaluation' (a backtest-harness/eval_suite.py comparison
    episode -- correction pass item E, prior audit M2; added so a real
    eval episode is never mistaken for training data or infra noise).

    Same INSERT ON CONFLICT DO NOTHING + fallback SELECT + mismatch-fails-
    loudly pattern as _record_decision: supplying the SAME (run_id,
    run_kind) twice is a safe no-op (a resumed run re-registering its own
    stored kind, or a retried harness invocation). Supplying a DIFFERENT
    run_kind for an already-registered run_id fails loudly instead of
    silently overwriting a real classification -- a run's kind is
    permanent once set, by design (see run_backtest.py's own --run-kind
    handling and eval_suite.py's run_configuration, the two callers that
    ever write here)."""
    if run_kind not in _VALID_RUN_KINDS:
        raise ValueError(f"run_kind must be one of {sorted(_VALID_RUN_KINDS)}, got {run_kind!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO backtest_run_registry (run_id, run_kind)
        VALUES ($1, $2)
        ON CONFLICT (run_id) DO NOTHING
        RETURNING *
        """,
        run_id, run_kind,
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT * FROM backtest_run_registry WHERE run_id = $1", run_id,
        )
        if row is None:
            raise ValueError(
                f"run_id {run_id} conflicted on insert but no row exists -- unexpected state"
            )
        if row["run_kind"] != run_kind:
            raise ValueError(
                f"run_id {run_id} is already registered as {row['run_kind']!r}, "
                f"cannot re-register as {run_kind!r} -- a run's classification "
                "is permanent once set."
            )
    return dict(row)


async def _get_run_kind(run_id: str) -> str | None:
    """Read-only lookup -- None if no registry row exists for this run_id
    (a pre-existing run that predates this migration, or a genuinely
    unregistered id). run_backtest.py treats None on resume as a hard
    failure rather than silently continuing -- see its own --run-kind
    handling."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT run_kind FROM backtest_run_registry WHERE run_id = $1", run_id,
    )
    return row["run_kind"] if row else None


async def _register_run_configuration(
    run_id: str, watchlist_snapshot: list[dict[str, Any]], watchlist_snapshot_hash: str,
    alias_mapping: dict[str, str], alias_mapping_hash: str, masking_seed: int,
    start_date: str, end_date: str, fundamentals_enabled: bool,
) -> dict[str, Any]:
    """Idempotently persist a fresh backtest run's immutable frozen identity
    -- watchlist snapshot, ticker<->alias mapping, masking seed, simulated
    window, fundamentals setting -- into the private backtest_run_
    configuration table (correction pass item D2 / freeform red-team R2
    durability). This is the long-term, Neon-durable copy of what run_
    backtest.py's own backtest_state.json already freezes locally
    (2026-09-15 correction pass item D); it survives a later reset or a
    lost checkout, which the local state file does not.

    Same INSERT ON CONFLICT DO NOTHING + fallback SELECT + mismatch-fails-
    loudly pattern as _register_run/_record_cycle_evidence: supplying the
    SAME values twice for the same run_id is a safe no-op (a retried
    harness invocation before its first successful cycle write). Supplying
    ANY different value fails loudly naming exactly which field(s) conflict
    -- a run's frozen identity is permanent once set, never silently
    overwritten.

    Caller must already have registered run_id in backtest_run_registry
    (this table's run_id foreign-keys to it -- see _register_run) and must
    call this BEFORE any market-data fetch, Hermes call, decision write, or
    cycle-evidence write for the run -- see run_backtest.py's main()."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO backtest_run_configuration (
            run_id, watchlist_snapshot, watchlist_snapshot_hash,
            alias_mapping, alias_mapping_hash, masking_seed,
            start_date, end_date, fundamentals_enabled
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (run_id) DO NOTHING
        RETURNING *
        """,
        run_id, json.dumps(watchlist_snapshot), watchlist_snapshot_hash,
        json.dumps(alias_mapping), alias_mapping_hash, masking_seed,
        start_date, end_date, fundamentals_enabled,
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT * FROM backtest_run_configuration WHERE run_id = $1", run_id,
        )
        if row is None:
            raise ValueError(
                f"run_id {run_id} conflicted on insert but no row exists -- unexpected state"
            )
        mismatches = []
        if row["watchlist_snapshot_hash"] != watchlist_snapshot_hash:
            mismatches.append("watchlist_snapshot_hash")
        if row["alias_mapping_hash"] != alias_mapping_hash:
            mismatches.append("alias_mapping_hash")
        if row["masking_seed"] != masking_seed:
            mismatches.append("masking_seed")
        if row["start_date"] != start_date:
            mismatches.append("start_date")
        if row["end_date"] != end_date:
            mismatches.append("end_date")
        if row["fundamentals_enabled"] != fundamentals_enabled:
            mismatches.append("fundamentals_enabled")
        if mismatches:
            raise ValueError(
                f"run_id {run_id} already has a private run-configuration "
                f"record that disagrees on: {', '.join(mismatches)} -- a "
                "run's frozen identity is permanent once set, same "
                "discipline as backtest_run_registry.run_kind."
            )
    return dict(row)


async def _get_run_configuration(run_id: str) -> dict[str, Any] | None:
    """Read-only lookup -- None if no private run-configuration record
    exists for this run_id (never written, e.g. a run that predates this
    migration -- the preserved Round 2 run included). run_backtest.py's
    resume path for a run whose local state already carries a frozen
    watchlist_snapshot treats None as a hard failure rather than silently
    re-freezing a fresh record -- see its own get_run_configuration /
    _check_run_configuration_matches."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM backtest_run_configuration WHERE run_id = $1", run_id,
    )
    return dict(row) if row else None


_VALID_RUN_STATUSES = {"running", "completed", "failed"}


async def _record_run_status(
    run_id: str, status: str, current_day: int, total_days: int | None = None,
) -> dict[str, Any]:
    """Upsert this run's lifecycle status -- backtest_run_status, the
    explicit record Dashboard V2's run-status indicator reads instead of
    ever inferring "is this run actively training" from a
    portfolio_snapshots timestamp (see run_backtest.py's own
    record_run_status, the only caller, which never calls this for a
    non-'training' run_kind).

    First write for a run_id sets `started_at` (the column default); every
    later call for the same run_id only advances `status`/`current_day`/
    `updated_at` -- `started_at` is never touched again. `total_days` is
    `None` on a plain per-cycle progress update (COALESCE keeps whatever
    was already stored); passed a real value only when the harness knows a
    fresh target (run start, or a resumed invocation with a new --days),
    so a stale target is never silently overwritten by an unrelated
    progress write.

    'running' with an old `updated_at` is how the dashboard derives
    "paused" -- there is deliberately no separate 'paused' status written
    here; see run_backtest.py's own best-effort write points for exactly
    when 'failed' is written (a halted cycle) vs. left to go stale
    (an invocation simply ending mid-window, expected between resumes)."""
    if status not in _VALID_RUN_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_RUN_STATUSES)}, got {status!r}")
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO backtest_run_status (run_id, status, current_day, total_days)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (run_id) DO UPDATE SET
            status = EXCLUDED.status,
            current_day = EXCLUDED.current_day,
            total_days = COALESCE(EXCLUDED.total_days, backtest_run_status.total_days),
            updated_at = now()
        RETURNING *
        """,
        run_id, status, current_day, total_days,
    )
    return dict(row)


def _jsonable(obj):
    """asyncpg returns datetime/Decimal objects FastMCP's JSON encoder chokes on."""
    return json.loads(json.dumps(obj, default=str))


@mcp.tool()
async def record_decision(
    ticker: str, action: str, intended_quantity: int, intended_notional: float,
    order_type: str, confidence: int, primary_signal: str,
    expected_reward_risk: float, invalidation_condition: str,
    limit_price: float | None = None, technical_basis: str | None = None,
    strategy_notes: str | None = None, data_timestamps: dict[str, Any] | None = None,
    stop_price: float | None = None, target_price: float | None = None,
) -> dict[str, Any]:
    """Persist an intended trade before attempting execution. Returns the row
    including a fresh decision_id — pass that same id to record_execution_result.

    The "report card" fields are required for every real decision, so this
    stays queryable across hundreds of cycles instead of relying on reading
    prose by hand:
      confidence: how sure you are, 1 (low) to 5 (high) -- rate it honestly,
        this only has value if it actually correlates with real outcomes.
      primary_signal: 'technical', 'sentiment', 'both', or 'other'. What
        actually drove this decision, per RULES.md §5a (technicals trigger,
        sentiment only vetoes -- 'sentiment' alone should be rare).
      technical_basis: required when primary_signal is 'technical' or
        'both' -- which maverick-mcp signal ('rsi', 'macd',
        'support_resistance', 'trend', 'multiple', or 'other').
      expected_reward_risk: on a BUY, the actual ratio you calculated
        (target distance ÷ stop distance) -- must be a real positive number
        meeting RULES.md §6b's current minimum. On a SELL (a closing
        decision), prospective entry reward doesn't apply -- report 0.0
        honestly rather than a fabricated ratio; any other non-negative
        finite number is also accepted if genuinely applicable.
      invalidation_condition: one sentence -- what specific thing happening
        would prove this trade wrong. Be concrete (a price level, a signal
        reversing), not vague ("if it goes down").

    'other' (for primary_signal or technical_basis) is a legitimate answer,
    never a shortcut -- if you use it, strategy_notes MUST explain what it
    actually was, or the call is rejected. Put any reasoning beyond the
    structured fields in strategy_notes -- it stays free-form on purpose.

    stop_price/target_price: the real levels set for this trade (RULES.md
    §6a/§6b) -- pass them on a BUY so the dashboard can show a still-open
    position's real stop/target, not just its entry price."""
    return _jsonable(await _record_decision(
        ticker, action, intended_quantity, intended_notional, order_type,
        confidence, primary_signal, expected_reward_risk, invalidation_condition,
        limit_price, technical_basis, strategy_notes, data_timestamps,
        stop_price=stop_price, target_price=target_price,
        execution_mode="alpaca",
        # exit_provenance: set here, server-side, from the row's own
        # action -- never a new field Hermes has to supply itself (no new
        # LLM response field, per the schema.sql migration's own contract).
        exit_provenance="agent_requested" if action == "SELL" else None,
    ))


@mcp.tool()
async def record_execution_result(
    decision_id: str, status: str, broker_order_id: str | None = None,
    fill_price: float | None = None, fill_quantity: int | None = None,
    rejection_reason: str | None = None,
) -> dict[str, Any]:
    """Record the outcome of attempting to execute decision_id. Safe to call
    more than once with the same decision_id — only the first call that finds
    the row still 'pending' writes; later calls return the resolved result.
    This public tool is live-only and tags the result 'alpaca'
    automatically. Backtest and manual simulator callers use the internal
    helper with their explicit mode, so the model cannot relabel a live
    result through this interface."""
    return _jsonable(await _record_execution_result(
        decision_id, status, broker_order_id, fill_price, fill_quantity,
        rejection_reason, "alpaca",
    ))


@mcp.tool()
async def get_trade_history(
    ticker: str | None = None, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """List past trades, optionally filtered by ticker/status. Use
    status="pending" on startup to reconcile against real broker state."""
    return _jsonable(await _get_trade_history(
        ticker=ticker, status=status, limit=limit, execution_mode="alpaca",
    ))


@mcp.tool()
async def log_insight(cycle_type: str, insight_text: str) -> dict[str, Any]:
    """Record reflective prose during a decision or weekly_reflection cycle,
    before drafting any proposed_skills/ change.

    You only ever call this tool from a live cycle (per AGENTS.md, backtest
    cycles never call their own tools) — mode is tagged 'live' automatically,
    same as log_activity, without you needing to pass anything."""
    return _jsonable(await _log_insight(cycle_type, insight_text, mode="live"))


@mcp.tool()
async def get_insight_history(cycle_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """List past insight-log entries, optionally filtered by cycle_type."""
    return _jsonable(await _get_insight_history(
        cycle_type=cycle_type, limit=limit, mode="live",
    ))


@mcp.tool()
async def log_activity(
    event_type: str, description: str, decision_id: str | None = None
) -> dict[str, Any]:
    """Narrate a step as you work through a cycle — fetching data,
    reasoning, checking risk, placing an order, updating the journal.
    event_type is free-form (e.g. "data_fetch", "reasoning", "risk_check",
    "order_placed") — pass decision_id once one exists for this trade, omit
    it for steps that happen before a decision is made (e.g. pulling a
    quote). This is what powers a live "what is Hermes doing right now"
    view — call it at every meaningful step, not just at the end.

    You only ever call this tool from a live cycle (per AGENTS.md, backtest
    cycles never call their own tools) — mode is tagged 'live' automatically,
    always correctly, without you needing to pass anything."""
    return _jsonable(await _log_activity(event_type, description, decision_id, mode="live"))


@mcp.tool()
async def get_recent_activity(
    decision_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List recent activity-log entries, optionally filtered to one
    decision_id's full step-by-step trail."""
    return _jsonable(await _get_recent_activity(decision_id, limit))


@mcp.tool()
async def record_portfolio_snapshot(
    mode: str, equity: float, cash: float, exposure_pct: float | None = None,
    drawdown_pct: float | None = None, day_label: str | None = None,
    open_positions: int = 0,
    input_tokens: int | None = None, output_tokens: int | None = None,
    cache_read_tokens: int | None = None, reasoning_tokens: int | None = None,
    total_tokens: int | None = None, api_calls: int | None = None,
    hermes_calls_used: int | None = None, weekly_budget: int | None = None,
    open_positions_detail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record equity/exposure at the end of one cycle. mode: 'backtest' or
    'live'. Call this once per cycle, after marking positions to market —
    it's the only source of equity-over-time data the dashboard has, since
    portfolio state otherwise only ever lived in the harness's local state
    file. day_label: the masked "Day N" string for backtest cycles (so a
    snapshot cross-references what Hermes itself saw); omit for live.

    The token fields are optional real usage for THIS cycle (from Hermes'
    own --usage-file report) -- pass them when known so the dashboard reads
    real numbers instead of estimating. hermes_calls_used/weekly_budget are
    historical fields from a removed weekly invocation counter (2026-08-31)
    -- kept nullable for compatibility with old rows, no longer populated.
    open_positions_detail: per-position dicts
    (ticker, quantity, entry_price, current_price, stop_price, target_price,
    days_held, unrealized_pl, unrealized_pl_pct) for a live open-positions
    view without a separate broker/journal round trip."""
    return _jsonable(await _record_portfolio_snapshot(
        mode, equity, cash, exposure_pct, drawdown_pct, day_label, open_positions,
        input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
        total_tokens, api_calls, hermes_calls_used, weekly_budget,
        open_positions_detail,
    ))


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
