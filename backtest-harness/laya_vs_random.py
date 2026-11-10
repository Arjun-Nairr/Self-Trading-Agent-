"""
laya_vs_random.py -- the explicit entry point for ONE user-authorized paired
experiment: the production Laya selector vs. a seeded uniform-random control,
each driving one fresh July 2022 Hermes episode over the same frozen pool.
Plan: docs/evaluations/laya_vs_random_plan_2026-10-06.md. Config (frozen,
committed): experiments/laya_vs_random_2022_07.json.

    python backtest-harness/laya_vs_random.py check            # read-only readiness + counts
    python backtest-harness/laya_vs_random.py run --arm laya --laya-checkpoint <dir>
    python backtest-harness/laya_vs_random.py run --arm random
    python backtest-harness/laya_vs_random.py --config <frozen config> run --arm laya ...   # another experiment

`--config` selects another frozen, committed experiment config (default: the
v1 pair below). The chosen config drives the code fingerprint, the run's
marker identity and its output directory (.laya_vs_random/<experiment_id>/).
A config may also pin `prompt_schema_version` (must equal the code's) and
`authorized_arms` (arms outside it are refused), and an explicit
`pool_coverage_exception` (an experiment-only waiver of the pool's pre-start
news-coverage rule, recorded in every arm's marker and result.json; daily news
checks are unaffected). Optional backtest tracing
(backtest_tracing.py) runs from the frozen masking through episode-end
resolution and is closed in `finally`; it is export-only and fail-open.

Not the ordinary backtest CLI and not eval_suite: neither exposes this paired
selector design, and the fixed-watchlist manifest's approval is NOT this
experiment's approval. Authorization is the committed config (status
"authorized", user-authorized 2026-10-06) pinned to the committed plan's hash.

Each arm is one fresh, non-resumable episode, registered as run_kind
"evaluation", through run_backtest's unchanged run_day/_run_cycle_or_halt
loop (the same shape as eval_suite._run_episode), with eval_suite's
episode-boundary semantics (resolve_episode_end_pending: terminal pending
orders rejected, open holdings kept and marked, no forced liquidation).
Guards: committed and clean experiment code; arm order laya -> random; an arm
never runs twice (write-once marker before any registration or Hermes call);
random only after a COMPLETED laya arm under identical code fingerprints; a
halted arm stops the experiment. All outputs are private and git-ignored
under .laya_vs_random/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import eval_suite as es
import run_backtest as rb
from anonymize import RunMasking

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG_PATH = HERE / "experiments" / "laya_vs_random_2022_07.json"
OUT_ROOT = HERE / ".laya_vs_random"
ARMS = ("laya", "random")
RUN_KIND = es.EVAL_RUN_KIND
# Experiment-relevant code/config; must be committed and clean, and identical across both arms.
CODE_FILES = (
    "backtest-harness/laya_vs_random.py", "backtest-harness/run_backtest.py", "backtest-harness/eval_suite.py",
    "backtest-harness/laya_experiment.py", "backtest-harness/laya_selector.py", "backtest-harness/laya_cards.py",
    "backtest-harness/laya_adapter.py", "backtest-harness/laya_worker.py", "backtest-harness/laya_preflight.py",
    "backtest-harness/anonymize.py", "risk-checker/risk_checker.py", "RULES.md", "AGENTS.md", "SOUL.md",
    "docker-compose.yml",
    # optional controller-side tracing (export-only) and the shared tracer it reuses
    "backtest-harness/backtest_tracing.py", "live/tracing/__init__.py", "live/tracing/otlp.py",
    "live/tracing/controller.py",
)


def _sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()


# ---------------------------------------------------------------- config + provenance
def load_config(path: Path = CONFIG_PATH) -> dict:
    """The frozen experiment config, verified: authorized, plan hash, pool, and (if pinned) prompt schema."""
    path = Path(path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if cfg.get("status") != "authorized":
        raise SystemExit(f"{path.name}: status {cfg.get('status')!r} is not 'authorized'")
    if "prompt_schema_version" in cfg and cfg["prompt_schema_version"] != rb.PROMPT_SCHEMA_VERSION:
        raise SystemExit(f"{path.name}: prompt_schema_version {cfg['prompt_schema_version']!r} is not the "
                         f"code's {rb.PROMPT_SCHEMA_VERSION!r}")
    if tuple(cfg["arms"]) != ARMS:
        raise SystemExit(f"{path.name}: arms must be exactly {ARMS} in that order")
    plan = REPO / cfg["plan"]["path"]
    if _sha_file(plan) != cfg["plan"]["sha256"]:
        raise SystemExit(f"{plan.name}: SHA-256 differs from the config's frozen plan hash")
    coverage_exception(cfg, path.name)
    return cfg


EXCEPTION_KEYS = ("scope", "rule", "window", "members", "failing", "price_coverage", "daily_news_checks",
                  "preflight_report", "preflight_sha256")


def coverage_exception(cfg: Mapping[str, Any], name: str = "config") -> dict | None:
    """An experiment-only waiver of the pool's pre-start news-coverage rule, or None.

    Explicit and checkable: the rule it waives must be the current one, its window must be the config's, and the
    private preflight report it cites (git-ignored, under .laya_vs_random/) must exist with the pinned hash. It
    waives only the pool-qualification rule; every daily decision-time and pre-fill news check stays in force."""
    exc = cfg.get("pool_coverage_exception")
    if exc is None:
        return None
    from laya_preflight import NEWS_RULE_VERSION
    missing = [k for k in EXCEPTION_KEYS if k not in exc]
    if missing:
        raise SystemExit(f"{name}: pool_coverage_exception lacks {missing}")
    if exc["scope"] != "this experiment only" or exc["rule"] != NEWS_RULE_VERSION or exc["window"] != cfg["window"]:
        raise SystemExit(f"{name}: pool_coverage_exception must be scoped to this experiment, waive the current rule "
                         f"{NEWS_RULE_VERSION!r} and cover exactly the config's window")
    report = HERE / exc["preflight_report"]
    if not report.is_file() or _sha_file(report) != exc["preflight_sha256"]:
        raise SystemExit(f"{name}: preflight report {exc['preflight_report']} is missing or not the pinned version")
    return dict(exc)


def load_pool(cfg: Mapping[str, Any]):
    from laya_preflight import load_frozen_pool
    universe, pool = load_frozen_pool(REPO / cfg["pool"]["path"])
    got = {"content_sha256": pool["content_sha256"], "evidence_sha256": pool["qualification"]["evidence_sha256"]}
    for key, value in got.items():
        if value != cfg["pool"][key]:
            raise SystemExit(f"frozen pool {key} {value[:12]} != config {cfg['pool'][key][:12]}")
    if universe.hash != cfg["pool"]["universe_hash"]:
        raise SystemExit("frozen pool universe hash differs from the config")
    return universe, pool


def code_fingerprint(config_path: Path = CONFIG_PATH) -> dict:
    """HEAD plus the SHA-256 of every experiment file and THIS run's config.
    Refuses uncommitted or dirty experiment files (provenance must point at
    committed code)."""
    files = (*CODE_FILES, str(Path(config_path).resolve().relative_to(REPO.resolve())).replace("\\", "/"))
    dirty = _git("status", "--porcelain", "--", *files)
    if dirty:
        raise SystemExit(f"experiment files are not committed/clean:\n{dirty}")
    return {"head": _git("rev-parse", "HEAD"), "files": {f: _sha_file(REPO / f) for f in files}}


def masking_for(universe, watchlist: list[dict], cfg: Mapping[str, Any]) -> RunMasking:
    """Identical in both arms: same pool, seed, run start, opaque scheme."""
    return RunMasking(rb._names_for_masking(watchlist), run_start=date.fromisoformat(cfg["window"]["start"]),
                      seed=int(cfg["masking_seed"]), alias_scheme="opaque",
                      word_like_tickers=list(universe.word_like_tickers))


# ---------------------------------------------------------------- arm guards
def _arm_dir(cfg: Mapping[str, Any], arm: str, root: Path = OUT_ROOT) -> Path:
    return Path(root) / cfg["experiment_id"] / arm


def _read_marker(cfg: Mapping[str, Any], arm: str, root: Path = OUT_ROOT) -> dict | None:
    p = _arm_dir(cfg, arm, root) / "ARM.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def check_arm_allowed(cfg: Mapping[str, Any], arm: str, fingerprint: Mapping[str, Any],
                      root: Path = OUT_ROOT) -> None:
    """Pure guard: an arm runs once; random only after a COMPLETED laya arm
    with the identical code fingerprint; nothing after a halted arm."""
    if arm not in ARMS:
        raise SystemExit(f"unknown arm {arm!r}")
    if "authorized_arms" in cfg and arm not in cfg["authorized_arms"]:
        raise SystemExit(f"arm {arm!r} is not authorized by {cfg['experiment_id']} "
                         f"(authorized: {list(cfg['authorized_arms'])})")
    if _arm_dir(cfg, arm, root).exists():
        raise SystemExit(f"arm {arm!r} already started (no retries, no resumes): {_arm_dir(cfg, arm, root)}")
    if arm == "random":
        laya = _read_marker(cfg, "laya", root)
        if laya is None or laya.get("status") != "completed":
            raise SystemExit("the random arm runs only after a COMPLETED laya arm "
                             f"(laya status: {None if laya is None else laya.get('status')!r})")
        if laya.get("fingerprint") != dict(fingerprint):
            raise SystemExit("code/config changed since the laya arm; the pair would not be comparable")


def _write_marker(arm_dir: Path, body: Mapping[str, Any], exclusive: bool) -> None:
    p = arm_dir / "ARM.json"
    text = json.dumps(body, indent=1, sort_keys=True)
    if exclusive:
        with open(p, "x", encoding="utf-8") as f:
            f.write(text)
    else:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)


# ---------------------------------------------------------------- fact recording (hash-only)
def _canon_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def recording(fn: Callable, kind: str, sink: Path) -> Callable:
    """Wrap a per-ticker fetch (tickers, ...) -> {ticker: result}: return
    the result unchanged and append one sha256 per (kind, ticker, args) to
    `sink`, so facts the two arms both requested can be compared."""
    def wrapped(tickers, *args, **kwargs):
        out = fn(tickers, *args, **kwargs)
        key_args = [a.isoformat() if hasattr(a, "isoformat") else str(a) for a in args]
        with open(sink, "a", encoding="utf-8") as f:
            for t in tickers:
                f.write(json.dumps({"kind": kind, "ticker": t, "args": key_args,
                                    "sha256": _canon_sha(out.get(t))}) + "\n")
        return out
    return wrapped


def compare_facts(a_path: Path, b_path: Path) -> dict:
    """Facts both arms requested with the same (kind, ticker, args): count
    and list every key whose hashes differ (or that one arm saw twice
    with different hashes)."""
    def load(p: Path) -> dict:
        seen: dict = {}
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            seen.setdefault((r["kind"], r["ticker"], tuple(r["args"])), set()).add(r["sha256"])
        return seen
    a, b = load(a_path), load(b_path)
    shared = sorted(set(a) & set(b))
    diffs = [k for k in shared if a[k] != b[k] or len(a[k]) > 1]
    internal = [k for k in set(a) | set(b) if len(a.get(k, ())) > 1 or len(b.get(k, ())) > 1]
    by_kind: dict = {}
    for k in shared:
        by_kind.setdefault(k[0], [0, 0])[0] += 1
    for k in diffs:
        by_kind[k[0]][1] += 1
    return {"shared": len(shared), "differing": len(diffs), "by_kind_shared_differing": by_kind,
            "differing_keys": [[k[0], k[1], list(k[2])] for k in diffs], "within_arm_inconsistent": len(internal)}


# ---------------------------------------------------------------- the arm
def _start_tracing(run_id: str, masking: RunMasking, meta: dict):
    """The arm's optional TraceSession (or None). Never raises; prints only an on/off status."""
    try:
        import backtest_tracing
        session, status = backtest_tracing.start(rb, run_id=run_id, masking=masking, meta=meta)
    except Exception as exc:
        session, status = None, f"off ({type(exc).__name__})"
    print(f"backtest tracing: {status}")
    return session


