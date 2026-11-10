"""Corrections from the halted July 2022 Laya run 6cc8687b (docs/evaluations/laya_schema5_2022_07_v2_run_2026-11-09.md):
1. the masker's missed short-name variant (recorded July 5 headlines), guard intact;
2. a halt after a simulated fill: the final report agrees with the journal (unreconciled, no return/drawdown), and
   episode end reports what the journal actually did -- never "rejected; journal row written" for a filled order;
3. identity-guard text never reaches the exported payload or the local spool.
Offline: no Docker, no model, no network. Run: python -m pytest -q test_halt_and_identity_corrections.py
"""
import json
from datetime import date
from unittest.mock import patch

import pytest

import backtest_tracing as bt
import eval_suite as es
import identity_cues as ic
import laya_vs_random as lvr
import run_backtest as rb
from test_backtest_tracing import Backend, _attrs, _io, _manifest, _spans_from, env_dir
from test_laya_wiring import UNIVERSE

V2 = lvr.HERE / "experiments" / "laya_schema5_2022_07_v2.json"
DAY = date(2022, 7, 5)
# The two decision-window headlines recorded for the halted run's 2022-07-05 cycle (provider text, verbatim).
RECORDED = [
    {"headline": "Analyst Ratings for Take-Two Interactive", "created_at": "2022-06-29T15:02:58Z",
     "summary": "Take-Two Interactive (NASDAQ:TTWO) has observed the following analyst ratings within the last "
                "quarter: ", "symbols": ["TTWO"]},
    {"headline": "UBS Maintains Neutral on Take-Two Interactive, Lowers Price Target to $143",
     "created_at": "2022-06-29T12:16:54Z", "summary": "", "symbols": ["TTWO"]},
]


def _pool_masking():
    """The frozen 48-name pool and the v2 masking (same aliases as the halted run)."""
    cfg = json.loads(V2.read_text(encoding="utf-8"))   # frozen at schema 5; only pool + masking are used
    universe, _ = lvr.load_pool(cfg)
    wl = rb._experimental_watchlist_entries(universe)
    return lvr.masking_for(universe, wl, cfg), wl


def _rendered(m):
    out = []
    for a in m.mask_news(RECORDED, DAY):
        out += [rb._decision_news_text(a), rb._prefill_news_text(a)]
    return out


# -- 1. masker ----------------------------------------------------------------------------------------------------

def test_the_recorded_july_5_headlines_are_fully_masked_and_pass_the_identity_guard():
    m, wl = _pool_masking()
    texts = _rendered(m)
    alias = m.alias("TTWO")
    for t in texts:
        ic.assert_prompt_clean(t, m, wl, where="decision prompt")       # the unchanged guard passes
        m.assert_clean(t)
        assert "Take-Two" not in t and "Interactive" not in t and "TTWO" not in t
    assert alias in texts[0]


def test_without_the_new_variant_the_guard_still_blocks_the_same_headlines(monkeypatch):
    monkeypatch.setitem(rb._BRAND_OVERRIDES, "TTWO", [])
    m, wl = _pool_masking()
    with pytest.raises(ic.IdentityCueDetected):
        for t in _rendered(m):
            ic.assert_prompt_clean(t, m, wl, where="decision prompt")


def test_the_alias_mapping_is_unchanged_by_the_new_variant():
    m, _ = _pool_masking()
    with patch.dict(rb._BRAND_OVERRIDES, {"TTWO": []}):
        old, _ = _pool_masking()
    assert m.mapping() == old.mapping(), "variants change what is masked, never which alias a name gets"


# -- 2. halt after a simulated fill -----------------------------------------------------------------------------------

class Journal:
    """The trade journal's resolution semantics: only a 'pending' row changes; otherwise its current state."""

    def __init__(self):
        self.rows = {}

    def queue(self, d):
        self.rows[d] = {"decision_id": d, "action": "BUY", "status": "pending"}

    def fill(self, d, fill=None):
        if self.rows[d]["status"] == "pending":
            self.rows[d]["status"] = "filled"

    def reject(self, d, reason):
        r = self.rows[d]
        if r["status"] == "pending":
            r.update(status="rejected", reason=reason)
            return {"status": "rejected", "applied": True}
        return {"status": r["status"], "applied": False}

    def statuses(self, run_id):
        return [dict(r) for r in self.rows.values()]


CUE = "Secretword"
BLOCKED = f"identity cue(s) ['ZQ11~{CUE}'] reached the decision prompt; refusing to send it"


