"""
laya_injection.py -- SHADOW-ONLY Laya prompt-injection monitor for the
backtest controller (opt-in: run_backtest.py --injection-monitor).

It asks Laya one fixed question (laya_injection_worker.QUESTION) of each
masked news snippet exactly as it is rendered into a Hermes prompt --
decision-time headlines and pre-fill headline/summary lines, produced by
run_backtest's own renderers (_decision_news_texts / _prefill_news_text, the
same functions its prompt builders call) -- and writes a
PRIVATE, write-once record per (run, day, path). It never blocks, rewrites or
removes news; never changes a prompt, selection, risk check, trade or exit;
never replaces an existing protection; and an unflagged snippet is NOT
evidence of safety. Nothing it produces is shown to Hermes.

Each record: the snippet's SHA-256 fingerprint and length, the question
version, model/image identity, Laya's P(injection attempt) ("suspicion"),
status flagged / not_flagged / unavailable, elapsed time, and a bounded
reason. FLAG_THRESHOLD 0.5 is an explicitly UNCALIBRATED display convention,
not a security guarantee.

Every failure -- Docker or the image missing, the worker failing to start,
a deadline, a malformed reply, an oversized snippet -- becomes `unavailable`
and never reaches the trading cycle. After a worker failure the monitor stops
trying for the rest of the session (so one hung start cannot stall every
later day); the selector and every existing check are untouched.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import laya_injection_worker as w
from laya_experiment import LayaWorkerScorer, _run_docker
from laya_selector import ScorerError, ScorerMetadata

MONITOR_IMAGE = "hermes-laya-injection-monitor:1"
FLAG_THRESHOLD = 0.5            # uncalibrated display convention only
MAX_SNIPPET_CHARS = 4000        # cheap host-side bound; the worker's token check is authoritative
REQUEST_TIMEOUT_S = 120.0       # per batch, after the model is loaded
STARTUP_TIMEOUT_S = 600.0       # one model load per session
RECORD_SCHEMA = 1
_RECORD_KEYS = frozenset({"suspicion", "answer_confidence", "state_tokens", "truncated"})


def expected_metadata() -> ScorerMetadata:
    import laya_adapter
    return ScorerMetadata(provider="laya", package_version=laya_adapter.LAYA_PACKAGE_VERSION,
                          checkpoint=laya_adapter.LAYA_MODEL_ID, revision=laya_adapter.LAYA_REVISION,
                          question=w.QUESTION, settings=w.settings_json())


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_classify_reply(reply: Any, request_id: int, n: int) -> tuple[int, list[dict]]:
    """Strict: exactly {id, room, records}; n records of exactly the four
    keys; a read record has genuine finite JSON floats in [0, 1]; a truncated
    one has nulls. Anything else raises ScorerError."""
    if not isinstance(reply, dict):
        raise ScorerError(f"monitor reply is a {type(reply).__name__}")
    if set(reply) == {"id", "error"} and isinstance(reply["error"], str):
        raise ScorerError(f"monitor worker error: {reply['error'][:200]}")
    if set(reply) != {"id", "room", "records"}:
        raise ScorerError(f"monitor reply has fields {sorted(reply)}")
    if type(reply["id"]) is not int or reply["id"] != request_id:
        raise ScorerError(f"monitor answered request {reply['id']!r}, expected {request_id}")
    room, records = reply["room"], reply["records"]
    if type(room) is not int or room <= 0:
        raise ScorerError("monitor room is not a positive int")
    if not isinstance(records, list) or len(records) != n:
        raise ScorerError(f"monitor returned {len(records) if isinstance(records, list) else '?'} records for {n}")
    for i, r in enumerate(records):
        if not isinstance(r, dict) or set(r) != _RECORD_KEYS:
            raise ScorerError(f"monitor record {i} malformed")
        if type(r["truncated"]) is not bool or type(r["state_tokens"]) is not int or r["state_tokens"] < 0:
            raise ScorerError(f"monitor record {i} truncated/state_tokens malformed")
        nums = (r["suspicion"], r["answer_confidence"])
        if r["truncated"]:
            if nums != (None, None):
                raise ScorerError(f"monitor record {i} scored a truncated text")
        elif not all(type(v) is float and math.isfinite(v) and 0.0 <= v <= 1.0 for v in nums):
            raise ScorerError(f"monitor record {i} has a non-finite or out-of-range score")
    return room, records


class InjectionWorker(LayaWorkerScorer):
    """The selector's container lifecycle (docker run --network none,
    read-only, live `docker inspect` isolation check, one model load,
    `docker rm -f` on close), pointed at the monitor image and its own
    handshake identity. `worker_cmd` is a test seam only."""

    def __init__(self, checkpoint_dir: str | os.PathLike[str], *, image: str = MONITOR_IMAGE,
                 worker_cmd: Sequence[str] | None = None, log_path: Path | None = None,
                 startup_timeout: float = STARTUP_TIMEOUT_S, request_timeout: float = REQUEST_TIMEOUT_S):
        super().__init__(checkpoint_dir, image=image, worker_cmd=worker_cmd, log_path=log_path,
                         startup_timeout=startup_timeout, request_timeout=request_timeout)
        self.metadata = expected_metadata()

    def classify(self, texts: Sequence[str]) -> tuple[int, list[dict]]:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        try:
            self._proc.stdin.write(json.dumps({"id": self._next_id, "texts": list(texts)}) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self.close()
            raise ScorerError(f"could not send texts to the monitor worker: {exc}") from exc
        reply = self._read(self._request_timeout)
        try:
            return parse_classify_reply(reply, self._next_id, len(texts))
        except ScorerError:
            self.close()
            raise


class InjectionMonitor:
    """`worker` needs only classify(texts) -> (room, records) and close()."""

    def __init__(self, worker: Any, out_dir: Path, run_id: str, *, image: str = MONITOR_IMAGE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.worker, self.out_dir, self.run_id, self.image = worker, Path(out_dir), run_id, image
        self._clock = clock
        self._broken: str | None = None
        self._image_id: str | None = None

    def close(self) -> None:
        try:
            self.worker.close()
        except Exception:
            pass

    def _identity(self) -> dict:
        if self._image_id is None and isinstance(self.worker, InjectionWorker) and self.worker._worker_cmd is None:
            try:
                r = _run_docker(["image", "inspect", self.image, "--format", "{{.Id}}"], 30)
                self._image_id = r.stdout.strip() if r.returncode == 0 else ""
            except Exception:
                self._image_id = ""
        m = expected_metadata()
        return {"question_version": w.QUESTION_VERSION, "question": w.QUESTION, "package_version": m.package_version,
                "checkpoint": m.checkpoint, "revision": m.revision, "image": self.image,
                "image_id": self._image_id or None}

    def _classify_all(self, texts: list[str]) -> list[dict]:
        """One record per text, in order. Never raises."""
        out: list[dict | None] = [None] * len(texts)
        send = []
        for i, t in enumerate(texts):
            if len(t) > MAX_SNIPPET_CHARS:
                out[i] = {"status": "unavailable", "reason": f"oversized: {len(t)} chars > {MAX_SNIPPET_CHARS}"}
            else:
                send.append(i)
        for start in range(0, len(send), w.MAX_TEXTS_PER_REQUEST):
            chunk = send[start:start + w.MAX_TEXTS_PER_REQUEST]
            if self._broken:
                for i in chunk:
                    out[i] = {"status": "unavailable", "reason": f"monitor disabled after failure: {self._broken}"}
                continue
            t0 = self._clock()
            try:
                room, recs = self.worker.classify([texts[i] for i in chunk])
            except Exception as exc:
                self._broken = f"{type(exc).__name__}: {str(exc)[:160]}"
                for i in chunk:
                    out[i] = {"status": "unavailable", "reason": self._broken}
                continue
            ms = round((self._clock() - t0) * 1000.0, 1)
            for i, r in zip(chunk, recs):
                if r["truncated"]:
                    out[i] = {"status": "unavailable", "state_tokens": r["state_tokens"], "batch_ms": ms,
                              "reason": f"exceeds token room: {r['state_tokens']} > {room} state tokens"}
                else:
                    out[i] = {"status": "flagged" if r["suspicion"] >= FLAG_THRESHOLD else "not_flagged",
                              "suspicion": r["suspicion"], "answer_confidence": r["answer_confidence"],
                              "state_tokens": r["state_tokens"], "batch_ms": ms}
        return [{"sha256": fingerprint(t), "chars": len(t), **rec} for t, rec in zip(texts, out)]

    def record(self, path: str, day: str, key: str, texts: Sequence[str]) -> dict:
        """Classify `texts` (the exact rendered snippets) once and store the
        private write-once record; a record that already exists for this
        (run, day, key) is REPLAYED, never re-scored. Returns a count summary
        (no text, no scores). Never raises."""
        try:
            f = self.out_dir / self.run_id / f"{day}-{key}.json"
            if f.exists():
                stored = json.loads(f.read_text(encoding="utf-8"))
                return {**_counts(stored["records"]), "replayed": True}
            t0 = self._clock()
            records = self._classify_all(list(texts))
            body = {"schema": RECORD_SCHEMA, "run_id": self.run_id, "day": day, "path": path, "key": key,
                    "monitor": self._identity(), "flag_threshold": FLAG_THRESHOLD,
                    "threshold_note": "uncalibrated display convention; not a security guarantee",
                    "shadow_only": True, "elapsed_ms": round((self._clock() - t0) * 1000.0, 1),
                    "records": records}
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
            try:
                os.link(tmp, f)            # exclusive publish: an existing record is never replaced
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
            return {**_counts(records), "replayed": False}
        except Exception as exc:
            return {"snippets": len(texts), "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _counts(records: Sequence[Mapping[str, Any]]) -> dict:
    s = [r["status"] for r in records]
    return {"snippets": len(s), "flagged": s.count("flagged"), "not_flagged": s.count("not_flagged"),
            "unavailable": s.count("unavailable")}

