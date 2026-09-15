"""Correction pass item E (prior audit M2) -- pure proof that
backtest_run_registry's run_kind vocabulary includes 'evaluation' and that
_register_run rejects anything outside it BEFORE touching the database.
No DB needed: _register_run's validation raises before its first `await
get_pool()`, so this never opens a real connection.

Run: python test_run_kind_validation.py
"""
import asyncio

from server import _VALID_RUN_KINDS, _register_run


def test_valid_run_kinds_include_evaluation():
    assert _VALID_RUN_KINDS == {"training", "smoke", "archived", "evaluation"}
    print("  ok  _VALID_RUN_KINDS is exactly {training, smoke, archived, evaluation}")


def test_register_run_rejects_an_unknown_run_kind_before_any_db_call():
    for bad_kind in ("comparison", "eval", "", "TRAINING"):
        try:
            asyncio.run(_register_run("11111111-1111-1111-1111-111111111111", bad_kind))
            raise AssertionError(f"expected run_kind={bad_kind!r} to be rejected")
        except ValueError as exc:
            assert bad_kind in str(exc) or bad_kind == ""
    print("  ok  an unrecognized run_kind is rejected before any database access")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("PASS: backtest_run_registry.run_kind validation covers 'evaluation'")


if __name__ == "__main__":
    main()
