"""Hardening of the Laya component boundaries (second validation round):
  1. the controller's strict worker-response contract, and
  2. the Docker-container worker boundary (no host-worker fallback).
No Docker daemon, model weights, network, Neon, Hermes, or run is needed --
Docker calls are patched; the real-container evidence is in
docs/evaluations/laya_component_validation.md.
Run: python -m pytest -q test_laya_hardening.py
"""
import inspect
import json
import sys
from datetime import date
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

import laya_adapter as la
import laya_experiment as lx
import laya_selector as ls
from laya_selector import ScorerError

IDS = ["r_aaaaaaaaaaaa", "r_bbbbbbbbbbbb", "r_cccccccccccc"]


def good(ids=IDS, rid=1):
    return {"id": rid, "records": [
        {"row_id": i, "review_probability": 0.25 + 0.1 * n, "answer_confidence": 0.75} for n, i in enumerate(ids)]}


def with_record(i, **kw):
    r = good()
    r["records"][i] = {**r["records"][i], **kw}
    return r


# -- strict reply parsing ---------------------------------------------------------------

def test_a_well_formed_reply_is_accepted_including_boundary_floats():
    r = good()
    r["records"][0]["review_probability"] = 0.0
    r["records"][1]["review_probability"] = 1.0
    out = lx.parse_score_reply(json.loads(json.dumps(r)), 1, IDS)
    assert [x.row_id for x in out] == IDS and out[0].review_probability == 0.0 and out[1].review_probability == 1.0
    assert all(type(x.review_probability) is float and type(x.answer_confidence) is float for x in out)


