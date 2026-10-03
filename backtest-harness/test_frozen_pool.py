"""Frozen coverage-qualified pools (laya_preflight.build_pool / write_pool /
load_frozen_pool) and the runner's --selector-universe. Synthetic coverage
results only -- no Docker, Neon, Hermes, or provider calls.
Run: python -m pytest -q test_frozen_pool.py
"""
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import laya_preflight as lp
import laya_selector as ls
import run_backtest as rb
from anonymize import RunMasking

PARENT = ls.REPO / "watchlist" / "experimental_candidates60.json"
U60 = ls.load_universe(PARENT)
START, END = date(2026, 9, 28), date(2026, 10, 2)
NOW = datetime(2026, 10, 3, 17, 0, tzinfo=timezone.utc)


def _price(symbols, bad=(), fetch_error=()):
    out = {}
    for s in symbols:
        reasons = []
        if s in fetch_error:
            reasons = ["history_fetch_error"]
        elif s in bad:
            reasons = ["missing_5_trading_days", "warmup_1_of_40_bars"]
        out[s] = {"pass": not reasons, "bars_in_window": 0 if reasons else 5,
                  "missing_days": None if s in fetch_error else (5 if reasons else 0),
                  "warmup_bars": 1 if reasons else 83, "bad_bars": None if s in fetch_error else 0,
                  "reasons": reasons}
    return out


def _news(symbols, short=()):
    return {s: {"pass": s not in short, "counts": [1, 1, 1] if s in short else [3, 4, 5],
                "total": 3 if s in short else 12,
                "reasons": ["only_3_articles_need_6"] if s in short else []} for s in symbols}


def _pool(universe=U60, parent=PARENT, bad=(), short=(), fetch_error=()):
    syms = list(universe.symbols)
    cands = lp.qualify(universe, _price(syms, bad, fetch_error), _news(syms, short), START, END)
    return lp.build_pool(universe, parent, cands, START, END, checked_at=NOW)


def _write(tmp_path, pool):
    return lp.write_pool(pool, tmp_path)


def _rewrite(path, pool, *, rehash_evidence=False, rehash_content=True):
    q = pool["qualification"]
    if rehash_evidence:
        q["evidence_sha256"] = lp._sha(lp._evidence_body(q))
    if rehash_content:
        pool["content_sha256"] = lp._sha({k: v for k, v in pool.items() if k != "content_sha256"})
    path.write_text(json.dumps(pool), encoding="utf-8")


# -- qualification and freezing ---------------------------------------------

def test_failing_names_are_excluded_without_blocking_the_rest(tmp_path):
    pool = _pool(bad={"EQR"}, short={"OMC", "TOL"})
    syms = [t["symbol"] for t in pool["tickers"]]
    assert len(syms) == 57 == pool["expected_count"] and {"EQR", "OMC", "TOL"}.isdisjoint(syms)
    assert syms == [s for s in U60.symbols if s not in {"EQR", "OMC", "TOL"}], "parent order"
    c = pool["qualification"]["candidates"]
    assert sorted(c) == sorted(U60.symbols), "a record for EVERY candidate"
    assert c["EQR"]["status"] == "excluded" and c["EQR"]["reasons"] == ["missing_5_trading_days", "warmup_1_of_40_bars"]
    assert c["EQR"]["price"]["warmup_bars_point_in_time"] == 1
    assert c["OMC"]["news"] == {"distinct_per_probe": [1, 1, 1], "distinct_total": 3}
    assert c["NUE"] == {"status": "qualified", "reasons": [], "news": {"distinct_per_probe": [3, 4, 5], "distinct_total": 12},
                        "price": {"warmup_bars_point_in_time": 83, "retrospective_bars_in_window": 5,
                                  "retrospective_missing_days": 0, "malformed_bars": 0}}
    q = pool["qualification"]
    assert q["parent"] == {"path": "watchlist/experimental_candidates60.json", "version": "candidates60-v1",
                           "universe_hash": U60.hash, "count": 60}
    assert q["policy"] == lp.COVERAGE_POLICY and q["window"] == {"start": "2026-09-28", "end": "2026-10-02"}
    assert all(b < "2026-09-28" for _, b in q["news_probe_windows"]), "news eligibility is pre-start only"
    assert pool["word_like_tickers"] == ["CF", "DINO", "RYAN"]
    universe, _ = lp.load_frozen_pool(_write(tmp_path, pool))
    assert universe.symbols == tuple(syms) and universe.version == pool["version"]


