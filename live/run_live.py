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
import hashlib
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
from tracing.controller import NullTracer, code_version, diagnostic_drain, load_tracer  # noqa: E402
from tracing.live_ops import observed  # noqa: E402
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


def _tickers(shortlist) -> list:
    """Diagnostic only: must never raise, even for a malformed shortlist (tracing may not change trading)."""
    try:
        return [c.get("ticker") for c in shortlist]
    except Exception:
        return []


def run_cycle(store: Store, broker, journal, *, review: bool, selector_fn: Callable, hermes_fn: Callable,
              sleep=time.sleep, tracer=None) -> dict:
    """One traced cycle (tracing is observational only: see tracing/controller.py). See `_run_cycle`."""
    tr = tracer or NullTracer()
    with tr.trace("live_cycle", kind="chain", metadata={"review": review}) as root, \
            observed(store, broker, journal, tr) as (broker, journal):
        out = _run_cycle(store, broker, journal, review=review, selector_fn=selector_fn, hermes_fn=hermes_fn,
                         sleep=sleep, tr=tr)
        if not review:                                 # explicit, so an absent span is not mistaken for missing work
            root.set(metadata={"skipped": "selection, prompt, Hermes research and actions: reconcile-only tick"})
        root.set(output=out)
        return out


def _run_cycle(store: Store, broker, journal, *, review: bool, selector_fn: Callable, hermes_fn: Callable,
               sleep, tr) -> dict:
    """One cycle. Reconciliation always runs first and needs no LLM; a review
    then asks Hermes for decisions, which this controller validates and
    executes against evidence fetched at execution time. Any failure after
    reconciliation leaves broker protection as is. The caller holds the cycle
    lock and loaded `store` inside it."""
    clock = broker.clock()
    now = now_from(clock)
    today = now.astimezone(ET).date().isoformat()
    with tr.span("reconcile"):
        out = {"today": today, "reconcile": reconcile(store, broker, journal, today, sleep)}
    if not review:
        return out
    positions = {p["symbol"]: p for p in broker.positions()}
    live = [p for p in store.plans.values() if p["status"] in ("open", "entry_pending")]
    held = {p["ticker"] for p in live if p["status"] == "open"}
    universe: list[str] = []
    with tr.span("selection") as sp:
        try:
            session = broker.last_completed_session(today, clock["is_open"])
            shortlist, sel_status, sel_event = selector_fn(date.fromisoformat(today), session,
                                                           {p["ticker"] for p in live} | set(positions))
            universe = sel_event.pop("universe", [])
        except Exception as exc:                       # selector failure blocks entries only
            shortlist, sel_status, sel_event = [], f"unavailable: {type(exc).__name__}: {str(exc)[:200]}", {}
        sp.set(output={"status": sel_status, "n": len(shortlist), "universe_size": len(universe),
                       "tickers": _tickers(shortlist)})
    store.event("shortlist", status=sel_status, n=len(shortlist), selector=sel_event)
    views = [holding_view(p, positions.get(p["ticker"]), broker.sessions_since(p["created"][:10], today))
             for p in live if p["status"] == "open"]
    reassess = store.data.pop("reassess", [])      # consumed by this prompt
    with tr.span("prompt_assembly") as sp:
        order_keys = ("symbol", "side", "type", "qty", "status", "order_class")
        prompt = build_prompt(now_et=now.astimezone(ET).isoformat(timespec="minutes"), account=broker.account(),
                              holdings=views,
                              open_orders=[{k: o.get(k) for k in order_keys} for o in broker.orders()],
                              shortlist=shortlist, selector_status=sel_status,
                              unmanaged=store.data.get("unmanaged", {}), reassess=reassess)
        sp.set(output={"prompt_chars": len(prompt), "holdings": len(views), "reassess": list(reassess)})
    decided_at = now.isoformat().replace("+00:00", "Z")   # the research cutoff: evidence after this was never reviewed
    store.save()
    try:
        with tr.span("hermes_research", kind="agent") as hs:
            raw, sha = hermes_fn(prompt)
            hs.set(output=raw, metadata={"prompt_sha256": sha})
    except Exception as exc:
        store.event("hermes_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        out["hermes"] = "failed"
        return out
    with tr.span("reply_validation") as sp:
        reply, errs = parse_reply(raw, held)
        sp.set(output={"accepted": reply is not None, "problems": errs})
    store.event("hermes_reply", prompt_sha256=sha, raw=raw, problems=errs)   # private evidence
    if reply is None:
        out["hermes"] = f"rejected reply: {errs}"
        return out
    # Research can take minutes: broker facts (fills, protective exits) may have moved. Re-read before acting.
    today = now_from(broker.clock()).astimezone(ET).date().isoformat()
    with tr.span("reconcile_before_actions"):
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
        with tr.span("holding_action", input=h, metadata={"ticker": t, "action": h["action"], "plan_id": pid}) as sp:
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
            sp.set(output={"problems": r})
        acts.append({"ticker": t, "action": h["action"], "problems": r})
    entries = []
    for plan in reply["entries"]:
        with tr.span("entry_validation", input=plan, metadata={"ticker": str(plan.get("ticker"))}) as sp:
            res = enter(store, broker, journal, plan, shortlist=[c["ticker"] for c in shortlist], universe=universe,
                        decided_at=decided_at, today=today, sleep=sleep)
            sp.set(output={"ok": res.ok, "reasons": res.reasons, "reassess": res.reassess, "plan_id": res.plan_id})
        store.event("entry_" + ("submitted" if res.ok else "rejected"),
                    ticker=str(plan.get("ticker")), reasons=res.reasons, plan_id=res.plan_id)
        if res.reassess:
            store.data.setdefault("reassess", []).append(str(plan.get("ticker")).upper())
        entries.append({"ticker": plan.get("ticker"), "ok": res.ok, "reasons": res.reasons})
    out.update(holdings=acts, entries=entries, no_entry_reason=reply.get("no_entry_reason"))
    with tr.span("reconcile_after"):
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


TRACE_PLUGIN = "hermes-trace"


def live_profile_config(base: str, tracing: bool = False) -> str:
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
    if tracing:                                            # live profile only; the stock langfuse plugin stays off
        if (chr(10) + "plugins:") in cfg:
            raise SystemExit("unexpected base profile config (plugins); refusing to guess")
        cfg = cfg.rstrip(chr(10)) + f"{chr(10)}plugins:{chr(10)}  enabled:{chr(10)}    - {TRACE_PLUGIN}{chr(10)}"
    return cfg


def trace_plugin_files() -> dict[str, str]:
    t = HERE / "tracing"
    return {"plugin.yaml": (t / "hermes_plugin" / "plugin.yaml").read_text(encoding="utf-8"),
            "__init__.py": (t / "hermes_plugin" / "__init__.py").read_text(encoding="utf-8"),
            "otlp.py": (t / "otlp.py").read_text(encoding="utf-8")}


def trace_profile_env(host_cfg: dict[str, str]) -> dict[str, str]:
    """HERMES_TRACE_* settings for the live profile's .env, derived from the host-side tracing.env."""
    m = {"LANGFUSE_PUBLIC_KEY": "HERMES_TRACE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY": "HERMES_TRACE_SECRET_KEY",
         "LANGFUSE_BASE_URL": "HERMES_TRACE_BASE_URL"}
    return {dst: host_cfg[src] for src, dst in m.items() if host_cfg.get(src)}


def merge_env_text(existing: str, updates: dict[str, str]) -> str:
    """Replace every HERMES_TRACE_* line with `updates` (possibly empty), leaving all other lines untouched."""
    def name(ln: str) -> str:
        return ln.split("=", 1)[0].strip()
    keep = [ln for ln in existing.splitlines() if not name(ln).startswith("HERMES_TRACE_") and name(ln) not in updates]
    lines = keep + [f"{k}={v}" for k, v in updates.items()]
    return (chr(10).join(lines) + chr(10)) if lines else ""


def setup_profile(state_dir: Path | None = None) -> None:
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
    from tracing.controller import ENV_FILE, read_env_file
    tracing_cfg = {}
    if state_dir is not None and (Path(state_dir) / ENV_FILE).is_file():
        tracing_cfg = trace_profile_env(read_env_file(Path(state_dir) / ENV_FILE))
    tracing = bool(tracing_cfg.get("HERMES_TRACE_PUBLIC_KEY") and tracing_cfg.get("HERMES_TRACE_SECRET_KEY"))
    for name, text in (("config.yaml", live_profile_config(base, tracing)),
                       ("SOUL.md", (HERE / "LIVE_SOUL.md").read_text(encoding="utf-8"))):
        subprocess.run(dc + ["sh", "-c", f"cat > {agent.LIVE_HERMES_HOME}/{name}"], input=text, text=True,
                       cwd=REPO, check=True)
    subprocess.run(dc + ["mkdir", "-p", agent.LIVE_HERMES_WORKDIR], cwd=REPO, check=True)
    pdir = f"{agent.LIVE_HERMES_HOME}/plugins/{TRACE_PLUGIN}"
    if tracing:
        subprocess.run(dc + ["mkdir", "-p", pdir], cwd=REPO, check=True)
        for fname, text in trace_plugin_files().items():
            subprocess.run(dc + ["sh", "-c", f"cat > {pdir}/{fname}"], input=text, text=True, cwd=REPO, check=True)
    else:
        subprocess.run(dc + ["rm", "-rf", pdir], cwd=REPO, check=True)   # not configured: nothing installed
    envp = f"{agent.LIVE_HERMES_HOME}/.env"                          # secrets travel on stdin, never argv or stdout
    cur = subprocess.run(dc + ["sh", "-c", f"cat {envp} 2>/dev/null || true"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout
    subprocess.run(dc + ["sh", "-c", f"cat > {envp}"], input=merge_env_text(cur, tracing_cfg if tracing else {}),
                   text=True, cwd=REPO, check=True)
    # exec runs as root; the Hermes process runs as `hermes` and must own its own profile
    subprocess.run(dc + ["chown", "-R", "hermes:hermes", agent.LIVE_HERMES_HOME], cwd=REPO, check=True)
    print("live profile ready:", agent.LIVE_HERMES_HOME, "| tracing plugin:", "installed" if tracing else "off")


def policy_version() -> str:
    """Short digest of the live policy and persona the agent runs under (diagnostic identity only)."""
    h = hashlib.sha256()
    for name in ("LIVE_POLICY.md", "LIVE_SOUL.md"):
        try:
            h.update((HERE / name).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["setup-profile", "dry-run", "cycle", "loop"])
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--laya-checkpoint", default=str(DEFAULT_CHECKPOINT))
    ap.add_argument("--universe", choices=["screened", "watchlist"], default="screened",
                    help="screened = the current eligible universe from the weekly screener (default); "
                         "watchlist = the frozen ten names, only when chosen explicitly")
    ap.add_argument("--hermes", action="store_true", help="dry-run: also ask Hermes (decisions are NOT executed)")
    ap.add_argument("--trace-drain", type=float, default=None, metavar="SECONDS",
                    help="DIAGNOSTIC, dry-run only: wait up to SECONDS (max 10) for the last trace spans of this "
                         "process and of the Hermes call before exiting. Never available to cycle/loop.")
    a = ap.parse_args(argv)
    if a.trace_drain is not None and (a.command != "dry-run" or not 0 < a.trace_drain <= 10):
        ap.error("--trace-drain is a diagnostic for `dry-run` only, with 0 < SECONDS <= 10")
    if a.command == "setup-profile":
        setup_profile(Path(a.state_dir))
        return 0
    from adapters import AlpacaBroker, Journal
    import agent
    from selector import LiveSelector
    state_dir = Path(a.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sel = LiveSelector(state_dir, a.laya_checkpoint, source=a.universe)
    tracer = load_tracer(state_dir, release=code_version())      # NullTracer unless <state-dir>/tracing.env is valid
    tracer.context = {"mode": a.command, "universe_source": a.universe, "hermes": bool(a.hermes),
                      "orders": "disabled (read-only broker)" if a.command == "dry-run" else "enabled",
                      "policy_sha256": policy_version()}
    sel.tracer = tracer
    drain_env = {"HERMES_TRACE_DIAGNOSTIC_DRAIN_S": f"{a.trace_drain:.1f}"} if a.trace_drain else {}
    hermes_traced = lambda p: agent.call_hermes(p, trace_env={**tracer.child_env(), **drain_env})  # noqa: E731
    try:
        if a.command == "dry-run":
            broker = ReadOnlyBroker(AlpacaBroker())
            hermes = hermes_traced if a.hermes else (lambda p: (_ for _ in ()).throw(RuntimeError("dry-run: no Hermes call")))
            with tempfile.TemporaryDirectory(prefix="live-dry-run-") as scratch:   # never touches real live state
                snap = Store(Path(scratch))
                snap.data = json.loads(json.dumps(Store(state_dir).data))
                print(json.dumps(run_cycle(snap, broker, NullJournal(), review=True, selector_fn=sel,
                                           hermes_fn=hermes, tracer=tracer), indent=1, default=str))
                print("dry-run events:", (Path(scratch) / "events.jsonl").read_text(encoding="utf-8")[:6000]
                      if (Path(scratch) / "events.jsonl").exists() else "", file=sys.stderr)
            if a.trace_drain:                                     # DIAGNOSTIC (orders disabled): see diagnostic_drain
                print("trace drain (diagnostic):", json.dumps(diagnostic_drain(tracer, a.trace_drain)), file=sys.stderr)
            return 0
        broker, journal = AlpacaBroker(), Journal()
        lock = state_dir / "cycle.lock"
        if a.command == "cycle":
            with CycleLock(lock):
                store = Store(state_dir)                          # state is loaded INSIDE exclusive ownership
                print(json.dumps(run_cycle(store, broker, journal, review=True, selector_fn=sel,
                                           hermes_fn=hermes_traced, tracer=tracer), indent=1, default=str))
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
                                      hermes_fn=hermes_traced, tracer=tracer)
                        print(json.dumps({"slot": slot, **r}, default=str)[:2000], flush=True)
            except CycleBusy as exc:
                print(f"skip: {exc}", flush=True)
            except Exception as exc:                              # log, keep the scheduler alive
                Store(state_dir).event("cycle_error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
                print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(RECONCILE_EVERY_MIN * 60)
    finally:
        sel.close()
        tracer.close()                                           # never waits: unsent telemetry is dropped


if __name__ == "__main__":
    sys.exit(main())
