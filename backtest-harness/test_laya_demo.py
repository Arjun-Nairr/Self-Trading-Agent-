"""Standalone Laya demo (laya_demo.py): cutoff handling, missing data, artifact
replay, identity exclusion, and failure reporting. Synthetic bars and a
deterministic fake scorer only -- no Docker, no model, no network.
Run: python -m pytest -q test_laya_demo.py
"""
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

import pytest

import laya_adapter
import laya_demo as ld
from laya_selector import NUMERIC_FIELDS, ScoreRecord, ScorerError, load_universe

UNIVERSE = load_universe()
SYMS = list(UNIVERSE.symbols)
SAT = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)        # Saturday
FRI = date(2026, 10, 2)


def bars(end: date, n: int = 80, seed: int = 1, skip: set = frozenset()) -> list[dict]:
    out, d, i = [], end, 0
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            c = 50 + 10 * math.sin((i + seed) / 5) + seed
            out.append({"date": d.isoformat(), "open": c, "high": c * 1.02, "low": c * 0.98,
                        "close": c, "volume": 1e6 * (1 + 0.3 * math.cos(i + seed))})
            i += 1
        d -= timedelta(days=1)
    return out[::-1]


def history(end=FRI, **override):
    h = {s: bars(end, seed=k + 1) for k, s in enumerate(SYMS)}
    h.update(override)
    return h


class FakeScorer:
    metadata = laya_adapter.expected_metadata()

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def score(self, cards):
        self.calls.append([c.row_id for c in cards])
        if self.fail:
            raise ScorerError("worker exited unexpectedly")
        return [ScoreRecord(c.row_id, round(0.3 + 0.01 * (i % 17), 4), 0.8) for i, c in enumerate(cards)]


def run(tmp_path, hist, scorer=None, now=SAT):
    scorer = scorer or FakeScorer()
    path = ld.run_demo(now_utc=now, universe=UNIVERSE, fetch_history=lambda syms, d: hist,
                       scorer=scorer, scorer_runtime={"image": "fake"}, out_dir=tmp_path,
                       git={"head": "x", "dirty": False})
    return path, json.loads(path.read_text()), scorer


# -- cutoff ----------------------------------------------------------------

@pytest.mark.parametrize("now,allowed", [
    (datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc), date(2026, 10, 2)),   # Sat 10:00 ET
    (datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc), date(2026, 10, 1)),   # Fri 15:00 ET: session open
    (datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc), date(2026, 10, 2)),  # Fri 17:30 ET: done
    (datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc), date(2026, 10, 4)),    # Sun 23:00 ET
])
def test_latest_allowed_session_uses_eastern_time_and_a_close_buffer(now, allowed):
    assert ld.latest_allowed_session(now) == allowed


def test_cutoff_is_latest_completed_session_and_later_bars_are_dropped(tmp_path):
    # A Friday-afternoon run: Friday's partial bar exists but must NOT be used.
    now = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
    path, art, scorer = run(tmp_path, history(end=FRI), now=now)
    assert art["cutoff"]["session"] == "2026-10-01"
    assert all(c["latest_bar_date"] <= "2026-10-01" for c in art["cards"])
    assert art["selection"]["as_of"] == "2026-10-01" and art["unavailable"] == {}


def test_holiday_moves_the_cutoff_back_and_is_noted():
    hol = date(2026, 10, 2)
    cutoff, info = ld.pick_cutoff({s: bars(FRI, skip={hol}) for s in SYMS}, SAT)
    assert cutoff == date(2026, 10, 1) and "holiday or provider lag" in info["note"]


def test_one_name_missing_the_cutoff_bar_is_stale_not_scored(tmp_path):
    hist = history(**{"OLN": bars(date(2026, 10, 1))})
    _, art, scorer = run(tmp_path, hist)
    m = _mapping(tmp_path)
    stale = [rid for rid, why in art["unavailable"].items() if why == "stale_no_cutoff_bar"]
    assert [m[r] for r in stale] == ["OLN"]
    assert stale[0] not in scorer.calls[0], "a stale card is never sent to the model"


# -- missing data ----------------------------------------------------------

def _mapping(tmp_path):
    (p,) = tmp_path.glob("*/private_mapping.json")
    return json.loads(p.read_text())["tickers"]


def test_missing_data_reasons_and_shortfall(tmp_path):
    hist = history()
    for s in SYMS[:22]:
        hist[s] = {"error": f"no data for {s}"}
    hist[SYMS[22]] = bars(FRI, n=10)          # too short for the 40-bar warm-up
    del hist[SYMS[23]]                        # provider omitted it entirely
    _, art, scorer = run(tmp_path, hist)
    m = _mapping(tmp_path)
    why = {m[r]: w for r, w in art["unavailable"].items()}
    assert {why[s] for s in SYMS[:22]} == {"history_fetch_error"}
    assert why[SYMS[22]] == "insufficient_or_degenerate_history" and why[SYMS[23]] == "history_missing"
    selected = [r for r in art["selection"]["rows"] if r["status"] == "selected"]
    assert len(selected) == 6 == len(scorer.calls[0]), "fewer than 10 -> all scorable selected, never padded"
    assert ld.replay(next(tmp_path.glob("*/artifact.json")))["content_sha256"] == art["content_sha256"]


# -- artifact contents, identity, immutability -----------------------------

