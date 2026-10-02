"""
laya_experiment.py -- the opt-in wiring layer for the experimental fixed-30
Laya selector (docs/scopes/LAYA_SELECTOR_V1.md, Phase 2). Nothing in the
default path imports or calls it: run_backtest.py only constructs a
SelectorContext when `--selector-mode laya_fixed30_experimental` is passed.

  SelectorContext.select_for_day(day, held)
      eligible = fixed-30 names not already held  ->  point-in-time cards
      -> ONE batched local Laya call -> deterministic top-10 -> an immutable
      per-(run, date) snapshot. If a valid snapshot already exists for the
      date it is REPLAYED (never re-scored; the model is not even started).
      Any failure (history fetch, card build, worker, scoring, snapshot
      replay mismatch) FAILS CLOSED for that day's NEW ENTRIES only: the
      outcome is an explicit "failed_closed" with a reason and an empty
      selection. Held-position exit management is outside this class and is
      never affected.

  LayaWorkerScorer
      The private local runtime boundary: a long-lived child process running
      the SEPARATE Laya virtualenv's python (laya_worker.py), spoken to over
      its own stdin/stdout pipes -- no port, no HTTP, no public endpoint,
      network blocked inside the worker, checkpoint loaded once per run
      (lazily, so a replay-only day never loads it), scrubbed environment.

SelectorOutcome carries real tickers and is private run state; its `event`
has no ticker fields (a failure `reason` is a truncated raw exception message, so it stays local: console and cycle checkpoint only, never Neon). Laya's scores, ranks, probabilities and confidences exist
only in the snapshot file -- they are never placed in any Hermes prompt.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

import laya_adapter
from laya_cards import build_day_cards
from laya_selector import (
    MAX_SELECTED, SELECTOR_VERSION, STATUS_DATA_UNAVAILABLE, STATUS_NOT_SELECTED,
    STATUS_SELECTED, CandidateCard, CardScorer, FixedUniverse, ScoreRecord, ScorerError,
    ScorerMetadata, SelectionSnapshot, SnapshotConflict, build_snapshot, load_snapshot,
    make_row_ids, score_complete_cards, snapshot_to_json, write_snapshot,
)

HERE = Path(__file__).resolve().parent

SELECTOR_MODE_OFF = "off"
SELECTOR_MODE_LAYA_FIXED30 = "laya_fixed30_experimental"
SELECTOR_MODES = (SELECTOR_MODE_OFF, SELECTOR_MODE_LAYA_FIXED30)

# Git-ignored runtime artifacts (see .gitignore): private per-run snapshots.
SNAPSHOT_DIR = HERE / ".selector_snapshots"

STATUS_FAILED_CLOSED = "failed_closed"
STATUS_OK = "selected"


class SelectorReplayError(RuntimeError):
    """A stored snapshot does not match this run (universe, seed, scorer,
    or eligible set) and therefore must not be trusted."""


# --------------------------------------------------------------------------
# outcome
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectorOutcome:
    ok: bool
    selected: tuple[str, ...]                       # real tickers, rank order (private)
    status_by_ticker: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)
    event: Mapping[str, Any] = field(default_factory=dict)  # no ticker fields, JSON-safe, local-only

    def to_record(self) -> dict[str, Any]:
        """JSON-safe form stored in the cycle checkpoint so a replay of an
        already-decided cycle reports the SAME selection/status it ran with."""
        return {
            "ok": self.ok, "selected": list(self.selected),
            "status_by_ticker": {t: list(v) for t, v in self.status_by_ticker.items()},
            "event": dict(self.event),
        }

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> "SelectorOutcome":
        return cls(
            ok=bool(rec["ok"]), selected=tuple(rec["selected"]),
            status_by_ticker={t: (v[0], v[1]) for t, v in rec["status_by_ticker"].items()},
            event=dict(rec["event"]),
        )


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------

class SelectorContext:
    def __init__(
        self, *, universe: FixedUniverse, seed: int, run_id: str, snapshot_dir: Path,
        scorer: CardScorer,
        fetch_history: Callable[[Sequence[str], date], Mapping[str, Any]],
    ) -> None:
        self.universe = universe
        self.seed = seed
        self.run_id = run_id
        self.snapshot_dir = Path(snapshot_dir)
        self.scorer = scorer
        self._fetch_history = fetch_history
        self.row_ids = make_row_ids(universe.symbols, seed, universe.hash)
        self._ticker_by_row = {rid: t for t, rid in self.row_ids.items()}

    # -- public ------------------------------------------------------------

    def select_for_day(self, day: date, held: Collection[str]) -> SelectorOutcome:
        eligible = [s for s in self.universe.symbols if s not in held]
        base = {
            "mode": SELECTOR_MODE_LAYA_FIXED30,
            "universe_hash": self.universe.hash,
            "eligible": len(eligible),
            "excluded_held_or_pending": len(self.universe.symbols) - len(eligible),
        }
        try:
            snap = load_snapshot(self.snapshot_dir, self.run_id, day.isoformat())
            replayed = snap is not None
            if snap is None:
                snap = self._build_and_store(day, eligible)
            return self._outcome(snap, day, eligible, replayed, base)
        except Exception as exc:  # fail closed for NEW entries on this day only
            event = {**base, "status": STATUS_FAILED_CLOSED, "replayed": False,
                     "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
                     "selected": 0}
            return SelectorOutcome(ok=False, selected=(), status_by_ticker={}, event=event)

    def close(self) -> None:
        closer = getattr(self.scorer, "close", None)
        if closer:
            closer()

    # -- internals ---------------------------------------------------------

    def _build_and_store(self, day: date, eligible: Sequence[str]) -> SelectionSnapshot:
        history = self._fetch_history(eligible, day) if eligible else {}
        ids = {s: self.row_ids[s] for s in eligible}
        cards, latest, _reasons = build_day_cards(history, eligible, day, ids)
        scores = score_complete_cards(self.scorer, cards)
        snap = build_snapshot(
            universe=self.universe, scorer=self.scorer.metadata, as_of=day, seed=self.seed,
            cards=cards, scores=scores, latest_bar_dates=latest, k=MAX_SELECTED,
        )
        try:
            write_snapshot(self.snapshot_dir, self.run_id, snap)
        except SnapshotConflict:
            # Another writer stored a different snapshot first: it is THE
            # record for this date. Never overwrite it; replay it instead.
            existing = load_snapshot(self.snapshot_dir, self.run_id, day.isoformat())
            if existing is None:
                raise
            return existing
        return snap

    def _outcome(self, snap: SelectionSnapshot, day: date, eligible: Sequence[str],
                 replayed: bool, base: Mapping[str, Any]) -> SelectorOutcome:
        mismatches = []
        if snap.as_of != day.isoformat():
            mismatches.append("as_of")
        if snap.universe_hash != self.universe.hash:
            mismatches.append("universe_hash")
        if snap.seed != self.seed:
            mismatches.append("seed")
        if snap.selector_version != SELECTOR_VERSION:
            mismatches.append("selector_version")
        if snap.k != MAX_SELECTED:
            mismatches.append("k")
        if snap.scorer != self.scorer.metadata:
            mismatches.append("scorer_metadata")
        if {r.row_id for r in snap.rows} != {self.row_ids[s] for s in eligible}:
            mismatches.append("eligible_set")
        if mismatches:
            raise SelectorReplayError(
                "snapshot_replay_mismatch: stored snapshot disagrees with this run on "
                + ", ".join(mismatches))

        status: dict[str, tuple[str, str | None]] = {}
        selected: list[tuple[int, str]] = []
        counts = {STATUS_SELECTED: 0, STATUS_NOT_SELECTED: 0, STATUS_DATA_UNAVAILABLE: 0}
        for row in snap.rows:
            ticker = self._ticker_by_row[row.row_id]
            status[ticker] = (row.status, row.reason)
            counts[row.status] += 1
            if row.status == STATUS_SELECTED:
                selected.append((row.rank or 0, ticker))
        selected.sort()
        event = {
            **base, "status": STATUS_OK, "replayed": replayed, "reason": None,
            "scored": counts[STATUS_SELECTED] + counts[STATUS_NOT_SELECTED],
            "data_unavailable": counts[STATUS_DATA_UNAVAILABLE],
            "selected": counts[STATUS_SELECTED],
            "snapshot_inputs_digest": snap.inputs_digest,
            "snapshot_sha256": hashlib.sha256(
                snapshot_to_json(snap).encode("utf-8")).hexdigest(),
        }
        return SelectorOutcome(ok=True, selected=tuple(t for _, t in selected),
                               status_by_ticker=status, event=event)


# --------------------------------------------------------------------------
# private local Laya worker (controller side)
# --------------------------------------------------------------------------

_ENV_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                    "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE")


def _worker_env() -> dict[str, str]:
    """Scrubbed environment for the Laya child: only what Python/torch need
    to start, plus offline switches. No API keys, no Neon/Alpaca/Hermes
    credentials from the controller's environment reach it."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8"})
    return env


