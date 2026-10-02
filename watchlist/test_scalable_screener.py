"""Tests for watchlist/scalable_screener.py (Stage 1). Every provider is a
fake: no network, no Docker, no real company data."""
import contextlib
import copy
import hashlib
import io
import json
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scalable_screener as ss  # noqa: E402

SAT = date(2026, 10, 3)
NOON = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
GOOD = {"name": "Good Co", "sector": "Industrials", "industry": "Machinery",
        "market_cap": 5e9, "avg_volume": 3e6, "price": 40.0}


def ticking(start=NOON):
    """A deterministic clock: one second later on every call."""
    t = [start]

    def clock():
        t[0] += timedelta(seconds=1)
        return t[0]
    return clock


class Fake:
    """Injected providers. `facts`/`history`/`news` map symbol -> entry;
    anything absent gets a passing default. Records which symbols each stage
    was asked for."""

    def __init__(self, symbols, facts=None, history=None, news=None, fail=(), stats=None):
        self.symbols, self.f, self.h, self.n, self.fail = list(symbols), facts or {}, history or {}, news or {}, fail
        self.stats, self.asked = stats, {}

    def universe(self):
        if "universe" in self.fail:
            raise RuntimeError("wiki down, password=hunter2")
        return {"source": "fake", "url": "fake://u", "symbols": self.symbols}

    def _stage(self, name, symbols, table, default):
        self.asked[name] = list(symbols)
        if name in self.fail:
            raise RuntimeError(f"{name} down, password=hunter2")
        rows = {s: table.get(s, default) for s in symbols}
        return {"rows": {s: v for s, v in rows.items() if v is not None}, "stats": self.stats}

    def facts(self, symbols):
        return self._stage("facts", symbols, self.f, GOOD)

    def history(self, symbols):
        return self._stage("history", symbols, self.h, {"bars": 1200})

    def news(self, symbols, windows):
        self.windows = list(windows)
        return self._stage("news", symbols, self.n, [{"articles": 3}] * len(windows))


def names(n):
    return [f"S{i:03d}" for i in range(n)]


def row(snap, symbol):
    return next(r for r in snap["rows"] if r["symbol"] == symbol)


def rehash(snap):
    snap["content_hash"] = ss._content_hash(snap)
    return snap


# 1. the fixed-10 path is untouched -------------------------------------

FIXED_10_FILES = {
    "watchlist.json": "816221cbe3b28f23d27647b4054859347cb207274aa64269cedbb5b41c1a75b6",
    "screen_watchlist.py": "6223191b88ea550ff9ef6d9e4788437c4e3b53bad7e1fa1b74ec55f1ec506235",
    "check_news_coverage.py": "dbd87d570cf5fbf7827af8449f382a7b69d57d4b101ae3acfd8316d27052b996",
}