def test_too_few_qualified_names_refuses_the_pool():
    short = set(U60.symbols[:41])                       # 19 qualify
    with pytest.raises(lp.PoolRefused, match="only 19 of 60"):
        _pool(short=short)
    assert len(_pool(short=set(U60.symbols[:40]))["tickers"]) == 20  # exactly the minimum


def test_provider_wide_fetch_failure_never_becomes_a_pool():
    with pytest.raises(lp.PoolRefused, match="provider-wide"):
        _pool(fetch_error=set(U60.symbols[:7]))         # 7/60 > 10%, though 53 'pass'
    assert len(_pool(fetch_error=set(U60.symbols[:6]))["tickers"]) == 54  # 6/60 = 10%: excluded only


def test_pools_are_write_once(tmp_path):
    pool = _pool()
    _write(tmp_path, pool)
    with pytest.raises(FileExistsError):
        _write(tmp_path, pool)


# -- re-verification ---------------------------------------------------------

@pytest.mark.parametrize("tamper,match", [
    (lambda p: p["tickers"].pop(), "content_sha256"),
    (lambda p: p["qualification"]["candidates"]["NUE"]["news"].update(distinct_total=99), "content_sha256"),
])
def test_tampering_is_detected(tmp_path, tamper, match):
    pool = _pool(bad={"EQR"})
    path = _write(tmp_path, pool)
    tamper(pool)
    _rewrite(path, pool, rehash_content=False)
    with pytest.raises(lp.PoolInvalid, match=match):
        lp.load_frozen_pool(path)


def test_evidence_edit_is_detected_even_with_a_recomputed_content_hash(tmp_path):
    pool = _pool(bad={"EQR"})
    path = _write(tmp_path, pool)
    pool["qualification"]["candidates"]["EQR"].update(status="qualified", reasons=[])
    _rewrite(path, pool)
    with pytest.raises(lp.PoolInvalid, match="evidence_sha256"):
        lp.load_frozen_pool(path)


def test_membership_must_be_exactly_the_qualified_candidates(tmp_path):
    pool = _pool(bad={"EQR"})
    path = _write(tmp_path, pool)
    pool["tickers"].append({"symbol": "EQR", "sector": "Real Estate", "name": "Equity Residential", "group": "added"})
    pool["expected_count"] += 1
    _rewrite(path, pool, rehash_evidence=True)
    with pytest.raises(lp.PoolInvalid, match="exactly the parent's qualified"):
        lp.load_frozen_pool(path)
    pool = _pool(bad={"EQR"})
    pool["qualification"]["candidates"]["EQR"]["reasons"] = []   # excluded but no reason
    _rewrite(path, pool, rehash_evidence=True)
    with pytest.raises(lp.PoolInvalid, match="disagrees with its reasons"):
        lp.load_frozen_pool(path)


def test_a_changed_parent_config_or_policy_invalidates_the_pool(tmp_path):
    parent = tmp_path / "parent.json"
    shutil.copy(PARENT, parent)
    pool = _pool(universe=ls.load_universe(parent), parent=parent)
    path = _write(tmp_path / "pools", pool)
    lp.load_frozen_pool(path)
    raw = json.loads(parent.read_text(encoding="utf-8"))
    raw["tickers"][0]["name"] = "Renamed Corporation"
    parent.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(lp.PoolInvalid, match="parent .* changed"):
        lp.load_frozen_pool(path)
    pool = _pool()
    pool["qualification"]["policy"]["min_news_articles"] = 1
    path2 = tmp_path / "p2.json"
    _rewrite(path2, pool, rehash_evidence=True)
    with pytest.raises(lp.PoolInvalid, match="different coverage policy"):
        lp.load_frozen_pool(path2)


# -- the runner uses exactly the frozen membership ----------------------------

