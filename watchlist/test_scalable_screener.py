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
    assert snap["started_at"] == "2026-10-03T12:00:01+00:00" < snap["universe"]["fetched_at"] < snap["finished_at"]
    assert all(snap["telemetry"][k]["started_at"].startswith("2026-10-03T12:00") for k in ss.STAGES)
    assert ss.main(["--write", "--screen-date", "2026-10-03", "--output-dir", str(tmp_path)], Fake([]), clock) == 0
    assert "reused existing result" in capsys.readouterr().out  # explicit today is accepted too


class LiveFake(Fake):
    live = True  # date-locked like the real providers, but still no network


def test_a_run_accepted_before_midnight_survives_crossing_it(tmp_path):
    ticks = iter([datetime(2026, 10, 3, 23, 59, 59, tzinfo=timezone.utc)]
                 + [datetime(2026, 10, 4, 0, 0, n, tzinfo=timezone.utc) for n in range(1, 40)])
    clock = lambda: next(ticks)  # noqa: E731 -- the first reading is Saturday, every later one is Sunday
    fake = LiveFake(names(6))
    assert ss.main(["--write", "--output-dir", str(tmp_path)], fake, clock) == 0
    snap = ss.load_result(tmp_path, ss.screen_id(SAT, ss.DEFAULT_CONFIG))
    assert snap["screen_date"] == "2026-10-03" and snap["started_at"].startswith("2026-10-03T23:59:59")
    assert snap["finished_at"].startswith("2026-10-04T00:00") and fake.asked["facts"] == names(6)
    # the library keeps the same rule: one captured start instant, never re-read
    late = iter([datetime(2026, 10, 4, 0, 0, n, tzinfo=timezone.utc) for n in range(1, 40)])
    ok = ss.run_screen(SAT, LiveFake(names(3)), clock=lambda: next(late),
                       started=datetime(2026, 10, 3, 23, 59, 59, tzinfo=timezone.utc))
    assert ok["screen_status"] == "ok"
    with pytest.raises(ss.LiveDateError):                      # ...and a start on the 4th cannot screen the 3rd
        ss.run_screen(SAT, LiveFake(names(3)), clock=lambda: datetime(2026, 10, 4, 0, 0, 1, tzinfo=timezone.utc))


def test_write_is_weekend_only_and_dry_run_works_any_weekday(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise AssertionError("a container/provider call was made")
    monkeypatch.setattr(ss.subprocess, "run", boom)
    monkeypatch.setattr(ss, "_run_in", boom)
    for day in range(5, 10):                                   # Mon 2026-10-05 .. Fri 2026-10-09
        weekday = lambda d=day: datetime(2026, 10, d, 15, 0, tzinfo=timezone.utc)  # noqa: E731
        fake = Fake(names(4))
        with pytest.raises(SystemExit) as e:
            ss.main(["--write", "--output-dir", str(tmp_path)], fake, weekday)
        assert e.value.code == 2 and fake.asked == {}
        with pytest.raises(SystemExit):                        # before the live providers even exist
            ss.main(["--write", "--output-dir", str(tmp_path)], clock=weekday)
        assert "Saturday or Sunday" in capsys.readouterr().err
        assert ss.main(["--dry-run", "--output-dir", str(tmp_path)], Fake(names(4)), weekday) == 0
        assert f"effective from 2026-10-12" in capsys.readouterr().out
    assert not tmp_path.exists() or not list(tmp_path.iterdir())
    for day in (10, 11):                                       # Saturday and Sunday are accepted
        weekend = lambda d=day: datetime(2026, 10, d, 15, 0, tzinfo=timezone.utc)  # noqa: E731
        assert ss.main(["--write", "--output-dir", str(tmp_path)], Fake(names(4)), weekend) == 0
    with pytest.raises(ss.LiveDateError, match="Saturday or Sunday"):   # the library rule for live providers
        ss.screen(date(2026, 10, 7), LiveFake(names(4)), tmp_path,
                  clock=lambda: datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc))
    assert ss.screen(date(2026, 10, 7), Fake(names(4)), tmp_path)[0]["screen_status"] == "ok"  # fixtures: any date


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