def _halting_run_day(journal):
    """Day 0 queues a BUY; the next session fills it, journals the fill, then the identity guard halts the cycle
    (so that session's state is discarded, exactly as in run 6cc8687b)."""
    def run_day(day, day_index, portfolio, masking, watchlist, *a, **kw):
        sym = watchlist[0]["symbol"]
        if not portfolio.pending_orders and not journal.rows:
            portfolio.pending_orders.append(rb.QueuedOrder(
                ticker=sym, alias=masking.alias(sym), action="BUY", quantity=650, stop_price=27.75,
                target_price=35.18, entry_price=30.72, queued_day_index=day_index, decision={}, decision_id="dec-1"))
            journal.queue("dec-1")
            return portfolio, {"day": str(day), "decisions": [], "blocked": [], "exits": [], "fills": [],
                               "proposals": [], "usage": {}}, (lambda: None)
        rb.resolve_trade_fill("dec-1", {"fill_price": 30.07, "fill_quantity": 650})
        raise ic.IdentityCueDetected(BLOCKED)
    return run_day


def _laya_arm(tmp_path, monkeypatch, mode):
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = tmp_path / "plan.md"
    plan.write_text("frozen plan")
    cfg = {**json.loads(V2.read_text(encoding="utf-8")), "experiment_id": "halt-test",
           "plan": {"path": "plan.md", "sha256": lvr._sha_file(plan)},
           "window": {"start": "2022-07-01", "end": "2022-07-08"},
           "prompt_schema_version": rb.PROMPT_SCHEMA_VERSION}
    cfg.pop("authorized_arms")
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(lvr, "REPO", tmp_path)
    journal, backend = Journal(), Backend("raise" if mode == "raising" else 200)
    if mode != "off":
        env_dir(tmp_path)
        monkeypatch.setattr(bt, "DEFAULT_DIR", tmp_path / "bt")
        real_load = bt.__dict__.get("_unpatched_load", bt.load)
        monkeypatch.setattr(bt, "_unpatched_load", real_load, raising=False)
        monkeypatch.setattr(bt, "load", lambda *a, **kw: real_load(*a, **{**kw, "post": backend}))

    class Sel:
        def __init__(self, **kw):
            pass

        def close(self):
            pass
    model = cfg["model"]
    with patch.object(lvr, "load_pool", return_value=(UNIVERSE, {})), \
         patch.object(lvr, "code_fingerprint", return_value={"head": "h"}), \
         patch.object(lvr, "check_arm_allowed", return_value=None), \
         patch("run_backtest.preflight", return_value={"runtime": None}), \
         patch("run_backtest.build_run_provenance",
               return_value={f"hermes_runtime_{k}": v for k, v in model.items()}), \
         patch("run_backtest.register_run"), patch("run_backtest.register_run_configuration"), \
         patch("run_backtest.apply_state_dir"), patch("run_backtest._clear_checkpoint"), \
         patch("run_backtest._cleanup_ephemeral_hermes_runtime"), \
         patch("run_backtest.run_in", return_value="{}"), \
         patch("laya_experiment.RandomSelectorContext", Sel), \
         patch("run_backtest.run_day", side_effect=_halting_run_day(journal)), \
         patch("run_backtest.resolve_trade_fill", side_effect=journal.fill), \
         patch("eval_suite.resolve_trade_rejection", side_effect=journal.reject), \
         patch("eval_suite.journal_trade_statuses", side_effect=journal.statuses):
        rc = lvr.run_arm("random", None, cfg_path, root=tmp_path / "out")
    result = json.loads((tmp_path / "out" / "halt-test" / "random" / "result.json").read_text())
    return rc, result, journal, backend


def test_a_halt_after_a_fill_is_reported_unreconciled_and_episode_end_reports_the_journals_answer(tmp_path,
                                                                                                    monkeypatch):
    rc, result, journal, _ = _laya_arm(tmp_path / "off", monkeypatch, "off")
    assert rc == 1 and result["status"] == "halted" and result["sessions_completed"] == 1
    assert journal.rows["dec-1"]["status"] == "filled", "the journal keeps the fill; nothing overwrote it"
    rec = result["portfolio_reconciliation"]
    assert rec["status"] == "unreconciled"
    assert {"decision_id": "dec-1", "journal_status": "filled", "portfolio": "pending order"} in rec["conflicts"]
    for k in ("equity_curve", "return_pct", "max_drawdown_pct", "portfolio"):
        assert result[k] is None, k
    assert result["last_committed_portfolio"]["pending_orders"], "kept, and labelled as the last committed state"
    [order] = result["terminal_pending_resolved"]
    assert order["journal_outcome"] == "not rejected: journal row already filled"
    assert "rejected; journal row written" not in json.dumps(result)