def test_artifact_ranks_by_probability_and_records_but_ignores_confidence(tmp_path):
    _, art, scorer = run(tmp_path, history())
    assert len(scorer.calls) == 1 and len(scorer.calls[0]) == 30, "one batch, every complete card"
    rows = [r for r in art["selection"]["rows"] if r["rank"] is not None]
    ps = [r["review_probability"] for r in rows]
    assert ps == sorted(ps, reverse=True)
    assert [r["status"] for r in rows] == ["selected"] * 10 + ["not_selected"] * 20
    assert all(r["answer_confidence"] == 0.8 for r in rows)
    assert art["scorer"]["question"] == laya_adapter.REVIEW_QUESTION
    assert json.loads(art["scorer"]["settings"])["question_format"] == "noul-criteria-v2"
    assert art["universe"]["hash"] == UNIVERSE.hash and art["universe"]["count"] == 30


def test_no_identity_in_artifact_or_model_input(tmp_path):
    path, art, _ = run(tmp_path, history())
    text = path.read_text()
    for m in UNIVERSE.members:
        assert not re.search(rf"\b{re.escape(m.symbol)}\b", text), m.symbol
        assert m.name not in text
    for state in art["model_inputs"].values():
        assert set(state) == set(NUMERIC_FIELDS) and len(state) == 7
        assert all(isinstance(v, float) for v in state.values())
    mapping = _mapping(tmp_path)
    assert sorted(mapping.values()) == sorted(SYMS)
    # the row IDs are salted per run: not derivable from the public universe + seed
    _, art2, _ = run(tmp_path / "again", history())
    assert {c["row_id"] for c in art["cards"]}.isdisjoint({c["row_id"] for c in art2["cards"]})


def test_artifacts_are_write_once(tmp_path):
    run(tmp_path, history())
    with pytest.raises(ld.DemoFailure, match="write-once"):
        run(tmp_path, history())


# -- replay ----------------------------------------------------------------

def test_replay_reproduces_without_fetching_or_scoring(tmp_path, monkeypatch, capsys):
    path, art, _ = run(tmp_path, history())
    import laya_experiment
    monkeypatch.setattr(laya_experiment.LayaWorkerScorer, "score",
                        lambda *a, **k: pytest.fail("replay must not score"))
    monkeypatch.setattr(ld, "_real_run", lambda *a: pytest.fail("replay must not fetch"))
    assert ld.main(["replay", str(path)]) == 0
    out = capsys.readouterr().out
    assert "REPLAY (verified" in out and "cards scored: 30 of 30" in out
    top = [r for r in art["selection"]["rows"] if r["rank"] == 1][0]
    assert _mapping(tmp_path)[top["row_id"]] in out, "the private mapping turns row IDs into names"


@pytest.mark.parametrize("tamper", [
    lambda a: a["selection"]["rows"][0].update(review_probability=0.99),
    lambda a: a["cards"][0].update(rsi14=12.0),
    lambda a: a["cutoff"].update(session="2026-10-01"),
])
def test_replay_rejects_a_tampered_artifact(tmp_path, tamper):
    path, art, _ = run(tmp_path, history())
    tamper(art)
    path.chmod(0o644)
    path.write_text(json.dumps(art))
    with pytest.raises(ld.DemoFailure, match="replay"):
        ld.replay(path)


def test_replay_rejects_rows_that_disagree_even_with_a_recomputed_hash(tmp_path):
    path, art, _ = run(tmp_path, history())
    rows = art["selection"]["rows"]
    rows[0]["status"], rows[10]["status"] = rows[10]["status"], rows[0]["status"]
    art["content_sha256"] = ld.content_sha256(art)
    path.write_text(json.dumps(art))
    with pytest.raises(ld.DemoFailure, match="does not reproduce"):
        ld.replay(path)


# -- failure reporting -----------------------------------------------------

def test_scorer_failure_is_reported_and_writes_nothing(tmp_path):
    with pytest.raises(ld.DemoFailure) as e:
        run(tmp_path, history(), scorer=FakeScorer(fail=True))
    assert e.value.stage == "score" and "worker exited" in str(e.value)
    assert not any(tmp_path.iterdir())


def test_fetch_failure_and_no_cards_are_reported(tmp_path):
    def boom(syms, d):
        raise RuntimeError("maverick-mcp failed: container not running")
    with pytest.raises(ld.DemoFailure) as e:
        ld.run_demo(now_utc=SAT, universe=UNIVERSE, fetch_history=boom, scorer=FakeScorer(),
                    scorer_runtime={}, out_dir=tmp_path, git={})
    assert e.value.stage == "fetch"
    with pytest.raises(ld.DemoFailure) as e:
        run(tmp_path, {s: {"error": "x"} for s in SYMS})
    assert e.value.stage == "cutoff"
    with pytest.raises(ld.DemoFailure) as e:
        run(tmp_path, history(**{s: bars(FRI, n=10) for s in SYMS}))
    assert e.value.stage == "cards"
    assert not any(tmp_path.iterdir())


def test_cli_exit_codes_and_messages(tmp_path, capsys):
    def preflight_fail(args):
        raise ld.DemoFailure("preflight", "laya experimental mode requires a running Docker daemon")
    assert ld.main(["run", "--out", str(tmp_path)], run=preflight_fail) == 2
    err = capsys.readouterr().err
    assert "FAILED at stage 'preflight'" in err and "No artifact was written" in err

    def score_fail(args):
        raise ld.DemoFailure("score", "ScorerError: bad reply")
    assert ld.main(["run"], run=score_fail) == 1

    path, _, _ = run(tmp_path, history())
    assert ld.main(["run"], run=lambda a: path) == 0
    assert "selected 10 of k=10" in capsys.readouterr().out