LIM = {"concurrency": 1, "attempts": 3, "timeout_s": 20, "backoff_base_s": 1, "backoff_cap_s": 3, "min_interval_s": 0}

# A child request that proves it is alive: it appends a heartbeat every 10 ms
# to a file named after the item and ITS OWN process id. "hang-*" never returns.
HEARTBEAT = """
import os, time
LOG = {log!r}
def one(item):
    path = os.path.join(LOG, item + "." + str(os.getpid()))
    end = time.time() + (600 if item.startswith("hang") else 0.2)
    while time.time() < end:
        with open(path, "a") as f:
            f.write(repr(time.time()) + "\\n")
        time.sleep(0.01)
    return item.upper()
"""


def _lifetimes(log):
    """{filename: (first heartbeat, last heartbeat)} -- one entry per child process."""
    out = {}
    for p in sorted(log.iterdir()):
        beats = [float(x) for x in p.read_text().split()]
        out[p.name] = (beats[0], beats[-1])
    return out


def _peak(spans):
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans], key=lambda e: (e[0], -e[1]))
    peak = live = 0
    for _, step in events:
        live += step
        peak = max(peak, live)
    return peak


def test_timed_out_requests_are_killed_and_true_in_flight_work_never_exceeds_the_limit(tmp_path):
    items = ["hang-a", "ok-1", "hang-b", "ok-2", "ok-3", "hang-c", "ok-4", "ok-5"]
    out, stats = ss.bounded_map(items, HEARTBEAT.format(log=str(tmp_path)), concurrency=2, attempts=3,
                                timeout_s=1.2, backoff_base_s=0, backoff_cap_s=0)
    assert out == {**{i: {"error": "timeout"} for i in items if i.startswith("hang")},
                   **{i: {"ok": i.upper()} for i in items if i.startswith("ok")}}
    assert stats == {"requests": 3 * 3 + 5, "retries": 6}

    lives = _lifetimes(tmp_path)
    assert len(lives) == 14                                   # one real process per request
    assert _peak(list(lives.values())) <= 2                   # TRUE in-flight work, measured in the children
    for item in ("hang-a", "hang-b", "hang-c"):
        tries = sorted(v for k, v in lives.items() if k.startswith(item + "."))
        assert len(tries) == 3                                # every retry was a fresh process...
        assert all(a[1] < b[0] for a, b in zip(tries, tries[1:]))   # ...started after the previous one stopped
        assert all(end - start < 1.2 + 1.0 for start, end in tries)  # none outlived its timeout

    sizes = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
    time.sleep(0.4)                                           # nothing is still running afterwards
    assert {p.name: p.stat().st_size for p in tmp_path.iterdir()} == sizes


def test_concurrency_one_is_strictly_serial(tmp_path):
    out, _ = ss.bounded_map(["ok-1", "ok-2", "ok-3"], HEARTBEAT.format(log=str(tmp_path)), **LIM)
    assert all("ok" in v for v in out.values()) and _peak(list(_lifetimes(tmp_path).values())) == 1


def test_bounded_map_retries_with_capped_backoff_then_gives_up(tmp_path):
    counter = str(tmp_path / "tries")
    flaky = f"""
import os
def one(item):
    n = int(open({counter!r}).read()) if os.path.exists({counter!r}) else 0
    open({counter!r}, "w").write(str(n + 1))
    if n < 2:
        raise ConnectionError("https://api.example/v1?key=SECRET")
    return "fine"
"""
    sleeps = []
    out, stats = ss.bounded_map(["flaky"], flaky, **LIM, sleep=sleeps.append)
    assert out == {"flaky": {"ok": "fine"}} and sleeps == [1, 2] and stats == {"requests": 3, "retries": 2}

    dead = 'def one(item):\n    raise ValueError("token=SECRET")\n'
    sleeps.clear()
    out, stats = ss.bounded_map(["dead"], dead, **{**LIM, "attempts": 5}, sleep=sleeps.append)
    assert out == {"dead": {"error": "ValueError"}}        # class name only, never the message
    assert sleeps == [1, 2, 3, 3] and stats == {"requests": 5, "retries": 4}  # bounded, capped
    assert ss.bounded_map(["dead"], dead, **{**LIM, "attempts": 0}, sleep=sleeps.append)[1]["requests"] == 1
    broken, _ = ss.bounded_map(["x"], "import no_such_module_xyz\n", **{**LIM, "attempts": 1})
    assert broken == {"x": {"error": "child_failed"}}       # a child that cannot even start is contained
    exits, _ = ss.bounded_map(["x"], "def one(i):\n    raise SystemExit(3)\n", **{**LIM, "attempts": 1})
    assert exits == {"x": {"error": "SystemExit"}}


