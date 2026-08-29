"""Proof the quota pacer actually stops cycles at the budget and resets on
a real new calendar week. Run: python test_quota_pacer.py
"""
import tempfile
from datetime import date
from pathlib import Path

from quota_pacer import QuotaPacer, _week_key


def _pacer(budget: int = 3) -> QuotaPacer:
    tmp = Path(tempfile.mktemp(suffix=".json"))
    return QuotaPacer(budget=budget, state_path=tmp)


def test_week_key_is_monday_start_and_stable_within_a_week():
    # Same real week -> same key, regardless of which day within it.
    monday = date(2026, 8, 17)
    wednesday = date(2026, 8, 19)
    sunday = date(2026, 8, 23)
    assert _week_key(monday) == _week_key(wednesday) == _week_key(sunday)

    # The following Monday is a genuinely different week.
    next_monday = date(2026, 8, 24)
    assert _week_key(next_monday) != _week_key(sunday)


def test_fresh_pacer_allows_cycles_up_to_budget():
    p = _pacer(budget=3)
    today = date(2026, 8, 17)

    assert p.can_run_cycle(today)
    assert p.remaining_this_week(today) == 3

    p.record_cycle(today)
    p.record_cycle(today)
    assert p.remaining_this_week(today) == 1
    assert p.can_run_cycle(today)

    p.record_cycle(today)
    # Budget exactly hit -- this is the real gate, not just a low number.
    assert p.remaining_this_week(today) == 0
    assert not p.can_run_cycle(today)


def test_budget_never_goes_negative_if_overcounted():
    """Defensive: a caller bug that calls record_cycle one extra time must
    not make remaining_this_week report a nonsensical negative number that
    a naive `if remaining > 0` check could misread."""
    p = _pacer(budget=1)
    today = date(2026, 8, 17)
    p.record_cycle(today)
    p.record_cycle(today)  # over budget by one
    assert p.remaining_this_week(today) == 0
    assert not p.can_run_cycle(today)


def test_state_persists_across_a_fresh_pacer_instance():
    """The real point of persisting to disk: a restarted process (script
    re-run, cron firing again) must not forget cycles already spent this
    week and silently double the effective budget."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    today = date(2026, 8, 17)

    p1 = QuotaPacer(budget=5, state_path=tmp)
    p1.record_cycle(today)
    p1.record_cycle(today)

    p2 = QuotaPacer(budget=5, state_path=tmp)  # simulates a restart
    assert p2.used_this_week(today) == 2
    assert p2.remaining_this_week(today) == 3


def test_new_real_week_resets_the_budget():
    p = _pacer(budget=2)
    week1_day = date(2026, 8, 17)  # a Monday
    week2_day = date(2026, 8, 24)  # the following Monday

    p.record_cycle(week1_day)
    p.record_cycle(week1_day)
    assert not p.can_run_cycle(week1_day)

    # Same pacer, same state file, but a genuinely new calendar week --
    # this is the actual point of the pacer (pause-then-resume, not a
    # permanent stop).
    assert p.can_run_cycle(week2_day)
    assert p.remaining_this_week(week2_day) == 2
    # And week1's exhausted count is untouched, not merged/reset globally.
    assert p.used_this_week(week1_day) == 2


def test_corrupt_state_file_fails_CLOSED_not_open():
    """The bug this test exists to catch (found by asking 'did you actually
    test an interruption, or just clean logic?'): a naive fallback of
    'corrupt file -> treat as {} -> 0 used -> full budget available' is
    FAIL-OPEN -- exactly backwards for a system whose entire job is
    stopping overspend. If corruption happens near the budget limit, that
    fallback would silently hand back a fresh full week right when the
    pacer is supposed to say no. Must fail CLOSED instead: unreadable
    state reports as budget-exhausted, not budget-fresh. It also must not
    crash the harness either way."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{not valid json")
    p = QuotaPacer(budget=3, state_path=tmp)
    today = date(2026, 8, 17)

    assert p.used_this_week(today) == p.budget  # reads as fully spent
    assert p.remaining_this_week(today) == 0
    assert not p.can_run_cycle(today)  # blocked, not allowed


def test_interrupted_write_never_corrupts_the_previous_valid_state():
    """The actual scenario the user asked about: the process gets cut off
    mid-run (hit quota, killed, crashed) WHILE a write is in flight. Direct
    write_text() would leave a half-written file; the atomic
    temp-file-then-rename in _save must mean readers only ever see the
    fully-old or fully-new file, never a partial one. Simulated here by
    writing a real, valid, high-usage state, then independently writing a
    truncated version of that same file (what a torn write looks like) and
    confirming the pacer still fails closed rather than losing the count
    and reopening the budget."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    today = date(2026, 8, 17)

    # A real prior state: already at budget for this week.
    p1 = QuotaPacer(budget=3, state_path=tmp)
    p1.record_cycle(today)
    p1.record_cycle(today)
    p1.record_cycle(today)
    assert not p1.can_run_cycle(today)

    # Simulate a torn write: truncate the valid JSON mid-object, exactly
    # the shape a kill-during-write leaves behind.
    valid = tmp.read_text()
    tmp.write_text(valid[: len(valid) // 2])

    p2 = QuotaPacer(budget=3, state_path=tmp)
    # Must NOT silently read as "0 used, budget reopened" -- that would be
    # the actual quota-overspend bug the user was pushing on.
    assert not p2.can_run_cycle(today)


def test_record_cycle_fails_closed_with_a_named_error_on_corrupt_state():
    """2026-08-29 correctness pass #4, item 6: record_cycle is called
    AFTER a real Hermes call already happened -- an unreadable state file
    used to crash here with a bare AttributeError (None.get), with no
    indication the failure was quota-state corruption specifically, right
    after real quota was already spent. Must raise a NAMED RuntimeError
    instead, and can_run_cycle must still report closed."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{not valid json")
    p = QuotaPacer(budget=3, state_path=tmp)
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

    # can_run_cycle must still report closed -- the failed record_cycle
    # call didn't somehow leave the pacer thinking budget is available.
    assert not p.can_run_cycle(today)


def test_next_week_starts_returns_the_coming_monday():
    p = _pacer()
    # Wednesday -> next Monday is 5 days later.
    assert p.next_week_starts(date(2026, 8, 19)) == date(2026, 8, 24)
    # Monday itself -> next week's Monday, not today.
    assert p.next_week_starts(date(2026, 8, 17)) == date(2026, 8, 24)
    # Sunday -> tomorrow.
    assert p.next_week_starts(date(2026, 8, 23)) == date(2026, 8, 24)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: quota pacer gates cycles at budget, persists across "
          "restarts, and resets on a real new week")


if __name__ == "__main__":
    main()