class LayaWorkerScorer:
    """CardScorer that talks to a long-lived private worker process. The
    model is loaded once, on first use, and reused for every later day of the
    run; one `score()` call is one batched request over the daily eligible
    cards (never a call per card)."""

    def __init__(
        self, python_exe: str | os.PathLike[str], checkpoint_dir: str | os.PathLike[str], *,
        worker_cmd: Sequence[str] | None = None, log_path: Path | None = None,
        startup_timeout: float = 600.0, request_timeout: float = 600.0,
    ) -> None:
        self._python = str(python_exe)
        self._checkpoint = str(checkpoint_dir)
        self._worker_cmd = list(worker_cmd) if worker_cmd else None
        self._log_path = log_path
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self.metadata: ScorerMetadata = laya_adapter.expected_metadata()
        self._proc: subprocess.Popen[str] | None = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._log = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def _start(self) -> None:
        cmd = self._worker_cmd or [self._python, str(HERE / "laya_worker.py"), self._checkpoint]
        if self._log_path:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self._log_path, "ab") if self._log_path else subprocess.DEVNULL
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            text=True, encoding="utf-8", bufsize=1, cwd=HERE, env=_worker_env(),
        )
        proc = self._proc

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._lines.put(line)
            self._lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        ready = self._read(self._startup_timeout)
        want = self.metadata
        if (ready.get("event") != "ready" or ready.get("package_version") != want.package_version
                or ready.get("checkpoint") != want.checkpoint
                or ready.get("revision") != want.revision
                or ready.get("question") != want.question
                or ready.get("settings") != want.settings):
            self.close()
            raise ScorerError(f"laya worker handshake does not match the pinned scorer: {ready!r}")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if self._log not in (None, subprocess.DEVNULL):
            try:
                self._log.close()
            except OSError:
                pass
        self._log = None

    def _read(self, timeout: float) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise ScorerError(f"laya worker did not answer within {timeout:.0f}s") from None
        if line is None:
            self.close()
            raise ScorerError("laya worker exited unexpectedly (see its log)")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            self.close()
            raise ScorerError(f"laya worker sent a non-JSON line: {line[:120]!r}") from None

    # -- CardScorer --------------------------------------------------------

    def score(self, cards: Sequence[CandidateCard]) -> list[ScoreRecord]:
        if not cards:
            return []
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        req = {"id": self._next_id, "cards": [{"row_id": c.row_id, **c.model_state()} for c in cards]}
        try:
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self.close()
            raise ScorerError(f"could not send cards to the laya worker: {exc}") from exc
        reply = self._read(self._request_timeout)
        if reply.get("id") != self._next_id:
            self.close()
            raise ScorerError(f"laya worker answered request {reply.get('id')!r}, expected {self._next_id}")
        if "error" in reply:
            raise ScorerError(f"laya worker error: {reply['error']}")
        return [ScoreRecord(r["row_id"], float(r["review_probability"]), float(r["answer_confidence"]))
                for r in reply["records"]]