def test_bounded_map_paces_starts_and_honours_the_stage_deadline():
    echo = "def one(item):\n    return item\n"
    waits = []
    ss.bounded_map(["a", "b", "c", "d"], echo, **{**LIM, "min_interval_s": 30}, sleep=waits.append)
    assert len(waits) == 3 and all(25 < w <= 90 for w in waits)  # each start waits for its slot
    out, stats = ss.bounded_map(["a", "b"], echo, **LIM, deadline_s=0)
    assert out == {"a": {"error": "stage_deadline"}, "b": {"error": "stage_deadline"}}
    assert stats == {"requests": 0, "retries": 0}             # past the deadline nothing new starts


def test_limits_are_conservative_bounded_and_overridable():
    for lim in ss.DEFAULT_LIMITS.values():
        assert 1 <= lim["concurrency"] <= 4 and 1 <= lim["attempts"] <= 3 and lim["timeout_s"] <= 60
    lim = ss.make_limits(concurrency=2, attempts=1, timeout_s=10)
    assert all((lim[s]["concurrency"], lim[s]["attempts"], lim[s]["timeout_s"]) == (2, 1, 10) for s in ss.STAGES)
    assert ss.make_limits() == ss.DEFAULT_LIMITS
    for bad in ({"concurrency": 0}, {"concurrency": 9}, {"attempts": 6}, {"timeout_s": 500}):
        with pytest.raises(ValueError):
            ss.make_limits(**bad)
    facts = ss.DEFAULT_LIMITS["facts"]
    assert ss._stage_deadline(400, facts) < ss._exec_timeout(400, facts) < 4 * 3600  # the whole stage is bounded
    script = ss._container_script("def one(i):\n    return i\n", ["A"], facts)
    assert f"deadline_s={ss._stage_deadline(1, facts)}" in script and "subprocess.Popen" in script
    assert "threading.Thread(" not in script                  # no un-killable request threads
    with pytest.raises(SystemExit):
        ss.main(["--dry-run", "--concurrency", "50"], clock=lambda: NOON)


STUBS = {
    "yfinance.py": """
class Ticker:
    def __init__(self, s):
        self.s = s
    @property
    def info(self):
        if self.s == "BAD":
            raise KeyError("https://query.example/?crumb=SECRET")
        return {"shortName": self.s + " Inc", "sector": "Energy", "industry": "Oil", "marketCap": 5e9,
                "averageVolume": 2e6, "currentPrice": 30.0}
    def history(self, period):
        return [0] * 1000
""",
    "httpx.py": """
class _R:
    text = "<html>"
    def raise_for_status(self):
        pass
def get(*a, **k):
    return _R()
""",
    "pandas.py": """
class _Table:
    columns = ["Symbol", "Security"]
    def __getitem__(self, col):
        return UNIVERSE
def read_html(_):
    return [_Table()]
UNIVERSE = {universe!r}
""",
    "server.py": """
async def get_historical_news(symbol, start_at, end_at):
    return {"articles": [{}] * 4, "complete": symbol != "T03"}
""",
}


def _stub_containers(monkeypatch, tmp_path, universe):
    """Run the REAL generated container scripts in-process; their child
    requests are real processes that import stub yfinance / httpx / pandas /
    alpaca `server` modules from PYTHONPATH. No Docker, no network."""
    for name, src in STUBS.items():
        (tmp_path / name).write_text(src.replace("{universe!r}", repr(list(universe))))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
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


FAST = {s: {**lim, "concurrency": 4, "min_interval_s": 0, "backoff_base_s": 0, "backoff_cap_s": 0}
        for s, lim in ss.DEFAULT_LIMITS.items()}


