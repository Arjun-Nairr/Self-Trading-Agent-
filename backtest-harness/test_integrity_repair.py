"""Integrity repair block (docs/evaluations/integrity_repair_2026-10-07.md):
identity masking + an independent known-cue check, and observed-only
technical levels with a checkable stop basis. Offline: no provider, Hermes,
Laya, Neon or Docker call can happen (run_day runs through
test_laya_wiring's mocked I/O boundary; _call_hermes's subprocess is
patched to fail if reached)."""
import ast
import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import identity_cues as ic
import run_backtest as rb
from anonymize import RunMasking
from laya_preflight import load_frozen_pool
from test_laya_wiring import DAY, WATCHLIST, _cycle, _masking

HERE = Path(__file__).resolve().parent
POOL = rb.REPO / "watchlist/experimental_pools/candidates60-v1-pool-2022-07-01-aafefb19a6c5.json"
DETECTED = (ic.IdentityCueDetected,)


def _legal_only(wl):
    """The pre-repair dictionary shape: legal-name variants only, no overrides."""
    out = {}
    for t in wl:
        v, cur = {t["name"]}, t["name"]
        while True:
            nxt = rb._CORP_SUFFIXES.sub("", cur).strip()
            if nxt == cur or not nxt:
                break
            v.add(nxt)
            cur = nxt
        out[t["symbol"]] = sorted(v)
    return out


def _pool_masking(names_fn=rb._names_for_masking):
    universe, _ = load_frozen_pool(POOL)
    wl = rb._experimental_watchlist_entries(universe)
    return RunMasking(names_fn(wl), run_start=date(2022, 7, 1), seed=42, alias_scheme="opaque",
                      word_like_tickers=universe.word_like_tickers), wl


def _fixed10_masking():
    wl = json.loads((rb.REPO / "watchlist/watchlist.json").read_text())["tickers"]
    names = {t["symbol"]: rb._name_variants(t["name"], t["symbol"]) for t in wl}
    return RunMasking(names, run_start=date(2022, 7, 1), seed=42), wl


# Synthetic headlines in the shapes the review verified. Company variants map
# to the alias; other parties (investor, partner, affiliate, subsidiary,
# product) are neutrally redacted.
FIXTURES = [
    ("ATI", "former name", "Keybanc Maintains Overweight on Allegheny Technologies, Lowers Price Target"),
    ("DINO", "former name", "HollyFrontier Completes Refinery Purchase"),
    ("DINO", "affiliate", "Holly Energy Partners Announce Management Changes"),
    ("WMG", "label", "Warner Records UK Teams Up With Audio Maker On Collectibles"),
    ("WMG", "subsidiary", "Label Signs Deal With Warner Chappell Music"),
    ("AIG", "abbreviation", "Barclays Maintains Equal-Weight on American Intl Gr, Lowers Price Target"),
    ("SCHW", "partial-name residue", "Expert Ratings for Charles Schwab"),
    ("HBAN", "partial-name residue", "Analyst Ratings for Huntington Bancshares"),
    ("MPC", "facility", "Marathon Los Angeles Refinery Reports A Petroleum Coke Release"),
    ("NYT", "product", "Toy Maker Brings 'Wordle' To Life Via New Board Game"),
    ("EMR", "subsidiary", "Majority Owned Subsidiary, AspenTech, Announces Software Acquisition"),
    ("VICI", "partner", "Expands Great Wolf Resorts Partnership With New Investment"),
    ("OXY", "investor", "Warren Buffett's Berkshire Boosts Stake In This Energy Company"),
    ("OXY", "investor", "Berkshire Hathaway's Charlie Munger Isn't Worried About Inflation"),
]
FIXED10_FIXTURES = [("PLNT", "product", "PF Black Card Members Get A New Perk")]
NEUTRAL = {"affiliate", "product", "partner", "investor"}


# -- masking ------------------------------------------------------------------

def test_every_verified_cue_is_masked_or_neutrally_redacted_in_both_populations():
    m, wl = _pool_masking()
    for sym, cat, text in FIXTURES:
        out = m.mask_text(text)
        assert ic.find_known_cues(out) == [] and ic.find_alias_residues(out, m, wl) == [], (sym, cat, out)
        m.assert_clean({"h": out})
    f10, wl10 = _fixed10_masking()
    for sym, cat, text in FIXED10_FIXTURES + [f for f in FIXTURES if f[0] in ("ATI", "DINO", "WMG")]:
        out = f10.mask_text(text)
        assert ic.find_known_cues(out) == [] and ic.find_alias_residues(out, f10, wl10) == [], (sym, out)


