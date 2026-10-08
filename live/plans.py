"""Live trade plans: the compact, structured record Hermes must produce before
an entry, and the revision rules for holdings (live/LIVE_POLICY.md §3-§4).
Pure: no I/O. A plan is a dict; the original is preserved and every later
change is appended to `revisions`.
"""
from __future__ import annotations

import math
from typing import Any

MAX_HORIZON_TRADING_DAYS = 60
MIN_LOOKBACK, MAX_LOOKBACK = 5, 120
_STR_FIELDS = ("setup", "why_now", "invalidation", "target_rationale")


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _strs(x: Any) -> bool:
    return isinstance(x, list) and len(x) > 0 and all(isinstance(s, str) and s.strip() for s in x)


def validate_entry_plan(p: Any) -> list[str]:
    """Structural checks only; prices are checked against fresh market data
    by the engine. Returns a list of problems (empty = valid)."""
    if not isinstance(p, dict):
        return ["plan is not an object"]
    errs = []
    if not isinstance(p.get("ticker"), str) or not p["ticker"].strip():
        errs.append("ticker missing")
    for f in _STR_FIELDS:
        if not isinstance(p.get(f), str) or not p[f].strip():
            errs.append(f"{f} missing")
    if not _strs(p.get("evidence")):
        errs.append("evidence must be a non-empty list of strings")
    if not _strs(p.get("contrary_evidence")):
        errs.append("contrary_evidence must be a non-empty list (state what was checked if none was material)")
    if not _num(p.get("stop_price")):
        errs.append("stop_price must be a positive number")
    if not _num(p.get("target_price")):
        errs.append("target_price must be a positive number")
    sb = p.get("stop_basis")
    if not (isinstance(sb, dict) and sb.get("kind") == "observed_low" and _num(sb.get("level"))
            and isinstance(sb.get("lookback_sessions"), int)
            and MIN_LOOKBACK <= sb["lookback_sessions"] <= MAX_LOOKBACK):
        errs.append(f"stop_basis must be {{kind: observed_low, level, lookback_sessions {MIN_LOOKBACK}-{MAX_LOOKBACK}}}")
    elif _num(p.get("stop_price")) and p["stop_price"] > sb["level"] + 0.01:
        errs.append("stop_price must be at or below the cited observed low")
    h = p.get("horizon_trading_days")
    if not (isinstance(h, int) and not isinstance(h, bool) and 1 <= h <= MAX_HORIZON_TRADING_DAYS):
        errs.append(f"horizon_trading_days must be an integer 1-{MAX_HORIZON_TRADING_DAYS}")
    if not isinstance(p.get("next_review"), str) or not p["next_review"].strip():
        errs.append("next_review missing (what to check, and when)")
    if _num(p.get("stop_price")) and _num(p.get("target_price")) and p["target_price"] <= p["stop_price"]:
        errs.append("target must be above stop")
    return errs


def validate_revision(plan: dict, rev: Any, current_price: float | None) -> list[str]:
    """A holding decision of REVISE. Never widens downside protection: a new
    stop may only be higher (tighter) than the current one."""
    if not isinstance(rev, dict):
        return ["revision is not an object"]
    errs = []
    if not isinstance(rev.get("reason"), str) or not rev["reason"].strip():
        errs.append("revision needs a recorded reason")
    if not _strs(rev.get("evidence")):
        errs.append("revision needs fresh evidence (non-empty list)")
    changed = [k for k in ("stop_price", "target_price", "horizon_trading_days", "next_review") if k in rev]
    if not changed:
        errs.append("revision changes nothing")
    cur = current_plan(plan)
    if "stop_price" in rev:
        s = rev["stop_price"]
        if not _num(s):
            errs.append("stop_price must be a positive number")
        elif s < cur["stop_price"] - 1e-9:
            errs.append(f"stop may only be raised (current {cur['stop_price']}); never widened (RULES.md §6a/§9.2)")
        elif current_price is not None and s >= current_price:
            errs.append("new stop must stay below the current price")
    if "target_price" in rev:
        t = rev["target_price"]
        if not _num(t) or t <= rev.get("stop_price", cur["stop_price"]):
            errs.append("target must be a positive number above the stop")
    if "horizon_trading_days" in rev:
        h = rev["horizon_trading_days"]
        if not (isinstance(h, int) and not isinstance(h, bool) and 1 <= h <= MAX_HORIZON_TRADING_DAYS):
            errs.append(f"horizon_trading_days must be an integer 1-{MAX_HORIZON_TRADING_DAYS}")
    if "next_review" in rev and (not isinstance(rev["next_review"], str) or not rev["next_review"].strip()):
        errs.append("next_review must be text")
    return errs


def current_plan(plan: dict) -> dict:
    """The plan as REQUESTED after every revision the broker has not refused
    (the original is kept). Broker-confirmed protection is `plan["protected"]`,
    which can lag this until the replacement is verified."""
    cur = {k: plan["original"][k] for k in ("stop_price", "target_price", "horizon_trading_days", "next_review")}
    for r in plan.get("revisions", []):
        if r.get("status") != "rejected":
            cur.update({k: r[k] for k in cur if k in r})
    return cur