def test_live_providers_end_to_end_through_the_real_container_scripts(monkeypatch, tmp_path):
    universe = [f"T{i:02d}" for i in range(18)] + ["BRK.B", "BAD"]
    calls = _stub_containers(monkeypatch, tmp_path, universe)
    snap = ss.run_screen(SAT, ss.LiveProviders(FAST), clock=ticking())
    assert [c[0] for c in calls] == ["maverick-mcp", "maverick-mcp", "maverick-mcp", "alpaca-mcp"]
    assert all(isinstance(c[1], int) and 0 < c[1] < 4 * 3600 for c in calls)
    assert snap["screen_status"] == "ok" and "BRK-B" in snap["pool"] and len(snap["pool"]) == 18
    assert row(snap, "BAD")["reasons"] == ["facts_fetch_error"]
    assert row(snap, "T03")["reasons"] == [f"news_probe_{i}_unavailable" for i in (1, 2, 3)]  # fail closed
    t = snap["telemetry"]
    assert (t["facts"]["requested"], t["facts"]["ok"], t["facts"]["unavailable"]) == (20, 19, 1)
    assert t["facts"]["errors"] == {"KeyError": 1} and t["facts"]["requests"] == 22 and t["facts"]["retries"] == 2
    assert (t["history"]["requested"], t["news"]["requested"]) == (19, 19)
    assert t["news"]["errors"] == {"incomplete_pagination": 3} and t["news"]["requests"] == 57
    assert t["news"]["retries"] == 0  # incomplete pagination is terminal, not retried
    assert t["limits"]["news"]["concurrency"] == 4
    assert t["universe"] == {**t["universe"], "requested": 1, "ok": 1, "unavailable": 0, "errors": {},
                             "requests": 1, "retries": 0, "stage_error": None}
    assert set(t["universe"]) == set(t["facts"])               # one telemetry contract for every stage
    assert "SECRET" not in ss.snapshot_to_json(snap)
    ss.validate_snapshot(snap, kind="attempt")


def test_universe_telemetry_records_a_failed_fetch_like_any_other_stage(monkeypatch, tmp_path):
    _stub_containers(monkeypatch, tmp_path, ["A"])
    (tmp_path / "httpx.py").write_text('def get(*a, **k):\n    raise OSError("https://secret.example/?token=SECRET")\n')
    snap = ss.run_screen(SAT, ss.LiveProviders(FAST), clock=ticking())
    u = snap["telemetry"]["universe"]
    assert snap["failure_reasons"] == ["universe_fetch_failed"]
    assert {k: u[k] for k in ("requested", "ok", "unavailable", "errors", "requests", "retries", "stage_error")} == {
        "requested": 1, "ok": 0, "unavailable": 1, "errors": {"OSError": 1}, "requests": 3, "retries": 2,
        "stage_error": "OSError"}
    assert isinstance(u["elapsed_s"], float) and "SECRET" not in ss.snapshot_to_json(snap)
    ss.validate_snapshot(snap, kind="attempt")
    empty = ss.run_screen(SAT, Fake([], stats={"requests": 1, "retries": 0}), clock=ticking())
    assert empty["telemetry"]["universe"]["errors"] == {"empty": 1} and empty["telemetry"]["universe"]["ok"] == 0


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
    assert gone["telemetry"]["universe"] == {**gone["telemetry"]["universe"], "requested": 1, "ok": 0,
                                             "unavailable": 1, "errors": {"RuntimeError": 1},
                                             "stage_error": "RuntimeError"}
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


