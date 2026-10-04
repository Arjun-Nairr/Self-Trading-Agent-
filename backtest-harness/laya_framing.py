"""
laya_framing.py -- the Laya input-framing x answer-label experiment
(plan: docs/evaluations/laya_framing_plan_2026-10-04.md).

    python backtest-harness/laya_framing.py run

A 2x2 over the SAME saved cards and the SAME question/criteria/checkpoint:
  state:  "json"      -- the production state, json.dumps of laya_adapter.card_to_state
          "explained" -- the same seven rounded numbers as text with units/definitions
  labels: "default"   -- laya's model-facing false/true labels (production)
          "neutral"   -- labels {"false": "B", "true": "A"} (laya 0.3.23 `labels` override;
                         the returned value is still P(true) = P(review-worthy))
plus a label-identity swap control ({"false": "A", "true": "B"}) for both neutral cells.

Reuses the saved complete cards of the Laya-vs-controls run (hash-verified); no
market data is fetched. Scores run in an experiment-only worker script mounted
read-only into the unchanged production worker image (laya_framing_worker.py).
Private outputs go to the git-ignored .laya_framing/. No Hermes, no trades, no Neon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import queue
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import laya_adapter
from laya_selector import NUMERIC_FIELDS, CandidateCard, build_card, inputs_digest, make_row_ids

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
EXPERIMENT_VERSION = "laya-framing-v1"
PLAN_PATH = REPO / "docs" / "evaluations" / "laya_framing_plan_2026-10-04.md"
SAVED_RUN = HERE / ".laya_controls" / "20261004T133319Z"
SAVED_RESULTS_SHA256 = "8d140f9912e57afd1daa887f6a2959a7acb569d53ed11820fbdd595f4f1408fa"
SAVED_DIGESTS = {"2026-09-28": "c20553e0e7e3e4bfc9a20d81e2878f4ddd774fe48273121a063195b6b71c7828",
                 "2026-09-29": "8eca206ab50dc568e6470ea7a7cd873ec039d34960fa036add423b4d49e194ef",
                 "2026-10-02": "3b07ca78412cf09d7dfe6c7be80a7c353cb9d38375d80322c86a25a5afda5ebc"}
OUT_DIR = HERE / ".laya_framing"
IMAGE = "hermes-laya-worker:2"
K = 10

LABELS = {"default": None, "neutral": {"false": "B", "true": "A"}, "neutral_swapped": {"false": "A", "true": "B"}}
VARIANTS = {   # name -> (state rendering, label set)
    "V1_json_default": ("json", "default"),
    "V2_explained_default": ("explained", "default"),
    "V3_json_neutral": ("json", "neutral"),
    "V4_explained_neutral": ("explained", "neutral"),
    "V3s_json_neutral_swapped": ("json", "neutral_swapped"),
    "V4s_explained_neutral_swapped": ("explained", "neutral_swapped"),
}
PRIMARY = ("V1_json_default", "V2_explained_default", "V3_json_neutral", "V4_explained_neutral")
SWAP_OF = {"V3_json_neutral": "V3s_json_neutral_swapped", "V4_explained_neutral": "V4s_explained_neutral_swapped"}

# -- questions & state renderings (pure) ---------------------------------------

def question(label_set: str) -> dict:
    """The production question, unchanged, plus the optional model-facing labels."""
    q = json.loads(json.dumps(laya_adapter.REVIEW_QUESTIONS))
    if LABELS[label_set] is not None:
        q[laya_adapter.ANSWER_FIELD]["labels"] = dict(LABELS[label_set])
    return q


EXPLAINED_HEADER = ("Daily technical snapshot of one anonymous US-listed stock at a session close. "
                    "The task is only to decide whether these numbers deserve a further technical and "
                    "news review; it is not a buy decision and not a profit forecast.")


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def render_state(card: CandidateCard, mode: str) -> dict | str:
    """The state shown to the model. Both modes carry exactly the production
    4-decimal values (laya_adapter.card_to_state); 'explained' adds units and
    definitions only -- no identity, news, history, verdict or answer."""
    s = laya_adapter.card_to_state(card)
    if mode == "json":
        return s
    if mode != "explained":
        raise ValueError(mode)
    return "\n".join([
        EXPLAINED_HEADER,
        f"5-day return: {_pct(s['five_day_return'])} (today's close vs the close 5 sessions earlier)",
        f"Distance from 20-day average: {_pct(s['sma20_distance'])} (close vs its 20-day simple moving average)",
        f"RSI(14): {s['rsi14']:.4f} (scale 0 to 100 from 14 sessions of gains vs losses; 50 = balanced)",
        f"MACD direction: {s['macd_direction']:+.4f} (normalized MACD(12,26,9) histogram, -1 to +1; "
        "sign = momentum direction, 0 = flat)",
        f"Relative volume: {s['relative_volume']:.4f}x (today's volume vs the average of the prior 20 sessions; "
        "1.0000x = average)",
        f"Realized volatility: {s['realized_volatility'] * 100:.2f}% (annualized, from the last 20 daily returns)",
        f"Position in 20-day range: {s['range_position_20d'] * 100:.2f}% (0% = at the 20-day low, "
        "100% = at the 20-day high)",
    ])


def parse_explained(text: str) -> dict[str, float]:
    """Inverse of render_state(..., 'explained') -- used by tests/audit to prove
    the explained text carries exactly the JSON numbers."""
    lines = text.split("\n")[1:]
    num = lambda line: line.split(": ", 1)[1].split(" ", 1)[0].rstrip("%x")
    v = [float(num(line)) for line in lines]
    return {"five_day_return": round(v[0] / 100, 4), "sma20_distance": round(v[1] / 100, 4), "rsi14": v[2],
            "macd_direction": v[3], "relative_volume": v[4], "realized_volatility": round(v[5] / 100, 4),
            "range_position_20d": round(v[6] / 100, 4)}


# -- fixed synthetic diagnostic cases (pure) -----------------------------------
# Diagnostic rubric, NOT financial truth. "Notable" = clearly beyond these
# fixed bands on a field named in the criterion; "ordinary" = every criterion
# field inside the ordinary band. Expected labels exist ONLY where the
# criterion text is unambiguous; everything else is "ambiguous".

ORDINARY = {"five_day_return": 0.0, "sma20_distance": 0.0, "rsi14": 50.0, "macd_direction": 0.0,
            "relative_volume": 1.0, "realized_volatility": 0.25, "range_position_20d": 0.5}


def _c(**kw) -> dict:
    return {**ORDINARY, **kw}


# (case id, split, expected: True = review-worthy, False = ordinary, None = ambiguous, values)
CASES: list[tuple[str, str, bool | None, dict]] = [
    ("O1", "dev", False, _c()),
    ("O2", "dev", False, _c(five_day_return=0.004, sma20_distance=-0.003, rsi14=51.5, relative_volume=1.03)),
    ("O3", "dev", False, _c(five_day_return=-0.005, sma20_distance=0.004, rsi14=48.5, macd_direction=0.04,
                            relative_volume=0.97, realized_volatility=0.22, range_position_20d=0.46)),
    ("O4", "conf", False, _c(realized_volatility=0.28, range_position_20d=0.53)),
    ("O5", "conf", False, _c(five_day_return=0.003, sma20_distance=0.002, rsi14=49.0, macd_direction=-0.03,
                             relative_volume=1.04, range_position_20d=0.54)),
    ("O6", "conf", False, _c(five_day_return=-0.004, sma20_distance=-0.005, rsi14=52.0, macd_direction=0.05,
                             relative_volume=0.96, realized_volatility=0.30, range_position_20d=0.48)),
    ("S_5d_up", "dev", True, _c(five_day_return=0.12)),
    ("S_sma_down", "dev", True, _c(sma20_distance=-0.10)),
    ("S_rsi_high", "dev", True, _c(rsi14=82.0)),
    ("S_macd_down", "dev", True, _c(macd_direction=-0.90)),
    ("S_vol_3.5x", "dev", True, _c(relative_volume=3.5)),
    ("S_range_low", "dev", True, _c(range_position_20d=0.02)),
    ("S_5d_down", "conf", True, _c(five_day_return=-0.12)),
    ("S_sma_up", "conf", True, _c(sma20_distance=0.10)),
    ("S_rsi_low", "conf", True, _c(rsi14=18.0)),
    ("S_macd_up", "conf", True, _c(macd_direction=0.90)),
    ("S_vol_4x", "conf", True, _c(relative_volume=4.0)),
    ("S_range_high", "conf", True, _c(range_position_20d=0.98)),
    ("C_breakout", "dev", True, _c(five_day_return=0.10, sma20_distance=0.08, rsi14=75.0, macd_direction=0.70,
                                   relative_volume=2.5, realized_volatility=0.40, range_position_20d=0.97)),
    ("C_oversold", "dev", True, _c(five_day_return=-0.03, sma20_distance=-0.07, rsi14=24.0,
                                   range_position_20d=0.05)),
    ("C_selloff", "conf", True, _c(five_day_return=-0.10, sma20_distance=-0.09, rsi14=22.0, macd_direction=-0.80,
                                   relative_volume=2.8, realized_volatility=0.45, range_position_20d=0.03)),
    ("C_volume_move", "conf", True, _c(five_day_return=0.07, relative_volume=3.0)),
    ("B_5d_4pct", "dev", None, _c(five_day_return=0.04)),
    ("B_rsi_66", "dev", None, _c(rsi14=66.0)),
    ("B_vol_1.4x", "conf", None, _c(relative_volume=1.4)),
    ("B_range_0.88", "conf", None, _c(range_position_20d=0.88)),
    ("X_rsi_high_at_low", "dev", None, _c(rsi14=80.0, range_position_20d=0.05, five_day_return=-0.08)),
    ("X_high_volatility_only", "dev", None, _c(realized_volatility=0.70)),
    ("X_low_volume_only", "conf", None, _c(relative_volume=0.30)),
    ("X_macd_up_price_down", "conf", None, _c(macd_direction=0.90, five_day_return=-0.10)),
]

# One-field paired changes from the ordinary base. predicted: "up" where the
# criterion says the change makes the card more review-worthy; None = the
# criterion is silent (volatility) or ambiguous (low volume).
PAIRED: list[tuple[str, str, float, str | None]] = [
    ("five_day_return", "low", -0.10, "up"), ("five_day_return", "high", 0.10, "up"),
    ("sma20_distance", "low", -0.08, "up"), ("sma20_distance", "high", 0.08, "up"),
    ("rsi14", "low", 25.0, "up"), ("rsi14", "high", 75.0, "up"),
    ("macd_direction", "low", -0.80, "up"), ("macd_direction", "high", 0.80, "up"),
    ("relative_volume", "low", 0.40, None), ("relative_volume", "high", 3.00, "up"),
    ("realized_volatility", "low", 0.10, None), ("realized_volatility", "high", 0.60, None),
    ("range_position_20d", "low", 0.02, "up"), ("range_position_20d", "high", 0.98, "up"),
]
PAIRED_MOVE = 0.0005


def synthetic_cards() -> list[tuple[str, CandidateCard]]:
    labels = [c[0] for c in CASES] + ["P_base"] + [f"P_{f}={side}" for f, side, _, _ in PAIRED]
    ids = make_row_ids(labels, 0, "laya-framing-synthetic-v1")
    out = [(cid, build_card(ids[cid], vals)) for cid, _, _, vals in CASES]
    out.append(("P_base", build_card(ids["P_base"], ORDINARY)))
    out += [(f"P_{f}={side}", build_card(ids[f"P_{f}={side}"], {**ORDINARY, f: v})) for f, side, v, _ in PAIRED]
    return out


# -- metrics (pure) --------------------------------------------------------------

def auroc(pos: Sequence[float], neg: Sequence[float]) -> float | None:
    """P(score of an expected-review-worthy case > an expected-ordinary case), ties = 1/2."""
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def case_metrics(p: Mapping[str, float], split: str) -> dict[str, Any]:
    rows = [(cid, exp) for cid, sp, exp, _ in CASES if sp == split]
    pos = [p[c] for c, e in rows if e is True]
    neg = [p[c] for c, e in rows if e is False]
    return {"auroc": auroc(pos, neg), "n_pos": len(pos), "n_neg": len(neg),
            "mean_pos": round(statistics.fmean(pos), 4), "mean_neg": round(statistics.fmean(neg), 4),
            "above_0.5_pos": sum(x > 0.5 for x in pos), "above_0.5_neg": sum(x > 0.5 for x in neg)}


def paired_metrics(p: Mapping[str, float]) -> dict[str, Any]:
    base = p["P_base"]
    rows, agree, predicted = [], 0, 0
    for f, side, v, pred in PAIRED:
        d = round(p[f"P_{f}={side}"] - base, 4)
        moved_up = d >= PAIRED_MOVE
        if pred == "up":
            predicted += 1
            agree += moved_up
        rows.append({"field": f, "side": side, "value": v, "delta": d, "predicted": pred})
    return {"base": base, "agree_up": agree, "predicted_up": predicted, "rows": rows}


def real_metrics(p: Sequence[float]) -> dict[str, Any]:
    ps = sorted(p, reverse=True)
    q = statistics.quantiles(ps, n=4)
    counts: dict[float, int] = {}
    for x in ps:
        counts[x] = counts.get(x, 0) + 1
    return {"min": ps[-1], "median": statistics.median(ps), "max": ps[0], "range": round(ps[0] - ps[-1], 4),
            "iqr": round(q[2] - q[0], 4), "distinct": len(counts),
            "rows_sharing_a_score": sum(c for c in counts.values() if c > 1),
            "rank10_vs_11_gap": round(ps[K - 1] - ps[K], 4)}


def top_k(ids: Sequence[str], p: Sequence[float], k: int = K) -> list[str]:
    """Production ranking semantics: descending p, seeded (42) row-ID hash tie-break."""
    from laya_selector import _tie_break_key
    return [i for _, i in sorted(zip(p, ids), key=lambda t: (-t[0], _tie_break_key(42, t[1])))][:k]


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    from laya_controls import spearman as sp
    return sp(xs, ys)


# Pre-registered decision rule (plan §6), applied to DEV metrics only.
RULE = {"min_auroc_gain": 0.15, "min_paired_gain": 3, "max_swap_mean_abs_delta": 0.02, "max_swap_auroc_shift": 0.10}


def choose(dev: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """dev[variant] = {"auroc", "agree_up", "swap_mean_abs_delta" (neutral only), "swap_auroc" (neutral only),
    "malformed", "truncated"}. Returns {"qualified": [...], "chosen": name | None, "reasons": {...}}."""
    base = dev["V1_json_default"]
    reasons, ok = {}, []
    for v in PRIMARY[1:]:
        d = dev[v]
        why = []
        if d["malformed"] or d["truncated"]:
            why.append("malformed or truncated output")
        if d["auroc"] is None or base["auroc"] is None or d["auroc"] < base["auroc"] + RULE["min_auroc_gain"]:
            why.append("dev AUROC gain < %.2f" % RULE["min_auroc_gain"])
        if d["agree_up"] < base["agree_up"] + RULE["min_paired_gain"]:
            why.append("paired agreement gain < %d" % RULE["min_paired_gain"])
        if "swap_mean_abs_delta" in d:
            if d["swap_mean_abs_delta"] > RULE["max_swap_mean_abs_delta"]:
                why.append("label-swap mean |dp| > %.2f" % RULE["max_swap_mean_abs_delta"])
            if d["swap_auroc"] is None or abs(d["swap_auroc"] - d["auroc"]) > RULE["max_swap_auroc_shift"]:
                why.append("label-swap AUROC shift > %.2f" % RULE["max_swap_auroc_shift"])
        reasons[v] = why or ["qualifies"]
        if not why:
            ok.append(v)
    chosen = max(ok, key=lambda v: (dev[v]["auroc"], -PRIMARY.index(v))) if ok else None
    return {"qualified": ok, "chosen": chosen, "reasons": reasons}


# -- worker (I/O) -----------------------------------------------------------------

def docker_command(checkpoint_dir: str, name: str) -> list[str]:
    """The production worker's isolation flags, plus ONE extra read-only mount
    for the experiment script; the image and its code are unchanged."""
    from laya_experiment import CHECKPOINT_MOUNT, WORKER_CONTAINER_USER, WORKER_LABEL
    return [
        "docker", "run", "--rm", "-i", "--init", "--name", name, "--label", WORKER_LABEL,
        "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
        "--user", WORKER_CONTAINER_USER,
        "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1", "-e", "HF_HUB_DISABLE_TELEMETRY=1", "-e", "HOME=/tmp",
        "-v", f"{checkpoint_dir}:{CHECKPOINT_MOUNT}:ro",
        "-v", f"{HERE / 'laya_framing_worker.py'}:/exp/laya_framing_worker.py:ro",
        "-w", "/app", "--entrypoint", "python", IMAGE, "/exp/laya_framing_worker.py", CHECKPOINT_MOUNT,
    ]


class Worker:
    def __init__(self, checkpoint_dir: str, log_path: Path):
        from laya_experiment import _docker_cli_env
        self.name = f"laya-framing-{uuid.uuid4().hex[:12]}"
        self._log = open(log_path, "ab")
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self.proc = subprocess.Popen(docker_command(checkpoint_dir, self.name), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._log, text=True, encoding="utf-8",
                                     bufsize=1, env=_docker_cli_env())
        threading.Thread(target=self._pump, daemon=True).start()
        self.ready = self._read(900)
        self._id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def _read(self, timeout: float) -> dict:
        line = self._q.get(timeout=timeout)
        if line is None:
            raise RuntimeError("framing worker exited (see its log)")
        return json.loads(line)

    def isolation_problems(self) -> list[str]:
        from laya_experiment import _run_docker, check_container_isolation
        r = _run_docker(["inspect", self.name], 30)
        return ["docker inspect failed"] if r.returncode else check_container_isolation(json.loads(r.stdout)[0])

    def score(self, questions: dict, states: list) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps({"id": self._id, "questions": questions, "states": states}) + "\n")
        self.proc.stdin.flush()
        reply = self._read(1800)
        if reply.get("id") != self._id or "error" in reply or len(reply.get("records", [])) != len(states):
            raise RuntimeError(f"bad worker reply: {str(reply)[:300]}")
        for r in reply["records"]:
            if not (isinstance(r["p"], float) and 0.0 <= r["p"] <= 1.0):
                raise RuntimeError(f"malformed probability {r['p']!r}")
        return reply

    def close(self):
        from laya_experiment import _run_docker
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        _run_docker(["rm", "-f", self.name], 30)
        self._log.close()


# -- run (I/O) -----------------------------------------------------------------------

def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_saved_cards() -> dict[str, list[CandidateCard]]:
    """The previous run's complete cards, hash- and digest-verified."""
    p = SAVED_RUN / "private_results.json"
    if _sha(p) != SAVED_RESULTS_SHA256:
        raise SystemExit("saved Laya-vs-controls results changed; refusing to run")
    saved = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for d in saved["days"]:
        cards = [build_card(c["row_id"], {f: c[f] for f in NUMERIC_FIELDS}) for c in d["cards"]]
        from datetime import date
        latest = {c["row_id"]: date.fromisoformat(c["latest_bar_date"]) for c in d["cards"]}
        if inputs_digest(cards, latest) != SAVED_DIGESTS[d["session"]]:
            raise SystemExit(f"{d['session']}: card digest mismatch")
        out[d["session"]] = cards
    return out, saved


