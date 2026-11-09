"""Controller-side tracing: one root trace per live cycle, nested spans, and the ids that connect the Hermes
process (the plugin in the gateway container) to the controller's `hermes_research` span.

Strictly observational. Nothing returned from here is read by trading code; every method swallows its own
errors (a `with` block's *trading* exception is recorded and then propagates untouched); network export is
the bounded background exporter in `otlp.py`. `NullTracer` is the default and does nothing at all.

Credentials live in a file OUTSIDE the repository and the environment (default
`<state-dir>/tracing.env`, KEY=VALUE lines) and are never put in `os.environ`, so they are not inherited by
any subprocess. See docs/live_tracing_runbook.md.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

try:                                   # imported as a package (live/ on sys.path) or beside the plugin
    from tracing import otlp
except ImportError:                    # pragma: no cover
    import otlp  # type: ignore

ENV_FILE = "tracing.env"


class _NullSpan:
    id = None
    trace_id = None

    def set(self, **kw: Any) -> "_NullSpan":
        return self

    def end(self, exc: BaseException | None = None) -> None:
        return None

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class NullTracer:
    enabled = False
    exporter = None

    def trace(self, name: str, **kw: Any) -> _NullSpan:
        return _NullSpan()

    span = trace

    def event(self, name: str, **data: Any) -> None:
        return None

    def child_env(self) -> dict[str, str]:
        return {}

    def close(self) -> None:
        return None


class Span:
    def __init__(self, tracer: "Tracer", name: str, kind: str, parent: "Span | None", root: bool,
                 data: dict[str, Any]):
        self._t, self.name, self.kind, self.parent, self._root = tracer, name, kind, parent, root
        self.trace_id = parent.trace_id if parent is not None else otlp.new_trace_id()
        self.id = otlp.new_span_id()
        self.start_ns = time.time_ns()
        self._data: dict[str, Any] = dict(data)
        self._ended = False
        self._exc: BaseException | None = None

    def set(self, **kw: Any) -> "Span":
        try:
            md = kw.pop("metadata", None)
            self._data.update({k: v for k, v in kw.items() if v is not None})
            if md:
                self._data.setdefault("metadata", {}).update(md)
        except Exception:
            pass
        return self

    def end(self, exc: BaseException | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            d = dict(self._data)
            level, message = d.pop("level", None), d.pop("status_message", None)
            if exc is not None:
                level, message = "ERROR", otlp.describe_exception(exc)
            attrs = otlp.observation_attrs(
                self.kind, input=d.pop("input", None), output=d.pop("output", None),
                metadata=d.pop("metadata", None), level=level, status_message=message, model=d.pop("model", None),
                usage=d.pop("usage", None), input_limit=d.pop("input_limit", otlp.MAX_CHARS),
                trace_name=self.name if self._root else None, session=self._t.session,
                environment=self._t.environment, release=self._t.release)
            self._t._emit(otlp.span_record(self.trace_id, self.id, self.parent.id if self.parent else None,
                                           self.name, self.start_ns, time.time_ns(), attrs,
                                           error=level == "ERROR", message=message))
        except Exception:
            pass

    def __enter__(self) -> "Span":
        try:
            self._t._push(self)
        except Exception:
            pass
        return self

    def __exit__(self, et: Any, ev: BaseException | None, tb: Any) -> bool:
        try:
            self._t._pop(self)
            self.end(ev if et is not None else None)
        except Exception:
            pass
        return False                                    # never swallow a trading exception


class Tracer:
    enabled = True

    def __init__(self, exporter: otlp.Exporter, *, environment: str = "live-paper", release: str | None = None,
                 session: str | None = None):
        self.exporter, self.environment, self.release = exporter, environment, release
        self.session = session or f"live-{time.strftime('%Y%m%d')}"
        self.context: dict[str, Any] = {}           # run-level facts added to every root (mode, code version, ...)
        self._local = threading.local()

    def _stack(self) -> list[Span]:
        s = getattr(self._local, "stack", None)
        if s is None:
            s = self._local.stack = []
        return s

    def _push(self, span: Span) -> None:
        self._stack().append(span)

    def _pop(self, span: Span) -> None:
        s = self._stack()
        if span in s:
            del s[s.index(span):]

    def _emit(self, record: dict) -> None:
        self.exporter.submit(record)

    def trace(self, name: str, kind: str = "chain", **data: Any) -> Span | _NullSpan:
        try:
            if self.context:
                data["metadata"] = {**self.context, **(data.get("metadata") or {})}
            return Span(self, name, kind, None, True, data)
        except Exception:
            return _NullSpan()

    def span(self, name: str, kind: str = "span", **data: Any) -> Span | _NullSpan:
        try:
            s = self._stack()
            return Span(self, name, kind, s[-1] if s else None, not s, data)
        except Exception:
            return _NullSpan()

    def event(self, name: str, **data: Any) -> None:
        """A point-in-time observation (Langfuse `event`) under the current span, e.g. an engine state transition."""
        try:
            s = self._stack()
            parent = s[-1] if s else None
            now = time.time_ns()
            attrs = otlp.observation_attrs("event", input=data or None, session=self.session,
                                           environment=self.environment, release=self.release)
            self._emit(otlp.span_record(parent.trace_id if parent else otlp.new_trace_id(), otlp.new_span_id(),
                                        parent.id if parent else None, name, now, now, attrs))
        except Exception:
            pass

    def child_env(self) -> dict[str, str]:
        """Ids the Hermes process needs to nest under the current span. Not secret; contain no payload."""
        try:
            s = self._stack()
            if not s:
                return {}
            return {"HERMES_TRACE_ID": s[-1].trace_id, "HERMES_TRACE_PARENT": s[-1].id,
                    "HERMES_TRACE_SESSION": self.session}
        except Exception:
            return {}

    def close(self) -> None:
        """Stop intake and write unacknowledged spans to the exit spool (a bounded local write, no network wait);
        the next invocation sends them."""
        try:
            self.exporter.close()
        except Exception:
            pass


DIAGNOSTIC_DRAIN_MAX_S = 10.0


def diagnostic_drain(tracer: Any, seconds: float) -> dict[str, Any]:
    """DIAGNOSTIC ONLY -- the one place anything waits for export. An operator opts in with
    `dry-run --trace-drain S` (orders disabled) to give the final spans up to `S` seconds (capped) to leave before
    the process exits. Never used by `cycle`, `loop`, agent completion in a trading run, or normal shutdown; it
    does not make normal one-shot delivery reliable (see docs/live_tracing_runbook.md)."""
    exporter = getattr(tracer, "exporter", None)
    if exporter is None:
        return {"drain": "skipped: tracing off"}
    limit = max(0.0, min(float(seconds), DIAGNOSTIC_DRAIN_MAX_S))
    t0 = time.monotonic()
    done = exporter.flush(limit)
    return {"drain": "complete" if done else "deadline reached; the rest was dropped",
            "waited_s": round(time.monotonic() - t0, 2), "limit_s": limit, "stats": dict(exporter.stats)}


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def load_tracer(state_dir: Path, *, release: str | None = None) -> "Tracer | NullTracer":
    """Tracer from `<state_dir>/tracing.env`; NullTracer if absent, incomplete or invalid. Never raises and
    makes no network call."""
    try:
        p = Path(state_dir) / ENV_FILE
        if not p.is_file():
            return NullTracer()
        cfg = read_env_file(p)
        pk, sk = cfg.get("LANGFUSE_PUBLIC_KEY", ""), cfg.get("LANGFUSE_SECRET_KEY", "")
        base = cfg.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        if not (pk.startswith("pk-lf-") and sk.startswith("sk-lf-")):
            return NullTracer()
        exp = otlp.Exporter(base, pk, sk, service="hermes-live-controller",
                            warn_path=str(Path(state_dir) / "tracing_warnings.log"),
                            spool_dir=str(Path(state_dir) / "trace_spool"))
        return Tracer(exp, release=release)
    except Exception:
        return NullTracer()


def code_version(repo: Path | None = None) -> str | None:
    """Short git commit of the working tree (local, no network), plus '+dirty' when it has changes. None if
    unknown. Read once per process start, never per cycle."""
    import subprocess
    try:
        cwd = str(repo or Path(__file__).resolve().parents[2])
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True,
                             timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=cwd,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (sha + ("+dirty" if dirty else "")) or None
    except Exception:
        return None
