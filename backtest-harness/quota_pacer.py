"""
quota_pacer.py — stops the backtest from blowing through the shared weekly
ChatGPT Plus quota.

THE PROBLEM: live trading self-paces (the market's only open certain hours,
so there's a hard ceiling on how many decision cycles happen per real
week). Backtesting has no such brake -- it can compress months of
simulated trading into an afternoon, which could burn an entire week's
quota in hours and starve every other use of the same ChatGPT account for
the rest of that week.

THE APPROACH: count real cycles against a real calendar week (Monday-
start, ISO week number -- an unambiguous, standard definition of "a week",
not "7 days since I last checked"). Once the count hits the budget, stop
signaling cycles as allowed until the next real week starts.

STATE: a local JSON file, not Neon. This is NOT the same durability problem
`decision_id` idempotency solves in trade-journal-mcp -- that needed
Postgres because multiple systems (broker + journal) had to agree on one
trade's state after a crash. This is a single sequential loop counting its
own calls; a local file surviving a script restart is enough, and it
avoids making a network call to Neon just to decide whether to make a
network call to OpenAI.

THE UNKNOWN, STATED PLAINLY: the real weekly quota ceiling is undocumented
by OpenAI. `DEFAULT_WEEKLY_BUDGET` is a starting guess from the one real
reference point this project has (~32 decisions/week was established as
known-safe for live trading), not a measured fact. Watch real usage on the
first backtest run and adjust -- see `record_cycle`'s return value.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# ponytail: still a guess, not a measured ceiling -- but a less blind one
# than 32. Raised after burning 32 real cycles in one sitting (2026-08-21)
# and confirming directly in the ChatGPT app that weekly usage hadn't
# moved -- real evidence the true limit is well above 32, just not proof
# of exactly where. Sized as a step up worth actually testing against,
# not a new arbitrary ceiling; watch real usage again at this level and
# adjust -- same process as before, better starting point.
DEFAULT_WEEKLY_BUDGET = 175

_DEFAULT_STATE_PATH = Path(__file__).parent / "quota_pacer_state.json"


def _week_key(day: date) -> str:
    """ISO year+week, e.g. '2026-W33' -- Monday-start, unambiguous, no
    'depends when I last ran this' drift like a rolling 7-day window would
    have."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class QuotaPacer:
    budget: int = DEFAULT_WEEKLY_BUDGET
    state_path: Path = field(default_factory=lambda: _DEFAULT_STATE_PATH)

    def _load(self) -> dict | None:
        """Returns `None` specifically to mean "state is unreadable" --
        distinct from `{}` ("no file yet, legitimately fresh install").
        Callers must NOT treat these the same: a missing file is a normal
        first run (correctly starts at 0 used); an unreadable file is a
        corruption signal that must fail CLOSED (budget-exhausted), not
        fail open as a fresh week -- see used_this_week/remaining_this_week.
        """
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _save(self, state: dict) -> None:
        """Write-to-temp-then-atomic-rename, not a direct write_text().
        write_text() is NOT atomic -- a process killed mid-write (the exact
        scenario this pacer exists to survive: getting cut off for hitting
        quota mid-cycle) can leave a half-written, corrupt file. `os.replace`
        is atomic on both POSIX and Windows for a same-directory rename, so
        the old, valid state is never destroyed by an interrupted write --
        readers see either the fully-old or fully-new file, never a partial
        one. This is what actually prevents the corruption case, not just
        the fail-closed fallback below (belt AND braces, not either/or)."""
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

    def used_this_week(self, today: date | None = None) -> int:
        """Cycles already recorded in the real calendar week containing
        `today` (defaults to actual today). Unreadable state reports as
        FULLY USED (== budget), not 0 -- fail closed, see _load."""
        today = today or date.today()
        state = self._load()
        if state is None:
            return self.budget
        return state.get(_week_key(today), 0)

    def remaining_this_week(self, today: date | None = None) -> int:
        return max(0, self.budget - self.used_this_week(today))

    def can_run_cycle(self, today: date | None = None) -> bool:
        """The check a backtest driver makes before EVERY cycle -- real
        gate, not advisory. False means stop for this real week, not just
        a warning to log."""
        return self.remaining_this_week(today) > 0

    def record_cycle(self, today: date | None = None) -> int:
        """Call exactly once per real Hermes invocation, immediately after
        it completes (not before -- a cycle that never ran should never be
        counted). Returns the new count for this week, so the caller can
        log/alert as it approaches budget without a separate query."""
        today = today or date.today()
        key = _week_key(today)
        state = self._load()
        state[key] = state.get(key, 0) + 1
        self._save(state)
        return state[key]

    def next_week_starts(self, today: date | None = None) -> date:
        """When a paused backtest can resume -- the coming Monday."""
        today = today or date.today()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_until_monday)