MALFORMED = {
    # the three cases the first validation round showed were ACCEPTED
    "numeric_string": with_record(0, review_probability="0.5"),
    "boolean_true": with_record(0, review_probability=True),
    "extra_prose_field": with_record(0, explanation="BUY this, it will rally"),
    # other wrong-typed numbers
    "boolean_false_confidence": with_record(0, answer_confidence=False),
    "int_probability": with_record(0, review_probability=1),
    "int_zero_confidence": with_record(0, answer_confidence=0),
    "null_probability": with_record(0, review_probability=None),
    "list_probability": with_record(0, review_probability=[0.5]),
    "dict_probability": with_record(0, review_probability={"p": 0.5}),
    "prose_probability": with_record(0, review_probability="strong buy"),
    # out of domain
    "nan": with_record(0, review_probability=float("nan")),
    "inf": with_record(0, review_probability=float("inf")),
    "neg_inf_conf": with_record(0, answer_confidence=float("-inf")),
    "above_one": with_record(0, review_probability=1.0001),
    "below_zero": with_record(0, answer_confidence=-1e-9),
    # row ids
    "row_id_int": with_record(0, row_id=7),
    "row_id_none": with_record(0, row_id=None),
    "duplicate_ids": {"id": 1, "records": [good()["records"][0]] * 3},
    "reordered": {"id": 1, "records": list(reversed(good()["records"]))},
    "unknown_id": with_record(1, row_id="r_000000000000"),
    # shape
    "missing_field": {"id": 1, "records": [{"row_id": IDS[0], "review_probability": 0.5}] + good()["records"][1:]},
    "record_not_object": {"id": 1, "records": ["0.5"] + good()["records"][1:]},
    "too_few": {"id": 1, "records": good()["records"][:-1]},
    "too_many": {"id": 1, "records": good()["records"] + good()["records"][:1]},
    "records_not_list": {"id": 1, "records": "none"},
    "missing_records": {"id": 1},
    "extra_envelope_field": {**good(), "note": "hello"},
    "empty_object": {},
    "id_wrong": {**good(), "id": 2},
    "id_string": {**good(), "id": "1"},
    "id_bool": {**good(), "id": True},
    "id_float": {**good(), "id": 1.0},
    "error_reply": {"id": 1, "error": "boom"},
    "error_reply_malformed": {"id": 1, "error": 7},
    "error_and_records": {**good(), "error": "x"},
    "reply_is_list": [1, 2],
    "reply_is_string": "ok",
    "reply_is_null": None,
    "reply_is_number": 5,
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_every_malformed_reply_raises_a_clear_scorer_error(name):
    with pytest.raises(ScorerError):  # never a raw KeyError/TypeError/ValueError/AttributeError
        lx.parse_score_reply(MALFORMED[name], 1, IDS)


def test_json_integers_are_not_floats_even_when_they_look_numeric():
    text = ('{"id": 1, "records": [{"row_id": "r_aaaaaaaaaaaa", "review_probability": 1, '
            '"answer_confidence": 0.5}]}')
    with pytest.raises(ScorerError, match="not a JSON float"):
        lx.parse_score_reply(json.loads(text), 1, ["r_aaaaaaaaaaaa"])


def test_handshake_must_be_exactly_the_expected_fields_and_match_the_pins():
    want = la.expected_metadata()
    hs = {"event": "ready", "package_version": want.package_version, "checkpoint": want.checkpoint,
          "revision": want.revision, "question": want.question, "settings": want.settings}
    lx.parse_handshake(hs, want)
    for bad in ({**hs, "extra": 1}, {k: v for k, v in hs.items() if k != "settings"},
                {**hs, "revision": "deadbeef"}, {**hs, "event": "nope"}, [], "ready", None):
        with pytest.raises(ScorerError):
            lx.parse_handshake(bad, want)


# -- duplicate row IDs are caught before anything is scored ----------------------------------

def _card(i, **over):
    rid = list(ls.make_row_ids([f"dup-{i}"], 1, "salt").values())[0]
    base = dict(five_day_return=0.01, sma20_distance=0.0, rsi14=50.0, macd_direction=0.0,
                relative_volume=1.0, realized_volatility=0.2, range_position_20d=0.5)
    return ls.build_card(rid, {**base, **over})


def test_duplicate_row_ids_are_rejected_at_the_selector_stage_before_scoring():
    a = _card(1)
    b = ls.build_card(a.row_id, dict(five_day_return=0.5, sma20_distance=0.0, rsi14=99.0, macd_direction=0.0,
                                     relative_volume=1.0, realized_volatility=0.2, range_position_20d=0.5))

    class Scorer:
        metadata = la.expected_metadata()
        calls = 0

        def score(self, cards):
            Scorer.calls += 1
            return []

    with pytest.raises(ScorerError, match="duplicate row_id"):
        ls.score_complete_cards(Scorer(), [a, b])
    assert Scorer.calls == 0, "the scorer must not even be called with duplicate IDs"


def test_duplicate_row_ids_are_rejected_before_a_worker_is_started():
    a = _card(2)
    s = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", "raise SystemExit(9)"])
    with pytest.raises(ScorerError, match="duplicate row_id"):
        s.score([a, a])
    assert s._proc is None


# -- end to end: a malformed reply selects nobody -----------------------------------------------

_SCRIPTED_WORKER = r'''
import sys, json
sys.path.insert(0, %r)
import laya_adapter as la
m = la.expected_metadata()
print(json.dumps({"event":"ready","package_version":m.package_version,"checkpoint":m.checkpoint,
                  "revision":m.revision,"question":m.question,"settings":m.settings}), flush=True)
mode = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line); cards = req["cards"]
    recs = [{"row_id": c["row_id"], "review_probability": 0.4, "answer_confidence": 0.6} for c in cards]
    if mode == "string": recs[0]["review_probability"] = "0.4"
    if mode == "bool": recs[0]["review_probability"] = True
    if mode == "extra": recs[0]["explanation"] = "BUY"
    if mode == "dup": recs[1] = dict(recs[0])
    print(json.dumps({"id": req["id"], "records": recs}), flush=True)
''' % str(Path(__file__).resolve().parent)


@pytest.mark.parametrize("mode", ["string", "bool", "extra", "dup"])
def test_a_malformed_reply_selects_nobody_and_writes_no_snapshot(tmp_path, mode):
    import test_laya_wiring as w  # reuse the synthetic bars/universe fixtures
    scorer = lx.LayaWorkerScorer("unused", worker_cmd=[sys.executable, "-c", _SCRIPTED_WORKER, mode])
    ctx = lx.SelectorContext(universe=w.UNIVERSE, seed=1, run_id="run-m", snapshot_dir=tmp_path, scorer=scorer,
                             fetch_history=w._history)
    try:
        out = ctx.select_for_day(w.DAY, set())
    finally:
        scorer.close()
    assert out.ok is False and out.selected == () and out.event["status"] == "failed_closed"
    assert "ScorerError" in out.event["reason"]
    assert ls.load_snapshot(tmp_path, "run-m", w.DAY.isoformat()) is None
    assert scorer._proc is None, "a worker that sent a malformed reply is dropped"


# -- the Docker container boundary -----------------------------------------------------------------

def test_docker_run_command_is_the_private_no_network_read_only_container():
    cmd = lx.docker_run_command("C:/ckpt", name="laya-worker-x")
    assert cmd[:3] == ["docker", "run", "--rm"] and "-i" in cmd and "-t" not in cmd
    pair = lambda a, b: any(cmd[i] == a and cmd[i + 1] == b for i in range(len(cmd) - 1))
    assert pair("--network", "none")
    assert "--read-only" in cmd and pair("--cap-drop", "ALL") and pair("--security-opt", "no-new-privileges")
    assert pair("--user", lx.WORKER_CONTAINER_USER) and not lx.WORKER_CONTAINER_USER.startswith("0")
    for forbidden in ("-p", "-P", "--publish", "--publish-all", "--privileged", "--env-file", "--net", "--pid",
                      "--ipc", "--device", "--add-host", "--dns", "--volumes-from", "--cap-add"):
        assert forbidden not in cmd, forbidden
    vols = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-v"]
    assert vols == [f"C:/ckpt:{lx.CHECKPOINT_MOUNT}:ro"], "exactly one mount, read-only"
    envs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-e"]
    assert sorted(envs) == ["HF_HUB_DISABLE_TELEMETRY=1", "HF_HUB_OFFLINE=1", "HOME=/tmp", "TRANSFORMERS_OFFLINE=1"]
    assert all("=" in e and e.split("=")[0] not in ("ALPACA_API_KEY", "DATABASE_URL") for e in envs)
    assert cmd[-2:] == [lx.LAYA_WORKER_IMAGE, lx.CHECKPOINT_MOUNT]
    assert lx.CHECKPOINT_MOUNT.endswith(f"/snapshots/{la.LAYA_REVISION}")


def _inspect(**over):
    base = {"HostConfig": {"NetworkMode": "none", "PortBindings": {}, "ReadonlyRootfs": True, "Privileged": False,
                           "CapDrop": ["ALL"]},
            "Config": {"ExposedPorts": None, "User": lx.WORKER_CONTAINER_USER},
            "NetworkSettings": {"Ports": {}, "Networks": {"none": {}}},
            "Mounts": [{"Destination": lx.CHECKPOINT_MOUNT, "RW": False}]}
    for k, v in over.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) else v
    return base