def test_the_halt_trace_shows_the_journals_answer_and_exports_no_identity_text(tmp_path, monkeypatch):
    off = _laya_arm(tmp_path / "off", monkeypatch, "off")
    on = _laya_arm(tmp_path / "on", monkeypatch, "on")
    boom = _laya_arm(tmp_path / "boom", monkeypatch, "raising")

    def same(r):
        return r[0], {k: v for k, v in r[1].items() if k not in ("run_id", "finished_at")}, r[2].rows
    assert same(off) == same(on) == same(boom), "tracing on or failing changes nothing"

    spans = _spans_from(on[3], tmp_path / "on" / "bt" / "trace_spool")
    end = next(x for x in spans if x["name"] == "backtest_episode_end")
    assert [o["outcome"] for o in _io(end)["orders"]] == ["not rejected: journal row already filled"]
    halted_day = [x for x in spans if x["name"] == "backtest_day" and x.get("status", {}).get("code") == 2]
    assert halted_day, "the halted session is recorded as an error"
    assert _attrs(halted_day[0])["langfuse.observation.status_message"]["stringValue"].strip('"') \
        .startswith("IdentityCueDetected: reason=identity_guard_blocked")
    sent = b"".join(on[3].bodies).decode("utf-8")
    spool = b"".join(p.read_bytes() for d in ("on", "boom")
                     for p in (tmp_path / d / "bt" / "trace_spool").glob("*.jsonl")).decode("utf-8")
    assert spool, "the failing tracer was live and spooled"
    for blob in (sent, spool):
        assert CUE not in blob and "ZQ11~" not in blob and "refusing to send" not in blob
    assert "journal.fill" in {x["name"] for x in spans}


def test_the_evaluation_runner_reports_a_halt_after_a_fill_the_same_way(tmp_path):
    journal = Journal()
    manifest = _manifest()
    manifest["episodes"][0]["end"] = "2023-01-03"
    manifest["content_hash"] = es.compute_content_hash(manifest)
    with patch("eval_suite.run_day", side_effect=_halting_run_day(journal)), \
         patch("eval_suite.register_run"), patch("eval_suite.log_cycle_failure"), \
         patch("run_backtest.resolve_trade_fill", side_effect=journal.fill), \
         patch("eval_suite.resolve_trade_rejection", side_effect=journal.reject), \
         patch("eval_suite.journal_trade_statuses", side_effect=journal.statuses):
        report = es.run_configuration(manifest, es.EpisodeConfig("baseline"), es.EVAL_RUN_KIND,
                                      [{"symbol": "OLN", "name": "Olin Corporation", "sector": "Chemicals"}],
                                      tmp_path / "ckpt")
    ep = report["per_episode"][0]
    assert ep["halted"] and ep["portfolio_reconciliation"]["status"] == "unreconciled"
    assert ep["return_pct"] is None and ep["max_drawdown_pct"] is None
    assert journal.rows["dec-1"]["status"] == "filled"
    agg = report["aggregate"]
    assert agg["completed_count"] == 0 and agg["halted_count"] == 1 and agg["performance"] is None


def test_reconciliation_and_outcome_labels():
    p = rb.Portfolio()
    assert es.halted_reconciliation("r", p, fetch_rows=lambda r: [])["status"] == "reconciled"
    assert es.halted_reconciliation("r", p, fetch_rows=lambda r: 1 / 0)["status"] == "unreconciled"
    stray = es.halted_reconciliation("r", p, fetch_rows=lambda r: [{"decision_id": "x", "action": "BUY",
                                                                    "status": "pending"}])
    assert stray["status"] == "unreconciled" and stray["conflicts"][0]["portfolio"] == "absent"
    exits = es.halted_reconciliation("r", p, fetch_rows=lambda r: [{"decision_id": "e", "action": "SELL",
                                                                   "status": "filled"}])
    assert exits["status"] == "unreconciled"
    assert es.journal_outcome_label({"status": "rejected", "applied": True}) == "rejected; journal row written"
    assert es.journal_outcome_label({"status": "rejected", "applied": False}) == "already rejected; no change"
    assert es.journal_outcome_label({"status": "filled", "applied": False}) == \
        "not rejected: journal row already filled"
    assert es.journal_outcome_label(None) == "rejection requested; journal status not reported"


def test_resolve_trade_rejection_returns_what_the_journal_replied():
    with patch("run_backtest.run_in", return_value='{"status": "filled", "rejection_reason": null}'):
        assert rb.resolve_trade_rejection("d", "why") == {"status": "filled", "applied": False}
    with patch("run_backtest.run_in", return_value='{"status": "rejected", "rejection_reason": "why"}'):
        assert rb.resolve_trade_rejection("d", "why") == {"status": "rejected", "applied": True}
    with patch("run_backtest.run_in", return_value=""):
        assert rb.resolve_trade_rejection("d", "why") is None


def test_the_scrubber_withholds_any_identity_guard_message():
    m, _ = _pool_masking()
    s = bt.Scrubber(m, run_id="11111111-2222-4333-8444-555555555555", key=b"k")
    for msg in (f"IdentityCueDetected: {BLOCKED}", "LeakDetected: 'Olin' survived masking",
                f"RuntimeError: wrapped IdentityCueDetected: {BLOCKED}"):
        assert s.text(msg) == bt.IDENTITY_BLOCKED
    assert s.text("RuntimeError: ordinary failure") == "RuntimeError: ordinary failure"
