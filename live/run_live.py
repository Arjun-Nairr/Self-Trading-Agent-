"""Live Alpaca PAPER trading controller.

    python live/run_live.py setup-profile          # once: create the isolated `live` Hermes profile
    python live/run_live.py dry-run [--hermes]     # read-only: real broker/selector state, no orders
    python live/run_live.py cycle                  # one reconcile + review cycle now
    python live/run_live.py loop                   # scheduler: reconcile every 5 min, reviews ~hourly

All take --state-dir (default ~/.hermes-trading-agent-live, outside the repo),
--laya-checkpoint and --universe (screened | watchlist). Paper only: the broker
adapter runs inside alpaca-mcp, whose base URL is hardcoded to Alpaca's paper API.

Schedule (broker clock and calendar, never weekday guesses):
- **Reconcile** (no LLM) every RECONCILE_EVERY_MIN minutes while the market
  is open. Broker fills, protection and broker-held exits are therefore
  handled even when no model call succeeds.
- **Agent reviews** 30 minutes after the open, then about hourly, plus one
  session-end review in the last 20 minutes. A review is not a trade quota.
- One cycle at a time (OS-level CycleLock, released only by its holder or the
  OS). State is loaded INSIDE the lock each cycle, so two controllers or a
  restart can never overwrite each other from an earlier snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agent import build_prompt, holding_view, parse_reply  # noqa: E402
from engine import (CycleBusy, CycleLock, Store, enter, exit_position, parse_ts, reconcile,  # noqa: E402
                    revise)

REPO = HERE.parent
ET = ZoneInfo("America/New_York")
RECONCILE_EVERY_MIN = 5
REVIEW_MINUTES_AFTER_OPEN = (30, 90, 150, 210, 270, 330)
SESSION_END_WINDOW_MIN = 20
LIVE_MAX_TURNS = 60                       # bounded tool loop per research call
DEFAULT_STATE_DIR = Path.home() / ".hermes-trading-agent-live"
DEFAULT_CHECKPOINT = (Path.home() / ".cache/huggingface/hub/models--convaiinnovations--laya/snapshots"
                      / "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851")


def now_from(clock: dict) -> datetime:
    t = parse_ts(clock.get("timestamp"))
    if t is None:
        raise RuntimeError(f"broker clock timestamp invalid: {clock.get('timestamp')!r}")
    return t


def review_slot(clock: dict, session: dict | None, done: list[str]) -> str | None:
    """Which review (if any) is due now. Pure."""
    if not clock["is_open"] or not session:
        return None
    now = now_from(clock).astimezone(ET)
    day = now.date().isoformat()
    open_at = datetime.fromisoformat(f"{day}T{session['open']}").replace(tzinfo=ET)
    close_at = datetime.fromisoformat(f"{day}T{session['close']}").replace(tzinfo=ET)
    since, until = (now - open_at).total_seconds() / 60, (close_at - now).total_seconds() / 60
    slot = None
    if until <= SESSION_END_WINDOW_MIN:
        slot = f"{day}-end"
    else:
        passed = [m for m in REVIEW_MINUTES_AFTER_OPEN if since >= m and open_at.timestamp() + m * 60 < close_at.timestamp()]
        slot = f"{day}-{passed[-1]}" if passed else None
    return slot if slot and slot not in done else None


def run_cycle(store: Store, broker, journal, *, review: bool, selector_fn: Callable, hermes_fn: Callable,
              sleep=time.sleep) -> dict:
    """One cycle. Reconciliation always runs first and needs no LLM; a review
    then asks Hermes for decisions, which this controller validates and
    executes against evidence fetched at execution time. Any failure after
    reconciliation leaves broker protection as is. The caller holds the cycle
    lock and loaded `store` inside it."""
    clock = broker.clock()
    now = now_from(clock)
    today = now.astimezone(ET).date().isoformat()
    out = {"today": today, "reconcile": reconcile(store, broker, journal, today, sleep)}
    if not review:
        return out
    positions = {p["symbol"]: p for p in broker.positions()}
    live = [p for p in store.plans.values() if p["status"] in ("open", "entry_pending")]
    held = {p["ticker"] for p in live if p["status"] == "open"}
    universe: list[str] = []
    try:
        session = broker.last_completed_session(today, clock["is_open"])
        shortlist, sel_status, sel_event = selector_fn(date.fromisoformat(today), session,
                                                       {p["ticker"] for p in live} | set(positions))
        universe = sel_event.pop("universe", [])
    except Exception as exc:                       # selector failure blocks entries only
        shortlist, sel_status, sel_event = [], f"unavailable: {type(exc).__name__}: {str(exc)[:200]}", {}
    store.event("shortlist", status=sel_status, n=len(shortlist), selector=sel_event)
    views = [holding_view(p, positions.get(p["ticker"]), broker.sessions_since(p["created"][:10], today))
             for p in live if p["status"] == "open"]
    reassess = store.data.pop("reassess", [])      # consumed by this prompt
    prompt = build_prompt(now_et=now.astimezone(ET).isoformat(timespec="minutes"), account=broker.account(),
                          holdings=views,
                          open_orders=[{k: o.get(k) for k in ("symbol", "side", "type", "qty", "status", "order_class")}
                                       for o in broker.orders()],
                          shortlist=shortlist, selector_status=sel_status, unmanaged=store.data.get("unmanaged", {}),
                          reassess=reassess)
    decided_at = now.isoformat().replace("+00:00", "Z")   # the research cutoff: evidence after this was never reviewed
    store.save()
    try:
        raw, sha = hermes_fn(prompt)
    except Exception as exc:
        store.event("hermes_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        out["hermes"] = "failed"
        return out
    reply, errs = parse_reply(raw, held)
    store.event("hermes_reply", prompt_sha256=sha, raw=raw, problems=errs)   # private evidence
    if reply is None:
        out["hermes"] = f"rejected reply: {errs}"
        return out
    # Research can take minutes: broker facts (fills, protective exits) may have moved. Re-read before acting.
    today = now_from(broker.clock()).astimezone(ET).date().isoformat()
    out["reconcile_before_actions"] = reconcile(store, broker, journal, today, sleep)
    positions = {p["symbol"]: p for p in broker.positions()}
    by_ticker = {p["ticker"]: pid for pid, p in store.plans.items() if p["status"] == "open"}
    acts = []
    for h in reply["holdings"]:
        t = h["ticker"].upper()
        pid = by_ticker.get(t)
        if pid is None:
            acts.append({"ticker": t, "action": h["action"], "problems": ["no longer an open position"]})
            continue
        if h["action"] == "EXIT":
            r = exit_position(store, broker, journal, pid, h["reason"], today, sleep)
        elif h["action"] == "REVISE":
            price = (positions.get(t) or {}).get("current_price")
            r = revise(store, broker, pid, {**(h.get("revision") or {}), "reason": h["reason"],
                                            "evidence": h.get("evidence")},
                       float(price) if price is not None else None, decided_at, sleep)
        else:
            store.event("hold", plan_id=pid, reason=h["reason"])
            r = []
        acts.append({"ticker": t, "action": h["action"], "problems": r})
    entries = []
    for plan in reply["entries"]:
        res = enter(store, broker, journal, plan, shortlist=[c["ticker"] for c in shortlist], universe=universe,
                    decided_at=decided_at, today=today, sleep=sleep)
        store.event("entry_" + ("submitted" if res.ok else "rejected"),
                    ticker=str(plan.get("ticker")), reasons=res.reasons, plan_id=res.plan_id)
        if res.reassess:
            store.data.setdefault("reassess", []).append(str(plan.get("ticker")).upper())
        entries.append({"ticker": plan.get("ticker"), "ok": res.ok, "reasons": res.reasons})
    out.update(holdings=acts, entries=entries, no_entry_reason=reply.get("no_entry_reason"))
    out["reconcile_after"] = reconcile(store, broker, journal, today, sleep)
    return out


# -- CLI ----------------------------------------------------------------------------------------

class ReadOnlyBroker:
    """Dry-run guard: every state-changing broker call raises."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ("submit", "cancel", "replace"):
            raise RuntimeError(f"dry-run: {name} is disabled")
        return getattr(self._inner, name)


