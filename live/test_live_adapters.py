"""Adapter request/response contracts. The scripts that adapters.py sends into
the service containers are EXECUTED here against a stub `httpx`/`server`, so
the exact URLs, params, bodies and response handling are checked -- not just
that a fake broker was called. This does not prove Alpaca's real behaviour."""
import contextlib
import io
import json
import sys
import types
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import adapters  # noqa: E402
import engine  # noqa: E402
from adapters import AlpacaBroker, Journal  # noqa: E402

B, D = "https://paper-api.alpaca.markets", "https://data.alpaca.markets"


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Stub:
    """Routes (method, url) -> (status, body); records every request."""

    def __init__(self):
        self.routes, self.requests = {}, []

    def install(self, monkeypatch):
        stub = self

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def _go(self, method, url, params=None, json=None, headers=None):
                assert headers == {"APCA": "secret-not-printed"}, "auth headers come from the container's own helper"
                stub.requests.append({"method": method, "url": url, "params": params, "json": json})
                status, body = stub.routes[(method, url)]
                body = body(params) if callable(body) else body
                return Resp(status, body)

            async def get(self, url, **kw): return await self._go("GET", url, **kw)
            async def post(self, url, **kw): return await self._go("POST", url, **kw)
            async def delete(self, url, **kw): return await self._go("DELETE", url, **kw)

        async def fetch_account(): return {"equity": 1000.0, "cash": 900.0}
        async def fetch_positions(): return [{"symbol": "OLN", "qty": "5"}]
        async def news(s, a, b): return {"complete": True, "articles": [], "symbol": s}

        monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=Client))
        monkeypatch.setitem(sys.modules, "server", types.SimpleNamespace(
            _headers=lambda: {"APCA": "secret-not-printed"}, BASE_URL=B, DATA_URL=D, _fetch_account=fetch_account,
            _fetch_positions=fetch_positions, get_historical_news=news))

        def run_in(service, script, timeout=60):
            compile(script, f"<{service}>", "exec")           # the container would fail on a syntax error
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                try:
                    exec(script, {"__name__": "__main__"})
                except Exception as exc:                      # the container exits non-zero
                    raise RuntimeError(f"{service} failed: {exc}") from exc
            line = next(ln for ln in reversed(out.getvalue().splitlines()) if ln.startswith("OUT "))
            return json.loads(line[4:])
        monkeypatch.setattr(adapters, "run_in", run_in)
        return self


@pytest.fixture
def stub(monkeypatch):
    return Stub().install(monkeypatch)


def test_clock_account_positions_orders(stub):
    stub.routes[("GET", f"{B}/v2/clock")] = (200, {"is_open": True, "timestamp": "2026-10-08T11:00:00-04:00"})
    stub.routes[("GET", f"{B}/v2/orders")] = (200, [{"id": "o1", "status": "new"}])
    br = AlpacaBroker()
    assert br.clock()["is_open"] is True
    assert br.account() == {"equity": 1000.0, "cash": 900.0} and br.positions()[0]["symbol"] == "OLN"
    assert br.orders() == [{"id": "o1", "status": "new"}]
    assert stub.requests[-1]["params"] == {"status": "open", "nested": "true", "limit": 500}


def test_order_lookup_by_client_id_returns_none_on_404_and_the_order_otherwise(stub):
    url = f"{B}/v2/orders:by_client_order_id"
    br = AlpacaBroker()
    stub.routes[("GET", url)] = (404, {"message": "order not found"})
    assert br.order_by_client_id("lv-x-entry0") is None
    stub.routes[("GET", url)] = (200, {"id": "o1", "client_order_id": "lv-x-entry0", "status": "filled"})
    assert br.order_by_client_id("lv-x-entry0")["status"] == "filled"
    assert stub.requests[-1]["params"] == {"client_order_id": "lv-x-entry0", "nested": "true"}