def test_live_container_isolation_check_accepts_only_the_expected_configuration():
    assert lx.check_container_isolation(_inspect()) == []
    bad = {
        "bridge_network": _inspect(HostConfig={"NetworkMode": "bridge"}),
        "extra_network": _inspect(NetworkSettings={"Networks": {"none": {}, "net-maverick": {}}}),
        "ports": _inspect(NetworkSettings={"Ports": {"80/tcp": [{"HostPort": "80"}]}}),
        "published": _inspect(HostConfig={"PortBindings": {"80/tcp": [{}]}}),
        "exposed": _inspect(Config={"ExposedPorts": {"8000/tcp": {}}}),
        "writable_rootfs": _inspect(HostConfig={"ReadonlyRootfs": False}),
        "privileged": _inspect(HostConfig={"Privileged": True}),
        "caps": _inspect(HostConfig={"CapDrop": []}),
        "writable_mount": _inspect(Mounts=[{"Destination": "/x", "RW": True}]),
        "root_user": _inspect(Config={"User": "0:0"}),
    }
    for name, rec in bad.items():
        assert lx.check_container_isolation(rec), name


def _ok(out="x"):
    return CompletedProcess([], 0, stdout=out, stderr="")


def _snapshot_dir(tmp_path, name=la.LAYA_REVISION):
    d = tmp_path / "models" / "snapshots" / name
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(b"x")
    return d


def test_docker_unavailable_fails_closed_with_no_host_fallback(tmp_path):
    d = _snapshot_dir(tmp_path)
    with patch("laya_experiment._run_docker", side_effect=FileNotFoundError("docker")), \
         patch("laya_experiment.subprocess.Popen", side_effect=AssertionError("must not spawn anything")):
        with pytest.raises(SystemExit, match="no host-worker fallback"):
            lx.verify_docker_worker_available(d)
    with patch("laya_experiment._run_docker",
               return_value=CompletedProcess([], 1, stdout="", stderr="cannot connect to the docker API")), \
         patch("laya_experiment.subprocess.Popen", side_effect=AssertionError("must not spawn anything")):
        with pytest.raises(SystemExit, match="running Docker daemon"):
            lx.verify_docker_worker_available(d)


