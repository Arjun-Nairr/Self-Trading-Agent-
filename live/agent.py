"""Live Hermes seam: build the cycle prompt, call Hermes's own agent/tool loop
in the isolated `live` profile, and parse its JSON reply strictly. Hermes
chooses its own research tools (maverick technicals, news); it has no order,
journal or shell tool, so it cannot act outside the controller."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from plans import current_plan

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LIVE_HERMES_HOME = "/opt/data/profiles/live"
LIVE_HERMES_WORKDIR = f"{LIVE_HERMES_HOME}/workspace"   # empty: no shared AGENTS.md/SOUL.md auto-injection
HERMES_TIMEOUT_S = 900                                   # bounded model + tool work per cycle
ACTIONS = {"HOLD", "EXIT", "REVISE"}


def build_prompt(*, now_et: str, account: dict, holdings: list[dict], open_orders: list[dict],
                 shortlist: list[dict], selector_status: str, unmanaged: dict, reassess: list[str] = ()) -> str:
    rules = (REPO / "RULES.md").read_text(encoding="utf-8")
    policy = (HERE / "LIVE_POLICY.md").read_text(encoding="utf-8")
    state = {"now_et": now_et,
             "account": {k: account.get(k) for k in ("equity", "cash", "exposure_pct", "drawdown_from_peak_pct",
                                                     "trading_blocked")},
             "holdings": holdings, "open_orders": open_orders, "unmanaged_positions": unmanaged,
             "shortlist": shortlist, "shortlist_status": selector_status,
             "reassess": {"tickers": list(reassess),
                          "why": "news was published after your last research cutoff; the entry was not executed. "
                                 "Re-research these before deciding again."} if reassess else None}
    return (f"===== LIVE_POLICY.md (authoritative for live mode; overrides RULES.md where it says so) =====\n"
            f"{policy}\n===== RULES.md (numeric law; see LIVE_POLICY.md §1-§2) =====\n{rules}\n"
            f"===== CURRENT STATE (reconciled with the broker just now) =====\n{json.dumps(state, indent=1)}\n"
            "Research with your tools as needed, then reply with ONLY the JSON object in LIVE_POLICY.md §6.")


def holding_view(plan: dict, position: dict | None, trading_days_held: int | None) -> dict:
    cur = current_plan(plan)
    return {"ticker": plan["ticker"], "qty": plan.get("held_qty"),
            "entry_avg_price": (plan.get("entry_result") or {}).get("avg_price"),
            "current_price": (position or {}).get("current_price"),
            "unrealized_pl": (position or {}).get("unrealized_pl"),
            "protection": plan.get("protection"),
            # broker-verified protection vs what is merely requested: they differ until a change is confirmed
            "protection_confirmed": plan.get("protected"),
            "revisions": [{k: r.get(k) for k in ("at", "status", "stop_price", "target_price", "horizon_trading_days",
                                                 "next_review", "reason")} for r in plan.get("revisions", [])],
            "plan_original": plan["original"], "plan_current": cur, "trading_days_held": trading_days_held,
            "past_intended_horizon": (trading_days_held is not None
                                      and trading_days_held >= cur["horizon_trading_days"])}


def parse_reply(raw: str, held: set[str]) -> tuple[dict | None, list[str]]:
    """Strict: one JSON object; one holdings decision per held ticker; known
    actions only. Returns (reply, problems) -- a malformed reply means NO
    actions this cycle (broker protection is unaffected)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        r = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None, ["reply is not a JSON object"]
    errs = []
    if not isinstance(r, dict) or not isinstance(r.get("holdings"), list) or not isinstance(r.get("entries"), list):
        return None, ["reply needs holdings[] and entries[]"]
    seen = set()
    for h in r["holdings"]:
        if not isinstance(h, dict) or h.get("action") not in ACTIONS or not isinstance(h.get("reason"), str) \
                or not h["reason"].strip():
            errs.append(f"bad holdings item {str(h)[:80]}")
            continue
        seen.add(str(h.get("ticker", "")).upper())
    if seen != held:
        errs.append(f"holdings decisions {sorted(seen)} do not match held {sorted(held)}")
    if not r["entries"] and not str(r.get("no_entry_reason") or "").strip():
        errs.append("no entries and no no_entry_reason")
    return (None if errs else r), errs


def call_hermes(prompt: str) -> tuple[str, str]:
    """(raw reply, prompt sha256). Prompt travels over stdin, never argv
    (same mechanism as the backtest's _call_hermes)."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", f"HERMES_HOME={LIVE_HERMES_HOME}", "--workdir",
         LIVE_HERMES_WORKDIR, "gateway", "xargs", "-0", "-I{}", "hermes", "-z", "{}"],
        input=prompt.encode("utf-8"), capture_output=True, cwd=REPO, timeout=HERMES_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"hermes failed: {result.stderr[-800:].decode('utf-8', 'replace')}")
    return result.stdout.decode("utf-8", "replace"), hashlib.sha256(prompt.encode("utf-8")).hexdigest()
