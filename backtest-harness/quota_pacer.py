"""quota_pacer.py — real, observability-only Hermes-invocation counting.

HISTORY: this used to be a hard weekly GATE (`can_run_cycle`/`budget`) that
stopped the backtest loop once a locally-guessed weekly ceiling (last set to
175) was hit -- a defensive brake against blowing through the shared
ChatGPT Plus weekly quota. That ceiling was never a measured fact (see the
git history on this file / HANDOFF.md's 2026-08-29 note): real usage across
125 real cycles (run 1) stayed at roughly 1-3% of the visible weekly
allowance in the ChatGPT app. 2026-08-31: the user reviewed that evidence
and approved removing the gate entirely -- training no longer stops on a
local invocation count, and this module does not invent a replacement
number to stop on.

WHAT THIS STILL DOES: count real Hermes invocations against a real calendar
week (Monday-start, ISO week number -- an unambiguous, standard definition
of "a week", not "7 days since I last checked"), purely so the dashboard
and HANDOFF.md can report truthful usage. Nothing reads this count to
decide whether to make the next call.

STATE: a local JSON file, not Neon -- same reasoning as before: a single
sequential loop counting its own calls doesn't need Postgres-grade
durability, and a local file surviving a script restart is enough.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_DEFAULT_STATE_PATH = Path(__file__).parent / "quota_pacer_state.json"


def _week_key(day: date) -> str:
    """ISO year+week, e.g. '2026-W33' -- Monday-start, unambiguous, no
    'depends when I last ran this' drift like a rolling 7-day window would
    have."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class QuotaPacer:
    state_path: Path = field(default_factory=lambda: _DEFAULT_STATE_PATH)

    def _load(self) -> dict | None:
        """Returns `None` specifically to mean "state is unreadable" --
        distinct from `{}` ("no file yet, legitimately fresh install").
        Callers must NOT treat these the same: a missing file is a normal
        first run (correctly starts at 0 used); an unreadable file means
        the count for this week is simply unknown -- see used_this_week."""
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _save(self, state: dict) -> None:
        """Write-to-temp-then-atomic-rename, not a direct write_text().
        write_text() is NOT atomic -- a process killed mid-write can leave
        a half-written, corrupt file. `os.replace` is atomic on both POSIX
        and Windows for a same-directory rename, so the old, valid state is
        never destroyed by an interrupted write -- readers see either the
        fully-old or fully-new file, never a partial one."""
        fd, tmp_path = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=".quota_pacer_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def used_this_week(self, today: date | None = None) -> int | None:
        """Real invocations already recorded in the calendar week
        containing `today` (defaults to actual today). `None` means the
        local state file is unreadable -- distinct from a legitimate `0`
        for a fresh week. This is pure observability now: unlike the old
        gated design, an unreadable count must never be reported as a
        number a caller could mistake for "budget exhausted, stop" -- it's
        simply unknown."""
        today = today or date.today()
        state = self._load()
        if state is None:
            return None
        return state.get(_week_key(today), 0)

    def record_cycle(self, today: date | None = None) -> int:
        """Call once per real Hermes invocation, immediately after it
        completes (not before -- an invocation that never happened should
        never be counted). Returns the new count for this week.

        Raises if local state is unreadable -- named loudly (not a bare
        AttributeError) so a caller that chooses to treat this as
        observability-only (see run_backtest.py's mark_invoked) can catch
        it deliberately rather than by accident. This function itself does
        NOT swallow the error: whether a broken counter should ever stop
        real work is the caller's decision, not this module's."""
        today = today or date.today()
        key = _week_key(today)
        state = self._load()
        if state is None:
            raise RuntimeError(
                "quota state unreadable — repair or delete the file to resume counting"
            )
        state[key] = state.get(key, 0) + 1
        self._save(state)
        return state[key]
