"""Live Laya candidate delivery: a narrow adapter over the closed-out
experimental selector (backtest-harness/laya_experiment.SelectorContext).

Universe: the CURRENT eligible population from the existing weekly screener
(`watchlist/scalable_screener.py`: today's S&P MidCap 400 membership filtered by
the RULES.md §8 thresholds on current data, recorded write-once on a weekend
and read here through its own `usable_pool` rules -- current, or carried
forward at most one failed refresh). It is not the frozen ten-name watchlist
and not a retrospective, coverage-filtered experiment pool. When no usable
screen exists the adapter raises `UniverseUnavailable` with the screener's own
reason; that blocks new entries and nothing else. `source="watchlist"` is an
explicit, labelled operator choice of the frozen ten names (never a silent
fallback).

Shortlist: up to LIVE_SHORTLIST_MAX (20) names, never padded. The selector is
parameterised narrowly (`k`; the historical default of ten is unchanged). The
day's immutable snapshot is built over the WHOLE universe at a larger k so it
cannot depend on holdings that change during the day (a held or pending name
is removed afterwards, in rank order, and the next ranked names fill the list).
Cards are the same identity-free numeric cards scored in the same isolated
worker; scores and ranks stay inside selection, and the shortlist handed to
Hermes is alphabetical ticker/sector/name only. Any failure yields an empty
shortlist, which blocks new candidate-driven entries only.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "backtest-harness"))
sys.path.insert(0, str(REPO / "watchlist"))

LIVE_SHORTLIST_MAX = 20
LIVE_SNAPSHOT_K = 40          # room for up to 20 held/pending names to sit in the top ranks and be removed
LIVE_SEED = 0
WATCHLIST_PATH = REPO / "watchlist" / "watchlist.json"


class UniverseUnavailable(RuntimeError):
    pass


def live_universe(on_date: date, *, source: str = "screened", screen_dir: Path | None = None):
    """-> (FixedUniverse, label). Raises UniverseUnavailable with a specific reason."""
    from laya_selector import FixedUniverse, UniverseMember, universe_hash
    if source == "watchlist":
        raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        members = tuple(UniverseMember(symbol=t["symbol"], sector=t["sector"], name=t["name"], group="baseline")
                        for t in raw["tickers"])
        version, label = f"live-watchlist-{raw.get('generated', 'unknown')}", "frozen ten-name watchlist (explicit)"
    else:
        import scalable_screener as ss
        d = Path(screen_dir) if screen_dir else ss.SNAPSHOT_DIR
        u = ss.usable_pool(d, on_date)
        if u["state"] not in ("current", "carried_forward") or not u["pool"]:
            raise UniverseUnavailable(
                f"no usable eligible-universe screen for {on_date} ({u['state']}: {u['reason']}); record one with "
                "`python watchlist/scalable_screener.py --write` on a Saturday/Sunday (needs the containers)")
        rows = {r["symbol"]: r for r in ss.load_result(d, u["screen_id"])["rows"]}
        members = tuple(UniverseMember(symbol=s, sector=rows[s].get("sector") or "unknown",
                                       name=rows[s].get("name") or s, group="baseline") for s in u["pool"])
        version, label = f"live-screen-{u['screen_id']}", f"screened eligible universe ({u['state']})"
    return FixedUniverse(version=version, members=members, hash=universe_hash(members)), label


class _TracedFetch:
    """Observes the history fetch that feeds the cards (it only runs when a snapshot is BUILT)."""

    def __init__(self, inner: Callable, tr: Any):
        self._inner, self._tr = inner, tr

    def __call__(self, symbols, day):
        with self._tr.span("laya.fetch_history", input={"symbols": len(symbols), "as_of": str(day)}) as sp:
            got = self._inner(symbols, day)
            try:
                sp.set(output={"returned": len(got), "missing": sorted(set(symbols) - set(got))[:50]})
            except Exception:
                pass
            return got


class _TracedScorer:
    """Observes the ACTUAL Laya inference: the identity-free numeric cards sent to the isolated worker and the
    scores it returned. The worker itself is untouched (no network, no credentials); this runs in the controller.
    Cards are shown by ticker, never with the opaque row ids that blind the model."""

    def __init__(self, inner: Any, tr: Any, ticker_by_row: dict):
        self._inner, self._tr, self._names = inner, tr, ticker_by_row
        self.calls = 0

    @property
    def metadata(self):
        return self._inner.metadata

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if closer:
            closer()

    def score(self, cards):
        self.calls += 1
        md = self._inner.metadata
        with self._tr.span("laya.inference", kind="generation", model=md.checkpoint,
                           input={"question": md.question, "cards": {
                               self._names.get(c.row_id, "?"): c.model_state() for c in cards}},
                           metadata={"provider": md.provider, "revision": md.revision, "settings": md.settings,
                                     "package_version": md.package_version, "cards": len(cards)}) as sp:
            records = self._inner.score(cards)
            try:
                sp.set(output={self._names.get(r.row_id, "?"): {"review_probability": r.review_probability,
                                                                "answer_confidence": r.answer_confidence}
                               for r in records})
            except Exception:
                pass
            return records


def _ranking(snapshot_dir: Path, run_id: str, as_of: date, ticker_by_row: dict) -> dict:
    """The stored snapshot's per-name outcome, by ticker (diagnostic; read after selection, never before)."""
    from laya_selector import load_snapshot
    snap = load_snapshot(snapshot_dir, run_id, as_of.isoformat())
    if snap is None:
        return {"snapshot": "none stored"}
    rows = sorted(snap.rows, key=lambda r: (r.rank is None, r.rank or 0, r.row_id))
    return {"selector_version": snap.selector_version, "k": snap.k, "scorer_question": snap.scorer.question,
            "scorer_checkpoint": snap.scorer.checkpoint, "inputs_digest": snap.inputs_digest,
            "rows": [{"ticker": ticker_by_row.get(r.row_id, "?"), "status": r.status, "rank": r.rank,
                      "review_probability": r.review_probability, "answer_confidence": r.answer_confidence,
                      "freshness": r.freshness, "reason": r.reason} for r in rows]}


