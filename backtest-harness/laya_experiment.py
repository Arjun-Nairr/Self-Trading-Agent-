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
      The private runtime boundary: a long-lived DOCKER container
      (network none, read-only root FS, read-only checkpoint mount, no
      capabilities, no secrets, only the three Laya modules inside) running
      laya_worker.py, spoken to over the docker client's stdin/stdout pipes --
      no port, no HTTP, no public endpoint. The checkpoint is loaded once per
      run (lazily, so a replay-only day never loads it). Docker unavailable =
      the experimental mode refuses to start; there is no host-worker fallback.
      Every reply is parsed strictly; any malformed reply fails closed.

SelectorOutcome carries real tickers and is private run state; its `event`
has no ticker fields (a failure `reason` is a truncated raw exception message, so it stays local: console and cycle checkpoint only, never Neon). Laya's scores, ranks, probabilities and confidences exist
only in the snapshot file -- they are never placed in any Hermes prompt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import subprocess
import threading
import uuid
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
# private Laya worker CONTAINER (controller side)
# --------------------------------------------------------------------------

# The worker runs ONLY inside a dedicated Docker container with networking
# disabled. There is no host-process worker and no fallback to one: if Docker
# or the image is unavailable the experimental mode refuses to start. The
# security boundary is the container (no network namespace route, no published
# port, read-only root filesystem, read-only checkpoint mount, no capabilities,
# no secrets, only the three Laya modules copied in). The Python-level socket
# deny inside laya_worker.py is a SECOND line of defence, not the boundary.

LAYA_WORKER_IMAGE = "hermes-laya-worker:1"
WORKER_CONTAINER_USER = "10001:10001"
# The checkpoint is mounted at the path huggingface_hub would give it, so the
# adapter can recover (and verify) the pinned commit from the directory name.
CHECKPOINT_MOUNT = f"/cache/models--convaiinnovations--laya/snapshots/{laya_adapter.LAYA_REVISION}"
WORKER_LABEL = "hermes.laya.worker=1"

_HANDSHAKE_KEYS = frozenset({"event", "package_version", "checkpoint", "revision", "question", "settings"})
_RECORD_KEYS = frozenset({"row_id", "review_probability", "answer_confidence"})
_ENV_PASSTHROUGH_FOR_DOCKER_CLI = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
)


def _docker_cli_env() -> dict[str, str]:
    """Scrubbed environment for the host-side `docker` CLI process. (The
    container's own environment is separate: `docker run` forwards nothing
    from the host unless `-e` is given, and none is except the offline flags.)"""
    return {k: os.environ[k] for k in _ENV_PASSTHROUGH_FOR_DOCKER_CLI if k in os.environ}


def docker_run_command(
    checkpoint_dir: str | os.PathLike[str], *, name: str, image: str = LAYA_WORKER_IMAGE,
) -> list[str]:
    """The exact `docker run` argv for the private worker. Pure.

    --network none ........ no network namespace route at all (no DNS/UDP/TCP,
                            no loopback to the host, no published port possible)
    -i (no -t, no -p) ..... JSON lines over stdin/stdout only
    --read-only + tmpfs ... read-only root FS; only a small noexec /tmp is writable
    -v ...:ro ............. checkpoint mounted read-only, nothing else mounted
    --cap-drop ALL, no-new-privileges, non-root user, pids limit
    -e (offline flags only)  no host environment, keys, or credentials forwarded
    """
    return [
        "docker", "run", "--rm", "-i", "--init",
        "--name", name, "--label", WORKER_LABEL,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "512", "--user", WORKER_CONTAINER_USER,
        "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1", "-e", "HF_HUB_DISABLE_TELEMETRY=1",
        "-e", "HOME=/tmp",
        "-v", f"{checkpoint_dir}:{CHECKPOINT_MOUNT}:ro",
        image, CHECKPOINT_MOUNT,
    ]


def check_container_isolation(inspect: Mapping[str, Any]) -> list[str]:
    """Pure: problems found in a `docker inspect` record of the running worker
    (empty list = the verifiable no-network / no-port / read-only configuration
    holds). Checked on the LIVE container, not assumed from the command line."""
    host = inspect.get("HostConfig") or {}
    cfg = inspect.get("Config") or {}
    net = inspect.get("NetworkSettings") or {}
    problems = []
    if host.get("NetworkMode") != "none":
        problems.append(f"NetworkMode is {host.get('NetworkMode')!r}, not 'none'")
    if set((net.get("Networks") or {})) - {"none"}:
        problems.append(f"attached to networks {sorted(net['Networks'])}")
    if net.get("Ports"):
        problems.append(f"ports present: {net['Ports']}")
    if host.get("PortBindings"):
        problems.append("published ports configured")
    if cfg.get("ExposedPorts"):
        problems.append(f"exposed ports: {sorted(cfg['ExposedPorts'])}")
    if not host.get("ReadonlyRootfs"):
        problems.append("root filesystem is not read-only")
    if host.get("Privileged"):
        problems.append("container is privileged")
    if "ALL" not in (host.get("CapDrop") or []):
        problems.append("capabilities not dropped")
    for m in inspect.get("Mounts") or []:
        if m.get("RW"):
            problems.append(f"writable mount at {m.get('Destination')}")
    if cfg.get("User") != WORKER_CONTAINER_USER:
        problems.append(f"user is {cfg.get('User')!r}")
    return problems


