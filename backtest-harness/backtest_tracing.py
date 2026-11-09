"""Controller-side Langfuse tracing for historical backtests: export-only and fail-open.

One trace per simulated session (`backtest_day`), session id = the run id, `environment="backtest"`. It reuses
the live exporter and tracer (`live/tracing/otlp.py`, `controller.py`; stdlib-only, no live orchestration) and
observes the existing seams of `run_backtest.run_day` by wrapping them only while that day runs. Every wrapper
calls the original with the same arguments, returns its result unchanged, and re-raises its exceptions. Nothing
recorded here is ever read back by the backtest.

Boundaries (docs/research/live_backtest_boundary_check.md):
- Hermes is untouched: no plugin, no credentials and no tools in any backtest profile. The single tool-less Hermes
  call is recorded by the controller (prompt, raw reply, provider-returned usage from `--usage-file`).
- Historical data retrieval is recorded as controller work (`controller.fetch.*`), never as a Hermes tool call.
- Identities: every exported record is rewritten so a real ticker or company name becomes the run's alias (the
  run's own `RunMasking` patterns) BEFORE it reaches the exporter. Attributes, outputs, errors and the exit spool
  therefore carry aliases only, and no alias mapping. Decision/cycle ids (uuid5 of real tickers) are exported
  only as keyed `ref-` digests. A record that cannot be scrubbed is dropped, never sent raw.
- Its own directory, outside the repository and never the live state dir: `~/.hermes-trading-agent-backtest/`
  (`tracing.env`, `trace_spool/`, `tracing_warnings.log`, `decision_ref.key`). Without a valid `tracing.env`
  nothing is installed.
- Span times are wall clock; the simulated date and day are metadata (`sim_date`, `day_index`, `day_label`).
- Verdicts: `risk_check` (`check_trade_allowed`, labelled decision- or fill-time) is one gate, never final
  approval; `sizing_check` records the controller-computed BUY quantity, its inputs and outcome.
- Episode-end resolution of pending orders (`eval_suite.resolve_episode_end_pending`) is its own
  `backtest_episode_end` trace; eval_suite._run_episode starts a session, so evaluation episodes reach it.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO / "live") not in sys.path:
    sys.path.append(str(REPO / "live"))           # appended: a backtest module of the same name always wins
from tracing import controller, otlp  # noqa: E402

DEFAULT_DIR = Path.home() / ".hermes-trading-agent-backtest"
LIVE_STATE_DIR = Path.home() / ".hermes-trading-agent-live"   # live's default --state-dir: never used here
SERVICE, SCOPE, ENVIRONMENT = "hermes-backtest-controller", "hermes-trading-agent.backtest", "backtest"
PROMPT_LIMIT = 60_000            # chars of a masked prompt kept in a model-call record (attribute cap still applies)
DECISION_STAGE = "decision_time (not final: BUY sizing, pre-fill news gate and fill-time check follow)"
SIZING_OWNER = "controller (run_backtest.required_entry_quantity; Hermes does not size BUYs)"
EPISODE_END_REASON = "episode window ended before execution"   # eval_suite.resolve_episode_end_pending's own


def _inside(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def trace_dir_problem(d: Path) -> str | None:
    """Why `d` may not hold backtest tracing state, or None if it may."""
    if _inside(d, LIVE_STATE_DIR) or _inside(LIVE_STATE_DIR, d):
        return "refused: overlaps the live state directory"
    if _inside(d, REPO):
        return "refused: inside the repository"
    return None


_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def ref_key(d: Path) -> bytes:
    """The tracing dir's local key for `ref-` digests (created once; never exported)."""
    p = d / "decision_ref.key"
    try:
        with open(p, "xb") as f:
            f.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    return p.read_bytes()


