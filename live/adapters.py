"""Real I/O for the live engine. Every call runs inside the existing service
container that already holds the credential (the hub-and-spoke pattern the
backtest uses): Alpaca paper REST inside `alpaca-mcp`, which is hardcoded to
the paper base URL; daily bars inside `maverick-mcp`, the same price source
Hermes's technical tools read; journal writes inside `trade-journal`. No
credential reaches this host process.

Tests replace these with deterministic fakes (see live/test_live.py)."""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# maverick's dependencies live in its uv environment (same invocation as the backtest harness)
_RUNNERS = {"alpaca-mcp": ["python", "-"], "maverick-mcp": ["uv", "run", "python", "-"],
            "trade-journal": ["python", "-"]}


def run_in(service: str, script: str, timeout: int = 60) -> dict:
    r = subprocess.run(["docker", "compose", "exec", "-T", service, *_RUNNERS[service]], input=script,
                       capture_output=True, text=True, cwd=REPO, timeout=timeout, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"{service} failed: {r.stderr[-600:]}")
    line = next(ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("OUT "))
    return json.loads(line[4:])


def _alpaca(body: str, **params) -> object:
    """Run `body` inside alpaca-mcp with `c` (httpx client), `H` (auth headers,
    never printed), `B`/`D` (paper/data base URLs) and `P` (params); it must
    set `out`."""
    script = ("import asyncio, json, httpx\nfrom server import _headers, BASE_URL as B, DATA_URL as D, "
              "_fetch_account, _fetch_positions, get_historical_news\n"
              f"P = json.loads({json.dumps(json.dumps(params))})\n"
              "async def main():\n    H = _headers()\n    async with httpx.AsyncClient(timeout=15) as c:\n"
              + "".join(f"        {ln}\n" for ln in body.strip().splitlines())
              + "    return out\nprint('OUT ' + json.dumps({'v': asyncio.run(main())}, default=str))\n")
    return run_in("alpaca-mcp", script)["v"]


def card_history(tickers, day: date, chunk: int = 20) -> dict:
    """Daily OHLCV for the selector's cards: maverick's price source in
    bounded chunks, so a large screened pool cannot hit one long call."""
    import sys
    sys.path.insert(0, str(REPO / "backtest-harness"))
    from laya_cards import history_script
    out: dict = {}
    for i in range(0, len(tickers), chunk):
        script = history_script(list(tickers[i:i + chunk]), day)
        script = script.replace("print(json.dumps(", "print('OUT ' + json.dumps(")
        out.update(run_in("maverick-mcp", script, timeout=300)["history"])
    return out


class AlpacaBroker:
    def clock(self) -> dict:
        return _alpaca("r = await c.get(f'{B}/v2/clock', headers=H); r.raise_for_status(); out = r.json()")

    def account(self) -> dict:
        return _alpaca("out = await _fetch_account()")

    def positions(self) -> list:
        return _alpaca("out = await _fetch_positions()")

    def orders(self) -> list:
        return _alpaca("r = await c.get(f'{B}/v2/orders', params={'status': 'open', 'nested': 'true', "
                       "'limit': 500}, headers=H); r.raise_for_status(); out = r.json()")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        return _alpaca("r = await c.get(f'{B}/v2/orders:by_client_order_id', params={'client_order_id': "
                       "P['id'], 'nested': 'true'}, headers=H)\nout = None if r.status_code == 404 else "
                       "(r.raise_for_status() or r.json())", id=client_order_id)

    def submit(self, body: dict) -> dict:
        return _alpaca("r = await c.post(f'{B}/v2/orders', json=P['b'], headers=H); r.raise_for_status(); "
                       "out = r.json()", b=body)

    def cancel(self, order_id: str) -> None:
        _alpaca("r = await c.delete(f'{B}/v2/orders/' + P['id'], headers=H)\nout = r.status_code", id=order_id)

    def replace(self, order_id: str, fields: dict) -> dict:
        return _alpaca("r = await c.patch(f'{B}/v2/orders/' + P['id'], json=P['f'], headers=H); "
                       "r.raise_for_status(); out = r.json()", id=order_id, f=fields)

    def latest_quote(self, symbol: str) -> dict | None:
        q = _alpaca("r = await c.get(f'{D}/v2/stocks/' + P['s'] + '/quotes/latest', params={'feed': 'iex'}, "
                    "headers=H); r.raise_for_status(); out = r.json().get('quote')", s=symbol)
        return {"ask": q.get("ap"), "bid": q.get("bp"), "timestamp": q.get("t"), "feed": "iex"} if q else None

    def news(self, symbol: str, lookback_hours: int) -> dict:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        return _alpaca("out = await get_historical_news(P['s'], P['a'], P['b'])", s=symbol,
                       a=start.isoformat().replace("+00:00", "Z"), b=end.isoformat().replace("+00:00", "Z"))

    def sessions_since(self, day: str, today: str) -> int:
        """Completed + current trading sessions after `day` up to `today`."""
        cal = _alpaca("r = await c.get(f'{B}/v2/calendar', params={'start': P['a'], 'end': P['b']}, headers=H); "
                      "r.raise_for_status(); out = r.json()", a=day, b=today)
        return sum(1 for d in cal if day < d["date"] <= today)

    def session(self, day: str) -> dict | None:
        cal = _alpaca("r = await c.get(f'{B}/v2/calendar', params={'start': P['d'], 'end': P['d']}, headers=H); "
                      "r.raise_for_status(); out = r.json()", d=day)
        return {"open": cal[0]["open"], "close": cal[0]["close"]} if cal else None

    def last_completed_session(self, today: str, is_open: bool) -> date:
        start = (date.fromisoformat(today) - timedelta(days=14)).isoformat()
        cal = _alpaca("r = await c.get(f'{B}/v2/calendar', params={'start': P['a'], 'end': P['b']}, headers=H); "
                      "r.raise_for_status(); out = r.json()", a=start, b=today)
        done = [d["date"] for d in cal if d["date"] < today or not is_open]
        return date.fromisoformat(max(done))

    def daily_bars(self, symbol: str, n: int) -> list:
        """The last n daily bars from maverick's price source -- what Hermes's
        own technical tools read (today's bar included while it is open)."""
        import sys
        sys.path.insert(0, str(REPO / "backtest-harness"))
        from laya_cards import history_script
        today = datetime.now(timezone.utc).date()
        script = history_script([symbol], today, start=today - timedelta(days=2 * n + 30))
        script = script.replace("print(json.dumps(", "print('OUT ' + json.dumps(")
        bars = run_in("maverick-mcp", script, timeout=120)["history"].get(symbol)
        return bars[-n:] if isinstance(bars, list) else []