def test_runner_loads_the_pool_for_masking_packaging_and_risk_membership(tmp_path):
    path = _write(tmp_path, _pool(bad={"EQR"}, short={"OMC"}))
    entries, universe = rb._load_current_watchlist(rb.SELECTOR_MODE_LAYA_FIXED30, str(path))
    syms = [e["symbol"] for e in entries]
    assert len(syms) == 58 and "EQR" not in syms and "OMC" not in syms and syms == list(universe.symbols)
    m = RunMasking(rb._names_for_masking(entries), run_start=START, seed=3, alias_scheme="opaque",
                   word_like_tickers=list(universe.word_like_tickers))
    acct = {"equity": 1e5, "cash": 1e5, "exposure_pct": 0.0, "daily_pl_pct": 0.0, "weekly_pl_pct": 0.0,
            "drawdown_pct": 0.0}
    block = rb.build_prompt("Day 1", acct, [], [], [], masking=m, experimental_universe=entries)
    m.assert_clean(block)
    assert "58-name universe" in block and f"| {m.alias('NUE')} |" in block
    assert len(set(m.mapping().values())) == 58, "distinct opaque aliases for every member"


def test_runner_refuses_a_tampered_pool_and_a_conflicting_resume(tmp_path):
    pool = _pool()
    path = _write(tmp_path, pool)
    pool["tickers"].pop()
    _rewrite(path, pool, rehash_content=False)
    with pytest.raises(SystemExit, match="--selector-universe refused"):
        rb._load_current_watchlist(rb.SELECTOR_MODE_LAYA_FIXED30, str(path))
    assert rb._resolve_selector_universe(False, None, "a.json") == "a.json"
    assert rb._resolve_selector_universe(True, {"selector_universe_path": "a.json"}, None) == "a.json"
    with pytest.raises(SystemExit, match="must keep its frozen pool"):
        rb._resolve_selector_universe(True, {"selector_universe_path": "a.json"}, "b.json")
    with pytest.raises(SystemExit, match="must keep its frozen pool"):
        rb._resolve_selector_universe(True, {}, "b.json")       # legacy fixed-30 run cannot switch
    assert rb._resolve_selector_universe(True, {}, None) is None  # legacy resume unchanged


def test_selector_off_rejects_a_pool_flag():
    args = rb.build_arg_parser().parse_args(["--selector-universe", "x.json"])
    with pytest.raises(SystemExit, match="--selector-universe were given"):
        rb._validate_selector_args(rb.SELECTOR_MODE_OFF, args)


def test_fresh_isolated_run_freezes_pool_membership_into_its_configuration(tmp_path):
    path = _write(tmp_path / "pools", _pool(bad={"EQR"}))
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    saved = (rb.STATE_PATH, rb.CHECKPOINT_PATH, rb.DIAGNOSTICS_DIR, rb.SELECTOR_SNAPSHOT_DIR)
    rb.apply_state_dir(str(tmp_path / "state"))
    seen = {}

    class Stop(Exception):
        pass

    def reg_cfg(run_id, watchlist, *a, **k):
        seen["watchlist"] = [w["symbol"] for w in watchlist]
        raise Stop
    try:
        args = rb.build_arg_parser().parse_args([
            "--state-dir", str(tmp_path / "state"), "--run-kind", "smoke",
            "--selector-mode", rb.SELECTOR_MODE_LAYA_FIXED30, "--laya-checkpoint", str(ckpt),
            "--selector-universe", str(path), "--start", "2026-09-28", "--end", "2026-10-02"])
        with patch("laya_experiment.verify_docker_worker_available"), \
             patch.object(rb, "register_run"), \
             patch.object(rb, "register_run_configuration", side_effect=reg_cfg), \
             patch.object(rb, "build_run_provenance", return_value={}):
            with pytest.raises(Stop):
                rb._run_backtest_main(args, {}, None)
    finally:
        rb.STATE_PATH, rb.CHECKPOINT_PATH, rb.DIAGNOSTICS_DIR, rb.SELECTOR_SNAPSHOT_DIR = saved
    assert seen["watchlist"] == [s for s in U60.symbols if s != "EQR"]