class NullJournal:
    def decision(self, *a, **k): pass
    def result(self, *a, **k): pass
    def exit(self, *a, **k): pass


# The ONLY MCP tools live Hermes may call. maverick-mcp also exposes portfolio/watchlist/journal writers,
# screen recomputation, ML training and web-research tools; none of those are research inputs here.
LIVE_MCP_TOOLS = {
    "maverick-mcp": ["market_data_get_price_history", "market_data_get_price_history_batch", "market_data_get_quote",
                     "market_data_get_stock_fundamentals", "market_data_get_market_overview",
                     "technical_get_rsi_analysis", "technical_get_macd_analysis",
                     "technical_get_support_resistance", "technical_get_full_technical_analysis"],
    "google-news-rss": ["fetch_headlines"],
}
_MCP_URL = {"maverick-mcp": "http://hermes-trading-agent-maverick-mcp:8000/mcp",
            "google-news-rss": "http://hermes-trading-agent-google-news-rss-mcp:8000/mcp"}


def live_profile_config(base: str) -> str:
    """The backtest profile's config (no built-in toolsets, memory off) with the research tools
    whitelisted and a bounded tool loop. Pure, so it is testable."""
    lines = ["mcp_servers:"]
    for name, tools in LIVE_MCP_TOOLS.items():
        lines += [f"  {name}:", f"    url: {_MCP_URL[name]}", "    enabled: true", "    tools:", "      include:"]
        lines += [f"        - {t}" for t in tools]
    if "mcp_servers: {}" not in base or "platform_toolsets:\n  cli: []" not in base:
        raise SystemExit("unexpected base profile config (needs no MCP servers and no built-in toolsets); "
                         "refusing to guess")
    cfg, n = re.subn(r"max_turns: \d+", f"max_turns: {LIVE_MAX_TURNS}",
                     base.replace("mcp_servers: {}", "\n".join(lines)))
    if n != 1:
        raise SystemExit("unexpected base profile config (max_turns); refusing to guess")
    return cfg