class Scrubber:
    """Real ticker / company name -> the run's alias, using the run's own masking patterns (longest first).
    Every UUID except the run id becomes a keyed `ref-` digest: decision ids are uuid5 of (run, day, REAL
    ticker, ...), so exported raw beside an alias they would let anyone holding the repo rebuild the mapping."""

    def __init__(self, masking: Any, *, run_id: str = "", key: bytes = b""):
        self._subs = [(pattern, masking.alias(ticker)) for pattern, ticker in masking._patterns]
        self._run_id, self._key = run_id.lower(), key or secrets.token_bytes(32)

    def _ref(self, m: re.Match) -> str:
        u = m.group(0).lower()
        return u if u == self._run_id else "ref-" + hmac.new(self._key, u.encode(), "sha256").hexdigest()[:16]

    def text(self, s: str) -> str:
        for pattern, alias in self._subs:
            s = pattern.sub(alias, s)
        return _UUID.sub(self._ref, s)

    def record(self, rec: dict) -> dict:
        out = dict(rec)
        out["name"] = self.text(rec["name"])
        if rec.get("message"):
            out["message"] = self.text(rec["message"])
        out["attrs"] = {k: self.text(v) if isinstance(v, str) else v for k, v in rec["attrs"].items()}
        return out


class BacktestTracer(controller.Tracer):
    """The live controller tracer with backtest labels and an identity scrub on every emitted record."""

    def __init__(self, exporter: otlp.Exporter, *, scrubber: Scrubber, run_id: str, release: str | None):
        super().__init__(exporter, environment=ENVIRONMENT, release=release, session=run_id)
        self.scrubber, self.dropped_unscrubbable = scrubber, 0

    def _emit(self, record: dict) -> None:
        try:
            record = self.scrubber.record(record)
        except Exception:
            self.dropped_unscrubbable += 1            # never export a record that could not be scrubbed
            return
        self.exporter.submit(record)


def load(run_id: str, masking: Any, *, trace_dir: Path | None = None, release: str | None = None,
         post: Callable[[bytes], int] | None = None) -> tuple[Any, str]:
    """(tracer, status line). NullTracer unless `<trace_dir>/tracing.env` holds valid keys and the directory is
    allowed. Never raises, makes no network call, prints nothing secret."""
    d = Path(trace_dir or DEFAULT_DIR)
    try:
        problem = trace_dir_problem(d)
        if problem:
            return controller.NullTracer(), f"off ({problem})"
        env = d / controller.ENV_FILE
        if not env.is_file():
            return controller.NullTracer(), "off (no tracing.env)"
        cfg = controller.read_env_file(env)
        pk, sk = cfg.get("LANGFUSE_PUBLIC_KEY", ""), cfg.get("LANGFUSE_SECRET_KEY", "")
        if not (pk.startswith("pk-lf-") and sk.startswith("sk-lf-")):
            return controller.NullTracer(), "off (tracing.env incomplete)"
        exp = otlp.Exporter(cfg.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"), pk, sk, service=SERVICE,
                            scope=SCOPE, warn_path=str(d / "tracing_warnings.log"),
                            spool_dir=str(d / "trace_spool"), post=post)
        scrubber = Scrubber(masking, run_id=run_id, key=ref_key(d))
        return BacktestTracer(exp, scrubber=scrubber, run_id=run_id, release=release), "on"
    except Exception as exc:
        return controller.NullTracer(), f"off ({type(exc).__name__})"


# -- seam summaries: small, aliased-by-scrub, never the raw payload of a data fetch ---------------------------

def _n(x: Any) -> int | None:
    try:
        return len(x)
    except Exception:
        return None


def _fetch_out(tickers: Any, result: Any) -> dict:
    asked, got = _n(tickers), _n(result)
    out = {"requested": asked, "returned": got}
    if isinstance(asked, int) and isinstance(got, int):
        out["missing"] = max(0, asked - got)
    return out


def _articles(result: Any) -> int | None:
    try:
        return sum(len(v.get("articles", v) if isinstance(v, dict) else v) for v in result.values())
    except Exception:
        return None