def test_an_orphaned_result_is_neither_a_predecessor_nor_a_usable_pool(tmp_path, capsys):
    good, _ = ss.screen(SAT, Fake(names(6)), tmp_path)
    ss.attempt_path(tmp_path, good["attempt_id"]).unlink()    # the result file is intact and self-consistent
    assert ss.load_snapshot(ss.result_path(tmp_path, good["screen_id"]), "result") == good
    with pytest.raises(ss.SnapshotCorrupt, match="orphaned"):
        ss.load_result(tmp_path, good["screen_id"])
    later = Fake(names(6))
    with pytest.raises(ss.SnapshotCorrupt, match="orphaned"):  # never named as previous_ok_screen...
        ss.run_screen(date(2026, 10, 10), later, snapshot_dir=tmp_path)
    assert later.asked == {}                                   # ...and found out before any fetch
    sat = lambda: datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)  # noqa: E731
    for mode in ("--dry-run", "--write"):
        cli = Fake(names(6))
        assert ss.main([mode, "--output-dir", str(tmp_path)], cli, sat) == 2 and cli.asked == {}
        assert "REFUSED" in capsys.readouterr().err
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["reason"] == "snapshot_invalid"
    # a backing attempt that exists but differs is no better
    other = ss.run_screen(SAT, Fake(names(5)))
    ss.write_attempt(tmp_path, other)
    with pytest.raises(ss.SnapshotCorrupt, match="differs"):
        ss.load_result(tmp_path, good["screen_id"])


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
    with pytest.raises(ss.SnapshotCorrupt, match="recorded evidence"):
        ss.validate_snapshot(rehash({**failed, "failure_reasons": []}), kind="attempt")
    with pytest.raises(ss.SnapshotCorrupt, match="altered"):
        ss.validate_snapshot({**good, "pool": good["pool"][:-1]}, kind="attempt")  # edited, not re-hashed
    for junk in ([], None, "x", {"schema_version": ss.SCHEMA_VERSION}):
        with pytest.raises(ss.SnapshotCorrupt):
            ss.validate_snapshot(junk, kind="attempt")
    with pytest.raises(ss.SnapshotCorrupt):                                    # nothing invalid is ever written
        ss.write_attempt(tmp_path, rehash({**good, "counts": {}}))
    assert not list(tmp_path.rglob("*.json"))


def _row(i, **changes):
    return lambda s: s["rows"][i].update(changes)


def _tel(stage, **changes):
    return lambda s: s["telemetry"][stage].update(changes)


def _forge_success(s):
    """A failed attempt dressed up as a success, internally consistent except for the evidence."""
    s.update(screen_status="ok", failure_reasons=[], pool=[], pool_hash=ss._hash([]), sector_mix={},
             effective_from="2026-10-05")


PREV = {"screen_id": "2026-09-26_" + ss.config_hash(ss.DEFAULT_CONFIG)[:12], "content_hash": "a" * 64}