def setup_profile() -> None:
    """Create (once) and configure the isolated `live` Hermes profile. Idempotent; never touches the
    default or backtest profiles. Only whitelisted maverick + news tools: no execution, journal,
    terminal, file or write tools; memory off; bounded turns; the live persona."""
    import agent
    dc = ["docker", "compose", "exec", "-T", "gateway"]
    exists = subprocess.run(dc + ["test", "-d", agent.LIVE_HERMES_HOME], cwd=REPO).returncode == 0
    if not exists:
        subprocess.run(dc + ["hermes", "profile", "create", "live", "--clone-from", "backtest", "--no-alias"],
                       cwd=REPO, check=True)
    base = subprocess.run(dc + ["cat", "/opt/data/profiles/backtest/config.yaml"], cwd=REPO, check=True,
                          capture_output=True, text=True).stdout
    for name, text in (("config.yaml", live_profile_config(base)),
                       ("SOUL.md", (HERE / "LIVE_SOUL.md").read_text(encoding="utf-8"))):
        subprocess.run(dc + ["sh", "-c", f"cat > {agent.LIVE_HERMES_HOME}/{name}"], input=text, text=True,
                       cwd=REPO, check=True)
    subprocess.run(dc + ["mkdir", "-p", agent.LIVE_HERMES_WORKDIR], cwd=REPO, check=True)
    # exec runs as root; the Hermes process runs as `hermes` and must own its own profile
    subprocess.run(dc + ["chown", "-R", "hermes:hermes", agent.LIVE_HERMES_HOME], cwd=REPO, check=True)
    print("live profile ready:", agent.LIVE_HERMES_HOME)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["setup-profile", "dry-run", "cycle", "loop"])
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--laya-checkpoint", default=str(DEFAULT_CHECKPOINT))
    ap.add_argument("--universe", choices=["screened", "watchlist"], default="screened",
                    help="screened = the current eligible universe from the weekly screener (default); "
                         "watchlist = the frozen ten names, only when chosen explicitly")
    ap.add_argument("--hermes", action="store_true", help="dry-run: also ask Hermes (decisions are NOT executed)")
    a = ap.parse_args(argv)
    if a.command == "setup-profile":
        setup_profile()
        return 0
    from adapters import AlpacaBroker, Journal
    import agent
    from selector import LiveSelector
    state_dir = Path(a.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sel = LiveSelector(state_dir, a.laya_checkpoint, source=a.universe)
    try:
        if a.command == "dry-run":
            broker = ReadOnlyBroker(AlpacaBroker())
            hermes = agent.call_hermes if a.hermes else (lambda p: (_ for _ in ()).throw(RuntimeError("dry-run: no Hermes call")))
            with tempfile.TemporaryDirectory(prefix="live-dry-run-") as scratch:   # never touches real live state
                snap = Store(Path(scratch))
                snap.data = json.loads(json.dumps(Store(state_dir).data))
                print(json.dumps(run_cycle(snap, broker, NullJournal(), review=True, selector_fn=sel,
                                           hermes_fn=hermes), indent=1, default=str))
                print("dry-run events:", (Path(scratch) / "events.jsonl").read_text(encoding="utf-8")[:6000]
                      if (Path(scratch) / "events.jsonl").exists() else "", file=sys.stderr)
            return 0
        broker, journal = AlpacaBroker(), Journal()
        lock = state_dir / "cycle.lock"
        if a.command == "cycle":
            with CycleLock(lock):
                store = Store(state_dir)                          # state is loaded INSIDE exclusive ownership
                print(json.dumps(run_cycle(store, broker, journal, review=True, selector_fn=sel,
                                           hermes_fn=agent.call_hermes), indent=1, default=str))
            return 0
        while True:                                               # loop
            try:
                with CycleLock(lock):
                    store = Store(state_dir)                      # fresh state every tick
                    clock = broker.clock()
                    if clock["is_open"]:
                        today = now_from(clock).astimezone(ET).date().isoformat()
                        slot = review_slot(clock, broker.session(today), store.data.setdefault("reviews", []))
                        if slot:                                  # claimed before the work: a crash never repeats a review
                            store.data["reviews"].append(slot)
                            store.save()
                        r = run_cycle(store, broker, journal, review=bool(slot), selector_fn=sel,
                                      hermes_fn=agent.call_hermes)
                        print(json.dumps({"slot": slot, **r}, default=str)[:2000], flush=True)
            except CycleBusy as exc:
                print(f"skip: {exc}", flush=True)
            except Exception as exc:                              # log, keep the scheduler alive
                Store(state_dir).event("cycle_error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
                print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(RECONCILE_EVERY_MIN * 60)
    finally:
        sel.close()


if __name__ == "__main__":
    sys.exit(main())