def _usage(u: Any) -> dict:
    """Provider-returned usage only (Hermes `--usage-file`); costs are never exported."""
    if not isinstance(u, dict):
        return {}
    m = {"input": "input_tokens", "output": "output_tokens", "total": "total_tokens",
         "cache_read_input_tokens": "cache_read_tokens", "reasoning_tokens": "reasoning_tokens"}
    return {k: int(u[v]) for k, v in m.items() if isinstance(u.get(v), (int, float))}


def _arg(a: tuple, kw: dict, i: int, name: str, default: Any = None) -> Any:
    return kw[name] if name in kw else (a[i] if len(a) > i else default)


class TraceSession:
    """Installed once per run. Wraps `run_day`; while a day runs, its seams are wrapped too (over whatever
    functions are current, so test fakes are observed exactly like the real ones) and restored afterwards."""

    def __init__(self, rb: Any, tracer: Any, *, meta: dict | None = None, run_day_owner: Any = None):
        self.rb, self.tracer, self.meta = rb, tracer, dict(meta or {})
        # The module whose `run_day` name the runner calls: run_backtest itself, or eval_suite (which imported
        # run_day by name). The seams below always live in run_backtest, where the real run_day looks them up.
        self._owner = run_day_owner if run_day_owner is not None else rb
        self._run_day = self._owner.run_day
        self._calls, self._last_sim_date = 0, None
        self._owner.run_day = self._traced_run_day
        # Episode-end resolution (evaluation episodes, laya_vs_random) lives in eval_suite; observed only if loaded.
        self._es = sys.modules.get("eval_suite")
        self._episode_end_fn = getattr(self._es, "resolve_episode_end_pending", None)
        self._traced_episode_end = None
        if callable(self._episode_end_fn):
            self._traced_episode_end = self._episode_end(self._episode_end_fn)
            self._es.resolve_episode_end_pending = self._traced_episode_end

    # -- lifecycle
    def close(self) -> None:
        try:
            if getattr(self._owner, "run_day", None) == self._traced_run_day:
                self._owner.run_day = self._run_day
            if self._traced_episode_end is not None and \
                    getattr(self._es, "resolve_episode_end_pending", None) is self._traced_episode_end:
                self._es.resolve_episode_end_pending = self._episode_end_fn
        finally:
            self.tracer.close()                       # stops intake; unacknowledged records go to the spool

    def _episode_end(self, fn: Callable) -> Callable:
        """`eval_suite.resolve_episode_end_pending` -> its own `backtest_episode_end` trace in the run's session,
        with one `journal.rejection` child per pending order and the actual outcome of each (written, or the
        write failed: the helper swallows that, so the trace is the only place it shows). Behaviour unchanged."""
        tr = self

        def wrapper(portfolio: Any, *a: Any, **kw: Any) -> Any:
            mod = sys.modules.get(getattr(fn, "__module__", ""))
            orig = getattr(mod, "resolve_trade_rejection", None)
            outcomes: dict[str, str] = {}
            pending: list[dict] = []
            try:
                pending = [{"decision_id": o.decision_id, "ticker": o.ticker, "action": o.action,
                            "quantity": o.quantity} for o in portfolio.pending_orders]
            except Exception:
                pass

            def rejection(decision_id: Any, reason: Any, *ra: Any, **rk: Any) -> Any:
                with tr.tracer.span("journal.rejection", input={"decision_id": decision_id, "reason": reason}):
                    try:
                        r = orig(decision_id, reason, *ra, **rk)
                    except Exception as exc:
                        outcomes[str(decision_id)] = f"journal write failed ({type(exc).__name__})"
                        raise
                    outcomes[str(decision_id)] = "rejected; journal row written"
                    return r

            root = tr.tracer.trace("backtest_episode_end", metadata={**tr.meta,
                                                                     "last_sim_date": tr._last_sim_date})
            with root:
                swapped = False
                try:
                    root.set(input={"pending_orders": len(pending), "open_positions": _n(portfolio.holdings)})
                    if callable(orig):
                        mod.resolve_trade_rejection, swapped = rejection, True
                except Exception:
                    pass
                try:
                    result = fn(portfolio, *a, **kw)
                finally:
                    if swapped:
                        mod.resolve_trade_rejection = orig
                try:
                    root.set(output={
                        "resolution": "no pending orders" if not pending else EPISODE_END_REASON,
                        "resolved": _n(result),
                        "orders": [{**p, "outcome": outcomes.get(str(p["decision_id"]), "not attempted")}
                                   for p in pending],
                        "open_positions_kept": _n(portfolio.holdings)})
                except Exception:
                    pass
                return result
        wrapper.__wrapped__ = fn                      # type: ignore[attr-defined]
        return wrapper

    def _span(self, fn: Callable, name: str, kind: str = "span", inp: Callable | None = None,
              out: Callable | None = None, model: Callable | None = None) -> Callable:
        tr = self

        def wrapper(*a: Any, **kw: Any) -> Any:
            sp = tr.tracer.span(name, kind=kind)
            with sp:
                try:
                    if inp is not None:
                        sp.set(input=inp(a, kw))
                except Exception:
                    pass
                result = fn(*a, **kw)
                try:
                    if out is not None:
                        sp.set(output=out(result))
                    if model is not None:
                        sp.set(**model(a, kw, result))
                except Exception:
                    pass
                return result
        wrapper.__wrapped__ = fn                      # type: ignore[attr-defined]
        return wrapper

    def _seams(self) -> dict[str, Callable]:
        S = self._span
        return {
            "fetch_trading_calendar_day": lambda f: S(
                f, "controller.fetch.calendar", inp=lambda a, kw: {"date": str(_arg(a, kw, 0, "day"))},
                out=lambda r: r if r else {"trading_day": False}),
            "fetch_bars": lambda f: S(f, "controller.fetch.daily_bars",
                                      inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers")),
                                                         "date": str(_arg(a, kw, 1, "day"))},
                                      out=lambda r: {"returned": _n(r)}),
            "fetch_intraday_bars": lambda f: S(f, "controller.fetch.intraday_bars",
                                               inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers")),
                                                                  "date": str(_arg(a, kw, 1, "day"))},
                                               out=lambda r: {"returned": _n(r),
                                                              "bars": sum(len(v) for v in r.values())}),
            "fetch_technicals": lambda f: S(f, "controller.fetch.technicals",
                                            inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers")),
                                                               "date": str(_arg(a, kw, 1, "day"))},
                                            out=lambda r: {"returned": _n(r)}),
            "fetch_news": lambda f: S(f, "controller.fetch.news",
                                      inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers")),
                                                         "date": str(_arg(a, kw, 1, "day")),
                                                         "decision_cutoff_at": str(
                                                             _arg(a, kw, 2, "decision_cutoff_at"))},
                                      out=lambda r: {"symbols": _n(r), "articles": _articles(r)}),
            "fetch_news_window": lambda f: S(f, "controller.fetch.news_window",
                                             inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers")),
                                                                "start_at": str(_arg(a, kw, 1, "start_at")),
                                                                "end_at": str(_arg(a, kw, 2, "end_at"))},
                                             out=lambda r: {"symbols": _n(r), "articles": _articles(r)}),
            "fetch_fundamentals": lambda f: S(f, "controller.fetch.fundamentals",
                                              inp=lambda a, kw: {"symbols": _n(_arg(a, kw, 0, "tickers"))},
                                              out=lambda r: {"returned": _n(r)}),
            "fetch_recent_insights": lambda f: S(f, "controller.fetch.insights", out=lambda r: {"count": _n(r)}),
            "fetch_recent_closed_trades": lambda f: S(f, "controller.fetch.closed_trades",
                                                      out=lambda r: {"count": _n(r)}),
            "build_prompt": lambda f: S(f, "prompt_assembly",
                                        inp=lambda a, kw: {"day_label": _arg(a, kw, 0, "day_label"),
                                                           "positions": _n(_arg(a, kw, 2, "positions")),
                                                           "candidates": _n(_arg(a, kw, 3, "candidates"))},
                                        out=lambda r: {"prompt_chars": len(r), "prompt_sha256": hashlib.sha256(
                                            r.encode("utf-8")).hexdigest()}),
            "ask_hermes": lambda f: S(f, "hermes_decision", kind="agent",
                                      inp=lambda a, kw: {"prompt_chars": len(a[0]) if a else None,
                                                         "reflection_day": bool(kw.get("is_reflection_day"))},
                                      out=lambda r: {"attempt_count": r.attempt_count,
                                                     "retry_diagnostics": r.retry_diagnostics,
                                                     "decisions": _n((r.response or {}).get("decisions")),
                                                     "no_action_reason": (r.response or {}).get("no_action_reason")}),
            "_call_hermes": lambda f: self._model_call(f),
            "validate_hermes_response": lambda f: S(f, "response_validation",
                                                    inp=lambda a, kw: {"reflection_day": bool(_arg(
                                                        a, kw, 1, "is_reflection_day"))},
                                                    out=lambda r: {"valid": True}),
            "apply_pre_fill_news_gate": lambda f: S(f, "pre_fill_gate",
                                                    inp=lambda a, kw: {"queued_orders": _n(a[0]) if a else None},
                                                    out=lambda r: {"orders_after": _n(r[0]), "events": r[1]}),
            # run_day calls check_trade twice per order: at decision time (validate_timing=False) and at the fill.
            # A decision-time pass is never final approval, so the verdict key says which check passed.
            "check_trade": lambda f: S(f, "risk_check",
                                       inp=lambda a, kw: {"check": "risk_checker.check_trade",
                                                          "stage": DECISION_STAGE if kw.get("validate_timing") is False
                                                          else "fill_time",
                                                          "ticker": getattr(a[0], "ticker", None),
                                                          "action": getattr(a[0], "action", None),
                                                          "quantity": getattr(a[0], "quantity", None)},
                                       out=lambda r: {"check_trade_allowed": r.allowed,
                                                      "violations": list(r.violations)}),
            # Controller-owned BUY sizing (prompt schema 5): run_day computes the quantity here, before check_trade.
            "required_entry_quantity": lambda f: self._sizing(f),
            "simulate_fill": lambda f: S(f, "execution_sim",
                                         inp=lambda a, kw: {"alias": a[0], "action": a[1], "quantity": a[2],
                                                            "reference_price": a[3]},
                                         out=lambda r: r),
            "_execute_exit": lambda f: S(f, "position_exit",
                                         inp=lambda a, kw: {"ticker": a[1], "quantity": a[2], "entry_price": a[3],
                                                            "exit_price": a[4], "reason": a[5], "stage": a[6]},
                                         out=lambda r: {"exit_decision_id": r}),
            "record_queued_decision": lambda f: S(f, "journal.queued_decision",
                                                  inp=lambda a, kw: {"decision_id": _arg(a, kw, 2, "decision_id"),
                                                                     "action": (a[0] or {}).get("action"),
                                                                     "alias": (a[0] or {}).get("alias")}),
            "record_trade": lambda f: S(f, "journal.trade",
                                        inp=lambda a, kw: {"decision_id": _arg(a, kw, 3, "decision_id"),
                                                           "status": (a[2] or {}).get("status")}),
            "resolve_trade_fill": lambda f: S(f, "journal.fill", inp=lambda a, kw: {"decision_id": a[0]}),
            "resolve_trade_rejection": lambda f: S(f, "journal.rejection",
                                                   inp=lambda a, kw: {"decision_id": a[0], "reason": a[1]}),
            "record_exit": lambda f: S(f, "journal.exit",
                                       inp=lambda a, kw: {"decision_id": _arg(a, kw, 4, "decision_id"),
                                                          "reason": _arg(a, kw, 3, "reason")}),
            "record_insight": lambda f: S(f, "journal.reflection",
                                          inp=lambda a, kw: {"chars": len(a[0]), "sample_size": kw.get("sample_size")},
                                          out=lambda r: {"inserted": r}),
            "record_cycle_evidence": lambda f: S(f, "journal.evidence",
                                                 inp=lambda a, kw: {"cycle_id": kw.get("cycle_id")}),
            "record_snapshot_once": lambda f: S(f, "journal.snapshot",
                                                inp=lambda a, kw: {"day_label": a[0] if a else None,
                                                                   "equity": a[1] if len(a) > 1 else None},
                                                out=lambda r: {"written": r}),
            "log_activity": lambda f: S(f, "journal.activity", inp=lambda a, kw: {"events": _n(a[0]) if a else None}),
        }

    def _sizing(self, fn: Callable) -> Callable:
        tr = self

        def wrapper(proposal: Any, ctx: Any, *a: Any, **kw: Any) -> Any:
            with tr.tracer.span("sizing_check") as sp:
                try:
                    sp.set(input={"owner": SIZING_OWNER, "ticker": proposal.ticker, "action": proposal.action,
                                  "entry_price": proposal.entry_price, "stop_price": proposal.stop_price,
                                  "equity": ctx.account.equity, "remaining_cash": ctx.account.cash,
                                  "risk_budget_pct": ctx.limits.risk_budget_pct,
                                  "max_position_pct": ctx.limits.max_position_pct})
                except Exception:
                    pass
                q = fn(proposal, ctx, *a, **kw)
                try:
                    sp.set(output={"computed_quantity": q,
                                   "outcome": "sized" if isinstance(q, int) and q > 0 else
                                   "rejected: zero shares" if q == 0 else "rejected: unusable sizing inputs"})
                except Exception:
                    pass
                return q
        wrapper.__wrapped__ = fn                      # type: ignore[attr-defined]
        return wrapper

    def _model_call(self, fn: Callable) -> Callable:
        """`_call_hermes` -> one generation per real model invocation (decision, retry or pre-fill gate)."""
        tr = self

        def wrapper(prompt: str, *a: Any, **kw: Any) -> Any:
            tr._calls += 1
            sp = tr.tracer.span(f"hermes_call_{tr._calls}", kind="generation")
            with sp:
                try:
                    sp.set(input={"prompt": prompt}, input_limit=PROMPT_LIMIT,
                           metadata={"prompt_chars": len(prompt)})
                except Exception:
                    pass
                raw, usage = fn(prompt, *a, **kw)
                try:
                    u = usage if isinstance(usage, dict) else {}
                    sp.set(output={"raw_reply": raw.decode("utf-8", "replace")}, usage=_usage(u) or None,
                           model=u.get("model"), metadata={k: u.get(k) for k in ("provider", "reasoning_effort",
                                                                                  "api_calls") if u.get(k)}
                           or {"usage": "unavailable"})
                except Exception:
                    pass
                return raw, usage
        wrapper.__wrapped__ = fn                      # type: ignore[attr-defined]
        return wrapper

    @contextlib.contextmanager
    def _installed(self, selector: Any = None):
        """Wrap the seams for the duration of one day (or its observability step), then restore them."""
        saved: dict[str, Any] = {}
        sel_saved: dict[str, Any] = {}
        try:
            for name, make in self._seams().items():
                f = getattr(self.rb, name, None)
                if callable(f):
                    saved[name] = f
                    setattr(self.rb, name, make(f))
            if selector is not None:
                sel_saved = self._wrap_selector(selector)
        except Exception:
            pass
        try:
            yield
        finally:
            for name, f in saved.items():
                setattr(self.rb, name, f)
            for attr, v in sel_saved.items():
                if v is None:
                    selector.__dict__.pop(attr, None)     # the class method shows through again
                else:
                    setattr(selector, attr, v)

    def _wrap_selector(self, sel: Any) -> dict[str, Any]:
        saved = {"select_for_day": sel.__dict__.get("select_for_day"), "scorer": sel.scorer,
                 "_fetch_history": sel._fetch_history}
        tr, inner, scorer = self, sel.select_for_day, sel.scorer

        class _Scorer:
            calls = 0

            def __getattr__(self, k: str) -> Any:
                return getattr(scorer, k)

            def score(self, cards: Any) -> Any:
                _Scorer.calls += 1
                with tr.tracer.span("laya.inference", kind="generation") as sp:
                    sp.set(input={"cards": _n(cards)}, model="convaiinnovations/laya",
                           metadata={"scorer": getattr(scorer, "metadata", None)})
                    out = scorer.score(cards)
                    sp.set(output={"scores": _n(out)})
                    return out

        def select_for_day(day: Any, held: Any) -> Any:
            _Scorer.calls = 0
            with tr.tracer.span("selection.laya", input={"date": str(day), "held_or_pending": _n(held)}) as sp:
                out = inner(day, held)
                try:
                    ev = dict(out.event)
                    sp.set(output={**ev, "selected": list(out.selected),
                                   "inference": ("ran: %d scoring call(s)" % _Scorer.calls if _Scorer.calls else
                                                 "snapshot reuse: no inference" if ev.get("replayed") else
                                                 "no scoring call")},
                           level=None if out.ok else "ERROR",
                           status_message=None if out.ok else str(ev.get("reason")))
                except Exception:
                    pass
                return out

        sel.scorer = _Scorer()
        sel._fetch_history = self._span(sel._fetch_history, "laya.fetch_history",
                                        inp=lambda a, kw: {"symbols": _n(a[0]), "as_of": str(a[1])},
                                        out=lambda r: {"returned": _n(r)})
        sel.select_for_day = select_for_day
        return saved

    def _traced_run_day(self, *a: Any, **kw: Any) -> Any:
        day, day_index = _arg(a, kw, 0, "day"), _arg(a, kw, 1, "day_index")
        masking, selector = _arg(a, kw, 3, "masking"), kw.get("selector")
        self._calls, self._last_sim_date = 0, str(day)
        meta = {**self.meta, "sim_date": str(day), "day_index": day_index,
                "day_label": masking.day_index(day) if masking is not None else None,
                "selection": "laya" if selector is not None else "off (fixed watchlist)",
                "fundamentals": "on" if kw.get("fundamentals_enabled") else "off (not fetched)",
                "injection_monitor": "on" if kw.get("injection_monitor") is not None else "off"}
        root = self.tracer.trace("backtest_day", metadata=meta)
        with root, self._installed(selector):
            new_p, summary, do_obs = self._run_day(*a, **kw)
            try:
                # run_day's "proposals" lists only orders that were queued; Hermes' own proposal count is
                # hermes_decision.output.decisions (a rejected proposal appears in "blocked").
                out = {k: summary.get(k) for k in ("decisions", "blocked", "exits", "fills")}
                out["queued_proposals"] = summary.get("proposals")
                if "skipped" in summary:
                    out = {"skipped": summary["skipped"]}       # e.g. a market holiday: no session ran
                root.set(output=out, metadata={"usage": _usage(summary.get("usage")) or "unavailable"})
            except Exception:
                pass
        tr = self

        def observability() -> Any:
            obs = controller.Span(tr.tracer, "observability", "span", root, False, {}) if tr.tracer.enabled \
                else controller._NullSpan()
            with obs, tr._installed():
                return do_obs()
        return new_p, summary, observability


def start(rb: Any, *, run_id: str, masking: Any, meta: dict | None = None, trace_dir: Path | None = None,
          run_day_owner: Any = None) -> tuple[TraceSession | None, str]:
    """Called once per run by run_backtest's main body or eval_suite._run_episode. Returns (session or None,
    status line)."""
    tracer, status = load(run_id, masking, trace_dir=trace_dir, release=controller.code_version(REPO))
    if not tracer.enabled:
        return None, status
    return TraceSession(rb, tracer, meta=meta, run_day_owner=run_day_owner), status
