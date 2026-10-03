"""
laya_demo.py -- standalone, current-data demonstration of the experimental
fixed-30 Laya selector (docs/scopes/LAYA_SELECTOR_V1.md). No Hermes, no
trades, no backtest state, no Neon, no dashboard: it only reads prices and
scores cards.

    python backtest-harness/laya_demo.py run              # fetch, score, save, print
    python backtest-harness/laya_demo.py replay <artifact.json>   # re-check + print, no fetch/score

`run`:
  1. loads the frozen watchlist/experimental_fixed30.json universe;
  2. fetches daily bars through maverick's existing price path
     (run_backtest.fetch_card_history -> laya_cards.history_script);
  3. picks the cutoff = the latest COMPLETED US session: the newest date on
     which at least half the names have a bar, no later than today (US/Eastern)
     once 17:00 ET has passed, else yesterday. Bars after the cutoff are
     dropped (laya_cards._clean_bars); a name with no cutoff bar is stale;
  4. builds the seven-number cards under random-salted opaque row IDs
     (laya_cards.build_day_cards) -- the model sees only those numbers;
  5. scores every complete card in ONE batch in the pinned, isolated Laya
     worker container (laya_experiment.LayaWorkerScorer; loaded once);
  6. ranks by review_probability and selects up to 10 (laya_selector) --
     answer_confidence is recorded, never used to select;
  7. writes a write-once artifact (no tickers, names, or free text) plus a
     separate private row-ID -> ticker mapping, both git-ignored.

review_probability is the model's P(true) for "is this card worth the full
review?" -- a review-priority signal from an uncalibrated model, not a buy
signal, a profit estimate, or evidence of market judgment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import laya_adapter
from laya_cards import build_day_cards
from laya_selector import (
    NUMERIC_FIELDS, STATUS_DATA_UNAVAILABLE, STATUS_SELECTED, CardScorer, FixedUniverse,
    ScoreRecord, build_card, build_snapshot, inputs_digest, load_universe, make_row_ids,
    score_complete_cards, select_pool, snapshot_to_dict,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEMO_DIR = HERE / ".laya_demo"            # git-ignored runtime output
DEMO_VERSION = "laya-selector-demo-v1"
ARTIFACT_VERSION = 1
TIE_BREAK_SEED = 0                        # public; row IDs use a separate private random salt
SESSION_DONE_ET = dtime(17, 0)            # a session counts as complete after 17:00 ET
ET = ZoneInfo("America/New_York")
DEFAULT_CHECKPOINT = (Path.home() / ".cache" / "huggingface" / "hub"
                      / "models--convaiinnovations--laya" / "snapshots" / laya_adapter.LAYA_REVISION)


class DemoFailure(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"{stage}: {message}")
        self.stage = stage


# -- cutoff ----------------------------------------------------------------

def latest_allowed_session(now_utc: datetime) -> date:
    """Latest calendar date that could be a completed session at `now_utc`."""
    now_et = now_utc.astimezone(ET)
    return now_et.date() if now_et.time() >= SESSION_DONE_ET else now_et.date() - timedelta(days=1)


def pick_cutoff(history: Mapping[str, Any], now_utc: datetime) -> tuple[date, dict[str, Any]]:
    """The newest date <= latest_allowed_session on which at least half the
    names that returned bars have one (so a holiday or one name's gap never
    moves it). Raises DemoFailure if no such date exists."""
    allowed = latest_allowed_session(now_utc)
    with_bars = [v for v in history.values() if isinstance(v, (list, tuple)) and v]
    counts: dict[date, int] = {}
    for bars in with_bars:
        for d in {str(b.get("date", ""))[:10] for b in bars if isinstance(b, Mapping)}:
            try:
                day = date.fromisoformat(d)
            except ValueError:
                continue
            if day <= allowed:
                counts[day] = counts.get(day, 0) + 1
    quorum = max(1, (len(with_bars) + 1) // 2)
    sessions = sorted((d for d, n in counts.items() if n >= quorum), reverse=True)
    if not sessions:
        raise DemoFailure("cutoff", "no date on or before the latest completed session has bars "
                          f"for at least half the names ({len(with_bars)} names returned bars)")
    cutoff = sessions[0]
    weekday = allowed
    while weekday.weekday() >= 5:
        weekday -= timedelta(days=1)
    now_et = now_utc.astimezone(ET)
    return cutoff, {
        "session": cutoff.isoformat(),
        "rule": (f"newest date with bars for >= half the names, not after today (US/Eastern) "
                 f"once {SESSION_DONE_ET:%H:%M} ET has passed, else yesterday; later bars dropped"),
        "now_utc": now_utc.isoformat(timespec="seconds"),
        "now_et": now_et.isoformat(timespec="seconds"),
        "latest_allowed_date": allowed.isoformat(),
        "names_with_cutoff_bar": counts[cutoff],
        "note": (None if cutoff == weekday else
                 f"cutoff precedes the last completed weekday {weekday} (market holiday or provider lag)"),
    }


# -- artifact I/O ----------------------------------------------------------

def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def content_sha256(artifact: Mapping[str, Any]) -> str:
    body = {k: v for k, v in artifact.items() if k != "content_sha256"}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def _write_once(path: Path, text: str) -> None:
    """Atomic create-if-absent (temp file + os.link): never overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


