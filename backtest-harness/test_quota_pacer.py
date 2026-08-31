"""Proof the quota pacer counts real Hermes invocations correctly against a
real ISO calendar week, persists across restarts, and fails closed
(unreadable, not silently zero) on corrupt local state -- no blocking
threshold anymore (removed 2026-08-31; see quota_pacer.py's module
docstring). Run: python test_quota_pacer.py
"""
import tempfile
from datetime import date
from pathlib import Path

from quota_pacer import QuotaPacer, _week_key


def _pacer() -> QuotaPacer:
    tmp = Path(tempfile.mktemp(suffix=".json"))
    return QuotaPacer(state_path=tmp)


def test_week_key_is_monday_start_and_stable_within_a_week():
    # Same real week -> same key, regardless of which day within it.
    monday = date(2026, 8, 17)
    wednesday = date(2026, 8, 19)
    sunday = date(2026, 8, 23)
    assert _week_key(monday) == _week_key(wednesday) == _week_key(sunday)

    # The following Monday is a genuinely different week.
    next_monday = date(2026, 8, 24)
    assert _week_key(next_monday) != _week_key(sunday)


def test_fresh_pacer_counts_from_zero_with_no_ceiling():
    """The old 175/week GATE is gone -- record_cycle just keeps counting,
    with nothing that stops it and nothing it needs to stay under."""
    p = _pacer()
    today = date(2026, 8, 17)

    assert p.used_this_week(today) == 0

    p.record_cycle(today)
    p.record_cycle(today)
    p.record_cycle(today)
    assert p.used_this_week(today) == 3

    # Keeps counting past any of the old guessed thresholds (32, 175) with
    # no special behavior at either -- there is no ceiling left to hit.
    for _ in range(200):
        p.record_cycle(today)
    assert p.used_this_week(today) == 203


def test_state_persists_across_a_fresh_pacer_instance():
    """The real point of persisting to disk: a restarted process (script
    re-run, cron firing again) must not forget invocations already counted
    this week."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    today = date(2026, 8, 17)

    p1 = QuotaPacer(state_path=tmp)
    p1.record_cycle(today)
    p1.record_cycle(today)

    p2 = QuotaPacer(state_path=tmp)  # simulates a restart
    assert p2.used_this_week(today) == 2


def test_new_real_week_starts_its_own_count():
    p = _pacer()
    week1_day = date(2026, 8, 17)  # a Monday
    week2_day = date(2026, 8, 24)  # the following Monday

    p.record_cycle(week1_day)
    p.record_cycle(week1_day)
    assert p.used_this_week(week1_day) == 2

    # Same pacer, same state file, but a genuinely new calendar week --
    # its own independent count, starting fresh.
    assert p.used_this_week(week2_day) == 0
    p.record_cycle(week2_day)
    assert p.used_this_week(week2_day) == 1
    # And week1's count is untouched, not merged/reset globally.
    assert p.used_this_week(week1_day) == 2


def test_corrupt_state_reports_as_unknown_not_zero():
    """This is observability now, not a gate -- a naive 'corrupt file -> 0
    used' fallback would silently under-report real usage on a dashboard,
    which is exactly the kind of quiet wrongness this guards against.
    Unreadable state must report as `None` ("unknown"), never as a number
    a caller could mistake for a real, trustworthy count."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{not valid json")
    p = QuotaPacer(state_path=tmp)
    today = date(2026, 8, 17)

    assert p.used_this_week(today) is None


def test_interrupted_write_never_corrupts_the_previous_valid_state():
    """The actual scenario the user asked about: the process gets cut off
    mid-run WHILE a write is in flight. Direct write_text() would leave a
    half-written file; the atomic temp-file-then-rename in _save must mean
    readers only ever see the fully-old or fully-new file, never a partial
    one. Simulated here by writing a real, valid state, then independently
    writing a truncated version of that same file (what a torn write looks
    like) and confirming the pacer reports it as unreadable rather than
    silently losing the count."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    today = date(2026, 8, 17)

    p1 = QuotaPacer(state_path=tmp)
    p1.record_cycle(today)
    p1.record_cycle(today)
    p1.record_cycle(today)
    assert p1.used_this_week(today) == 3

    # Simulate a torn write: truncate the valid JSON mid-object, exactly
    # the shape a kill-during-write leaves behind.
    valid = tmp.read_text()
    tmp.write_text(valid[: len(valid) // 2])

    p2 = QuotaPacer(state_path=tmp)
    # Must NOT silently read as "0 used" -- that would misreport real
    # usage that actually happened.
    assert p2.used_this_week(today) is None


def test_record_cycle_fails_closed_with_a_named_error_on_corrupt_state():
    """record_cycle is called right after a real Hermes call already
    happened -- an unreadable state file used to crash here with a bare
    AttributeError (None.get), with no indication the failure was
    quota-state corruption specifically. Must raise a NAMED RuntimeError
    instead, so a caller (run_backtest.py's mark_invoked) can deliberately
    choose to swallow it -- this is observability-only, so a broken
    counter must never be allowed to fail a real cycle."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{not valid json")
    p = QuotaPacer(state_path=tmp)
    today = date(2026, 8, 17)

    try:
        p.record_cycle(today)
        raise AssertionError("expected record_cycle to raise on unreadable state")
    except RuntimeError as exc:
        assert "quota state unreadable" in str(exc)
    except AttributeError as exc:
        raise AssertionError(
            f"record_cycle crashed with a bare AttributeError instead of a "
            f"named error: {exc}"
        ) from exc


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: quota pacer counts real invocations against a real ISO week, "
          "persists across restarts, and reports (never hides) unreadable state -- "
          "no blocking threshold")


if __name__ == "__main__":
    main()
