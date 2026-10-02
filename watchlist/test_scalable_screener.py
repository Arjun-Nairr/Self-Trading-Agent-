"""Tests for watchlist/scalable_screener.py (Stage 1). Every provider is a
fake: no network, no Docker, no real company data."""
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scalable_screener as ss  # noqa: E402

SAT = date(2026, 10, 3)
T0 = "2026-10-03T12:00:00+00:00"
GOOD = {"name": "Good Co", "sector": "Industrials", "industry": "Machinery",
        "market_cap": 5e9, "avg_volume": 3e6, "price": 40.0}


class Fake:
    """Injected providers. `facts`/`history`/`news` map symbol -> entry;
    anything absent gets a passing default. Records which symbols each stage
    was asked for."""

    def __init__(self, symbols, facts=None, history=None, news=None, fail=()):
        self.symbols, self.f, self.h, self.n, self.fail = list(symbols), facts or {}, history or {}, news or {}, fail
        self.asked = {}

    def universe(self):
        if "universe" in self.fail:
            raise RuntimeError("wiki down")
        return {"source": "fake", "url": "fake://u", "fetched_at": T0, "symbols": self.symbols}

    def _stage(self, name, symbols, table, default):
        self.asked[name] = list(symbols)
        if name in self.fail:
            raise RuntimeError(f"{name} down")
        rows = {s: table.get(s, default) for s in symbols}
        return {"fetched_at": T0, "rows": {s: v for s, v in rows.items() if v is not None}}

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
    assert 'open("watchlist.json"' not in src and '"watchlist.json")' not in src
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
    assert ss.evaluate_news([{"articles": 5}, {"error": "x"}, {"articles": True}], 3, cfg) == (
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


# 3. variable pool ------------------------------------------------------

@pytest.mark.parametrize("n", [1, 10, 11, 57, 400])
def test_pool_is_every_eligible_name_with_no_target_size(n):
    snap = ss.run_screen(SAT, Fake(names(n)))  # all one sector: no per-sector trim either
    assert snap["screen_status"] == "ok" and snap["pool"] == names(n)
    assert snap["sector_mix"] == {"Industrials": n}


def test_empty_pool_is_a_valid_ok_screen_and_never_padded():
    snap = ss.run_screen(SAT, Fake(names(5), facts={s: {**GOOD, "price": 1.0} for s in names(5)}))
    assert (snap["screen_status"], snap["pool"], snap["sector_mix"]) == ("ok", [], {})


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
    snap = ss.run_screen(date(2027, 3, 6), fake)
    assert fake.windows == later == snap["data"]["news_windows"]
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
    assert all(r["status"] in ("eligible", "rejected", "data_unavailable") for r in snap["rows"])


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


# 7. canonical form, hashes, write-once ---------------------------------

def test_ids_and_hashes():
    cfg2 = {**ss.DEFAULT_CONFIG, "min_price": 12.0}
    assert ss.config_hash(dict(reversed(list(ss.DEFAULT_CONFIG.items())))) == ss.config_hash(ss.DEFAULT_CONFIG)
    assert ss.config_hash(cfg2) != ss.config_hash(ss.DEFAULT_CONFIG)
    assert ss.screen_id(SAT, ss.DEFAULT_CONFIG) == f"2026-10-03_{ss.config_hash(ss.DEFAULT_CONFIG)[:12]}"
    a = ss.run_screen(SAT, Fake(["B", "A", "C"]))
    b = ss.run_screen(SAT, Fake(["C", "B", "A"]))
    assert a["universe"]["constituents_hash"] == b["universe"]["constituents_hash"] == ss._hash(["A", "B", "C"])
    assert a["pool_hash"] == ss._hash(["A", "B", "C"]) and a["config"] == ss.DEFAULT_CONFIG
    assert ss.run_screen(SAT, Fake(["A", "B"]))["pool_hash"] != a["pool_hash"]
    for key in ("schema_version", "screener_version", "screen_id", "config_hash", "universe", "data", "rows",
                "pool", "pool_hash", "screen_status", "effective_from", "previous_ok_screen", "content_hash"):
        assert key in a
    assert a["universe"] == {"source": "fake", "url": "fake://u", "fetched_at": T0, "count": 3,
                             "constituents_hash": ss._hash(["A", "B", "C"])}
    assert a["data"]["fetched_at"] == {"facts": T0, "history": T0, "news": T0}
    assert a["effective_from"] == "2026-10-05"  # the Monday after a weekend screen
    text = ss.snapshot_to_json(a)
    assert text.endswith("\n") and text.isascii() and json.loads(text) == a


def test_write_is_atomic_write_once_and_rerun_reuses(tmp_path):
    fake = Fake(names(12))
    snap, reused = ss.screen(SAT, fake, tmp_path)
    path = ss.snapshot_path(tmp_path, snap["screen_id"])
    stored = path.read_bytes()
    assert not reused and stored == ss.snapshot_to_json(snap).encode() and b"\r" not in stored
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temp file left behind

    assert ss.write_snapshot(tmp_path, snap) == path and path.read_bytes() == stored  # identical: no-op

    again = Fake(names(3))
    snap2, reused2 = ss.screen(SAT, again, tmp_path)  # same ID: verify + reuse, fetch nothing
    assert reused2 and snap2 == snap and again.asked == {}

    different = ss.run_screen(SAT, Fake(names(3)))
    assert different["screen_id"] == snap["screen_id"]
    with pytest.raises(ss.ScreenConflict):
        ss.write_snapshot(tmp_path, different)
    assert path.read_bytes() == stored

    path.write_text(path.read_text().replace('"S000"', '"EVIL"', 1), newline="\n")
    with pytest.raises(ss.SnapshotCorrupt):
        ss.screen(SAT, Fake(names(3)), tmp_path)


def test_failed_attempt_is_also_recorded_write_once(tmp_path):
    snap, _ = ss.screen(SAT, Fake(names(5), fail=("universe",)), tmp_path)
    assert snap["screen_status"] == "failed" and ss.load_snapshot(ss.snapshot_path(tmp_path, snap["screen_id"])) == snap
    again, reused = ss.screen(SAT, Fake(names(5)), tmp_path)  # same date: the record stands
    assert reused and again["screen_status"] == "failed"
    retry, reused = ss.screen(date(2026, 10, 4), Fake(names(5)), tmp_path)  # next day: a new record
    assert not reused and retry["screen_status"] == "ok" and len(list(tmp_path.glob("*.json"))) == 2


# 8. last-good carry-forward --------------------------------------------

def test_carry_forward_lasts_one_refresh_only(tmp_path):
    assert ss.usable_pool(tmp_path, date(2026, 10, 5))["reason"] == "no_successful_screen"
    good, _ = ss.screen(SAT, Fake(names(8)), tmp_path)                       # effective Mon 2026-10-05
    failed, _ = ss.screen(date(2026, 10, 10), Fake(names(8), fail=("news",)), tmp_path)
    assert failed["previous_ok_screen"] == {"screen_id": good["screen_id"], "content_hash": good["content_hash"]}
    assert good["previous_ok_screen"] is None

    def state(d):
        u = ss.usable_pool(tmp_path, d)
        return u["state"], u["pool"]

    assert state(date(2026, 10, 4)) == ("unavailable", None)                 # not yet effective
    assert state(date(2026, 10, 5)) == state(date(2026, 10, 11)) == ("current", names(8))
    assert state(date(2026, 10, 12)) == state(date(2026, 10, 18)) == ("carried_forward", names(8))
    expired = ss.usable_pool(tmp_path, date(2026, 10, 19))
    assert expired == {"state": "unavailable", "reason": "last_successful_screen_expired",
                       "screen_id": good["screen_id"], "pool": None}

    fresh, _ = ss.screen(date(2026, 10, 17), Fake(names(4)), tmp_path)        # a later good screen takes over
    assert fresh["previous_ok_screen"]["screen_id"] == good["screen_id"]
    assert state(date(2026, 10, 18)) == ("carried_forward", names(8))         # not effective until Monday
    assert state(date(2026, 10, 19)) == ("current", names(4))


# 9. determinism + CLI --------------------------------------------------

def test_identical_inputs_give_identical_bytes(tmp_path):
    def build(d):
        syms = names(30)
        fake = Fake(list(reversed(syms)), facts={"S003": {"error": "x"}, "S004": {**GOOD, "sector": "Energy"}},
                    history={"S005": {"bars": 3}})
        return ss.snapshot_to_json(ss.screen(SAT, fake, d)[0])

    a, b = build(tmp_path / "a"), build(tmp_path / "b")
    assert a == b and json.loads(a)["sector_mix"] == {"Energy": 1, "Industrials": 27}


def test_cli_dry_run_writes_nothing_and_write_uses_the_explicit_dir(tmp_path, capsys):
    out = tmp_path / "out"
    assert ss.main(["--screen-date", "2026-10-03", "--dry-run", "--output-dir", str(out)], Fake(names(4))) == 0
    assert "DRY RUN -- nothing written" in capsys.readouterr().out and not out.exists()
    assert ss.main(["--screen-date", "2026-10-03", "--write", "--output-dir", str(out)], Fake(names(4))) == 0
    assert [p.suffix for p in out.iterdir()] == [".json"] and "pool (4)" in capsys.readouterr().out
    assert ss.main(["--screen-date", "2026-10-03", "--write", "--output-dir", str(out)],
                   Fake(names(4), fail=("universe",))) == 0  # reused, not re-fetched
    assert "reused existing" in capsys.readouterr().out
    assert ss.main(["--screen-date", "2026-10-10", "--dry-run", "--output-dir", str(out)],
                   Fake(names(4), fail=("universe",))) == 1
    capsys.readouterr()
    assert ss.main(["--screen-date", "2026-10-06", "--usable", "--output-dir", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "current"
    with pytest.raises(SystemExit):
        ss.main(["--screen-date", "2026-10-03"])  # a mode is mandatory: nothing runs by default


def test_live_container_scripts_are_valid_python_and_never_run_here(monkeypatch):
    seen = []
    monkeypatch.setattr(ss, "_run_in", lambda service, script, timeout=0: seen.append((service, script)) or {})
    live = ss.LiveProviders()
    live.universe(), live.facts(["A"]), live.history(["A"]), live.news(["A"], ss.news_windows(SAT))
    assert [s for s, _ in seen] == ["maverick-mcp", "maverick-mcp", "maverick-mcp", "alpaca-mcp"]
    for _, script in seen:
        compile(script, "<container script>", "exec")