def _git() -> dict[str, Any]:
    def g(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        return {"head": g("rev-parse", "HEAD") or None, "dirty": bool(g("status", "--porcelain"))}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}


# -- run -------------------------------------------------------------------

def run_demo(
    *, now_utc: datetime, universe: FixedUniverse,
    fetch_history: Callable[[Sequence[str], date], Mapping[str, Any]],
    scorer: CardScorer, scorer_runtime: Mapping[str, Any], out_dir: Path,
    clock: Callable[[], float] = time.monotonic, git: Mapping[str, Any] | None = None,
) -> Path:
    """Fetch -> cutoff -> cards -> one batched score -> select -> write-once
    artifact + private mapping. Returns the artifact path. Raises DemoFailure
    (naming the stage) and writes nothing on any failure."""
    t0 = clock()
    timing: dict[str, float] = {}
    symbols = list(universe.symbols)
    try:
        history = fetch_history(symbols, now_utc.astimezone(ET).date())
    except Exception as exc:
        raise DemoFailure("fetch", f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    timing["fetch_s"] = round(clock() - t0, 1)

    cutoff, cutoff_info = pick_cutoff(history, now_utc)
    salt = secrets.randbits(63)
    row_ids = make_row_ids(symbols, salt, universe.hash)
    cards, latest, build_reasons = build_day_cards(history, symbols, cutoff, row_ids)
    rows_pre = select_pool(cards, {}, as_of=cutoff, seed=TIE_BREAK_SEED, latest_bar_dates=latest)
    stale = {r.row_id for r in rows_pre if r.freshness == "stale"}
    to_score = [c for c in cards if c.data_complete and c.row_id not in stale]
    if not to_score:
        raise DemoFailure("cards", f"no complete, current card for cutoff {cutoff} "
                                   f"({len(build_reasons)} incomplete, {len(stale)} stale)")

    t1 = clock()
    try:
        scores = score_complete_cards(scorer, to_score)
    except Exception as exc:
        raise DemoFailure("score", f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    timing["score_s"] = round(clock() - t1, 1)

    snap = build_snapshot(universe=universe, scorer=scorer.metadata, as_of=cutoff, seed=TIE_BREAK_SEED,
                          cards=cards, scores=scores, latest_bar_dates=latest)
    unavailable = {r.row_id: (build_reasons.get(r.row_id)
                              or ("stale_no_cutoff_bar" if r.row_id in stale else "not_scored"))
                   for r in snap.rows if r.status == STATUS_DATA_UNAVAILABLE}
    timing["total_s"] = round(clock() - t0, 1)

    artifact = {
        "artifact_version": ARTIFACT_VERSION, "demo_version": DEMO_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cutoff": cutoff_info, "timing": timing,
        "universe": {"source": "watchlist/experimental_fixed30.json", "version": universe.version,
                     "hash": universe.hash, "count": len(symbols)},
        "scorer": {**snapshot_to_dict(snap)["scorer"], **scorer_runtime},
        "code": dict(git if git is not None else _git()),
        "cards": [{"row_id": c.row_id, **{n: getattr(c, n) for n in NUMERIC_FIELDS},
                   "data_complete": c.data_complete,
                   "latest_bar_date": latest[c.row_id].isoformat() if c.row_id in latest else None}
                  for c in sorted(cards, key=lambda c: c.row_id)],
        "model_inputs": {c.row_id: laya_adapter.card_to_state(c) for c in to_score},
        "unavailable": unavailable,
        "selection": snapshot_to_dict(snap),
    }
    artifact["content_sha256"] = content_sha256(artifact)
    demo_id = f"{cutoff.isoformat()}_{now_utc.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    run_dir = Path(out_dir) / demo_id
    if run_dir.exists():
        raise DemoFailure("write", f"{run_dir} already exists; artifacts are write-once")
    path = run_dir / "artifact.json"
    errors = {row_ids[s]: str(history[s].get("error"))[:200]
              for s in symbols if isinstance(history.get(s), Mapping)}
    _write_once(run_dir / "private_mapping.json", _canon({
        "PRIVATE": "row_id -> ticker for one demo artifact. Never commit or share.",
        "artifact_content_sha256": artifact["content_sha256"], "row_id_salt": salt,
        "tickers": {rid: s for s, rid in row_ids.items()}, "fetch_errors": errors}))
    _write_once(path, _canon(artifact))
    return path


# -- replay ----------------------------------------------------------------

def replay(path: Path) -> dict[str, Any]:
    """Re-verify a saved artifact WITHOUT fetching or scoring: content hash,
    card validity, inputs digest, and that re-running the pure selection on
    the stored cards + stored scores reproduces the stored rows exactly.
    Raises DemoFailure("replay", ...) on any mismatch."""
    art = json.loads(Path(path).read_text(encoding="utf-8"))
    if art.get("artifact_version") != ARTIFACT_VERSION:
        raise DemoFailure("replay", f"unsupported artifact_version {art.get('artifact_version')!r}")
    if content_sha256(art) != art.get("content_sha256"):
        raise DemoFailure("replay", "content_sha256 mismatch: the artifact was altered")
    sel = art["selection"]
    cards, latest = [], {}
    for c in art["cards"]:
        cards.append(build_card(c["row_id"], {n: c[n] for n in NUMERIC_FIELDS}))
        if cards[-1].data_complete != c["data_complete"]:
            raise DemoFailure("replay", f"{c['row_id']}: data_complete flag disagrees with its numbers")
        if c["latest_bar_date"]:
            latest[c["row_id"]] = date.fromisoformat(c["latest_bar_date"])
    if inputs_digest(cards, latest) != sel["inputs_digest"]:
        raise DemoFailure("replay", "inputs_digest mismatch: cards differ from the scored inputs")
    by_id = {c.row_id: c for c in cards}
    for rid, state in art["model_inputs"].items():
        if rid not in by_id or state != laya_adapter.card_to_state(by_id[rid]):
            raise DemoFailure("replay", f"{rid}: stored model input is not exactly the card's seven numbers")
    scores = {r["row_id"]: ScoreRecord(r["row_id"], r["review_probability"], r["answer_confidence"])
              for r in sel["rows"] if r["review_probability"] is not None}
    rows = select_pool(cards, scores, as_of=date.fromisoformat(sel["as_of"]), seed=sel["seed"],
                       latest_bar_dates=latest, k=sel["k"])
    if [r.__dict__ for r in rows] != sel["rows"]:
        raise DemoFailure("replay", "re-running the selection on the stored scores does not reproduce the stored rows")
    return art


def load_mapping(artifact_path: Path, art: Mapping[str, Any]) -> dict[str, str] | None:
    p = Path(artifact_path).with_name("private_mapping.json")
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    return m["tickers"] if m.get("artifact_content_sha256") == art["content_sha256"] else None


def summary(art: Mapping[str, Any], mapping: Mapping[str, str] | None, path: Path) -> str:
    name = (lambda rid: mapping.get(rid, rid)) if mapping else (lambda rid: rid)
    sel, rows = art["selection"], art["selection"]["rows"]
    scored = [r for r in rows if r["rank"] is not None]
    picked = [r for r in scored if r["status"] == STATUS_SELECTED]
    out = [
        f"Laya fixed-30 demo -- cutoff session {art['cutoff']['session']} "
        f"(run {art['cutoff']['now_utc']}; {art['cutoff']['note'] or 'latest completed weekday'})",
        f"  universe {art['universe']['version']} ({art['universe']['count']} names, hash {art['universe']['hash'][:12]}); "
        f"scorer laya {art['scorer']['package_version']} @ {str(art['scorer']['revision'])[:12]}, "
        f"question {json.loads(art['scorer']['settings']).get('question_format', 'v1')}",
        "  considered: " + ", ".join(sorted(name(c["row_id"]) for c in art["cards"])),
        f"  cards scored: {len(scored)} of {len(rows)} (one batch); selected {len(picked)} of k={sel['k']}"
        + ("" if len(picked) >= sel["k"] else f"  [SHORTFALL: only {len(picked)} scorable]"),
        "", "  rank  name   review_probability  answer_confidence (logged only)",
    ]
    for r in picked:
        out.append(f"  {r['rank']:>4}  {name(r['row_id']):<6} {r['review_probability']:>18.4f}  {r['answer_confidence']:>17.4f}")
    if scored:
        ps = [r["review_probability"] for r in scored]
        out.append(f"  all scored: min {min(ps):.4f}  max {max(ps):.4f}  distinct {len(set(ps))}/{len(ps)}"
                   f"  (rank 10 vs 11 gap: {(ps[9] - ps[10]) if len(ps) > 10 else float('nan'):.4f})")
    unavail = art["unavailable"]
    out.append("  unavailable: " + (", ".join(f"{name(rid)} ({why})" for rid, why in sorted(unavail.items()))
                                    if unavail else "none"))
    t = art["timing"]
    out += [f"  elapsed: total {t['total_s']}s (fetch {t['fetch_s']}s, model load + score {t['score_s']}s)",
            f"  artifact: {path}" + ("" if mapping else "  (no private mapping found: row IDs shown)"),
            "  NOTE: review_probability = an uncalibrated model's 'worth a full review?' P(true); not a buy",
            "        signal or market judgment. Synthetic checks show only weak state sensitivity."]
    return "\n".join(out)


# -- CLI -------------------------------------------------------------------

def _real_run(args: argparse.Namespace) -> Path:
    from laya_experiment import LAYA_WORKER_IMAGE, LayaWorkerScorer, _run_docker, verify_docker_worker_available
    from run_backtest import fetch_card_history

    image = args.image or LAYA_WORKER_IMAGE
    try:
        verify_docker_worker_available(args.checkpoint, image)
    except SystemExit as exc:
        raise DemoFailure("preflight", str(exc)) from None
    image_id = _run_docker(["image", "inspect", image, "--format", "{{.Id}}"], 30).stdout.strip()
    now = datetime.now(timezone.utc)
    log = Path(args.out) / f".worker_{now:%Y%m%dT%H%M%SZ}.log"
    scorer = LayaWorkerScorer(args.checkpoint, image=image, log_path=log)
    try:
        return run_demo(now_utc=now, universe=load_universe(), fetch_history=fetch_card_history,
                        scorer=scorer, out_dir=Path(args.out),
                        scorer_runtime={"image": image, "image_id": image_id,
                                        "weights_sha256": laya_adapter.LAYA_SHA256["model.safetensors"],
                                        "isolation": "docker --network none, read-only, verified by inspect"})
    finally:
        scorer.close()


def main(argv: Sequence[str] | None = None, run: Callable[[argparse.Namespace], Path] = _real_run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch real prices, score with the pinned Laya worker, save + print")
    r.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    r.add_argument("--image", default=None, help="worker image tag (default: laya_experiment.LAYA_WORKER_IMAGE)")
    r.add_argument("--out", default=str(DEMO_DIR))
    p = sub.add_parser("replay", help="re-verify and print a saved artifact (no fetch, no scoring)")
    p.add_argument("artifact")
    args = ap.parse_args(argv)
    try:
        path = run(args) if args.cmd == "run" else Path(args.artifact)
        art = replay(path)
    except DemoFailure as exc:
        print(f"LAYA DEMO FAILED at stage '{exc.stage}': {exc}", file=sys.stderr)
        print("No artifact was written." if args.cmd == "run" else "Artifact NOT verified.", file=sys.stderr)
        return 2 if exc.stage == "preflight" else 1
    print(("REPLAY (verified, nothing fetched or scored)\n" if args.cmd == "replay" else "")
          + summary(art, load_mapping(path, art), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