class LiveSelector:
    """Callable `(today, session, held_or_pending) -> (shortlist, status, event)`.

    `tracer` (set by the CLI) only observes: with tracing off nothing below changes, and with it on the same
    snapshot, scores and shortlist come out (the wrappers forward every call and re-raise every error)."""

    tracer: Any = None

    def __init__(self, state_dir: Path, checkpoint: str | None = None, *, source: str = "screened",
                 screen_dir: Path | None = None, fetch_history: Callable | None = None, scorer: Any = None):
        self.snapshots = Path(state_dir) / "selector"
        self.checkpoint, self.source, self.screen_dir = checkpoint, source, screen_dir
        self._fetch, self._scorer = fetch_history, scorer

    def _scorer_(self):
        if self._scorer is None:
            from laya_experiment import LayaWorkerScorer
            self._scorer = LayaWorkerScorer(self.checkpoint, log_path=self.snapshots / "laya_worker.log")
        return self._scorer

    def __call__(self, today: date, session: date, held_or_pending: set[str]) -> tuple[list[dict], str, dict]:
        from laya_experiment import SelectorContext
        from tracing.controller import NullTracer
        tr = self.tracer or NullTracer()
        with tr.span("selection.universe", input={"source": self.source, "on_date": today.isoformat()}) as sp:
            universe, label = live_universe(today, source=self.source, screen_dir=self.screen_dir)
            sp.set(output={"label": label, "universe_version": universe.version, "universe_hash": universe.hash,
                           "members": len(universe.members)})
        fetch = self._fetch
        if fetch is None:
            from adapters import card_history as fetch
        run_id = f"live-{universe.hash[:12]}"
        ctx = SelectorContext(universe=universe, seed=LIVE_SEED, run_id=run_id,
                              snapshot_dir=self.snapshots, scorer=self._scorer_(),
                              fetch_history=_TracedFetch(fetch, tr) if tr.enabled else fetch, k=LIVE_SNAPSHOT_K)
        names = {rid: t for t, rid in ctx.row_ids.items()}
        traced = _TracedScorer(ctx.scorer, tr, names) if tr.enabled else None
        if traced is not None:
            ctx.scorer = traced
        with tr.span("selection.laya", input={"session": session.isoformat(), "k": LIVE_SNAPSHOT_K,
                                              "seed": LIVE_SEED, "run_id": run_id}) as sp:
            out = ctx.select_for_day(session, frozenset())        # whole universe: holdings must not alter the snapshot
            if traced is not None:
                try:
                    inference = (f"ran: {traced.calls} scoring call(s) in the isolated worker" if traced.calls else
                                 "snapshot reuse: no inference, cards not rebuilt" if out.event.get("replayed") else
                                 "built without scoring (no data-complete card)" if out.ok else "failed")
                    sp.set(output={**out.event, "inference": inference,
                                   "ranking": _ranking(self.snapshots, run_id, session, names) if out.ok else None},
                           level=None if out.ok else "ERROR",
                           status_message=None if out.ok else str(out.event.get("reason")))
                except Exception:
                    pass
        by = {m.symbol: m for m in universe.members}
        ranked = [t for t in out.selected if t not in held_or_pending][:LIVE_SHORTLIST_MAX]
        # alphabetical: rank order is a selection internal, never shown to Hermes
        picks = [{"ticker": t, "sector": by[t].sector, "name": by[t].name} for t in sorted(ranked)]
        status = f"ok ({label}; {len(universe.symbols)} eligible)" if out.ok else f"unavailable: {out.event.get('reason')}"
        with tr.span("selection.exclusions", input={"held_or_pending": sorted(held_or_pending)}) as sp:
            sp.set(output={"removed_in_rank_order": [t for t in out.selected if t in held_or_pending],
                           "delivered_in_rank_order": ranked, "delivered": [p["ticker"] for p in picks],
                           "max": LIVE_SHORTLIST_MAX, "padded": False})
        return picks, status, {**out.event, "universe": [m.symbol for m in universe.members],
                               "universe_version": universe.version, "universe_hash": universe.hash}

    def close(self) -> None:
        if self._scorer is not None and hasattr(self._scorer, "close"):
            self._scorer.close()
