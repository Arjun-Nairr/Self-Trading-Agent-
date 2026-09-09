"""
Deterministic coverage for the durable run-classification decision
(training / smoke / archived) -- resolve_run_kind() and build_arg_parser()
in run_backtest.py.

No Docker, no Neon writes, no Hermes calls, no state deletion, no
--reset, no real backtest cycle. resolve_run_kind() is pure (zero I/O);
the registry lookup/write that surrounds it in main() is exercised only
via cases 1-9 below, all against the function directly -- nothing here
launches a real run.

Run directly (no Docker/Neon/network needed):
    python test_run_kind_resolution.py
"""
import contextlib
import inspect
import io

from run_backtest import (  # type: ignore
    build_arg_parser, resolve_run_kind, RunKindError, main,
)

RUN_ID = "00000000-0000-0000-0000-000000000001"


def _fails(fn, *args, **kwargs) -> str:
    """Runs fn(*args, **kwargs), asserts it raises RunKindError, returns
    the message."""
    try:
        fn(*args, **kwargs)
    except RunKindError as exc:
        return str(exc)
    raise AssertionError(f"expected RunKindError, got no exception from {fn.__name__}{args!r}")


def test_fresh_no_run_kind_fails_clearly():
    """1. Fresh run, no --run-kind -> fails clearly."""
    msg = _fails(resolve_run_kind, False, None, RUN_ID, None)
    assert "requires an explicit --run-kind" in msg
    assert "training|smoke" in msg
    print("ok  fresh run with no --run-kind fails clearly")


def test_fresh_training_accepted():
    """2. Fresh run, --run-kind training -> accepted as training."""
    assert resolve_run_kind(False, "training", RUN_ID, None) == "training"
    print("ok  fresh run with --run-kind training is accepted as training")


def test_fresh_smoke_accepted():
    """3. Fresh run, --run-kind smoke -> accepted as smoke."""
    assert resolve_run_kind(False, "smoke", RUN_ID, None) == "smoke"
    print("ok  fresh run with --run-kind smoke is accepted as smoke")


def test_fresh_run_ignores_stale_registered_kind():
    """A fresh run (is_resume=False) must never consult registered_kind --
    even a leftover/garbage value there must not leak into the decision."""
    assert resolve_run_kind(False, "smoke", RUN_ID, "training") == "smoke"
    print("ok  a fresh run's decision never depends on registered_kind")


def test_resume_no_supplied_kind_reuses_registered():
    """4. Resume of a registered 'training' run, no supplied kind ->
    reuses training."""
    assert resolve_run_kind(True, None, RUN_ID, "training") == "training"
    print("ok  resume with no supplied kind reuses the registered classification")


def test_resume_matching_supplied_kind_accepted():
    """5. Resume with the matching supplied kind -> accepted."""
    assert resolve_run_kind(True, "training", RUN_ID, "training") == "training"
    assert resolve_run_kind(True, "smoke", RUN_ID, "smoke") == "smoke"
    print("ok  resume with a matching supplied kind is accepted")


def test_resume_conflicting_supplied_kind_fails_clearly():
    """6. Resume with a conflicting supplied kind -> fails clearly."""
    msg = _fails(resolve_run_kind, True, "smoke", RUN_ID, "training")
    assert "conflicts with run_id" in msg
    assert "'smoke'" in msg and "'training'" in msg
    print("ok  resume with a conflicting supplied kind fails clearly")


def test_resume_unregistered_run_fails_clearly():
    """7. Resume of an unregistered run (no registry row -> None) ->
    fails clearly."""
    msg = _fails(resolve_run_kind, True, None, RUN_ID, None)
    assert "no backtest_run_registry row" in msg
    assert "cannot be resumed silently" in msg
    print("ok  resume of an unregistered run fails clearly")


def test_resume_archived_run_fails_clearly():
    """8. Resume of an 'archived' run -> fails clearly, even if a
    matching --run-kind were somehow supplied (archived isn't a valid
    CLI choice at all -- see test_archived_impossible_via_cli -- but the
    resolution function itself must reject it independent of that)."""
    msg = _fails(resolve_run_kind, True, None, RUN_ID, "archived")
    assert "registered as 'archived'" in msg
    assert "migration-only" in msg
    print("ok  resume of an archived run fails clearly")


def test_archived_impossible_via_cli():
    """9. 'archived' remains impossible to select for a fresh CLI run --
    structurally excluded from --run-kind's choices, rejected by argparse
    itself before main() (and therefore before preflight()/Docker/Neon)
    is ever reached."""
    parser = build_arg_parser()
    try:
        # argparse always prints usage+error to stderr on an invalid
        # choice, regardless of the args list passed -- suppressed here
        # only for tidy test output, not part of what's being verified.
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--run-kind", "archived"])
    except SystemExit as exc:
        assert exc.code != 0
        print("ok  --run-kind archived is rejected by the CLI parser itself")
        return
    raise AssertionError("--run-kind archived should have been rejected by argparse")


def test_classification_gate_precedes_the_cycle_loop():
    """The classification decision must happen BEFORE the cycle loop /
    Hermes call path, not just in principle -- prove it structurally
    against main()'s own real source: the resolve_run_kind(...) call site
    appears strictly before the `while completed < args.days` cycle loop
    that drives run_day() (where the Hermes call ultimately happens)."""
    source, start_line = inspect.getsourcelines(main)
    resolve_call_lines = [i for i, line in enumerate(source) if "resolve_run_kind(" in line and "def " not in line]
    loop_lines = [i for i, line in enumerate(source) if "while completed < args.days" in line]
    assert resolve_call_lines, "resolve_run_kind(...) call site not found in main()"
    assert loop_lines, "the cycle loop was not found in main() -- has it been renamed?"
    assert resolve_call_lines[0] < loop_lines[0], (
        "resolve_run_kind(...) must be called before the cycle loop that "
        "drives Hermes calls, not after"
    )
    print("ok  the run-kind decision happens before the cycle loop, verified against main()'s real source")


if __name__ == "__main__":
    test_fresh_no_run_kind_fails_clearly()
    test_fresh_training_accepted()
    test_fresh_smoke_accepted()
    test_fresh_run_ignores_stale_registered_kind()
    test_resume_no_supplied_kind_reuses_registered()
    test_resume_matching_supplied_kind_accepted()
    test_resume_conflicting_supplied_kind_fails_clearly()
    test_resume_unregistered_run_fails_clearly()
    test_resume_archived_run_fails_clearly()
    test_archived_impossible_via_cli()
    test_classification_gate_precedes_the_cycle_loop()
    print("PASS: run-kind resolution is deterministic, fails closed on every "
          "invalid case, and is gated before the cycle/Hermes path")