def test_submit_posts_the_exact_body_and_a_broker_refusal_raises(stub):
    body = {"symbol": "OLN", "qty": "5", "side": "sell", "type": "limit", "time_in_force": "gtc",
            "order_class": "oco", "take_profit": {"limit_price": "60.00"}, "stop_loss": {"stop_price": "46.00"},
            "client_order_id": "lv-x-protect0"}
    stub.routes[("POST", f"{B}/v2/orders")] = (200, {"id": "o2", **body, "status": "new"})
    assert AlpacaBroker().submit(body)["id"] == "o2"
    assert stub.requests[-1]["json"] == body
    stub.routes[("POST", f"{B}/v2/orders")] = (422, {"message": "insufficient qty available"})
    with pytest.raises(RuntimeError):
        AlpacaBroker().submit(body)


def test_cancel_deletes_the_order_and_does_not_raise_on_an_already_terminal_one(stub):
    stub.routes[("DELETE", f"{B}/v2/orders/o2")] = (422, {"message": "order is not cancelable"})
    AlpacaBroker().cancel("o2")
    assert stub.requests[-1]["method"] == "DELETE" and stub.requests[-1]["url"] == f"{B}/v2/orders/o2"


def test_quote_uses_the_iex_feed_and_maps_fields(stub):
    stub.routes[("GET", f"{D}/v2/stocks/OLN/quotes/latest")] = (
        200, {"quote": {"ap": 50.1, "bp": 50.0, "t": "2026-10-08T14:59:58.123456789Z"}})
    q = AlpacaBroker().latest_quote("OLN")
    assert q == {"ask": 50.1, "bid": 50.0, "timestamp": "2026-10-08T14:59:58.123456789Z", "feed": "iex"}
    assert stub.requests[-1]["params"] == {"feed": "iex"}
    assert engine.quote_problem(q, engine.parse_ts("2026-10-08T15:00:03Z")) is None


def test_calendar_helpers_follow_the_broker_calendar(stub):
    cal = [{"date": "2026-10-06", "open": "09:30", "close": "16:00"},
           {"date": "2026-10-07", "open": "09:30", "close": "16:00"},
           {"date": "2026-10-08", "open": "09:30", "close": "13:00"}]
    stub.routes[("GET", f"{B}/v2/calendar")] = (200, lambda p: [d for d in cal if p["start"] <= d["date"] <= p["end"]])
    br = AlpacaBroker()
    assert br.session("2026-10-08") == {"open": "09:30", "close": "13:00"}
    assert br.sessions_since("2026-10-06", "2026-10-08") == 2
    assert br.last_completed_session("2026-10-08", True) == date(2026, 10, 7)
    assert br.last_completed_session("2026-10-08", False) == date(2026, 10, 8)


def test_news_is_requested_for_the_symbol_and_window(stub):
    out = AlpacaBroker().news("OLN", 72)
    assert out["complete"] is True and out["symbol"] == "OLN"


def test_journal_calls_compile_and_use_stable_ids(monkeypatch):
    sent = []

    def run_in(svc, script, timeout=60):
        compile(script, "<journal>", "exec")
        sent.append(script)
        return {"ok": True}
    monkeypatch.setattr(adapters, "run_in", run_in)
    j = Journal()
    plan = {"id": "abc123", "ticker": "OLN", "sizing": {"qty": 399, "notional_usd": 19989.9, "limit": 50.1},
            "original": {"target_price": 60.0, "stop_price": 46.0, "invalidation": "below 46", "setup": "pullback"},
            "exit_request": {"reason": "thesis broke"}}
    j.decision(plan)
    j.result(plan, "filled", fill_price=50.1, fill_qty=399)
    e = {"key": "exit0", "qty": 99, "requested": 399, "price": 52.0, "kind": "agent"}
    j.exit(plan, "agent_requested", e)
    j.exit(plan, "agent_requested", e)
    assert len(sent) == 6 and sent[2] == sent[4], "a repeated exit write is byte-identical (stable decision id)"
    assert all("_record_" in s and "execution_mode" in s for s in sent)
    assert "intended_quantity" in sent[2] and "fill_quantity" in sent[3] and "99" in sent[3]


def test_maverick_scripts_run_in_its_uv_environment_like_the_backtest_harness():
    assert adapters._RUNNERS["maverick-mcp"] == ["uv", "run", "python", "-"]
    import re
    harness = (HERE.parent / "backtest-harness" / "run_backtest.py").read_text(encoding="utf-8")
    assert re.search(r'"maverick-mcp": \["uv", "run", "python", "-"\]', harness)