def _run_docker(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          env=_docker_cli_env(), encoding="utf-8")


def verify_docker_worker_available(
    checkpoint_dir: str | os.PathLike[str], image: str = LAYA_WORKER_IMAGE,
) -> None:
    """Fail closed BEFORE a run starts: SystemExit unless the Docker daemon is
    reachable, the worker image exists, and the checkpoint directory is a
    local snapshot of the pinned commit. There is no host-worker fallback."""
    try:
        r = _run_docker(["info", "--format", "{{.ServerVersion}}"], 30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"laya experimental mode requires Docker, which is unavailable "
                         f"({type(exc).__name__}: {exc}). Refusing to start; there is no host-worker fallback.")
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("laya experimental mode requires a running Docker daemon "
                         f"(docker info failed: {r.stderr.strip()[-200:]}). Refusing to start; "
                         "there is no host-worker fallback.")
    try:
        r = _run_docker(["image", "inspect", image, "--format", "{{.Id}}"], 30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"could not inspect the Laya worker image {image!r}: {exc}")
    if r.returncode != 0:
        raise SystemExit(
            f"Laya worker image {image!r} not found. Build it once with:\n"
            "  docker build -f backtest-harness/laya_worker_image/Dockerfile "
            f"-t {image} backtest-harness")
    p = Path(checkpoint_dir)
    if not (p / "model.safetensors").is_file():
        raise SystemExit(f"--laya-checkpoint {str(p)!r} has no model.safetensors")
    if p.resolve().name != laya_adapter.LAYA_REVISION or p.resolve().parent.name != "snapshots":
        raise SystemExit(
            f"--laya-checkpoint must be the huggingface_hub snapshot directory of the pinned commit "
            f"(.../snapshots/{laya_adapter.LAYA_REVISION}); got {str(p.resolve())!r}")


# -- strict reply parsing (pure) ---------------------------------------------------------

def _is_json_float(x: Any) -> bool:
    return type(x) is float  # excludes bool, int, str, None, and every subclass


def parse_handshake(reply: Any, want: ScorerMetadata) -> None:
    """The worker's ready line must be exactly the expected fields and match
    the pinned scorer identity. Raises ScorerError otherwise."""
    if not isinstance(reply, dict) or set(reply) != _HANDSHAKE_KEYS:
        raise ScorerError(f"worker handshake has unexpected fields: {sorted(reply) if isinstance(reply, dict) else type(reply).__name__}")
    if (reply["event"] != "ready" or reply["package_version"] != want.package_version
            or reply["checkpoint"] != want.checkpoint or reply["revision"] != want.revision
            or reply["question"] != want.question or reply["settings"] != want.settings):
        raise ScorerError("worker handshake does not match the pinned scorer")