def run(checkpoint_dir: str) -> Path:
    from laya_demo import _canon, _git, _write_once

    days, saved = load_saved_cards()
    synth = synthetic_cards()
    t0 = time.monotonic()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    w = Worker(checkpoint_dir, OUT_DIR / f".worker_{stamp}.log")
    timing = {"cold_start_s": round(time.monotonic() - t0, 1), "variant_s": {}}
    try:
        iso = w.isolation_problems()
        if iso:
            raise SystemExit("worker isolation failed: " + "; ".join(iso))
        raw: dict[str, Any] = {}
        for v, (mode, lab) in VARIANTS.items():                     # sequential, one model load
            t = time.monotonic()
            q = question(lab)
            res = {"synthetic": w.score(q, [render_state(c, mode) for _, c in synth])}
            for day, cards in days.items():
                res[day] = w.score(q, [render_state(c, mode) for c in cards])
            raw[v] = res
            timing["variant_s"][v] = round(time.monotonic() - t, 1)
    finally:
        w.close()

    # ---- measurements
    synth_ids = [cid for cid, _ in synth]
    out_variants, dev_for_rule, conf = {}, {}, {}
    ps = {v: {cid: r["p"] for cid, r in zip(synth_ids, raw[v]["synthetic"]["records"])} for v in VARIANTS}
    for v in VARIANTS:
        recs = [r for part in raw[v].values() for r in part["records"]]
        truncated = sum(bool(r["truncated"]) for r in recs)
        st_tokens = [r["state_tokens"] for r in recs]
        real = {}
        for day, cards in days.items():
            p = [r["p"] for r in raw[v][day]["records"]]
            real[day] = {**real_metrics(p), "top10": top_k([c.row_id for c in cards], p)}
        out_variants[v] = {
            "state": VARIANTS[v][0], "labels": VARIANTS[v][1], "options": raw[v]["synthetic"]["options"],
            "dev": case_metrics(ps[v], "dev"), "conf": case_metrics(ps[v], "conf"), "paired": paired_metrics(ps[v]),
            "real": real, "truncated_answers": truncated, "state_tokens_min_max": [min(st_tokens), max(st_tokens)],
            "input_tokens_max": max(r["input_tokens"] for r in recs),
            "ambiguous_cases": {cid: ps[v][cid] for cid, _, e, _ in CASES if e is None},
        }
    for v in PRIMARY:
        d = {"auroc": out_variants[v]["dev"]["auroc"], "agree_up": out_variants[v]["paired"]["agree_up"],
             "malformed": 0, "truncated": out_variants[v]["truncated_answers"]}
        if v in SWAP_OF:
            s = SWAP_OF[v]
            dev_ids = [cid for cid, sp, _, _ in CASES if sp == "dev"]
            d["swap_mean_abs_delta"] = round(statistics.fmean(abs(ps[v][c] - ps[s][c]) for c in dev_ids), 4)
            d["swap_auroc"] = out_variants[s]["dev"]["auroc"]
        dev_for_rule[v] = d
    decision = choose(dev_for_rule)
    for v in PRIMARY:                                                 # confirmation: computed for all, read after the choice
        c = {"auroc": out_variants[v]["conf"]["auroc"]}
        if v in SWAP_OF:
            conf_ids = [cid for cid, sp, _, _ in CASES if sp == "conf"]
            c["swap_mean_abs_delta"] = round(statistics.fmean(abs(ps[v][x] - ps[SWAP_OF[v]][x]) for x in conf_ids), 4)
            c["swap_auroc"] = out_variants[SWAP_OF[v]]["conf"]["auroc"]
        conf[v] = c
    survives = None
    if decision["chosen"]:
        ch, b = conf[decision["chosen"]], conf["V1_json_default"]
        survives = (ch["auroc"] >= b["auroc"] + RULE["min_auroc_gain"]
                    and ("swap_auroc" not in ch or (abs(ch["swap_auroc"] - ch["auroc"]) <= RULE["max_swap_auroc_shift"]
                                                    and ch["swap_mean_abs_delta"] <= RULE["max_swap_mean_abs_delta"])))
    # baseline reproduction against the saved production scores
    repro = {}
    for d in saved["days"]:
        p_saved = [d["scores"][c["row_id"]][0] for c in d["cards"]]
        p_now = [r["p"] for r in raw["V1_json_default"][d["session"]]["records"]]
        repro[d["session"]] = {"identical": sum(a == b for a, b in zip(p_saved, p_now)), "n": len(p_now),
                               "max_abs_diff": round(max(abs(a - b) for a, b in zip(p_saved, p_now)), 6)}
    overlap = {}
    for day, cards in days.items():
        tops = {v: set(out_variants[v]["real"][day]["top10"]) for v in VARIANTS}
        overlap[day] = {v: len(tops[v] & tops["V1_json_default"]) for v in VARIANTS}
        rank = {v: [r["p"] for r in raw[v][day]["records"]] for v in VARIANTS}
        overlap[day]["spearman_vs_V1"] = {v: spearman(rank["V1_json_default"], rank[v]) for v in VARIANTS}
    swap_real = {v: {day: round(statistics.fmean(abs(a["p"] - b["p"]) for a, b in
                                                 zip(raw[v][day]["records"], raw[SWAP_OF[v]][day]["records"])), 4)
                     for day in days} for v in SWAP_OF}

    public = {
        "experiment_version": EXPERIMENT_VERSION, "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_sha256": _sha(PLAN_PATH), "runner_sha256": _sha(Path(__file__)), "worker_sha256": _sha(HERE / "laya_framing_worker.py"),
        "code": _git(), "worker_ready": w.ready, "saved_results_sha256": SAVED_RESULTS_SHA256, "timing": timing,
        "baseline_reproduction": repro, "decision": decision, "dev_rule_inputs": dev_for_rule, "confirmation": conf,
        "confirmation_survives": survives, "real_top10_overlap_vs_V1": overlap, "label_swap_real_mean_abs_delta": swap_real,
        "variants": {v: {k: x for k, x in m.items() if k != "real"} | {"real": {d: {k: x for k, x in r.items() if k != "top10"}
                                                                                for d, r in m["real"].items()}}
                     for v, m in out_variants.items()},
    }
    run_dir = OUT_DIR / stamp
    text = _canon(public)
    _write_once(run_dir / "public_summary.json", text)
    _write_once(run_dir / "private_results.json", _canon({
        "PRIVATE": "per-card scores and top-10 row IDs by variant. Never commit or publish.",
        "public_summary_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "raw": raw, "top10": {v: {d: m["real"][d]["top10"] for d in days} for v, m in out_variants.items()}}))
    return run_dir


def main(argv: Sequence[str] | None = None) -> int:
    from laya_demo import DEFAULT_CHECKPOINT
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    sub.add_parser("render", help="print the exact renderings of one identity-free example card")
    args = ap.parse_args(argv)
    if args.cmd == "render":
        card = dict(synthetic_cards())["C_breakout"]
        for v, (mode, lab) in VARIANTS.items():
            print(f"== {v}\nquestion: {json.dumps(question(lab))}\nstate: {json.dumps(render_state(card, mode))}\n")
        return 0
    print(run(args.checkpoint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