class Journal:
    """Existing trades table, execution_mode 'alpaca'. Idempotent: decision
    ids are derived from the plan id; a repeated result is a no-op there."""

    @staticmethod
    def _id(plan: dict, kind: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"live/{plan['id']}/{kind}"))

    def _call(self, fn: str, **kw) -> None:
        run_in("trade-journal", "import asyncio, json\nfrom server import " + fn +
               f"\nkw = json.loads({json.dumps(json.dumps(kw, default=str))})\n"
               f"r = asyncio.run({fn}(**kw))\nprint('OUT ' + json.dumps({{'ok': True}}))\n")

    def decision(self, plan: dict) -> None:
        o, s = plan["original"], plan["sizing"]
        self._call("_record_decision", ticker=plan["ticker"], action="BUY", intended_quantity=s["qty"],
                   intended_notional=s["notional_usd"], order_type="LIMIT", confidence=None,
                   primary_signal="technical", expected_reward_risk=round(
                       (o["target_price"] - s["limit"]) / (s["limit"] - o["stop_price"]), 2),
                   invalidation_condition=o["invalidation"], limit_price=s["limit"],
                   technical_basis="support_resistance", strategy_notes=f"live plan {plan['id']}: {o['setup']}"[:900],
                   decision_id=self._id(plan, "entry"), stop_price=o["stop_price"], target_price=o["target_price"],
                   execution_mode="alpaca", proposed_entry_price=s["limit"])

    def result(self, plan: dict, status: str, broker_order_id=None, fill_price=None, fill_qty=None, reason=None):
        self._call("_record_execution_result", decision_id=self._id(plan, "entry"), status=status,
                   broker_order_id=broker_order_id, fill_price=fill_price, fill_quantity=fill_qty,
                   rejection_reason=reason, execution_mode="alpaca")

    def exit(self, plan: dict, provenance: str, e: dict) -> None:
        """One SELL row per exit order that actually filled: `e` carries the
        REAL filled qty/price (and the requested qty), never an assumption."""
        did = self._id(plan, e["key"])
        qty, price = e.get("qty") or 0, e.get("price") or 0
        reason = (plan.get("exit_request") or {}).get("reason") or ""
        self._call("_record_decision", ticker=plan["ticker"], action="SELL",
                   intended_quantity=e.get("requested") or qty, intended_notional=qty * price,
                   order_type="MARKET", confidence=None, primary_signal="technical", expected_reward_risk=0.0,
                   invalidation_condition="exit", decision_id=did, execution_mode="alpaca",
                   strategy_notes=f"live plan {plan['id']} exit: {e.get('kind')} {reason}"[:900],
                   exit_provenance=provenance)
        self._call("_record_execution_result", decision_id=did, status="filled", fill_price=e.get("price"),
                   fill_quantity=qty, execution_mode="alpaca")