def parse_score_reply(reply: Any, request_id: int, expected_row_ids: Sequence[str]) -> list[ScoreRecord]:
    """Strictly parse one worker reply into ScoreRecords. EVERY malformed shape
    raises ScorerError (never a raw KeyError/TypeError/ValueError). Accepts only:

      {"id": <int == request_id>, "records": [ {row_id, review_probability,
        answer_confidence} x len(expected_row_ids) ]}

    - envelope: exactly the keys id+records (an error reply is exactly id+error,
      error a string, and is reported as a ScorerError);
    - each record: exactly the three keys; row_id a string equal to the expected
      id at the same position (so duplicates, reordered, unknown IDs and count
      mismatches are all rejected); both numbers genuine JSON floats -- not
      bool, int, str, or null -- finite, within [0, 1].
    """
    if not isinstance(reply, dict):
        raise ScorerError(f"worker reply is a {type(reply).__name__}, not an object")
    keys = set(reply)
    if keys == {"id", "error"}:
        if type(reply["id"]) is not int or not isinstance(reply["error"], str):
            raise ScorerError("worker error reply is malformed")
        raise ScorerError(f"laya worker error: {reply['error'][:300]}")
    if keys != {"id", "records"}:
        raise ScorerError(f"worker reply has unexpected fields: {sorted(keys)}")
    if type(reply["id"]) is not int or reply["id"] != request_id:
        raise ScorerError(f"worker answered request {reply['id']!r}, expected {request_id}")
    records = reply["records"]
    if not isinstance(records, list):
        raise ScorerError("worker 'records' is not a list")
    if len(records) != len(expected_row_ids):
        raise ScorerError(f"worker returned {len(records)} records for {len(expected_row_ids)} cards")
    ids = [r.get("row_id") if isinstance(r, dict) else None for r in records]
    if len({i for i in ids if isinstance(i, str)}) != len([i for i in ids if isinstance(i, str)]):
        raise ScorerError("worker returned duplicate row_ids")
    out: list[ScoreRecord] = []
    for i, (rec, want_id) in enumerate(zip(records, expected_row_ids)):
        if not isinstance(rec, dict):
            raise ScorerError(f"record {i} is not an object")
        if set(rec) != _RECORD_KEYS:
            raise ScorerError(f"record {i} has fields {sorted(rec)}; expected exactly {sorted(_RECORD_KEYS)}")
        if not isinstance(rec["row_id"], str):
            raise ScorerError(f"record {i} row_id is not a string")
        if rec["row_id"] != want_id:
            raise ScorerError(f"record {i} is for {rec['row_id']!r}, expected {want_id!r} "
                              "(unknown, duplicate, or reordered row_id)")
        for field_name in ("review_probability", "answer_confidence"):
            v = rec[field_name]
            if not _is_json_float(v):
                raise ScorerError(f"record {i} {field_name} is {type(v).__name__}, not a JSON float")
            if not (math.isfinite(v) and 0.0 <= v <= 1.0):
                raise ScorerError(f"record {i} {field_name} {v!r} is not finite in [0, 1]")
        out.append(ScoreRecord(rec["row_id"], rec["review_probability"], rec["answer_confidence"]))
    return out


class LayaWorkerScorer:
    """CardScorer that talks to the private worker CONTAINER over its stdin/
    stdout pipes. The model is loaded once, on first use, and reused for every
    later day of the run; one `score()` call is one batched request over the
    daily eligible cards (never a call per card). Every reply is parsed by
    parse_score_reply, so any malformed reply fails closed with a ScorerError.

    `worker_cmd` is a TEST SEAM only (a scripted fake worker); the production
    command is always docker_run_command -- there is no host-worker mode."""

    def __init__(
        self, checkpoint_dir: str | os.PathLike[str], *, image: str = LAYA_WORKER_IMAGE,
        worker_cmd: Sequence[str] | None = None, log_path: Path | None = None,
        startup_timeout: float = 600.0, request_timeout: float = 600.0,
    ) -> None:
        self._checkpoint = str(checkpoint_dir)
        self._image = image
        self._worker_cmd = list(worker_cmd) if worker_cmd else None
        self._log_path = log_path
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self.metadata: ScorerMetadata = laya_adapter.expected_metadata()
        self._proc: subprocess.Popen[str] | None = None
        self._container: str | None = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._log = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def _start(self) -> None:
        if self._worker_cmd:
            cmd, self._container = self._worker_cmd, None
        else:
            self._container = f"laya-worker-{uuid.uuid4().hex[:12]}"
            cmd = docker_run_command(self._checkpoint, name=self._container, image=self._image)
        if self._log_path:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self._log_path, "ab") if self._log_path else subprocess.DEVNULL
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            text=True, encoding="utf-8", bufsize=1, cwd=HERE, env=_docker_cli_env(),
        )
        proc = self._proc

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._lines.put(line)
            self._lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        try:
            parse_handshake(self._read(self._startup_timeout), self.metadata)
            if self._container:  # verify the LIVE container's isolation, not just the command line
                r = _run_docker(["inspect", self._container], 30)
                problems = (["docker inspect failed"] if r.returncode != 0
                            else check_container_isolation(json.loads(r.stdout)[0]))
                if problems:
                    raise ScorerError("worker container isolation check failed: " + "; ".join(problems))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        proc, self._proc = self._proc, None
        container, self._container = self._container, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        if container:  # guarantee the container is gone even if the CLI was killed
            try:
                _run_docker(["rm", "-f", container], 30)
            except Exception:
                pass
        if self._log not in (None, subprocess.DEVNULL):
            try:
                self._log.close()
            except OSError:
                pass
        self._log = None

    def _read(self, timeout: float) -> Any:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise ScorerError(f"laya worker did not answer within {timeout:g}s") from None
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
        ids = [c.row_id for c in cards]
        if len(set(ids)) != len(ids):
            raise ScorerError("duplicate row_id among the cards to score")
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
        try:
            return parse_score_reply(reply, self._next_id, ids)
        except ScorerError:
            # A malformed/erroring reply means the worker cannot be trusted
            # for this exchange: drop it (a later call starts a fresh one).
            if not (isinstance(reply, dict) and set(reply) == {"id", "error"}):
                self.close()
            raise