@pytest.mark.parametrize("name,expected", FIXED_10_FILES.items())
def test_fixed_10_files_are_byte_identical(name, expected):
    data = (HERE / name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == expected


def test_fixed_10_watchlist_still_ten_and_new_module_is_isolated():
    assert len(json.loads((HERE / "watchlist.json").read_text())["tickers"]) == 10
    src = (HERE / "scalable_screener.py").read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    for banned in ("screen_watchlist", "check_news_coverage", "run_backtest", "laya", "risk_checker"):
        assert not any(banned in ln for ln in imports)
    harness = HERE.parent / "backtest-harness"
    assert not any("scalable_screener" in p.read_text(encoding="utf-8", errors="ignore")
                   for p in harness.glob("*.py"))


# 2. statuses and ordered reason codes ----------------------------------

@pytest.mark.parametrize("change,status,reasons", [
    ({}, None, []),
    ({"market_cap": 1e9}, "rejected", ["market_cap_below_min"]),
    ({"market_cap": 6e10}, "rejected", ["market_cap_above_max"]),
    ({"avg_volume": 5e5}, "rejected", ["avg_volume_below_min"]),
    ({"avg_volume": 2e7}, "rejected", ["avg_volume_above_max"]),
    ({"price": 9.99}, "rejected", ["price_below_min"]),
    ({"industry": "Biotechnology"}, "rejected", ["industry_excluded"]),
    ({"market_cap": 1e9, "avg_volume": 2e7, "price": 5.0, "industry": "Biotechnology"}, "rejected",
     ["market_cap_below_min", "avg_volume_above_max", "price_below_min", "industry_excluded"]),
    ({"market_cap": 2e9, "avg_volume": 1.2e7, "price": 10.0}, None, []),  # bounds are inclusive
    ({"market_cap": None, "price": float("nan")}, "data_unavailable", ["market_cap_missing", "price_missing"]),
    ({"avg_volume": True}, "data_unavailable", ["avg_volume_missing"]),
    ({"industry": ""}, "data_unavailable", ["industry_missing"]),
])
def test_facts_outcomes(change, status, reasons):
    assert ss.evaluate_facts({**GOOD, **change}, ss.DEFAULT_CONFIG)[1:] == (status, reasons)


def test_fetch_errors_and_missing_entries_are_data_unavailable():
    cfg = ss.DEFAULT_CONFIG
    assert ss.evaluate_facts({"error": "x"}, cfg)[1:] == ("data_unavailable", ["facts_fetch_error"])
    assert ss.evaluate_facts(None, cfg)[1:] == ("data_unavailable", ["facts_missing"])
    assert ss.evaluate_history({"error": "x"}, cfg) == (None, "data_unavailable", ["history_fetch_error"])
    assert ss.evaluate_history(None, cfg) == (None, "data_unavailable", ["history_missing"])
    assert ss.evaluate_history({"bars": 749}, cfg) == (749, "rejected", ["history_below_min_bars"])
    assert ss.evaluate_history({"bars": 750}, cfg) == (750, None, [])
    assert ss.evaluate_news(None, 3, cfg) == (None, "data_unavailable", ["news_missing"])
    assert ss.evaluate_news([{"articles": 5}, {"error": "incomplete_pagination"}, {"articles": True}], 3, cfg) == (
        [5, None, None], "data_unavailable", ["news_probe_2_unavailable", "news_probe_3_unavailable"])
    assert ss.evaluate_news([{"articles": 2}, {"articles": 2}, {"articles": 1}], 3, cfg) == (
        [2, 2, 1], "rejected", ["news_coverage_below_min"])
    assert ss.evaluate_news([{"articles": 6}, {"articles": 0}, {"articles": 0}], 3, cfg) == (
        [6, 0, 0], "eligible", [])


def test_cost_order_stops_at_the_first_deciding_stage():
    syms = names(40)
    fake = Fake(syms, facts={"S001": {**GOOD, "price": 1.0}}, history={"S002": {"bars": 10}},
                news={"S003": [{"articles": 0}] * 3})
    snap = ss.run_screen(SAT, fake)
    assert fake.asked["facts"] == syms
    assert "S001" not in fake.asked["history"] and "S002" in fake.asked["history"]
    assert "S002" not in fake.asked["news"] and "S003" in fake.asked["news"]
    assert (row(snap, "S001")["status"], row(snap, "S001")["history_bars"]) == ("rejected", None)
    assert (row(snap, "S002")["reasons"], row(snap, "S002")["news_counts"]) == (["history_below_min_bars"], None)
    assert row(snap, "S003")["reasons"] == ["news_coverage_below_min"]
    assert row(snap, "S004") == {"symbol": "S004", **GOOD, "history_bars": 1200, "news_counts": [3, 3, 3],
                                 "status": "eligible", "reasons": []}
    assert snap["counts"] == {"eligible": 37, "rejected": 3, "data_unavailable": 0}
    assert [snap["telemetry"][k]["requested"] for k in ss.STAGES] == [40, 39, 38]  # news only for survivors


# 3. variable pool ------------------------------------------------------

@pytest.mark.parametrize("n", [1, 10, 11, 57, 400])
def test_pool_is_every_eligible_name_with_no_target_size(n):
    snap = ss.run_screen(SAT, Fake(names(n)))  # all one sector: no per-sector trim either
    assert snap["screen_status"] == "ok" and snap["pool"] == names(n)
    assert snap["sector_mix"] == {"Industrials": n}


def test_empty_pool_is_a_valid_ok_screen_and_never_padded(tmp_path):
    snap, _ = ss.screen(SAT, Fake(names(5), facts={s: {**GOOD, "price": 1.0} for s in names(5)}), tmp_path)
    assert (snap["screen_status"], snap["pool"], snap["sector_mix"]) == ("ok", [], {})
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["pool"] == []  # no minimum pool size


# 4. dynamic news windows -----------------------------------------------

def test_news_windows_trail_the_screen_date():
    assert ss.news_windows(SAT) == [
        {"start_at": "2026-09-19T00:00:00+00:00", "end_at": "2026-10-03T00:00:00+00:00"},
        {"start_at": "2026-06-21T00:00:00+00:00", "end_at": "2026-07-05T00:00:00+00:00"},
        {"start_at": "2026-03-23T00:00:00+00:00", "end_at": "2026-04-06T00:00:00+00:00"},
    ]
    later = ss.news_windows(date(2027, 3, 6))
    assert later[0]["end_at"] == "2027-03-06T00:00:00+00:00" and later != ss.news_windows(SAT)
    fake = Fake(names(3))
    snap = ss.run_screen(date(2027, 3, 6), fake)  # injected fixtures may use any date
    assert fake.windows == later == snap["news_windows"]
    assert "2026-02-20" not in (HERE / "scalable_screener.py").read_text(encoding="utf-8")


# 5. missing data is visible --------------------------------------------

def test_missing_data_is_recorded_never_dropped():
    syms = names(80)
    fake = Fake(syms + ["S000", " "],  # duplicates/blank collapse; nothing else disappears
                facts={"S001": None, "S002": {"error": "boom"}, "S003": {**GOOD, "market_cap": None}},
                history={"S004": None, "S005": {"error": "boom"}},
                news={"S006": None, "S007": [{"articles": 9}, {"error": "x"}, {"articles": 9}]})
    snap = ss.run_screen(SAT, fake)
    assert [r["symbol"] for r in snap["rows"]] == syms and snap["universe"]["count"] == 80
    got = {s: row(snap, s)["reasons"] for s in syms[1:8]}
    assert got == {"S001": ["facts_missing"], "S002": ["facts_fetch_error"], "S003": ["market_cap_missing"],
                   "S004": ["history_missing"], "S005": ["history_fetch_error"], "S006": ["news_missing"],
                   "S007": ["news_probe_2_unavailable"]}
    assert all(row(snap, s)["status"] == "data_unavailable" for s in got)
    assert not set(got) & set(snap["pool"]) and snap["counts"]["data_unavailable"] == 7
    assert all(r["status"] in ss.STATUSES for r in snap["rows"])


# 6. screen failure -----------------------------------------------------

def test_ten_percent_data_unavailable_threshold():
    syms = names(50)
    at = ss.run_screen(SAT, Fake(syms, facts={s: {"error": "x"} for s in syms[:5]}))   # exactly 10%
    over = ss.run_screen(SAT, Fake(syms, facts={s: {"error": "x"} for s in syms[:6]}))  # 12%
    assert (at["screen_status"], at["failure_reasons"], len(at["pool"])) == ("ok", [], 45)
    assert (over["screen_status"], over["failure_reasons"]) == ("failed", ["data_unavailable_above_threshold"])
    assert over["pool"] is None and over["pool_hash"] is None and over["effective_from"] is None
    assert len(over["rows"]) == 50  # the evidence is kept even though no pool is published


def test_universe_failure_fails_the_screen():
    snap = ss.run_screen(SAT, Fake(names(5), fail=("universe",)))
    assert (snap["screen_status"], snap["failure_reasons"]) == ("failed", ["universe_fetch_failed"])
    assert snap["rows"] == [] and snap["pool"] is None and snap["universe"]["count"] == 0
    empty = ss.run_screen(SAT, Fake([]))
    assert (empty["screen_status"], empty["failure_reasons"]) == ("failed", ["universe_empty"])
    ss.validate_snapshot(snap, kind="attempt") and ss.validate_snapshot(empty, kind="attempt")


def test_news_failure_fails_the_screen_even_under_the_threshold():
    # 60 names, only 3 reach news: 5% unavailable, but coverage could not be completed.
    syms = names(60)
    snap = ss.run_screen(SAT, Fake(syms, facts={s: {**GOOD, "price": 1.0} for s in syms[3:]}, fail=("news",)))
    assert (snap["screen_status"], snap["failure_reasons"]) == ("failed", ["news_coverage_incomplete"])
    assert row(snap, "S000")["reasons"] == ["news_fetch_error"] and snap["pool"] is None


def test_a_failed_facts_or_history_stage_fails_the_screen():
    for stage in ("facts", "history"):
        snap = ss.run_screen(SAT, Fake(names(20), fail=(stage,)))
        assert snap["screen_status"] == "failed" and f"{stage}_provider_failed" in snap["failure_reasons"]


# 7. temporal identity --------------------------------------------------

def test_live_dates_are_refused_before_any_provider_or_container_call(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("a container/provider call was made")
    monkeypatch.setattr(ss.subprocess, "run", boom)
    monkeypatch.setattr(ss, "_run_in", boom)
    monkeypatch.setattr(ss, "LiveProviders", boom)
    clock = lambda: NOON  # noqa: E731 -- "today" is 2026-10-03 UTC
    for mode in ("--dry-run", "--write"):
        for bad in ("2026-10-02", "2026-10-04", "2025-01-01"):
            with pytest.raises(SystemExit) as e:
                ss.main([mode, "--screen-date", bad, "--output-dir", str(tmp_path)], clock=clock)
            assert e.value.code == 2
    # even injected providers cannot push a historical date through the live CLI
    fake = Fake(names(3))
    with pytest.raises(SystemExit):
        ss.main(["--write", "--screen-date", "2026-10-02", "--output-dir", str(tmp_path)], fake, clock)
    assert fake.asked == {} and not list(tmp_path.iterdir())


def test_library_refuses_live_providers_for_any_other_date(monkeypatch):
    calls = []
    monkeypatch.setattr(ss, "_run_in", lambda *a, **k: calls.append(a))
    for bad in (date(2026, 10, 2), date(2026, 10, 4)):
        with pytest.raises(ss.LiveDateError):
            ss.run_screen(bad, ss.LiveProviders(), clock=lambda: NOON)
    assert calls == []
    late = datetime(2026, 10, 3, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # already 10-04 in UTC
    with pytest.raises(ss.LiveDateError):
        ss.run_screen(SAT, ss.LiveProviders(), clock=lambda: late)


def test_cli_defaults_to_today_and_records_real_timestamps(tmp_path, capsys):
    clock = ticking()
    assert ss.main(["--write", "--output-dir", str(tmp_path)], Fake(names(4)), clock) == 0
    snap = ss.load_result(tmp_path, ss.screen_id(SAT, ss.DEFAULT_CONFIG))
    assert snap["screen_date"] == "2026-10-03" and "recorded" in capsys.readouterr().out
    assert snap["started_at"] == "2026-10-03T12:00:02+00:00" < snap["universe"]["fetched_at"] < snap["finished_at"]
    assert all(snap["telemetry"][k]["started_at"].startswith("2026-10-03T12:00") for k in ss.STAGES)
    assert ss.main(["--write", "--screen-date", "2026-10-03", "--output-dir", str(tmp_path)], Fake([]), clock) == 0
    assert "reused existing result" in capsys.readouterr().out  # explicit today is accepted too


def test_usable_is_the_only_mode_that_takes_another_date(tmp_path, capsys):
    ss.screen(SAT, Fake(names(4)), tmp_path)
    capsys.readouterr()
    assert ss.main(["--usable", "--on-date", "2026-10-06", "--output-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "current"
    for bad in (["--usable"], ["--usable", "--screen-date", "2026-10-06", "--on-date", "2026-10-06"],
                ["--dry-run", "--on-date", "2026-10-03"], ["--on-date", "2026-10-03"], []):
        with pytest.raises(SystemExit):
            ss.main(bad + ["--output-dir", str(tmp_path)], Fake(names(4)), lambda: NOON)


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    assert ss.main(["--dry-run", "--output-dir", str(out)], Fake(names(4)), ticking()) == 0
    text = capsys.readouterr().out
    assert "DRY RUN -- nothing written" in text and "pool (4)" in text and not out.exists()
    assert "facts: requested 4, ok 4, unavailable 0" in text
    assert ss.main(["--dry-run", "--output-dir", str(out)], Fake(names(4), fail=("universe",)), ticking()) == 1


# 8. bounded live workload ----------------------------------------------

LIM = {"concurrency": 1, "attempts": 3, "timeout_s": 5, "backoff_base_s": 1, "backoff_cap_s": 3, "min_interval_s": 0}


def test_bounded_map_caps_concurrency():
    lock, active, peak = threading.Lock(), [0], [0]

    def one(item):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return item * 2

    out, stats = ss.bounded_map(range(12), one, **{**LIM, "concurrency": 3})
    assert out == {i: {"ok": i * 2} for i in range(12)} and 1 < peak[0] <= 3
    assert stats == {"requests": 12, "retries": 0}


def test_bounded_map_retries_with_capped_backoff_then_gives_up():
    sleeps, tries = [], {"flaky": 0}

    def one(item):
        if item == "flaky":
            tries["flaky"] += 1
            if tries["flaky"] < 3:
                raise ConnectionError("https://api.example/v1?key=SECRET")
            return "fine"
        raise ValueError("token=SECRET")

    out, stats = ss.bounded_map(["flaky"], one, **LIM, sleep=sleeps.append)
    assert out == {"flaky": {"ok": "fine"}} and sleeps == [1, 2] and stats == {"requests": 3, "retries": 2}

    sleeps.clear()
    out, stats = ss.bounded_map(["dead"], one, **{**LIM, "attempts": 5}, sleep=sleeps.append)
    assert out == {"dead": {"error": "ValueError"}}        # class name only, never the message
    assert sleeps == [1, 2, 3, 3] and stats == {"requests": 5, "retries": 4}  # bounded, capped
    assert ss.bounded_map(["dead"], one, **{**LIM, "attempts": 0}, sleep=sleeps.append)[1]["requests"] == 1


def test_bounded_map_times_out_a_hung_request_and_paces_starts():
    out, stats = ss.bounded_map(["hang"], lambda _: time.sleep(0.5), sleep=lambda s: None,
                                **{**LIM, "attempts": 2, "timeout_s": 0.05})
    assert out == {"hang": {"error": "timeout"}} and stats == {"requests": 2, "retries": 1}
    waits = []
    ss.bounded_map(range(4), lambda i: i, **{**LIM, "min_interval_s": 30}, sleep=waits.append)
    assert len(waits) == 3 and all(25 < w <= 90 for w in waits)  # each start waits for its slot


def test_limits_are_conservative_bounded_and_overridable():
    for lim in ss.DEFAULT_LIMITS.values():
        assert 1 <= lim["concurrency"] <= 4 and 1 <= lim["attempts"] <= 3 and lim["timeout_s"] <= 60
    lim = ss.make_limits(concurrency=2, attempts=1, timeout_s=10)
    assert all((lim[s]["concurrency"], lim[s]["attempts"], lim[s]["timeout_s"]) == (2, 1, 10) for s in ss.STAGES)
    assert ss.make_limits() == ss.DEFAULT_LIMITS
    for bad in ({"concurrency": 0}, {"concurrency": 9}, {"attempts": 6}, {"timeout_s": 500}):
        with pytest.raises(ValueError):
            ss.make_limits(**bad)
    assert ss._exec_timeout(400, ss.DEFAULT_LIMITS["facts"]) < 3 * 3600  # the whole stage is bounded too
    with pytest.raises(SystemExit):
        ss.main(["--dry-run", "--concurrency", "50"], clock=lambda: NOON)


def _stub_containers(monkeypatch, universe, bad=(), incomplete=()):
    """Run the REAL generated container scripts in-process against stub
    yfinance / httpx / pandas / alpaca `server` modules. No Docker, no network."""
    class Ticker:
        def __init__(self, s):
            self.s = s

        @property
        def info(self):
            if self.s in bad:
                raise KeyError("https://query.example/?crumb=SECRET")
            return {"shortName": f"{self.s} Inc", "sector": "Energy", "industry": "Oil", "marketCap": 5e9,
                    "averageVolume": 2e6, "currentPrice": 30.0}

        def history(self, period):
            return [0] * 1000

    class Table:
        columns = ["Symbol", "Security"]

        def __getitem__(self, col):
            return list(universe)

    async def get_historical_news(symbol, start_at, end_at):
        return {"articles": [{}] * 4, "complete": symbol not in incomplete}

    for name, attrs in {
        "yfinance": {"Ticker": Ticker},
        "httpx": {"get": lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None, text="<html>")},
        "pandas": {"read_html": lambda _: [Table()]},
        "server": {"get_historical_news": get_historical_news},
    }.items():
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
    calls = []

    def run_in(service, script, timeout):
        calls.append((service, timeout))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(script, "<container script>", "exec"), {"__name__": "container"})
        line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith(ss._MARK))
        return json.loads(line[len(ss._MARK):])

    monkeypatch.setattr(ss, "_run_in", run_in)
    return calls


FAST = {s: {**lim, "min_interval_s": 0, "backoff_base_s": 0, "backoff_cap_s": 0} for s, lim in ss.DEFAULT_LIMITS.items()}


def test_live_providers_end_to_end_through_the_real_container_scripts(monkeypatch):
    universe = [f"T{i:02d}" for i in range(18)] + ["BRK.B", "BAD"]
    calls = _stub_containers(monkeypatch, universe, bad={"BAD"}, incomplete={"T03"})
    snap = ss.run_screen(SAT, ss.LiveProviders(FAST), clock=ticking())
    assert [c[0] for c in calls] == ["maverick-mcp", "maverick-mcp", "maverick-mcp", "alpaca-mcp"]
    assert all(isinstance(c[1], int) and 0 < c[1] < 3 * 3600 for c in calls)
    assert snap["screen_status"] == "ok" and "BRK-B" in snap["pool"] and len(snap["pool"]) == 18
    assert row(snap, "BAD")["reasons"] == ["facts_fetch_error"]
    assert row(snap, "T03")["reasons"] == [f"news_probe_{i}_unavailable" for i in (1, 2, 3)]  # fail closed
    t = snap["telemetry"]
    assert (t["facts"]["requested"], t["facts"]["ok"], t["facts"]["unavailable"]) == (20, 19, 1)
    assert t["facts"]["errors"] == {"KeyError": 1} and t["facts"]["requests"] == 22 and t["facts"]["retries"] == 2
    assert (t["history"]["requested"], t["news"]["requested"]) == (19, 19)
    assert t["news"]["errors"] == {"incomplete_pagination": 3} and t["news"]["requests"] == 57
    assert t["news"]["retries"] == 0  # incomplete pagination is terminal, not retried
    assert t["limits"]["news"]["concurrency"] == 2 and t["universe"]["ok"] is True
    assert "SECRET" not in ss.snapshot_to_json(snap)
    ss.validate_snapshot(snap, kind="attempt")


def test_container_failures_become_sanitized_stage_failures(monkeypatch):
    def run(*a, **k):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback ... ALPACA_SECRET_KEY=abc123")
    monkeypatch.setattr(ss.subprocess, "run", run)
    snap = ss.run_screen(SAT, ss.LiveProviders(), clock=ticking())
    assert snap["failure_reasons"] == ["universe_fetch_failed"]
    assert snap["telemetry"]["universe"]["stage_error"] == "container_failed"
    assert "abc123" not in ss.snapshot_to_json(snap) and "Traceback" not in ss.snapshot_to_json(snap)

    def missing(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(ss.subprocess, "run", missing)
    with pytest.raises(ss.ProviderError, match="^docker_unavailable$"):
        ss._run_in("alpaca-mcp", "", 5)


def test_telemetry_is_bounded_and_never_leaks_exception_detail():
    syms = names(300)
    leaky = {s: {"error": f"Traceback (most recent call last): key=SECRET{i}"} for i, s in enumerate(syms[:5])}
    coded = {s: {"error": f"Code{i}"} for i, s in enumerate(syms[5:20])}
    snap = ss.run_screen(SAT, Fake(syms, facts={**leaky, **coded}, stats={"requests": 310, "retries": 10}))
    t = snap["telemetry"]["facts"]
    assert (t["requested"], t["ok"], t["unavailable"]) == (300, 280, 20)
    assert t["errors"]["error"] == 5 and len(t["errors"]) == ss.MAX_ERROR_CODES + 1
    assert sum(t["errors"].values()) == 20 and t["errors"]["other"] == 6
    assert (t["requests"], t["retries"], t["stage_error"]) == (310, 10, None)
    assert isinstance(t["elapsed_s"], float) and "SECRET" not in ss.snapshot_to_json(snap)

    failed = ss.run_screen(SAT, Fake(names(20), fail=("news",), stats="garbage"))
    assert failed["telemetry"]["news"]["stage_error"] == "RuntimeError"
    assert failed["telemetry"]["facts"]["requests"] is None and failed["telemetry"]["limits"] is None
    assert "hunter2" not in ss.snapshot_to_json(failed)
    gone = ss.run_screen(SAT, Fake(names(5), fail=("universe",)))
    assert gone["telemetry"]["universe"] == {**gone["telemetry"]["universe"], "ok": False, "stage_error": "RuntimeError"}
    assert "hunter2" not in ss.snapshot_to_json(gone)


# 9. attempts versus the logical screen ---------------------------------

def test_ids_and_hashes():
    cfg2 = {**ss.DEFAULT_CONFIG, "min_price": 12.0}
    assert ss.config_hash(dict(reversed(list(ss.DEFAULT_CONFIG.items())))) == ss.config_hash(ss.DEFAULT_CONFIG)
    assert ss.config_hash(cfg2) != ss.config_hash(ss.DEFAULT_CONFIG)
    sid = ss.screen_id(SAT, ss.DEFAULT_CONFIG)
    assert sid == f"2026-10-03_{ss.config_hash(ss.DEFAULT_CONFIG)[:12]}"
    a = ss.run_screen(SAT, Fake(["B", "A", "C"]), attempt=7)
    b = ss.run_screen(SAT, Fake(["C", "B", "A"]))
    assert (a["screen_id"], a["attempt_id"], b["attempt_id"]) == (sid, f"{sid}.a007", f"{sid}.a001")
    assert a["universe"]["constituents_hash"] == b["universe"]["constituents_hash"] == ss._hash(["A", "B", "C"])
    assert a["pool_hash"] == ss._hash(["A", "B", "C"]) and a["config"] == ss.DEFAULT_CONFIG
    assert ss.run_screen(SAT, Fake(["A", "B"]))["pool_hash"] != a["pool_hash"]
    assert a["universe"]["source"] == "fake" and a["universe"]["count"] == 3
    assert a["effective_from"] == "2026-10-05"  # the Monday after a weekend screen
    text = ss.snapshot_to_json(a)
    assert text.endswith("\n") and text.isascii() and json.loads(text) == a


def test_failed_attempt_then_a_separate_successful_attempt_same_day(tmp_path):
    sid = ss.screen_id(SAT, ss.DEFAULT_CONFIG)
    failed, reused = ss.screen(SAT, Fake(names(9), fail=("news",)), tmp_path, clock=ticking())
    a1 = ss.attempt_path(tmp_path, f"{sid}.a001")
    frozen = a1.read_bytes()
    assert not reused and failed["screen_status"] == "failed" and not ss.results_dir(tmp_path).exists()
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["reason"] == "no_successful_screen"

    good, reused = ss.screen(SAT, Fake(names(9)), tmp_path, clock=ticking())  # same date: a NEW attempt
    a2, result = ss.attempt_path(tmp_path, f"{sid}.a002"), ss.result_path(tmp_path, sid)
    assert not reused and good["attempt_id"] == f"{sid}.a002" and good["screen_status"] == "ok"
    assert a1.read_bytes() == frozen                                         # the failure is still there
    assert result.read_bytes() == a2.read_bytes() == ss.snapshot_to_json(good).encode() and b"\r" not in frozen
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == sorted([a1.name, a2.name, result.name])

    again = Fake(names(3), fail=("universe",))
    snap, reused = ss.screen(SAT, again, tmp_path)                           # identical rerun: verify + reuse
    assert reused and snap == good and again.asked == {} and ss.next_attempt(tmp_path, sid) == 3
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["pool"] == names(9)


def test_a_conflicting_successful_result_fails_loudly_and_replaces_nothing(tmp_path):
    sid = ss.screen_id(SAT, ss.DEFAULT_CONFIG)
    good, _ = ss.screen(SAT, Fake(names(6)), tmp_path)
    stored = ss.result_path(tmp_path, sid).read_bytes()
    other = ss.run_screen(SAT, Fake(names(4)), attempt=2)
    ss.write_attempt(tmp_path, other)                                         # kept as an attempt...
    with pytest.raises(ss.ScreenConflict, match="NOT the usable result"):
        ss.publish_result(tmp_path, other)                                    # ...but never the result
    assert ss.result_path(tmp_path, sid).read_bytes() == stored
    ss.publish_result(tmp_path, good)                                         # identical: a safe no-op
    with pytest.raises(ss.ScreenConflict):
        ss.write_attempt(tmp_path, ss.run_screen(SAT, Fake(names(5)), attempt=1))
    with pytest.raises(ss.SnapshotCorrupt):                                   # a failed attempt can never be a result
        ss.publish_result(tmp_path, ss.run_screen(date(2026, 10, 4), Fake([])))
    assert ss.load_result(tmp_path, sid) == good


def test_a_result_without_its_attempt_record_is_not_usable(tmp_path):
    sid = ss.screen_id(SAT, ss.DEFAULT_CONFIG)
    ss.screen(SAT, Fake(names(6)), tmp_path)
    ss.attempt_path(tmp_path, f"{sid}.a001").unlink()
    with pytest.raises(ss.SnapshotCorrupt):
        ss.screen(SAT, Fake(names(6)), tmp_path)
    assert ss.usable_pool(tmp_path, date(2026, 10, 6)) == {
        "state": "unavailable", "reason": "snapshot_invalid", "screen_id": None, "pool": None, "evidence": []}


# 10. carry-forward needs a recorded failure ----------------------------

def test_carry_forward_requires_a_recorded_failed_refresh_and_lasts_one_week(tmp_path):
    good, _ = ss.screen(SAT, Fake(names(8)), tmp_path)                        # effective Mon 2026-10-05

    def state(d):
        u = ss.usable_pool(tmp_path, d)
        return u["state"], u["reason"], u["pool"]

    assert state(date(2026, 10, 4)) == ("unavailable", "no_successful_screen", None)  # not yet effective
    assert state(date(2026, 10, 5)) == state(date(2026, 10, 11)) == ("current", None, names(8))
    # The next refresh was never attempted: that is NOT a failure, so nothing carries forward.
    assert state(date(2026, 10, 12)) == state(date(2026, 10, 18)) == ("unavailable", "refresh_not_attempted", None)

    failed, _ = ss.screen(date(2026, 10, 10), Fake(names(8), fail=("news",)), tmp_path)
    assert failed["previous_ok_screen"] == {"screen_id": good["screen_id"], "content_hash": good["content_hash"]}
    assert good["previous_ok_screen"] is None
    carried = ss.usable_pool(tmp_path, date(2026, 10, 12))
    assert carried == {"state": "carried_forward", "reason": None, "screen_id": good["screen_id"],
                       "pool": names(8), "evidence": [failed["attempt_id"]]}
    assert state(date(2026, 10, 18)) == ("carried_forward", None, names(8))

    ss.screen(date(2026, 10, 17), Fake(names(8), fail=("news",)), tmp_path)   # a second failed refresh
    assert state(date(2026, 10, 19)) == ("unavailable", "last_successful_screen_expired", None)  # one only

    fresh, _ = ss.screen(date(2026, 10, 18), Fake(names(4)), tmp_path)        # a later good screen takes over
    assert fresh["previous_ok_screen"]["screen_id"] == good["screen_id"]
    assert state(date(2026, 10, 18)) == ("carried_forward", None, names(8))  # not effective until Monday
    assert state(date(2026, 10, 19)) == ("current", None, names(4))


def test_a_failure_from_the_wrong_week_does_not_authorize_carry_forward(tmp_path):
    ss.screen(SAT, Fake(names(8), fail=("news",)), tmp_path)                  # failed, then...
    ss.screen(SAT, Fake(names(8)), tmp_path)                                  # ...succeeded the same day
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["state"] == "current"
    # the only failure on record belongs to this pool's OWN refresh, not the one that should replace it
    assert ss.usable_pool(tmp_path, date(2026, 10, 13))["reason"] == "refresh_not_attempted"
    other_cfg = {**ss.DEFAULT_CONFIG, "min_price": 12.0}
    ss.screen(date(2026, 10, 10), Fake(names(8), fail=("news",)), tmp_path, config=other_cfg)
    assert ss.usable_pool(tmp_path, date(2026, 10, 13))["reason"] == "refresh_not_attempted"  # other config
    ss.screen(date(2026, 10, 10), Fake(names(8), fail=("news",)), tmp_path)
    assert ss.usable_pool(tmp_path, date(2026, 10, 13))["state"] == "carried_forward"


def test_usable_pool_fails_closed_on_any_invalid_record(tmp_path):
    good, _ = ss.screen(SAT, Fake(names(8)), tmp_path)
    ss.screen(date(2026, 10, 10), Fake(names(8), fail=("news",)), tmp_path)
    assert ss.usable_pool(tmp_path, date(2026, 10, 13))["state"] == "carried_forward"
    junk = ss.attempts_dir(tmp_path) / "2026-10-11_deadbeef0000.a001.json"
    junk.write_text("{not json")
    assert ss.usable_pool(tmp_path, date(2026, 10, 13))["reason"] == "snapshot_invalid"
    junk.unlink()
    path = ss.result_path(tmp_path, good["screen_id"])
    tampered = rehash({**good, "pool": names(8) + ["EVIL"]})                  # self-hashed, still rejected
    path.write_text(ss.snapshot_to_json(tampered), newline="\n")
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["reason"] == "snapshot_invalid"


# 11. validation --------------------------------------------------------

def _ok_snapshot():
    return ss.run_screen(SAT, Fake(names(12), facts={"S003": {**GOOD, "price": 1.0}}), clock=ticking())


def _failed_snapshot():
    return ss.run_screen(SAT, Fake(names(12), fail=("news",)), clock=ticking())


def _set(path, value):
    def apply(s):
        *parents, last = path
        for key in parents:
            s = s[key]
        s[last] = value
    return apply


def _over_threshold(s):
    """Internally consistent in every other respect, so only the threshold trips."""
    for r in s["rows"][4:8]:
        r.update(status="data_unavailable", reasons=["facts_fetch_error"])
    elig = [r["symbol"] for r in s["rows"] if r["status"] == "eligible"]
    s.update(pool=elig, pool_hash=ss._hash(elig), sector_mix={"Industrials": len(elig)},
             counts={"eligible": len(elig), "rejected": 1, "data_unavailable": 4})


MALFORMED = {
    "missing key": lambda s: s.pop("telemetry"),
    "extra key": _set(["current_pool"], ["S000"]),
    "rows wrong type": _set(["rows"], {}),
    "attempt is a bool": _set(["attempt"], True),
    "old schema": _set(["schema_version"], 1),
    "pool reordered": lambda s: s["pool"].reverse(),
    "pool duplicated": lambda s: s["pool"].append(s["pool"][0]),
    "pool has a rejected name": lambda s: (s["pool"].append("S003"), s["pool"].sort()),
    "pool hash wrong": _set(["pool_hash"], "0" * 64),
    "counts wrong": _set(["counts", "eligible"], 12),
    "universe count wrong": _set(["universe", "count"], 400),
    "constituents hash wrong": _set(["universe", "constituents_hash"], "0" * 64),
    "row status invalid": _set(["rows", 0, "status"], "maybe"),
    "eligible row with reasons": _set(["rows", 0, "reasons"], ["price_below_min"]),
    "rejected row without reasons": _set(["rows", 3, "reasons"], []),
    "row missing field": lambda s: s["rows"][0].pop("price"),
    "duplicate row": lambda s: s["rows"].__setitem__(1, copy.deepcopy(s["rows"][0])),
    "sector mix wrong": _set(["sector_mix"], {"Industrials": 99}),
    "effective_from wrong day": _set(["effective_from"], "2026-10-06"),
    "effective_from not a date": _set(["effective_from"], "next week"),
    "ok but failure reasons": _set(["failure_reasons"], ["news_coverage_incomplete"]),
    "failed but carries a pool": _set(["screen_status"], "failed"),
    "unknown screen status": _set(["screen_status"], "partial"),
    "wrong screen id": _set(["screen_id"], "2026-10-02_" + "a" * 12),
    "wrong attempt id": _set(["attempt_id"], "2026-10-03_aaaaaaaaaaaa.a001"),
    "attempt zero": _set(["attempt"], 0),
    "config changed, hash not": _set(["config", "min_price"], 1.0),
    "config hash wrong": _set(["config_hash"], "0" * 64),
    "bad screen date": _set(["screen_date"], "2026-13-40"),
    "naive timestamp": _set(["started_at"], "2026-10-03T12:00:00"),
    "telemetry gutted": _set(["telemetry"], {}),
    "over the unavailable threshold": _over_threshold,
}


@pytest.mark.parametrize("label", MALFORMED)
def test_malformed_but_self_hashed_snapshots_are_rejected(label, tmp_path):
    good = _ok_snapshot()
    ss.validate_snapshot(copy.deepcopy(good), kind="result", stem=good["screen_id"])  # the control passes
    bad = copy.deepcopy(good)
    MALFORMED[label](bad)
    rehash(bad)
    for kind in ("attempt", "result"):
        with pytest.raises(ss.SnapshotCorrupt):
            ss.validate_snapshot(copy.deepcopy(bad), kind=kind)
    path = tmp_path / "attempts" / f"{good['attempt_id']}.json"
    path.parent.mkdir()
    path.write_text(ss.snapshot_to_json(bad), newline="\n")
    with pytest.raises(ss.SnapshotCorrupt):
        ss.load_snapshot(path, "attempt")


def test_wrong_ids_filenames_and_attempt_state_are_rejected(tmp_path):
    good, failed = _ok_snapshot(), _failed_snapshot()
    assert ss.validate_snapshot(good, kind="attempt", stem=good["attempt_id"]) is good
    assert ss.validate_snapshot(failed, kind="attempt", stem=failed["attempt_id"]) is failed
    with pytest.raises(ss.SnapshotCorrupt, match="filename"):
        ss.validate_snapshot(good, kind="attempt", stem=good["screen_id"])    # a result name on an attempt
    with pytest.raises(ss.SnapshotCorrupt, match="filename"):
        ss.validate_snapshot(good, kind="result", stem=good["attempt_id"])
    with pytest.raises(ss.SnapshotCorrupt, match="filename"):
        ss.validate_snapshot(good, kind="attempt", stem="2026-10-04_" + good["config_hash"][:12] + ".a001")
    with pytest.raises(ss.SnapshotCorrupt, match="successful"):
        ss.validate_snapshot(failed, kind="result")                            # a failure is never a result
    for field, value in (("pool", []), ("pool_hash", ss._hash([])), ("sector_mix", {}), ("effective_from", "2026-10-05")):
        with pytest.raises(ss.SnapshotCorrupt, match="failed attempt"):
            ss.validate_snapshot(rehash({**failed, field: value}), kind="attempt")
    with pytest.raises(ss.SnapshotCorrupt, match="disagree"):
        ss.validate_snapshot(rehash({**failed, "failure_reasons": []}), kind="attempt")
    with pytest.raises(ss.SnapshotCorrupt, match="altered"):
        ss.validate_snapshot({**good, "pool": good["pool"][:-1]}, kind="attempt")  # edited, not re-hashed
    for junk in ([], None, "x", {"schema_version": ss.SCHEMA_VERSION}):
        with pytest.raises(ss.SnapshotCorrupt):
            ss.validate_snapshot(junk, kind="attempt")
    with pytest.raises(ss.SnapshotCorrupt):                                    # nothing invalid is ever written
        ss.write_attempt(tmp_path, rehash({**good, "counts": {}}))
    assert not list(tmp_path.rglob("*.json"))


# 12. determinism -------------------------------------------------------

def test_identical_inputs_give_identical_bytes(tmp_path):
    def build(d):
        syms = names(30)
        fake = Fake(list(reversed(syms)), facts={"S003": {"error": "x"}, "S004": {**GOOD, "sector": "Energy"}},
                    history={"S005": {"bars": 3}})
        return ss.snapshot_to_json(ss.screen(SAT, fake, d, clock=ticking())[0])

    a, b = build(tmp_path / "a"), build(tmp_path / "b")
    assert a == b and json.loads(a)["sector_mix"] == {"Energy": 1, "Industrials": 27}
