"""
workflow_contract.py -- the public-safe contract for backtest workflow
progress events (table `workflow_events`, anonymous view
`public_workflow_events`; dashboard-v2 Workflow page).

Pure and dependency-free: the trade-journal server enforces it at the write
boundary (`_record_workflow_events`), and host tests import the same file.

An event says only WHICH major stage of a cycle started/completed/failed/was
skipped, WHEN (real wall clock), and a few allowlisted counts/enums. It can
never carry a ticker, alias, company name, prompt, model reasoning, tool
payload, path, credential, or free text: every metric key is allowlisted per
stage and every value is an int, bool, 4-decimal fraction, or a member of a
fixed enum. Journaled orders link by their existing `decision_id` (whose
public_trades row is already public); proposals rejected before journaling
carry a positional key `p<day_index>-<n>` (the n-th decision in that day's
response), which encodes no identity.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime

CONTRACT_VERSION = "workflow-events-v1"

STATUSES = ("started", "completed", "failed", "skipped")

# Major stages only, in the order the backtest controller runs them inside a
# cycle (run_backtest.run_day): the ORDER path (yesterday's queue) runs at
# the simulated open, BEFORE the CANDIDATE path (after the close).
STAGES = (
    "run", "cycle",
    # order / position path
    "prefill_news", "execution", "positions",
    # candidate path
    "population", "laya", "shortlist", "evidence", "hermes", "decision_risk",
    # small linked indicator
    "journal",
)

_ACTIONS = ("BUY", "SELL")
_ORDER_OUTCOMES = ("filled", "rejected", "cancelled", "superseded")
_PROPOSAL_OUTCOMES = ("queued", "rejected")
_EXIT_KINDS = ("opening_gap", "intraday", "time_stop", "discretionary", "other")
_EXIT_REASONS = ("stop", "target", "time_stop", "discretionary", "other")
_NAMED_CODES = ("selector_boundary", "not_in_candidates", "invalid_proposal", "duplicate_ticker",
                "quantity_mismatch", "pre_fill_news_veto", "pre_fill_news_unavailable",
                "sim_rejected", "superseded", "unknown_alias", "unsupported_action")
_RULE_CODE = re.compile(r"^§\d{1,2}[a-z]?(\.\d{1,2})?$")
MAX_CODES = 6

INT, BOOL, FRAC, CODES = "int", "bool", "frac", "codes"


def _enum(*values):
    return ("enum", values)


_COMMON = {"replayed": BOOL}
METRICS: dict[str, dict[str, object]] = {
    "run": {"sessions_requested": INT, "sessions_completed": INT, "halted": BOOL, "resumed": BOOL,
            "selector_mode": _enum("off", "laya"), "fundamentals": _enum("off", "on"),
            "open_positions": INT, "pending_orders": INT, "telemetry_dropped": INT},
    "cycle": {"skip_reason": _enum("no_market_data"), "checkpoint_reused": BOOL},
    "prefill_news": {"queued_buys": INT, "confirmed": INT, "cancelled": INT, "articles": INT,
                     "hermes_calls": INT},
    "execution": {"queued_orders": INT, "filled": INT, "rejected": INT, "superseded": INT,
                  "action": _enum(*_ACTIONS), "outcome": _enum(*_ORDER_OUTCOMES), "rule_codes": CODES},
    "positions": {"monitored": INT, "exits": INT, "open_after": INT,
                  "exit_kind": _enum(*_EXIT_KINDS), "exit_reason": _enum(*_EXIT_REASONS)},
    "population": {"universe": INT, "excluded_held_or_pending": INT, "eligible": INT,
                   "selector_mode": _enum("off", "laya")},
    "laya": {"cards": INT, "complete": INT, "scored": INT, "unavailable": INT,
             "score_min": FRAC, "score_median": FRAC, "score_max": FRAC, "worker_batches": INT,
             "status": _enum("selected", "failed_closed")},
    "shortlist": {"selected": INT, "k": INT, "shortfall": BOOL, "status": _enum("selected", "failed_closed")},
    "evidence": {"names": INT, "shortlisted": INT, "held": INT, "technicals_ok": INT,
                 "technicals_errors": INT, "news_items": INT, "fundamentals": _enum("off", "on")},
    "hermes": {"candidates": INT, "positions": INT, "decisions": INT, "buys": INT, "sells": INT,
               "attempts": INT, "reflection": BOOL, "total_tokens": INT},
    "decision_risk": {"proposals": INT, "queued": INT, "rejected": INT,
                      "action": _enum(*_ACTIONS), "outcome": _enum(*_PROPOSAL_OUTCOMES), "rule_codes": CODES},
    "journal": {"trade_rows": INT, "resolutions": INT, "snapshot": BOOL, "activity_rows": INT},
}
for _m in METRICS.values():
    _m.update(_COMMON)

_PROPOSAL_KEY = re.compile(r"^p\d{1,5}-\d{1,3}$")
_DAY_LABEL = re.compile(r"^Day \d{1,5}$")
MAX_INT = 10_000_000


class EventRejected(ValueError):
    pass


def _uuid(v, field, required=False):
    if v is None:
        if required:
            raise EventRejected(f"{field} required")
        return None
    try:
        return str(uuid.UUID(str(v)))
    except ValueError:
        raise EventRejected(f"{field} is not a UUID") from None


def _value(kind, v, key):
    if kind == INT:
        if type(v) is not int or not 0 <= v <= MAX_INT:
            raise EventRejected(f"{key} must be a non-negative int")
        return v
    if kind == BOOL:
        if type(v) is not bool:
            raise EventRejected(f"{key} must be a bool")
        return v
    if kind == FRAC:
        if type(v) not in (int, float) or isinstance(v, bool) or not 0.0 <= float(v) <= 1.0:
            raise EventRejected(f"{key} must be a fraction in [0, 1]")
        return round(float(v), 4)
    if kind == CODES:
        if not isinstance(v, list) or len(v) > MAX_CODES:
            raise EventRejected(f"{key} must be a list of at most {MAX_CODES} codes")
        for c in v:
            if not isinstance(c, str) or not (_RULE_CODE.match(c) or c in _NAMED_CODES):
                raise EventRejected(f"{key}: code {c!r} is not allowlisted")
        return sorted(set(v))
    if isinstance(kind, tuple) and kind[0] == "enum":
        if v not in kind[1]:
            raise EventRejected(f"{key}: {v!r} not in {kind[1]}")
        return v
    raise EventRejected(f"{key}: unknown kind")  # pragma: no cover


def validate_event(e: dict) -> dict:
    """-> a sanitized copy containing ONLY contract fields, or raises
    EventRejected. Unknown top-level fields are rejected, not dropped."""
    allowed = {"run_id", "invocation_id", "seq", "cycle_id", "day_index", "day_label", "market_date",
               "stage", "status", "occurred_at", "metrics", "decision_id", "proposal_key"}
    if not isinstance(e, dict):
        raise EventRejected("event must be an object")
    extra = sorted(set(e) - allowed)
    if extra:
        raise EventRejected(f"fields not allowed: {extra}")
    stage, status = e.get("stage"), e.get("status")
    if stage not in STAGES:
        raise EventRejected(f"stage {stage!r} not allowlisted")
    if status not in STATUSES:
        raise EventRejected(f"status {status!r} not allowlisted")
    seq = e.get("seq")
    if type(seq) is not int or not 0 <= seq <= MAX_INT:
        raise EventRejected("seq must be a non-negative int")
    try:
        occurred = datetime.fromisoformat(str(e.get("occurred_at")))
    except ValueError:
        raise EventRejected("occurred_at must be an ISO timestamp") from None
    if occurred.tzinfo is None:
        raise EventRejected("occurred_at must be timezone-aware")
    out = {"run_id": _uuid(e.get("run_id"), "run_id", True),
           "invocation_id": _uuid(e.get("invocation_id"), "invocation_id", True),
           "seq": seq, "stage": stage, "status": status, "occurred_at": occurred.isoformat(),
           "cycle_id": _uuid(e.get("cycle_id"), "cycle_id"),
           "decision_id": _uuid(e.get("decision_id"), "decision_id"),
           "day_index": None, "day_label": None, "market_date": None, "proposal_key": None}
    di = e.get("day_index")
    if di is not None:
        if type(di) is not int or not 0 <= di <= 100_000:
            raise EventRejected("day_index must be a non-negative int")
        out["day_index"] = di
    dl = e.get("day_label")
    if dl is not None:
        if not isinstance(dl, str) or not _DAY_LABEL.match(dl):
            raise EventRejected("day_label must look like 'Day N'")
        out["day_label"] = dl
    md = e.get("market_date")
    if md is not None:
        try:
            out["market_date"] = date.fromisoformat(str(md)).isoformat()
        except ValueError:
            raise EventRejected("market_date must be an ISO date") from None
    pk = e.get("proposal_key")
    if pk is not None:
        if not isinstance(pk, str) or not _PROPOSAL_KEY.match(pk):
            raise EventRejected("proposal_key must look like 'p<day>-<n>'")
        out["proposal_key"] = pk
    if stage != "run" and out["cycle_id"] is None:
        raise EventRejected("cycle-level events need cycle_id")
    metrics = e.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise EventRejected("metrics must be an object")
    spec = METRICS[stage]
    bad = sorted(set(metrics) - set(spec))
    if bad:
        raise EventRejected(f"metrics not allowlisted for {stage}: {bad}")
    out["metrics"] = {k: _value(spec[k], v, k) for k, v in metrics.items()}
    return out


def rule_codes(text: str) -> list[str]:
    """Allowlisted rule codes from a DETERMINISTIC harness/risk-checker
    rejection string (never model text): the §-citations it carries plus any
    named harness code. Unknown text yields an empty list."""
    codes = set(re.findall(r"§\d{1,2}[a-z]?(?:\.\d{1,2})?", text or ""))
    low = (text or "").lower()
    for name, needle in (("selector_boundary", "selector boundary"), ("superseded", "superseded"),
                         ("pre_fill_news_veto", "pre_fill_news_veto"),
                         ("pre_fill_news_unavailable", "pre_fill_news_unavailable")):
        if needle in low:
            codes.add(name)
    return sorted(c for c in codes if _RULE_CODE.match(c) or c in _NAMED_CODES)[:MAX_CODES]