def test_company_variants_map_to_the_alias_and_other_parties_never_do():
    m, _ = _pool_masking()
    for sym, cat, text in FIXTURES:
        out = m.mask_text(text)
        if cat in NEUTRAL or sym == "EMR":
            assert "[REDACTED" in out and not any(a in out for a in m.mapping().values()), (sym, out)
        else:
            assert m.alias(sym) in out, (sym, out)
    assert "Petroleum Coke Release" in m.mask_text(FIXTURES[8][2]), "adverse-news meaning survives"
    assert "Boosts Stake" in m.mask_text(FIXTURES[12][2])


def test_case_punctuation_and_overlapping_variants():
    m, _ = _pool_masking()
    for text in ("AMERICAN INTL GR. downgrade", "charles schwab's outlook", "Allegheny Technologies' guidance",
                 "American Intl Group results", "HOLLYFRONTIER-led deal", "warner chappell music signs"):
        assert ic.find_known_cues(m.mask_text(text)) == [], text


def test_ordinary_prose_and_unrelated_companies_are_not_corrupted():
    m, _ = _pool_masking()
    for text in ("Marathon Oil rises as runners finish a marathon", "Warner Bros. Discovery shares slip",
                 "the charles river flooded", "a grey wolf was seen", "bancshares index flat",
                 "Market Cap Hits A Record As Shares Rise"):
        assert m.mask_text(text) == text, text


def test_independent_detector_catches_a_cue_the_masker_dictionary_lacks():
    legacy, wl = _pool_masking(_legal_only)          # the pre-repair dictionary: no override variants
    text = legacy.mask_text(FIXTURES[0][2])
    legacy.assert_clean({"h": text})                 # the masker's own check cannot see it ...
    assert ic.find_known_cues(text) == ["Allegheny Technologies"]    # ... the independent list does
    with pytest.raises(DETECTED):
        ic.assert_prompt_clean(text, legacy, wl)