# base "ok": S000-S011, S003 rejected at facts (price), everything else eligible.
NESTED = {
    # universe
    "universe source empty": _set(["universe", "source"], ""),
    "universe url not text": _set(["universe", "url"], 7),
    "universe extra key": _set(["universe", "payload"], "<html>"),
    "universe timestamp naive": _set(["universe", "fetched_at"], "2026-10-03T12:00:03"),
    "universe timestamp missing": _set(["universe", "fetched_at"], None),
    "universe timestamp before the run": _set(["universe", "fetched_at"], "2026-10-03T11:00:00+00:00"),
    "universe count is a bool": _set(["universe", "count"], True),
    # rows: types
    "row name is a number": _row(0, name=5),
    "row market cap is text": _row(0, market_cap="5e9"),
    "row market cap negative": _row(0, market_cap=-5e9),
    "row price is a bool": _row(0, price=True),
    "row history bars negative": _row(0, history_bars=-1),
    "row history bars fractional": _row(0, history_bars=1200.5),
    "row history bars bool": _row(0, history_bars=True),
    "row news counts wrong length": _row(0, news_counts=[9, 9]),
    "row news counts negative": _row(0, news_counts=[9, -1, 9]),
    "row news counts text": _row(0, news_counts=["3", "3", "3"]),
    "row news counts not a list": _row(0, news_counts=9),
    "row reason is prose": _row(3, reasons=["Traceback (most recent call last): key=SECRET"]),
    # rows: a status the recorded numbers do not produce
    "eligible but price below the floor": _row(0, price=1.0),
    "eligible but cap above the ceiling": _row(0, market_cap=9e10),
    "eligible but excluded industry": _row(0, industry="Biotechnology"),
    "eligible but too little history": _row(0, history_bars=10),
    "eligible but too little news": _row(0, news_counts=[1, 1, 1]),
    "eligible but a news probe missing": _row(0, news_counts=[9, None, 9]),
    "eligible but no facts at all": _row(0, name=None, sector=None, industry=None, market_cap=None,
                                         avg_volume=None, price=None),
    "rejected for the wrong reason": _row(3, reasons=["market_cap_below_min"]),
    "rejected but its numbers pass": _row(3, price=40.0),
    "rejected at facts yet has history": _row(3, history_bars=1200),
    "rejected at facts yet has news": _row(3, news_counts=[3, 3, 3]),
    "unavailable with invented reason": _row(3, status="data_unavailable", reasons=["facts_fetch_error"]),
    # telemetry
    "telemetry stage missing a key": lambda s: s["telemetry"]["facts"].pop("retries"),
    "telemetry extra stage": _set(["telemetry", "laya"], {}),
    "telemetry negative requested": _tel("facts", requested=-1),
    "telemetry counters do not add up": _tel("facts", ok=3),
    "telemetry requested mismatch": _tel("history", requested=12, ok=12),
    "telemetry news requested mismatch": _tel("news", requested=1, ok=1),
    "telemetry requests negative": _tel("news", requests=-4),
    "telemetry retries text": _tel("news", retries="2"),
    "telemetry elapsed negative": _tel("facts", elapsed_s=-1.0),
    "telemetry elapsed text": _tel("facts", elapsed_s="fast"),
    "telemetry timestamp naive": _tel("history", started_at="2026-10-03T12:00:05"),
    "telemetry timestamp outside the run": _tel("history", started_at="2027-01-01T00:00:00+00:00"),
    "telemetry error key is a message": _tel("facts", errors={"HTTP 401 for https://x/?apikey=SECRET": 1}),
    "telemetry error count zero": _tel("facts", errors={"KeyError": 0}),
    "telemetry too many error codes": _tel("facts", errors={f"E{i}": 1 for i in range(20)}),
    "telemetry stage error is prose": _tel("news", stage_error="Traceback: boom at line 3"),
    "telemetry universe requested 2": _tel("universe", requested=2, ok=2),
    "telemetry universe says it failed": _tel("universe", ok=0, unavailable=1),
    "telemetry limits malformed": _set(["telemetry", "limits"], {"facts": {"concurrency": "lots"}}),
    # timestamps
    "finished before started": _set(["finished_at"], "2026-10-03T11:59:00+00:00"),
    "finished naive": _set(["finished_at"], "2026-10-03T12:30:00"),
    # sector mix
    "sector mix bool value": _set(["sector_mix"], {"Industrials": True}),
    "sector mix negative": _set(["sector_mix"], {"Industrials": 12, "Energy": -1}),
    "sector mix zero entry": _set(["sector_mix"], {"Industrials": 11, "Energy": 0}),
    "sector mix right total wrong sector": _set(["sector_mix"], {"Energy": 11}),
    # lineage
    "previous empty": _set(["previous_ok_screen"], {}),
    "previous extra key": _set(["previous_ok_screen"], {**PREV, "pool": ["S000"]}),
    "previous id malformed": _set(["previous_ok_screen"], {**PREV, "screen_id": "last week"}),
    "previous is this screen": _set(["previous_ok_screen"], {**PREV, "screen_id": "2026-10-03_" + PREV["screen_id"][11:]}),
    "previous is in the future": _set(["previous_ok_screen"], {**PREV, "screen_id": "2026-12-05_" + PREV["screen_id"][11:]}),
    "previous is another config": _set(["previous_ok_screen"], {**PREV, "screen_id": "2026-09-26_" + "b" * 12}),
    "previous hash malformed": _set(["previous_ok_screen"], {**PREV, "content_hash": "abc"}),
    # news windows / config
    "news windows altered": _set(["news_windows", 0, "end_at"], "2026-10-02T00:00:00+00:00"),
    "news windows dropped": lambda s: s["news_windows"].pop(),
    # state forgery
    "ok relabelled failed": lambda s: s.update(screen_status="failed", failure_reasons=["universe_fetch_failed"],
                                               pool=None, pool_hash=None, sector_mix=None, effective_from=None),
    "ok with an invented stage failure": _tel("news", stage_error="RuntimeError"),
}