def run_arm(arm: str, laya_checkpoint: str | None, config_path: Path = CONFIG_PATH,
            root: Path | None = None) -> int:
    config_path = Path(config_path)
    root = Path(root) if root is not None else OUT_ROOT
    cfg = load_config(config_path)
    universe, _pool = load_pool(cfg)
    fp = code_fingerprint(config_path)
    check_arm_allowed(cfg, arm, fp, root)
    if arm == "laya":
        if not laya_checkpoint:
            raise SystemExit("--laya-checkpoint is required for the laya arm")
        from laya_experiment import verify_docker_worker_available
        verify_docker_worker_available(laya_checkpoint, cfg["laya"]["image"])
        image_id = subprocess.run(["docker", "image", "inspect", cfg["laya"]["image"], "--format", "{{.Id}}"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        if image_id != cfg["laya"]["image_id"]:
            raise SystemExit(f"worker image {image_id[:19]} is not the pinned {cfg['laya']['image_id'][:19]}")
    watchlist = rb._experimental_watchlist_entries(universe)
    start, end = date.fromisoformat(cfg["window"]["start"]), date.fromisoformat(cfg["window"]["end"])
    seed = int(cfg["masking_seed"])

    arm_dir = _arm_dir(cfg, arm, root)
    isolation_report = rb.preflight()                # services incl. execution-sim + disposable Hermes profile
    runtime = isolation_report["runtime"]
    selector = tracing = None
    saved = {n: getattr(rb, n) for n in ("fetch_technicals", "fetch_news", "fetch_news_window")}
    marker = {"experiment_id": cfg["experiment_id"], "arm": arm, "fingerprint": fp, "run_kind": RUN_KIND,
              "config": {"path": config_path.name, "sha256": _sha_file(config_path)},
              "prompt_schema_version": rb.PROMPT_SCHEMA_VERSION,
              "pool_coverage_exception": coverage_exception(cfg)}
    try:
        prov = rb.build_run_provenance(isolation_report)
        want = cfg["model"]
        got = {k: prov.get(f"hermes_runtime_{k}") for k in ("model", "provider", "reasoning_effort")}
        if got != want:
            raise SystemExit(f"Hermes runtime {got} differs from the frozen config {want}")
        # From here the arm is used: one attempt only, never retried or resumed.
        arm_dir.mkdir(parents=True)
        rb.apply_state_dir(str(arm_dir / "state"))
        run_id = str(uuid.uuid4())
        _write_marker(arm_dir, {**marker, "status": "started", "run_id": run_id, "started_at": _now()},
                      exclusive=True)
        rb.register_run(run_id, RUN_KIND)
        masking = masking_for(universe, watchlist, cfg)
        alias_mapping = masking.mapping()
        alias_hash = hashlib.sha256(json.dumps(alias_mapping, sort_keys=True).encode()).hexdigest()
        rb.register_run_configuration(run_id, watchlist, rb._watchlist_hash(watchlist), alias_mapping, alias_hash,
                                      seed, cfg["window"]["start"], cfg["window"]["end"], False, provenance=prov)
        # Optional tracing: from here through episode-end resolution; closed in `finally`.
        tracing = _start_tracing(run_id, masking, {
            "runner": "laya_vs_random", "experiment_id": cfg["experiment_id"], "arm": arm, "run_kind": RUN_KIND,
            "window_start": cfg["window"]["start"], "window_end": cfg["window"]["end"],
            "prompt_schema_version": rb.PROMPT_SCHEMA_VERSION, "fundamentals": False})

        facts = arm_dir / "facts.jsonl"
        facts.touch()
        rb.fetch_technicals = recording(saved["fetch_technicals"], "technicals", facts)
        rb.fetch_news = recording(saved["fetch_news"], "news", facts)
        rb.fetch_news_window = recording(saved["fetch_news_window"], "prefill_news", facts)
        history = recording(rb.fetch_card_history, "card_history", facts)
        from laya_experiment import LayaWorkerScorer, RandomSelectorContext, SelectorContext
        common = dict(universe=universe, seed=seed, run_id=run_id, snapshot_dir=rb.SELECTOR_SNAPSHOT_DIR,
                      fetch_history=history)
        if arm == "laya":
            selector = SelectorContext(scorer=LayaWorkerScorer(
                laya_checkpoint, image=cfg["laya"]["image"],
                log_path=rb.SELECTOR_SNAPSHOT_DIR / run_id / "laya_worker.log"), **common)
        else:
            selector = RandomSelectorContext(experiment_seed=cfg["random_seed"], **common)

        portfolio = rb.Portfolio()
        ledger = arm_dir / "days.jsonl"
        halted, sessions, cursor = False, 0, start
        while cursor <= end:
            if cursor.weekday() >= 5:
                cursor += timedelta(days=1)
                continue
            day_index = (cursor - start).days
            limits_ctx = rb._limits_for_profile(portfolio.profile_level)
            result, halted = rb._run_cycle_or_halt(
                lambda: rb.run_day(cursor, day_index, portfolio, masking, watchlist, limits_ctx, run_id,
                                   fundamentals_enabled=False, runtime=runtime, selector=selector),
                day_label=masking.day_index(cursor), run_id=run_id)
            if halted:
                break
            portfolio, summary, do_observability = result
            if "skipped" not in summary:
                sessions += 1
                with open(ledger, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"date": cursor.isoformat(), "summary": summary,
                                        "after": portfolio.to_dict()}, default=str) + "\n")
                try:
                    do_observability()
                except Exception as exc:                 # best effort, as in eval_suite._run_episode
                    print(f"  OBSERVABILITY FAILED {cursor}: {type(exc).__name__}: {exc}")
            rb._clear_checkpoint()
            cursor += timedelta(days=1)

        # A halted cycle may already have journaled a fill/exit that its discarded state never committed.
        rec = es.halted_reconciliation(run_id, portfolio) if halted else {"status": "not_needed"}
        committed = portfolio.to_dict()              # before episode-end resolution clears pending orders
        outcomes: dict = {}
        dangling = es.resolve_episode_end_pending(portfolio, outcomes)
        unreconciled = rec["status"] == "unreconciled"
        curve = portfolio.equity_curve
        final = {
            "status": "halted" if halted else "completed", "run_id": run_id, "finished_at": _now(),
            "sessions_completed": sessions, "halted_on": cursor.isoformat() if halted else None,
            "terminal_pending_resolved": [{"ticker": o.ticker, "decision_id": o.decision_id,
                                           "journal_outcome": outcomes.get(o.decision_id)} for o in dangling],
            "portfolio_reconciliation": rec,
            # Unreconciled: the journal holds activity this portfolio never committed, so neither the equity
            # curve nor return/drawdown is reported; the last committed portfolio is kept, labelled as such.
            "equity_curve": None if unreconciled else curve,
            "return_pct": None if unreconciled or not curve else round((curve[-1] - curve[0]) / curve[0] * 100, 4),
            "max_drawdown_pct": None if unreconciled else es._max_drawdown_pct(curve),
            "portfolio": None if unreconciled else portfolio.to_dict(),
            "last_committed_portfolio": committed if unreconciled else None,
            "alias_mapping_hash": alias_hash, "masking_seed": seed, "provenance": prov,
            "pool_coverage_exception": marker["pool_coverage_exception"],
        }
        (arm_dir / "result.json").write_text(json.dumps(final, indent=1, default=str), encoding="utf-8")
        _write_marker(arm_dir, {**marker, "status": final["status"], "run_id": run_id,
                                "sessions_completed": sessions}, exclusive=False)
        equity = "UNRECONCILED (see result.json)" if unreconciled else f"{portfolio.equity_curve[-1]:,.2f}"
        print(f"arm {arm}: {final['status']}, run_id {run_id}, {sessions} sessions, "
              f"equity {equity}, terminal pending resolved {len(dangling)}")
        return 0 if not halted else 1
    except BaseException as exc:
        if (arm_dir / "ARM.json").exists():
            body = json.loads((arm_dir / "ARM.json").read_text(encoding="utf-8"))
            if body.get("status") == "started":
                _write_marker(arm_dir, {**body, "status": "halted",
                                        "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, exclusive=False)
        raise
    finally:
        if tracing is not None:
            try:
                tracing.close()                      # restores run_day and the episode-end helper
            except Exception:
                pass
        for n, f in saved.items():
            setattr(rb, n, f)
        if selector is not None:
            selector.close()
        rb._cleanup_ephemeral_hermes_runtime(runtime)


# ---------------------------------------------------------------- read-only checks and the private ledger
def neon_counts(run_ids: list[str]) -> dict:
    """Read-only per-table totals and per-run counts through the trade-journal container."""
    script = f'''
import asyncio, json
from server import get_pool
TABLES = ["trades", "insight_log", "agent_activity", "portfolio_snapshots", "backtest_cycle_evidence",
          "backtest_run_registry", "backtest_run_configuration"]
async def main():
    pool = await get_pool()
    out = {{"tables": {{}}, "per_run": {{}}}}
    for t in TABLES:
        out["tables"][t] = await pool.fetchval(f"SELECT count(*) FROM {{t}}")
    for rid in {run_ids!r}:
        out["per_run"][rid] = {{t: await pool.fetchval(f"SELECT count(*) FROM {{t}} WHERE run_id = $1::uuid", rid)
                               for t in TABLES}}
    print(json.dumps(out))
asyncio.run(main())
'''
    return rb.json_from(rb.run_in("trade-journal", script))


def run_journal(run_id: str) -> dict:
    """Read-only: every trades row (full rejection reasons) and activity
    counts by type for one run."""
    script = f'''
import asyncio, json
from server import get_pool
async def main():
    pool = await get_pool()
    trades = await pool.fetch("SELECT decision_id, ticker, action, status, intended_quantity, fill_price, "
        "fill_quantity, rejection_reason, confidence, primary_signal, expected_reward_risk, stop_price, "
        "target_price, exit_provenance, strategy_notes, created_at FROM trades WHERE run_id = $1::uuid "
        "ORDER BY created_at", "{run_id}")
    acts = await pool.fetch("SELECT event_type, count(*) AS n FROM agent_activity WHERE run_id = $1::uuid "
        "GROUP BY event_type", "{run_id}")
    print(json.dumps({{"trades": [dict(r) for r in trades], "activity": {{r["event_type"]: r["n"] for r in acts}}}},
                     default=str))
asyncio.run(main())
'''
    return rb.json_from(rb.run_in("trade-journal", script))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=CONFIG_PATH,
                    help="frozen, committed experiment config (default: the v1 Laya-vs-random pair)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="read-only: config/plan/pool/code verification and Neon counts")
    r = sub.add_parser("run", help="run ONE arm (laya first, then random); never retried")
    r.add_argument("--arm", choices=ARMS, required=True)
    r.add_argument("--laya-checkpoint", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run_arm(a.arm, a.laya_checkpoint, a.config)
    cfg = load_config(a.config)
    universe, _pool = load_pool(cfg)
    print(json.dumps({"config": cfg["experiment_id"], "pool_members": len(universe.members),
                      "pool_coverage_exception": coverage_exception(cfg),
                      "fingerprint": code_fingerprint(a.config),
                      "arms": {arm: _read_marker(cfg, arm) for arm in ARMS}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