def test_detector_reference_set_is_maintained_independently_of_the_masker():
    tree = ast.parse((HERE / "identity_cues.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"run_backtest", "anonymize"}, "the reference set must not come from the masker"
    src = (HERE / "identity_cues.py").read_text(encoding="utf-8")
    assert "_BRAND_OVERRIDES" not in src and "NEUTRAL_REDACTIONS" not in src and "_names" not in src


def test_alias_adjacent_legal_name_residue_is_detected():
    m, wl = _pool_masking()
    residue = f"Expert Ratings for {m.alias('HBAN')} Bancshares"
    assert ic.find_alias_residues(residue, m, wl)
    with pytest.raises(DETECTED):
        ic.assert_prompt_clean(residue, m, wl)
    assert ic.find_alias_residues(f"{m.alias('HBAN')} shares rose; {m.alias('HBAN')} Market Cap", m, wl) == []


def test_call_hermes_refuses_a_cue_before_any_model_invocation():
    with patch("run_backtest.subprocess.run", side_effect=AssertionError("the model must not be called")):
        with pytest.raises(DETECTED):
            rb._call_hermes("CANDIDATES\n  news: Keybanc on Allegheny Technologies")


class _Everyone(dict):
    """A stub provider result: the same value for every ticker asked."""
    def __init__(self, value):
        super().__init__()
        self.value = value

    def get(self, key, default=None):
        return self.value

    def __bool__(self):
        return True


def test_final_decision_prompt_is_checked_before_hermes(tmp_path):
    names = _legal_only(WATCHLIST)
    legacy = RunMasking(names, run_start=date(2026, 2, 17), seed=42, alias_scheme="opaque",
                        word_like_tickers=_masking().word_like_tickers)
    news = _Everyone([{"headline": "Keybanc Maintains Overweight on Allegheny Technologies", "summary": "",
                       "created_at": f"{DAY}T12:00:00Z", "symbols": []}])
    cap = {}
    with pytest.raises(DETECTED):
        _cycle(tmp_path, None, cap=cap, news=news, masking=legacy)
    assert cap["prompts"] == [], "Hermes must never receive the leaking prompt"
    # the repaired masker sends the same raw news cleanly
    ok = _cycle(tmp_path / "ok", None, news=news)
    assert ok["prompts"] and ic.find_known_cues(ok["prompts"][0]) == []


def _buy_order(alias):
    return rb.QueuedOrder(ticker="OLN", alias=alias, action="BUY", quantity=10, stop_price=90.0,
                          target_price=120.0, entry_price=100.0, queued_day_index=0, decision={},
                          decision_id="d-1", decision_cutoff_at="2022-07-01T16:00:00-04:00")


def test_pre_fill_gate_prompt_with_an_unmasked_cue_is_refused_not_confirmed():
    legacy, wl = _pool_masking(_legal_only)
    art = {"headline": "Keybanc on Allegheny Technologies", "summary": "", "symbols": [],
           "created_at": "2022-07-05T12:00:00Z"}
    calls = []
    with pytest.raises(DETECTED):
        rb.apply_pre_fill_news_gate(
            [_buy_order(legacy.alias("OLN"))], date(2022, 7, 5), legacy, {},
            fetch_news_fn=lambda ts, a, b: {t: {"articles": [art], "complete": True} for t in ts},
            call_hermes_fn=lambda p: calls.append(p) or (b"", {}), watchlist=wl)
    assert calls == [], "never sent, never confirmed, never read as 'no adverse news'"
    # repaired masker: the gate proceeds to Hermes with clean text
    fixed, wl2 = _pool_masking()
    sent = []
    rb.apply_pre_fill_news_gate(
        [_buy_order(fixed.alias("OLN"))], date(2022, 7, 5), fixed, {},
        fetch_news_fn=lambda ts, a, b: {t: {"articles": [art], "complete": True} for t in ts},
        call_hermes_fn=lambda p: sent.append(p) or (json.dumps({"verdicts": []}).encode(), {}), watchlist=wl2)
    assert len(sent) == 1 and ic.find_known_cues(sent[0]) == []


# -- observed technical levels -------------------------------------------------

def _bars(n=40, end=DAY):
    return [{"date": (end - timedelta(days=n - 1 - i)).isoformat(), "high": 101.0 + (i % 3),
             "low": 95.0 - (i % 2), "close": 100.0} for i in range(n)]


def test_observed_levels_come_from_the_eligible_window_with_provenance():
    bars = _bars()
    bars[-5]["low"] = 88.0
    lv = rb.observed_levels_from_bars(bars, DAY)
    ev = lv["level_evidence"]
    assert lv["support"] == [88.0] and lv["resistance"] == [103.0]
    assert ev["levels"] == {"S1": {"price": 88.0, "kind": "observed_low"},
                            "R1": {"price": 103.0, "kind": "observed_high"}}
    assert ev["lookback_sessions"] == 30 and "not pivot analysis" in ev["method"]
    assert not re.search(r"\b(19|20)\d{2}\b", json.dumps(lv)), "no calendar metadata model-facing"


def test_bars_outside_the_window_or_after_the_as_of_date_never_become_levels():
    old = _bars()
    old[0]["low"] = 50.0                              # 40 sessions back: outside the 30-session window
    assert rb.observed_levels_from_bars(old, DAY)["support"] == [94.0]
    future = {"date": (DAY + timedelta(days=1)).isoformat(), "high": 500.0, "low": 1.0, "close": 3.0}
    lv = rb.observed_levels_from_bars(_bars() + [future], DAY)
    assert lv["support"] == [94.0] and lv["resistance"] == [103.0]


def test_an_observed_extreme_equal_to_a_percentage_level_stays_observed():
    bars = _bars()
    for b in bars:
        b["low"] = 95.0                               # == 100 x 0.95, but it IS the observed low
    lv = rb.observed_levels_from_bars(bars, DAY)
    assert lv["support"] == [95.0] and lv["level_evidence"]["levels"]["S1"]["kind"] == "observed_low"


@pytest.mark.parametrize("bars, reason", [
    ([], "no bars"),
    ({"error": "provider down"}, "no bars"),
    (_bars(n=20), "fewer than 30"),
    (_bars(end=DAY - timedelta(days=1)), "no bar on the decision date"),
    (_bars()[:-1] + [{"date": DAY.isoformat(), "high": float("nan"), "low": 90.0, "close": 95.0}], "malformed"),
    (_bars()[:-1] + [{"date": DAY.isoformat(), "high": "x", "low": 90.0, "close": 95.0}], "malformed"),
    (_bars()[:-1] + [{"date": DAY.isoformat(), "high": 80.0, "low": 90.0, "close": 95.0}], "malformed"),
    (_bars()[:-1] + [{"date": DAY.isoformat(), "low": 90.0, "close": 95.0}], "malformed"),
])
def test_missing_short_stale_or_malformed_history_yields_no_levels(bars, reason):
    lv = rb.observed_levels_from_bars(bars, DAY)
    assert lv["support"] == [] and lv["resistance"] == []
    assert reason in lv["level_evidence"]["unavailable"]


def test_technicals_adapter_drops_synthetic_levels_and_raw_bars(monkeypatch):
    bars = _bars()
    bars[-2]["low"] = 88.0
    raw = {"OLN": {"price": 100.0, "rsi": 50.0, "rsi_signal": "neutral", "macd_signal": "bullish",
                   "trend": "up", "outlook": "neutral", "bars": bars,
                   "levels": {"support": [90.0, 95.0, 88.0], "resistance": [103.0, 105.0, 110.0]}},
           "DINO": {"error": "no data"}}
    seen = []
    monkeypatch.setattr(rb, "run_in", lambda svc, script: seen.append((svc, script)) or json.dumps({"tech": raw}))
    out = rb.fetch_technicals(["OLN", "DINO"], DAY)
    t = out["OLN"]
    assert t["support"] == [88.0] and t["resistance"] == [103.0]
    assert set(t) == {"price", "rsi", "rsi_signal", "macd_signal", "trend", "outlook", "support", "resistance",
                      "level_evidence"}
    assert out["DINO"] == {"error": "no data"}
    assert seen[0][0] == "maverick-mcp" and 'd["levels"]' not in seen[0][1] and "get_price_history" in seen[0][1]


def test_prompt_labels_observed_levels_and_shows_no_synthetic_values(tmp_path):
    tech = {"price": 100.0, "rsi": 50.0, "rsi_signal": "neutral", "macd_signal": "bullish", "trend": "up",
            "outlook": "neutral", **rb.levels_payload(91.0, 104.0)}
    prompt = _cycle(tmp_path, None, technicals=_Everyone(tech))["prompts"][0]
    assert "S1 $91.00 (observed low)" in prompt and "R1 $104.00 (observed high)" in prompt
    assert "not pivot analysis" in prompt and "STOP EVIDENCE" in prompt
    assert "95.0" not in prompt and "105.0" not in prompt and "support [" not in prompt
    unavailable = {**tech, **rb._no_levels("no bars")}
    prompt2 = _cycle(tmp_path / "u", None, technicals=_Everyone(unavailable))["prompts"][0]
    assert "observed levels unavailable (no bars)" in prompt2


# -- stop evidence ------------------------------------------------------------

def _tech(s1, r1):
    return {"price": 100.0, "rsi": 50.0, "rsi_signal": "neutral", "macd_signal": "bullish", "trend": "up",
            "outlook": "neutral", **rb.levels_payload(s1, r1)}


def _buy(alias, stop, target, qty):
    return {"alias": alias, "action": "BUY", "quantity": qty, "entry_price": 100.0, "stop_price": stop,
            "target_price": target, "confidence": 3, "primary_signal": "technical",
            "technical_basis": "support_resistance",
            "expected_reward_risk": round((target - 100.0) / (100.0 - stop), 2),
            "invalidation_condition": "closes below the observed low", "reasoning": "test"}


def _run(tmp_path, stop, target, qty, s1, r1):
    alias = _masking().alias("OLN")
    return _cycle(tmp_path, None, technicals=_Everyone(_tech(s1, r1)),
                  decisions=[_buy(alias, stop, target, qty)]), alias


def _required(cap):
    m = re.search(r"required whole-share size (\d+)", " ".join(cap["summary"]["blocked"]))
    return int(m.group(1)) if m else None


def test_unsupported_percentage_stop_is_rejected_before_queueing(tmp_path):
    cap, alias = _run(tmp_path, 95.0, 110.0, 10, s1=92.0, r1=112.0)     # 95 = 100 x 0.95, not supplied
    assert cap["queued"] == []
    assert any("not a supplied observed support level" in b and "§6a" in b for b in cap["summary"]["blocked"])
    status = {s["alias"]: s for s in cap["evidence"][0]["candidate_status"]}
    assert status[alias]["stop_evidence"]["stop_basis"] == "unsupported"


def test_evidenced_stop_is_accepted_when_other_rules_pass_and_its_basis_is_recorded(tmp_path):
    probe, _ = _run(tmp_path / "probe", 92.0, 112.0, 1, s1=92.0, r1=112.0)
    assert "observed support level" not in " ".join(probe["summary"]["blocked"])
    cap, alias = _run(tmp_path, 92.0, 112.0, _required(probe), s1=92.0, r1=112.0)
    assert len(cap["queued"]) == 1 and cap["summary"]["blocked"] == []
    status = {s["alias"]: s for s in cap["evidence"][0]["candidate_status"]}
    assert status[alias]["stop_evidence"] == {"stop_ref": "S1", "stop_basis": "observed_low",
                                              "target_ref": "R1", "target_basis": "observed_high"}


def test_derived_target_is_labelled_honestly(tmp_path):
    cap, alias = _run(tmp_path, 92.0, 116.0, 1, s1=92.0, r1=112.0)
    status = {s["alias"]: s for s in cap["evidence"][0]["candidate_status"]}
    assert status[alias]["stop_evidence"]["target_basis"] == "derived_not_a_supplied_level"
    assert status[alias]["stop_evidence"]["target_ref"] is None


def test_no_in_band_level_means_no_entry_and_no_fabricated_stop(tmp_path):
    # the only observed support is 1% below price, outside the 3-12% band
    cap, _ = _run(tmp_path, 99.0, 103.0, 10, s1=99.0, r1=103.0)
    assert cap["queued"] == [] and any("§6a" in b for b in cap["summary"]["blocked"])
    cap2, _ = _run(tmp_path / "b", 95.0, 110.0, 10, s1=99.0, r1=103.0)
    assert cap2["queued"] == [] and any("observed support" in b for b in cap2["summary"]["blocked"])


@pytest.mark.parametrize("evidence", [
    None, {"unavailable": "no bars"}, {"levels": {"S1": {"price": float("nan"), "kind": "observed_low"}}},
    {"levels": {"S1": {"price": 92.0, "kind": "synthetic"}}}, {"levels": "bad"},
    {"levels": {"S1": {"price": -1.0, "kind": "observed_low"}}}, {"levels": {"S1": {"kind": "observed_low"}}},
])
def test_missing_or_malformed_level_evidence_rejects_the_buy(evidence):
    cand = {"alias": "KV11", "tech": {"price": 100.0, "support": [92.0], "resistance": [112.0],
                                      **({"level_evidence": evidence} if evidence is not None else {})}}
    ok, rec = rb.stop_evidence({"action": "BUY", "stop_price": 92.0, "target_price": 112.0}, cand)
    assert not ok and rec["stop_basis"] == "unsupported"


def test_sells_held_positions_and_exits_are_not_subject_to_the_stop_evidence_check(tmp_path):
    assert rb.stop_evidence({"action": "SELL"}, {"alias": "KV11", "tech": {}}) == (True, None)
    alias = _masking().alias("OLN")
    held = rb.Portfolio(cash=80_000.0, equity_curve=[100_000.0] * 6,
                        holdings={"OLN": rb.Holding("OLN", 100, 100.0, 90.0, 120.0, 28, 2)})
    sell = {"alias": alias, "action": "SELL", "quantity": 100, "entry_price": 100.0, "stop_price": None,
            "target_price": None, "confidence": 3, "primary_signal": "technical", "technical_basis": "trend",
            "expected_reward_risk": 0.0, "invalidation_condition": "n/a", "reasoning": "exit"}
    cap = _cycle(tmp_path, None, portfolio=held, decisions=[sell],
                 technicals=_Everyone({**_tech(92.0, 112.0), **rb._no_levels("no bars")}))
    assert len(cap["queued"]) == 1 and not any("evidence" in b for b in cap["summary"]["blocked"])
