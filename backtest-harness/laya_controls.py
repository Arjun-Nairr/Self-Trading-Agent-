"""
laya_controls.py -- bounded comparison of the Laya selector with simple
controls (plan: docs/evaluations/laya_vs_controls_plan_2026-10-04.md).

    python backtest-harness/laya_controls.py run [--smoke-snapshots DIR]

Fetches each session's cards once through the existing point-in-time card
builder; scores them in the pinned, isolated Laya worker (model loaded once,
sessions scored sequentially); applies the random and deterministic controls
to exactly the same complete-card set; scores a fixed synthetic sensitivity
batch; and writes write-once PRIVATE outputs (cards, scores, shortlists, the
row-ID -> ticker mapping) plus an aggregate public summary, all under the
git-ignored .laya_controls/. No Hermes, no trades, no backtest state, no Neon.

review_probability is an uncalibrated model's P(true) for "is this card worth
the full review?" -- a review-priority signal, never a probability of profit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from datetime import date, datetime, timezone
from math import comb
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import laya_adapter
from laya_cards import build_day_cards
from laya_selector import (
    NUMERIC_FIELDS, STATUS_SELECTED, CandidateCard, CardScorer, ScoreRecord, _tie_break_key,
    build_card, inputs_digest, make_row_ids, score_complete_cards, select_pool,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
EXPERIMENT_VERSION = "laya-vs-controls-v1"
PLAN_PATH = REPO / "docs" / "evaluations" / "laya_vs_controls_plan_2026-10-04.md"
POOL_PATH = REPO / "watchlist" / "experimental_pools" / "candidates60-v1-pool-2026-09-28-eec1e3f2203f.json"
POOL_CONTENT_SHA256 = "7d1e5f7334dbae5847e348e9213eb678739e3208591a429ce05f4c78bf8f8702"
POOL_UNIVERSE_HASH = "536a1ecac51b48224c769dc691d55c089ad7c035cab1edd194a09d7159016fe6"
SMOKE_RUN_ID = "5573597c-2f6c-454f-99d3-fdce9f25b62f"
SESSIONS = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 2))
ROW_ID_SEED = 42          # = the smoke run's, so its snapshot digests can verify the rebuilt cards
TIE_BREAK_SEED = 42
K = 10
RANDOM_SEEDS = tuple(range(20))
OUT_DIR = HERE / ".laya_controls"

# "Ordinary" anchors taken from Laya's own "no" criterion (realized_volatility has none).
ANCHORS = {"five_day_return": 0.0, "sma20_distance": 0.0, "rsi14": 50.0, "macd_direction": 0.0,
           "relative_volume": 1.0, "range_position_20d": 0.5}

SENS_BASE = {"five_day_return": 0.0, "sma20_distance": 0.0, "rsi14": 50.0, "macd_direction": 0.0,
             "relative_volume": 1.0, "realized_volatility": 0.25, "range_position_20d": 0.5}
SENS_VARIANTS = {"five_day_return": (-0.10, 0.10), "sma20_distance": (-0.08, 0.08), "rsi14": (25.0, 75.0),
                 "macd_direction": (-0.80, 0.80), "relative_volume": (0.40, 3.00),
                 "realized_volatility": (0.10, 0.60), "range_position_20d": (0.02, 0.98)}
SENS_ALL_NOTABLE = {"five_day_return": 0.10, "sma20_distance": 0.08, "rsi14": 75.0, "macd_direction": 0.80,
                    "relative_volume": 3.00, "realized_volatility": 0.60, "range_position_20d": 0.98}
SENS_MOVE_THRESHOLD = 0.0005


# -- controls (pure) ---------------------------------------------------------

def random_shortlists(row_ids: Sequence[str], session: date, seeds: Sequence[int] = RANDOM_SEEDS,
                      k: int = K) -> dict[int, list[str]]:
    """seed -> k IDs drawn without replacement from the sorted IDs; the session
    is in the seed string so a seed draws independently on each day."""
    pop = sorted(row_ids)
    return {s: random.Random(f"{session.isoformat()}|{s}").sample(pop, min(k, len(pop))) for s in seeds}


def deterministic_shortlist(cards: Sequence[CandidateCard], k: int = K, seed: int = TIE_BREAK_SEED) -> list[str]:
    """Largest absolute five-day move first; ties by the selector's seeded row-ID hash."""
    ranked = sorted((c for c in cards if c.data_complete),
                    key=lambda c: (-abs(c.five_day_return), _tie_break_key(seed, c.row_id)))
    return [c.row_id for c in ranked[:k]]