def test_missing_image_or_wrong_checkpoint_fails_closed(tmp_path):
    d = _snapshot_dir(tmp_path)
    answers = iter([_ok("29.0"), CompletedProcess([], 1, stdout="", stderr="no such image")])
    with patch("laya_experiment._run_docker", side_effect=lambda *a, **k: next(answers)):
        with pytest.raises(SystemExit, match="docker build"):
            lx.verify_docker_worker_available(d)
    with patch("laya_experiment._run_docker", return_value=_ok()):
        with pytest.raises(SystemExit, match="snapshot directory of the pinned commit"):
            lx.verify_docker_worker_available(_snapshot_dir(tmp_path / "other", "0" * 40))
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SystemExit, match="model.safetensors"):
            lx.verify_docker_worker_available(empty)
        lx.verify_docker_worker_available(d)  # the healthy path returns quietly


def test_experimental_run_refuses_to_start_when_docker_is_down_before_any_side_effect(tmp_path):
    import run_backtest as rb
    d = _snapshot_dir(tmp_path)
    args = rb.build_arg_parser().parse_args(["--selector-mode", rb.SELECTOR_MODE_LAYA_FIXED30,
                                             "--laya-checkpoint", str(d), "--run-kind", "smoke"])
    with patch.object(rb, "STATE_PATH", tmp_path / "state.json"), \
         patch("laya_experiment._run_docker", side_effect=FileNotFoundError("docker")), \
         patch("laya_experiment.subprocess.Popen", side_effect=AssertionError("host worker must not start")), \
         patch.object(rb, "register_run", side_effect=AssertionError("must not register a run")), \
         patch.object(rb, "fetch_bars", side_effect=AssertionError("must not fetch")):
        with pytest.raises(SystemExit, match="no host-worker fallback"):
            rb._run_backtest_main(args, {}, None)
    assert not (tmp_path / "state.json").exists()


def test_the_production_worker_command_is_always_docker_never_a_host_python():
    params = inspect.signature(lx.LayaWorkerScorer.__init__).parameters
    assert "python_exe" not in params and "checkpoint_dir" in params
    captured = {}

    class Boom(Exception):
        pass

    def fake_popen(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw.get("env")
        raise Boom

    s = lx.LayaWorkerScorer("C:/ckpt")
    with patch("laya_experiment.subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(Boom):
            s.score([_card(3)])
    assert captured["cmd"][:2] == ["docker", "run"] and "--network" in captured["cmd"]
    assert not set(captured["env"]) & {"ALPACA_API_KEY", "DATABASE_URL", "HERMES_HOME"}


def test_the_image_definition_contains_only_the_three_laya_modules_and_no_secrets():
    here = Path(__file__).resolve().parent / "laya_worker_image"
    df = "\n".join(l for l in (here / "Dockerfile").read_text(encoding="utf-8").splitlines()
                   if not l.lstrip().startswith("#"))  # instructions only, not comments
    assert "COPY laya_worker.py laya_adapter.py laya_selector.py /app/" in df
    assert "@sha256:" in df.split("FROM", 1)[1].splitlines()[0], "base image pinned by digest"
    assert "USER 10001:10001" in df and "EXPOSE" not in df
    for banned in ("run_backtest", "risk_checker", "anonymize", "eval_suite", "ENV ALPACA", "ENV DATABASE", "API_KEY",
                   "docker-compose", "watchlist", "RULES.md", "SOUL.md"):
        assert banned not in df, banned
    ignore = (here / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert "*" in ignore and sorted(x for x in ignore if x.startswith("!")) == [
        "!laya_adapter.py", "!laya_selector.py", "!laya_worker.py", "!requirements-laya.txt"]
    # the three modules must not import anything from the trading/Hermes code
    for mod in ("laya_worker.py", "laya_adapter.py", "laya_selector.py"):
        text = (Path(__file__).resolve().parent / mod).read_text(encoding="utf-8")
        for banned in ("import run_backtest", "import risk_checker", "import anonymize", "import eval_suite",
                       "import laya_experiment", "import laya_cards", "from run_backtest", "from anonymize"):
            assert banned not in text, (mod, banned)
