"""--state-dir: every run-owned local file moves under one explicit directory;
omitting it changes nothing. Drives the REAL _run_backtest_main up to its
first database call (stubbed to stop there) -- no Docker, Neon, or Hermes.
Every test also proves the preserved default-location state file is
byte-for-byte untouched.
Run: python -m pytest -q test_state_dir.py
"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import run_backtest as rb

HERE = Path(__file__).resolve().parent
DEFAULT_STATE = HERE / "backtest_state.json"


class Stop(Exception):
    pass


def _sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


@pytest.fixture(autouse=True)
def restore_paths():
    saved = (rb.STATE_PATH, rb.CHECKPOINT_PATH, rb.DIAGNOSTICS_DIR, rb.SELECTOR_SNAPSHOT_DIR)
    before = _sha(DEFAULT_STATE), _sha(HERE / "cycle_checkpoint.json")
    yield
    rb.STATE_PATH, rb.CHECKPOINT_PATH, rb.DIAGNOSTICS_DIR, rb.SELECTOR_SNAPSHOT_DIR = saved
    assert (_sha(DEFAULT_STATE), _sha(HERE / "cycle_checkpoint.json")) == before, \
        "the default-location state/checkpoint must never change"


def _args(*extra):
    return rb.build_arg_parser().parse_args(list(extra))


def _state_with_progress(run_id: str) -> dict:
    p = rb.Portfolio()
    p.trading_days_elapsed = 3
    return {"portfolio": p.to_dict(), "cursor": "2022-07-06", "run_id": run_id, "seed": 7,
            "start": "2022-07-01", "end": "2022-07-08"}


def test_omitted_flag_keeps_every_original_location():
    rb.apply_state_dir(None)
    assert rb.STATE_PATH == HERE / "backtest_state.json"
    assert rb.CHECKPOINT_PATH == HERE / "cycle_checkpoint.json"
    assert rb.DIAGNOSTICS_DIR == HERE / ".diagnostics"
    assert rb.SELECTOR_SNAPSHOT_DIR is None  # -> laya_experiment.SNAPSHOT_DIR, unchanged
    assert _args().state_dir is None


def test_flag_routes_all_run_owned_files(tmp_path):
    d = tmp_path / "smoke"
    rb.apply_state_dir(str(d))
    assert d.is_dir()
    assert rb.STATE_PATH == d / "backtest_state.json"
    assert rb.CHECKPOINT_PATH == d / "cycle_checkpoint.json"
    assert rb.DIAGNOSTICS_DIR == d / ".diagnostics"
    assert rb.SELECTOR_SNAPSHOT_DIR == d / "selector_snapshots"
    rb._atomic_write_json(rb.CHECKPOINT_PATH, {"run_id": "x", "day_index": 0})
    assert rb._load_checkpoint("x", 0) == {"run_id": "x", "day_index": 0}
    rb._clear_checkpoint()
    assert not rb.CHECKPOINT_PATH.exists()


def test_refuses_the_default_directory_and_a_file(tmp_path):
    for alias in (str(HERE), str(HERE).lower(), str(HERE) + "/.", str(HERE / ".." / HERE.name)):
        with pytest.raises(SystemExit, match="default harness directory"):
            rb.apply_state_dir(alias)
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(SystemExit, match="not a directory"):
        rb.apply_state_dir(str(f))
    assert rb.STATE_PATH == HERE / "backtest_state.json", "a refused flag must not move anything"


def test_fresh_run_in_an_empty_state_dir_registers_a_new_run(tmp_path):
    rb.apply_state_dir(str(tmp_path / "s"))
    seen = {}

    def reg(run_id, kind):
        seen["run_id"], seen["kind"] = run_id, kind
        raise Stop
    with patch.object(rb, "get_registered_run_kind", side_effect=AssertionError("fresh: no lookup")), \
         patch.object(rb, "register_run", side_effect=reg):
        with pytest.raises(Stop):
            rb._run_backtest_main(_args("--run-kind", "smoke", "--state-dir", str(tmp_path / "s")), {}, None)
    assert seen["kind"] == "smoke"
    if DEFAULT_STATE.exists():
        assert seen["run_id"] != json.loads(DEFAULT_STATE.read_text())["run_id"]
    assert not rb.STATE_PATH.exists(), "nothing is written before the first cycle commits"


def test_fresh_run_without_run_kind_fails_before_any_write(tmp_path):
    rb.apply_state_dir(str(tmp_path / "s"))
    with patch.object(rb, "register_run", side_effect=AssertionError("must not register")):
        with pytest.raises(SystemExit, match="requires an explicit"):
            rb._run_backtest_main(_args("--state-dir", str(tmp_path / "s")), {}, None)


def test_resume_reads_only_the_state_dir(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "backtest_state.json").write_text(json.dumps(_state_with_progress("isolated-run")))
    rb.apply_state_dir(str(d))
    with patch.object(rb, "get_registered_run_kind", side_effect=lambda rid: (_ for _ in ()).throw(Stop(rid))):
        with pytest.raises(Stop) as e:
            rb._run_backtest_main(_args("--state-dir", str(d), "--start", "2022-07-01",
                                        "--end", "2022-07-08"), {}, None)
    assert e.value.args[0] == "isolated-run"


def test_reset_only_touches_the_state_dir(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "backtest_state.json").write_text(json.dumps(_state_with_progress("old")))
    (d / "cycle_checkpoint.json").write_text("{}")
    rb.apply_state_dir(str(d))
    with pytest.raises(SystemExit, match="confirm-destructive-reset"):
        rb._run_backtest_main(_args("--state-dir", str(d), "--reset", "--run-kind", "smoke"), {}, None)
    assert (d / "backtest_state.json").exists(), "an unconfirmed reset deletes nothing"
    with patch.object(rb, "register_run", side_effect=Stop):
        with pytest.raises(Stop):
            rb._run_backtest_main(_args("--state-dir", str(d), "--reset", "--confirm-destructive-reset",
                                        "--run-kind", "smoke"), {}, None)
    assert not (d / "backtest_state.json").exists() and not (d / "cycle_checkpoint.json").exists()


def test_main_applies_the_state_dir_before_hermes_provisioning(tmp_path, monkeypatch):
    d = tmp_path / "s"
    seen = {}

    def fake_preflight():
        seen["state_path"] = rb.STATE_PATH
        raise Stop
    monkeypatch.setattr(sys, "argv", ["run_backtest.py", "--state-dir", str(d), "--run-kind", "smoke"])
    with patch.object(rb, "preflight", side_effect=fake_preflight):
        with pytest.raises(Stop):
            rb.main()
    assert seen["state_path"] == d.resolve() / "backtest_state.json"