def laya_shortlist(cards: Sequence[CandidateCard], scores: Mapping[str, ScoreRecord], session: date,
                   latest: Mapping[str, date] | None = None, k: int = K) -> list[str]:
    rows = select_pool(cards, scores, as_of=session, seed=TIE_BREAK_SEED, latest_bar_dates=latest, k=k)
    return [r.row_id for r in sorted((r for r in rows if r.status == STATUS_SELECTED), key=lambda r: r.rank)]


# -- measurements (pure) -----------------------------------------------------

def notable_flags(card: CandidateCard) -> int:
    """How many of Laya's own 'notable' criteria the card meets (plan §4.6 thresholds)."""
    return sum((
        abs(card.five_day_return) >= 0.05,
        abs(card.sma20_distance) >= 0.05,
        card.range_position_20d <= 0.10 or card.range_position_20d >= 0.90,
        card.rsi14 <= 30 or card.rsi14 >= 70,
        abs(card.macd_direction) >= 0.50,
        card.relative_volume >= 1.5,
    ))


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for t in range(i, j + 1):
            r[order[t]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return None if den == 0 else round(num / den, 4)


def hypergeom_tail(n: int, a: int, b: int, observed: int) -> float:
    """P(|A ∩ B| >= observed) when B (size b) is a uniform random subset of n
    items and A (size a) is fixed."""
    total = comb(n, b)
    return round(sum(comb(a, x) * comb(n - a, b - x) for x in range(observed, min(a, b) + 1)) / total, 6)


def _dist(xs: Sequence[float]) -> dict[str, float]:
    return {"mean": round(statistics.fmean(xs), 3), "min": min(xs), "max": max(xs)}


def score_stats(probs: Sequence[float], k: int = K) -> dict[str, Any]:
    ps = sorted(probs, reverse=True)
    q = statistics.quantiles(ps, n=4) if len(ps) >= 4 else [None, None, None]
    counts: dict[float, int] = {}
    for p in ps:
        counts[p] = counts.get(p, 0) + 1
    kth = ps[k - 1] if len(ps) >= k else None
    return {
        "n": len(ps), "min": ps[-1], "median": statistics.median(ps), "max": ps[0],
        "range": round(ps[0] - ps[-1], 4), "iqr": None if q[0] is None else round(q[2] - q[0], 4),
        "distinct": len(counts), "rows_sharing_a_score": sum(c for c in counts.values() if c > 1),
        "rank10_vs_11_gap": round(ps[k - 1] - ps[k], 4) if len(ps) > k else None,
        "within_0.001_of_rank10": None if kth is None else sum(1 for p in ps if abs(p - kth) <= 0.001),
    }


def profile(cards: Sequence[CandidateCard]) -> dict[str, float]:
    out = {f: round(statistics.median(getattr(c, f) for c in cards), 4) for f in NUMERIC_FIELDS}
    out["mean_notable_flags"] = round(statistics.fmean(notable_flags(c) for c in cards), 3)
    out["share_with_any_flag"] = round(sum(notable_flags(c) > 0 for c in cards) / len(cards), 3)
    return out


def _mean_profile(profiles: Sequence[Mapping[str, float]]) -> dict[str, float]:
    return {f: round(statistics.fmean(p[f] for p in profiles), 4) for f in profiles[0]}


def analyse_day(session: date, cards: Sequence[CandidateCard], scores: Mapping[str, ScoreRecord],
                latest: Mapping[str, date] | None = None) -> dict[str, Any]:
    """All comparison metrics for one day's complete-card set. Returns
    {'public': aggregates only, 'private': shortlists by row ID}."""
    complete = [c for c in cards if c.data_complete]
    by_id = {c.row_id: c for c in complete}
    ids = sorted(by_id)
    n = len(ids)
    lay = laya_shortlist(complete, scores, session, latest)
    det = deterministic_shortlist(complete)
    rnd = random_shortlists(ids, session)
    ov = lambda a, b: len(set(a) & set(b))
    lay_det = ov(lay, det)
    lay_rnd = [ov(lay, r) for r in rnd.values()]
    det_rnd = [ov(det, r) for r in rnd.values()]
    probs = [scores[i].review_probability for i in ids]
    fields = {f: [getattr(by_id[i], f) for i in ids] for f in NUMERIC_FIELDS}
    corr = {f: spearman(probs, v) for f, v in fields.items()}
    corr_abs = {f: spearman(probs, [abs(x - ANCHORS[f]) for x in v]) for f, v in fields.items() if f in ANCHORS}
    det_score = [abs(by_id[i].five_day_return) for i in ids]
    public = {
        "session": session.isoformat(), "complete_cards": n,
        "overlap": {
            "laya_vs_deterministic": lay_det,
            "laya_vs_deterministic_jaccard": round(lay_det / len(set(lay) | set(det)), 3),
            "laya_vs_random": _dist(lay_rnd), "deterministic_vs_random": _dist(det_rnd),
            "random_expectation": round(K * K / n, 3),
            "p_random_overlap_ge_laya_det": hypergeom_tail(n, K, K, lay_det),
        },
        "laya_scores": score_stats(probs),
        "spearman_laya_vs_field": corr, "spearman_laya_vs_abs_deviation": corr_abs,
        "spearman_laya_vs_deterministic_score": spearman(probs, det_score),
        "profiles": {
            "all_complete": profile(complete),
            "laya": profile([by_id[i] for i in lay]),
            "deterministic": profile([by_id[i] for i in det]),
            "random_mean_of_seed_medians": _mean_profile([profile([by_id[i] for i in r]) for r in rnd.values()]),
        },
        "random_notable_flags_range": _dist([profile([by_id[i] for i in r])["mean_notable_flags"] for r in rnd.values()]),
    }
    return {"public": public, "private": {"laya": lay, "deterministic": det, "random": {str(s): r for s, r in rnd.items()}}}


def turnover(private_days: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive-day shortlist overlap per method (row IDs are stable across days)."""
    out = []
    for a, b in zip(private_days, private_days[1:]):
        pa, pb = a["shortlists"], b["shortlists"]
        rnd = [len(set(pa["random"][s]) & set(pb["random"][s])) for s in pa["random"]]
        out.append({"pair": f"{a['session']} -> {b['session']}",
                    "laya": len(set(pa["laya"]) & set(pb["laya"])),
                    "deterministic": len(set(pa["deterministic"]) & set(pb["deterministic"])),
                    "random_same_seed": _dist(rnd)})
    return out


def sensitivity_cards() -> list[tuple[str, CandidateCard]]:
    """Plan §5: base, 14 one-field variants, 'all notable', duplicate base."""
    specs: list[tuple[str, dict[str, float]]] = [("base", dict(SENS_BASE))]
    for f, (lo, hi) in SENS_VARIANTS.items():
        specs += [(f"{f}=low", {**SENS_BASE, f: lo}), (f"{f}=high", {**SENS_BASE, f: hi})]
    specs += [("all_notable", dict(SENS_ALL_NOTABLE)), ("base_duplicate", dict(SENS_BASE))]
    ids = make_row_ids([label for label, _ in specs], 0, "synthetic-sensitivity-v1")
    return [(label, build_card(ids[label], values)) for label, values in specs]


def sensitivity_table(labelled: Sequence[tuple[str, CandidateCard]], scores: Mapping[str, ScoreRecord]) -> dict[str, Any]:
    p = {label: scores[c.row_id].review_probability for label, c in labelled}
    base = p["base"]
    rows = []
    for f, (lo, hi) in SENS_VARIANTS.items():
        dl, dh = round(p[f"{f}=low"] - base, 4), round(p[f"{f}=high"] - base, 4)
        rows.append({"field": f, "low": lo, "high": hi, "delta_low": dl, "delta_high": dh,
                     "moves": max(abs(dl), abs(dh)) >= SENS_MOVE_THRESHOLD})
    return {"base": base, "all_notable": p["all_notable"], "all_notable_delta": round(p["all_notable"] - base, 4),
            "duplicate_identical": p["base_duplicate"] == base, "fields": rows,
            "threshold": SENS_MOVE_THRESHOLD}


# -- run (I/O) ---------------------------------------------------------------

def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_against_smoke(snapshot: Mapping[str, Any], cards: Sequence[CandidateCard],
                        latest: Mapping[str, date], scores: Mapping[str, ScoreRecord]) -> dict[str, Any]:
    """Rebuilt cards restricted to the smoke run's eligible rows must reproduce
    its stored inputs_digest; compare fresh Laya scores with the stored ones."""
    rows = {r["row_id"]: r for r in snapshot["rows"]}
    subset = [c for c in cards if c.row_id in rows]
    sub_latest = {k: v for k, v in latest.items() if k in rows}
    diffs = [abs(scores[i].review_probability - r["review_probability"]) for i, r in rows.items()
             if r["review_probability"] is not None and i in scores]
    return {"smoke_rows": len(rows), "rebuilt_rows_found": len(subset),
            "inputs_digest_match": inputs_digest(subset, sub_latest) == snapshot["inputs_digest"],
            "scores_compared": len(diffs), "scores_identical": sum(d == 0 for d in diffs),
            "max_abs_score_diff": round(max(diffs), 6) if diffs else None}


def run(*, fetch_history: Callable[[Sequence[str], date], Mapping[str, Any]], scorer: Any,
        scorer_runtime: Mapping[str, Any], out_dir: Path, smoke_dir: Path | None,
        clock: Callable[[], float] = time.monotonic) -> Path:
    from laya_demo import _canon, _git, _write_once
    from laya_preflight import load_frozen_pool

    universe, pool = load_frozen_pool(POOL_PATH)
    if pool["content_sha256"] != POOL_CONTENT_SHA256 or universe.hash != POOL_UNIVERSE_HASH:
        raise SystemExit("frozen pool hash differs from the plan; refusing to run")
    symbols = list(universe.symbols)
    row_ids = make_row_ids(symbols, ROW_ID_SEED, universe.hash)
    timing: dict[str, Any] = {"card_retrieval_s": {}, "warm_score_s": {}, "controls_s": {}}

    days: list[dict[str, Any]] = []
    for session in SESSIONS:                                  # 1. cards, all days, before any scoring
        t = clock()
        history = fetch_history(symbols, session)
        cards, latest, reasons = build_day_cards(history, symbols, session, row_ids)
        timing["card_retrieval_s"][session.isoformat()] = round(clock() - t, 1)
        stale = {rid for rid, d in latest.items() if d < session}
        for rid in stale:
            reasons.setdefault(rid, "stale_no_session_bar")
        current = [c for c in cards if c.data_complete and c.row_id not in stale]
        days.append({"session": session, "cards": cards, "latest": latest, "reasons": reasons, "current": current,
                     "digest": inputs_digest(current, {c.row_id: latest[c.row_id] for c in current})})

    t = clock()
    scorer._start()                                           # 2. cold start (model load + handshake + isolation)
    timing["cold_start_s"] = round(clock() - t, 1)

    for d in days:                                            # 3. warm scoring, one batch per day, in order
        t = clock()
        d["scores"] = score_complete_cards(scorer, d["current"])
        timing["warm_score_s"][d["session"].isoformat()] = round(clock() - t, 1)
    labelled = sensitivity_cards()                            # 4. synthetic batch
    t = clock()
    sens_scores = score_complete_cards(scorer, [c for _, c in labelled])
    timing["synthetic_score_s"] = round(clock() - t, 1)

    public_days, private_days = [], []
    for d in days:
        t = clock()
        res = analyse_day(d["session"], d["current"], d["scores"],
                          {c.row_id: d["latest"][c.row_id] for c in d["current"]})
        timing["controls_s"][d["session"].isoformat()] = round(clock() - t, 4)
        smoke = None
        if smoke_dir:
            p = Path(smoke_dir) / f"{d['session'].isoformat()}.json"
            if p.exists():
                smoke = check_against_smoke(json.loads(p.read_text(encoding="utf-8")), d["cards"], d["latest"], d["scores"])
        reasons = sorted(d["reasons"].values())
        public_days.append({**res["public"], "cards_total": len(d["cards"]),
                            "unavailable": len(d["cards"]) - len(d["current"]),
                            "unavailable_reasons": {r: reasons.count(r) for r in set(reasons)},
                            "inputs_digest": d["digest"], "smoke_check": smoke})
        private_days.append({"session": d["session"].isoformat(), "shortlists": res["private"],
                             "cards": [{"row_id": c.row_id, **{f: getattr(c, f) for f in NUMERIC_FIELDS},
                                        "data_complete": c.data_complete,
                                        "latest_bar_date": d["latest"].get(c.row_id, None) and d["latest"][c.row_id].isoformat()}
                                       for c in d["cards"]],
                             "scores": {i: [s.review_probability, s.answer_confidence] for i, s in d["scores"].items()}})

    summary = {
        "experiment_version": EXPERIMENT_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_sha256": _sha_file(PLAN_PATH), "runner_sha256": _sha_file(Path(__file__)),
        "code": _git(),
        "pool": {"path": str(POOL_PATH.relative_to(REPO)).replace("\\", "/"), "content_sha256": pool["content_sha256"],
                 "evidence_sha256": pool["qualification"]["evidence_sha256"], "universe_hash": universe.hash,
                 "members": len(symbols)},
        "scorer": {**laya_adapter.expected_metadata().__dict__, **scorer_runtime},
        "settings": {"k": K, "row_id_seed": ROW_ID_SEED, "tie_break_seed": TIE_BREAK_SEED,
                     "random_seeds": list(RANDOM_SEEDS), "deterministic": "abs(five_day_return) desc, seeded row-ID tie-break"},
        "timing": timing, "days": public_days, "turnover": turnover(private_days),
        "sensitivity": sensitivity_table(labelled, sens_scores),
    }
    run_dir = Path(out_dir) / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    if run_dir.exists():
        raise SystemExit(f"{run_dir} exists; outputs are write-once")
    summary_text = _canon(summary)
    _write_once(run_dir / "public_summary.json", summary_text)
    _write_once(run_dir / "private_results.json", _canon({
        "PRIVATE": "cards, scores and shortlists by row ID. Never commit or publish.",
        "public_summary_sha256": hashlib.sha256(summary_text.encode()).hexdigest(),
        "days": private_days,
        "sensitivity_scores": {label: sens_scores[c.row_id].review_probability for label, c in labelled}}))
    _write_once(run_dir / "private_mapping.json", _canon({
        "PRIVATE": "row_id -> ticker. Never commit or share.", "row_id_seed": ROW_ID_SEED,
        "tickers": {rid: s for s, rid in row_ids.items()}}))
    return run_dir


def _real_run(args: argparse.Namespace) -> Path:
    from laya_experiment import LAYA_WORKER_IMAGE, LayaWorkerScorer, _run_docker, verify_docker_worker_available
    from run_backtest import fetch_card_history

    verify_docker_worker_available(args.checkpoint, LAYA_WORKER_IMAGE)
    image_id = _run_docker(["image", "inspect", LAYA_WORKER_IMAGE, "--format", "{{.Id}}"], 30).stdout.strip()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scorer = LayaWorkerScorer(args.checkpoint, image=LAYA_WORKER_IMAGE,
                              log_path=OUT_DIR / f".worker_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log")
    try:
        return run(fetch_history=fetch_card_history, scorer=scorer, out_dir=OUT_DIR,
                   smoke_dir=Path(args.smoke_snapshots) if args.smoke_snapshots else None,
                   scorer_runtime={"image": LAYA_WORKER_IMAGE, "image_id": image_id,
                                   "weights_sha256": laya_adapter.LAYA_SHA256["model.safetensors"],
                                   "isolation": "docker --network none, read-only, verified by inspect"})
    finally:
        scorer.close()


def main(argv: Sequence[str] | None = None) -> int:
    from laya_demo import DEFAULT_CHECKPOINT

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch cards, score Laya once, apply controls, write private outputs")
    r.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    r.add_argument("--smoke-snapshots", default=None, help=f"selector_snapshots/{SMOKE_RUN_ID} dir to verify against")
    args = ap.parse_args(argv)
    print(_real_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