@pytest.mark.parametrize("label", NESTED)
def test_nested_corrupt_but_rehashed_records_are_rejected(label, tmp_path):
    good = _ok_snapshot()
    control = rehash({**copy.deepcopy(good), "previous_ok_screen": dict(PREV)})
    ss.validate_snapshot(control, kind="result", stem=good["screen_id"])   # a valid predecessor reference passes
    bad = copy.deepcopy(good)
    NESTED[label](bad)
    rehash(bad)
    for kind in ("attempt", "result"):
        with pytest.raises(ss.SnapshotCorrupt):
            ss.validate_snapshot(copy.deepcopy(bad), kind=kind)
    # ...and therefore can never be written, reused, or served as a pool
    with pytest.raises(ss.SnapshotCorrupt):
        ss.write_attempt(tmp_path, bad)
    ss.write_attempt(tmp_path, good)
    ss.publish_result(tmp_path, good)
    for path in (ss.attempt_path(tmp_path, good["attempt_id"]), ss.result_path(tmp_path, good["screen_id"])):
        path.write_text(ss.snapshot_to_json(bad), newline="\n")            # both copies swapped, still identical
    with pytest.raises(ss.SnapshotCorrupt):
        ss.screen(SAT, Fake(names(12)), tmp_path)
    assert ss.usable_pool(tmp_path, date(2026, 10, 6))["reason"] == "snapshot_invalid"


def test_state_cannot_be_forged_with_a_new_content_hash():
    failed = _failed_snapshot()                                 # the news stage failed
    forged = copy.deepcopy(failed)
    _forge_success(forged)
    with pytest.raises(ss.SnapshotCorrupt, match="recorded evidence"):
        ss.validate_snapshot(rehash(forged), kind="attempt")
    for reasons in (["data_unavailable_above_threshold"], ["news_coverage_incomplete", "universe_empty"],
                    ["universe_fetch_failed"], ["Traceback"]):
        with pytest.raises(ss.SnapshotCorrupt, match="recorded evidence"):
            ss.validate_snapshot(rehash({**copy.deepcopy(failed), "failure_reasons": reasons}), kind="attempt")
    gone = ss.run_screen(SAT, Fake(names(5), fail=("universe",)), clock=ticking())
    ss.validate_snapshot(gone, kind="attempt")
    for change in ({"universe": {**gone["universe"], "fetched_at": gone["started_at"]}},   # "fetched" yet failed
                   {"rows": _ok_snapshot()["rows"]}):                                       # rows from nowhere
        with pytest.raises(ss.SnapshotCorrupt):
            ss.validate_snapshot(rehash({**copy.deepcopy(gone), **change}), kind="attempt")
    cfg_bad = copy.deepcopy(_ok_snapshot())
    for mutate in (lambda c: c.pop("min_price"), lambda c: c.update(min_price="10"),
                   lambda c: c.update(excluded_industries="Biotechnology"), lambda c: c.update(extra=1),
                   lambda c: c.update(refresh_interval_days=0)):
        snap = copy.deepcopy(cfg_bad)
        mutate(snap["config"])
        snap["config_hash"] = ss.config_hash(snap["config"])    # even with a matching config hash and screen id
        snap["screen_id"] = ss.screen_id(SAT, snap["config"])
        snap["attempt_id"] = ss.attempt_id(snap["screen_id"], 1)
        with pytest.raises(ss.SnapshotCorrupt, match="config"):
            ss.validate_snapshot(rehash(snap), kind="attempt")


# 12. determinism -------------------------------------------------------

def test_identical_inputs_give_identical_bytes(tmp_path):
    def build(d):
        syms = names(30)
        fake = Fake(list(reversed(syms)), facts={"S003": {"error": "x"}, "S004": {**GOOD, "sector": "Energy"}},
                    history={"S005": {"bars": 3}})
        return ss.snapshot_to_json(ss.screen(SAT, fake, d, clock=ticking())[0])

    a, b = build(tmp_path / "a"), build(tmp_path / "b")
    assert a == b and json.loads(a)["sector_mix"] == {"Energy": 1, "Industrials": 27}
