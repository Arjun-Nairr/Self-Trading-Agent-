"""
workflow_events.py -- best-effort, non-blocking workflow PROGRESS telemetry
for the dashboard-v2 Workflow page (contract:
trade-journal-mcp/workflow_contract.py; table workflow_events).

The trading thread only ENQUEUES an event, stamped with the real wall-clock
time the stage happened; one background thread writes batches through the
existing trade-journal container path. Telemetry can never change a trading
decision, state commit, risk check, or execution order:

  * `emit` never raises -- metrics may be passed as a callable, evaluated
    inside the same try, so even a bug building a count cannot escape;
  * the queue is bounded -- when full, events are DROPPED and counted, the
    trading thread never waits;
  * a failed batch is retried once (safe: (invocation_id, seq) makes a
    re-send a no-op), then counted and reported, never retried forever;
  * nothing in the harness ever reads these events back.

Each harness invocation gets a fresh invocation_id, so a resumed or retried
cycle's events stay distinguishable from an earlier interrupted attempt.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

_STOP = object()

# Named codes for the harness's own deterministic rejection strings (never
# model text); the trade-journal contract re-validates every code.
_NAMED = (("selector boundary", "selector_boundary"), ("not present in this cycle's candidate", "not_in_candidates"),
          ("only one queued decision per ticker", "duplicate_ticker"),
          ("does not equal required whole-share size", "quantity_mismatch"),
          ("unknown alias", "unknown_alias"), ("unsupported action", "unsupported_action"),
          ("missing/invalid quantity", "invalid_proposal"), ("quantity must be positive", "invalid_proposal"),
          ("missing stop/target", "invalid_proposal"), ("invalid entry_price", "invalid_proposal"),
          ("superseded", "superseded"), ("pre_fill_news_veto", "pre_fill_news_veto"),
          ("pre_fill_news_unavailable", "pre_fill_news_unavailable"))


def rejection_codes(text: str) -> list[str]:
    """Rule codes cited by a deterministic harness / risk-checker rejection
    string: its section citations plus a named harness code. At most 6."""
    t = text or ""
    codes = set(re.findall(r"§\d{1,2}[a-z]?(?:\.\d{1,2})?", t))
    low = t.lower()
    codes.update(code for needle, code in _NAMED if needle in low)
    return sorted(codes)[:6]


class _NullCycle:
    def emit(self, *a: Any, **k: Any) -> None:
        return None


class NullEmitter:
    """Telemetry off (tests, eval_suite, any caller that passes nothing)."""
    invocation_id = None

    def for_cycle(self, *a: Any, **k: Any) -> _NullCycle:
        return _NullCycle()

    def emit(self, *a: Any, **k: Any) -> None:
        return None

    def close(self, timeout: float = 0) -> dict:
        return {}


NULL_EMITTER = NullEmitter()


class _Cycle:
    def __init__(self, emitter: "WorkflowEmitter", ident: dict):
        self._e, self._ident = emitter, ident

    def emit(self, stage: str, status: str, metrics: "dict | Callable[[], dict] | None" = None,
             decision_id: str | None = None, proposal_key: str | None = None) -> None:
        self._e.emit(stage, status, metrics, decision_id=decision_id, proposal_key=proposal_key,
                     **self._ident)


class WorkflowEmitter:
    def __init__(self, run_id: str, writer: Callable[[list[dict]], dict], *, max_queue: int = 2000,
                 batch_max: int = 100, log: Callable[[str], None] = print):
        self.run_id = str(run_id)
        self.invocation_id = str(uuid.uuid4())
        self._writer, self._log, self._batch_max = writer, log, batch_max
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)
        self._seq_lock = threading.Lock()
        self._seq = 0
        self.stats = {"emitted": 0, "dropped": 0, "written": 0, "duplicates": 0, "rejected": 0,
                      "failed_batches": 0}
        self._thread = threading.Thread(target=self._run, name="workflow-telemetry", daemon=True)
        self._thread.start()

    def for_cycle(self, cycle_id: str, day_index: int, day_label: str, market_date: date) -> _Cycle:
        return _Cycle(self, {"cycle_id": str(cycle_id), "day_index": int(day_index),
                             "day_label": day_label, "market_date": market_date.isoformat()})

    def emit(self, stage: str, status: str, metrics: "dict | Callable[[], dict] | None" = None, *,
             decision_id: str | None = None, proposal_key: str | None = None, cycle_id: str | None = None,
             day_index: int | None = None, day_label: str | None = None,
             market_date: str | None = None) -> None:
        try:
            m = metrics() if callable(metrics) else (metrics or {})
            with self._seq_lock:
                seq, self._seq = self._seq, self._seq + 1
            event = {"run_id": self.run_id, "invocation_id": self.invocation_id, "seq": seq,
                     "stage": stage, "status": status,
                     "occurred_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "metrics": m, "decision_id": decision_id, "proposal_key": proposal_key,
                     "cycle_id": cycle_id, "day_index": day_index, "day_label": day_label,
                     "market_date": market_date}
            self._q.put_nowait(event)
            self.stats["emitted"] += 1
        except Exception:  # queue full, or a bug building metrics: never reaches trading
            self.stats["dropped"] += 1

    def _write(self, batch: list[dict]) -> None:
        for attempt in (1, 2):
            try:
                res = self._writer(batch)
                self.stats["written"] += int(res.get("inserted", 0))
                self.stats["duplicates"] += int(res.get("duplicates", 0))
                if res.get("rejected"):
                    self.stats["rejected"] += len(res["rejected"])
                    self._log(f"  WORKFLOW TELEMETRY: {len(res['rejected'])} event(s) rejected by the "
                              f"contract: {res['rejected'][:2]}")
                return
            except Exception as exc:
                if attempt == 2:
                    self.stats["failed_batches"] += 1
                    self._log(f"  WORKFLOW TELEMETRY FAILED ({len(batch)} event(s) not recorded; "
                              f"trading unaffected): {type(exc).__name__}: {str(exc)[:160]}")

    def _run(self) -> None:
        while True:
            item = self._q.get()
            stop = item is _STOP
            batch = [] if stop else [item]
            while len(batch) < self._batch_max:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                batch.append(nxt)
            if batch:
                self._write(batch)
            if stop:
                return

    def close(self, timeout: float = 60.0) -> dict:
        """Flush what is queued (bounded wait), stop the thread, return stats.
        Never raises; anything not flushed within `timeout` is counted."""
        try:
            self._q.put(_STOP, timeout=timeout)
            self._thread.join(timeout)
            if self._thread.is_alive():
                self._log("  WORKFLOW TELEMETRY: flush timed out; remaining events not recorded")
        except Exception:
            pass
        return dict(self.stats)


def journal_writer(run_in: Callable[[str, str], str], json_from: Callable[[str], dict]):
    """The production writer: one trade-journal container call per batch."""
    def write(batch: list[dict]) -> dict:
        payload = json.dumps(batch, separators=(",", ":"))
        script = f'''
import asyncio, json
from server import _record_workflow_events

async def main():
    res = await _record_workflow_events(json.loads({payload!r}))
    print(json.dumps(res))

asyncio.run(main())
'''
        return json_from(run_in("trade-journal", script))
    return write
