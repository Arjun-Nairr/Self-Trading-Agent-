"""
laya_ft_pilot.py -- offline groundwork for a possible Laya review-priority
fine-tune (scope: docs/scopes/LAYA_FINETUNE_PILOT_V0.md). PREPARATION ONLY:
no training, no model load, no inference, no market data, no network.

    python backtest-harness/laya_ft_pilot.py build
    python backtest-harness/laya_ft_pilot.py revise <parent run dir> --parent-sha256 <hex>

`build` makes a 24-example pilot (12 real cards sampled reproducibly from the
saved, hash-verified Laya-vs-controls cards + 12 synthetic cards) labelled by
the current PROPOSED rubric. `revise` relabels an existing pilot under the
current rubric into a NEW versioned folder, after checking the parent file's
hash and that every card value still equals its source; the parent is never
modified. Outputs are private and git-ignored under .laya_ft_pilot/: the
dataset, a blind review sheet and a separate proposals sheet. Labels are
AI-proposed (the rubric's deterministic rules, no overrides), not ground truth.

Never read here: Laya scores or ranks, Hermes decisions, prior shortlists,
future prices or outcomes, tickers or company names.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import laya_adapter
from laya_selector import NUMERIC_FIELDS, build_card

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / ".laya_ft_pilot"
RUBRIC_VERSION = "review-rubric-v0.2-proposed"
PREVIOUS_RUBRIC_VERSION = "review-rubric-v0.1-proposed"
SAMPLE_SALT = "laya-ft-pilot-v1"
REAL_PER_SESSION = 4
LABELS = ("review_worthy", "ordinary", "ambiguous")
TRAINABLE = {"review_worthy": True, "ordinary": False}       # ambiguous is never a training label
DEV_SESSIONS = frozenset({"2026-09-28", "2026-09-29", "2026-10-02"})   # already explored: never test
EMBARGO_DAYS = 30            # calendar-day gap between partitions (> the 20-session feature window)
PARTITION_ORDER = ("train", "validation", "calibration", "test")
WORKFLOW = "laya_review_priority"
PROVENANCE = ("AI-proposed: Claude (claude-opus-5-5) applied the rubric v0.2 rules exactly, with no "
              "judgement overrides; not human-reviewed, not ground truth, not approved for training")

# ---------------------------------------------------------------- rubric v0.2 (PROPOSED)
# Every number below is a proposed policy choice, not an empirically optimal threshold.
STRONG = {"five_day_return": 0.07, "sma20_distance": 0.07, "rsi_low": 25.0, "rsi_high": 75.0,
          "relative_volume": 2.5, "move_vs_own_vol": 2.5}
MODERATE = {"five_day_return": 0.04, "sma20_distance": 0.04, "rsi_low": 32.0, "rsi_high": 68.0,
            "macd_direction": 0.6, "range_low": 0.10, "range_high": 0.90, "relative_volume": 1.6}
NEAR = 0.10                  # within 10% of an anchor's distance from neutral -> flagged, label unchanged
VOL_ONLY = 0.60              # 20-day annualized volatility; with no counted signal -> insufficient evidence
LOW_VOLUME = 0.40            # relative volume; with no counted signal -> insufficient evidence
# Correlated fields form one family; review-worthy needs a strong signal or moderate signals in >= 2 families.
FAMILY = {"five_day_return": "move", "move_vs_own_vol": "move", "sma20_distance": "location",
          "range_position_20d": "location", "rsi14": "oscillator", "relative_volume": "participation",
          "macd_direction": "momentum"}
# (field, neutral value, anchors) for near-anchor flags; distance is measured from neutral.
_NEAR_CHECKS = (("five_day_return", 0.0, (0.04, 0.07)), ("sma20_distance", 0.0, (0.04, 0.07)),
                ("rsi14", 50.0, (32.0, 68.0, 25.0, 75.0)), ("relative_volume", 1.0, (1.6, 2.5)),
                ("range_position_20d", 0.5, (0.10, 0.90)))
INSUFFICIENT = {
    "low_volume": ("very low relative volume and no other activity: the card cannot tell a shortened "
                   "session or data gap from genuinely thin trading"),
    "high_volatility": ("high 20-day volatility but quiet current fields: the card has no longer-run "
                        "baseline, so it cannot show whether this is unusual for the stock or already over"),
}


def state(card: Mapping[str, float]) -> dict[str, float]:
    """The 4-decimal values the model sees (laya_adapter.STATE_DECIMALS); the rubric uses exactly these."""
    return {f: round(float(card[f]), laya_adapter.STATE_DECIMALS) for f in NUMERIC_FIELDS}


def move_vs_own_vol(card: Mapping[str, float]) -> float:
    """5-day return in units of the card's own 5-day standard deviation
    (sigma * sqrt(5/252)). Volatility includes the recent move itself."""
    sd5 = card["realized_volatility"] * math.sqrt(5 / 252)
    return card["five_day_return"] / sd5 if sd5 > 0 else 0.0


def rubric_signals(card: Mapping[str, float]) -> list[tuple[str, str, int, float]]:
    """-> [(field, strength, direction, value)]; strength strong/moderate/supporting; direction +1/-1/0."""
    card = state(card)
    r5, d, rsi = card["five_day_return"], card["sma20_distance"], card["rsi14"]
    m, v, p = card["macd_direction"], card["relative_volume"], card["range_position_20d"]
    sgn = lambda x: 1 if x > 0 else -1
    out = []
    for f, x in (("five_day_return", r5), ("sma20_distance", d)):
        if abs(x) >= STRONG[f]:
            out.append((f, "strong", sgn(x), x))
        elif abs(x) >= MODERATE[f]:
            out.append((f, "moderate", sgn(x), x))
    z = move_vs_own_vol(card)
    if abs(z) >= STRONG["move_vs_own_vol"] and MODERATE["five_day_return"] <= abs(r5) < STRONG["five_day_return"]:
        out.append(("move_vs_own_vol", "strong", sgn(z), z))
    if rsi <= STRONG["rsi_low"] or rsi >= STRONG["rsi_high"]:
        out.append(("rsi14", "strong", 1 if rsi >= 50 else -1, rsi))
    elif rsi <= MODERATE["rsi_low"] or rsi >= MODERATE["rsi_high"]:
        out.append(("rsi14", "moderate", 1 if rsi >= 50 else -1, rsi))
    if abs(m) >= MODERATE["macd_direction"]:     # supporting only: saturates often and echoes the move
        out.append(("macd_direction", "supporting", sgn(m), m))
    if p >= MODERATE["range_high"] or p <= MODERATE["range_low"]:
        out.append(("range_position_20d", "moderate", 1 if p >= 0.5 else -1, p))
    if v >= STRONG["relative_volume"]:
        out.append(("relative_volume", "strong", 0, v))
    elif v >= MODERATE["relative_volume"]:
        out.append(("relative_volume", "moderate", 0, v))
    return out


def near_anchors(card: Mapping[str, float]) -> list[str]:
    """Fields within NEAR of any anchor (measured from neutral). Reviewer attention only."""
    card = state(card)
    out = []
    for f, neutral, anchors in _NEAR_CHECKS:
        x = card[f] if neutral else abs(card[f])
        if any(abs(x - a) <= NEAR * abs(a - neutral) for a in anchors):
            out.append(f)
    return out


def rubric_reference(card: Mapping[str, float]) -> tuple[str, str, list[str], str | None]:
    """Rubric v0.2 as deterministic rules -> (label, direction, flags, ambiguity_reason).
    This IS a fixed formula; a model trained only on its output distils it.
    Direction and indicator agreement never change the label."""
    card = state(card)
    counted = [s for s in rubric_signals(card) if s[1] != "supporting"]
    families = {FAMILY[s[0]] for s in counted}
    dirs = {s[2] for s in counted if s[2] != 0}
    direction = "mixed" if len(dirs) > 1 else {1: "up", -1: "down"}.get(next(iter(dirs), 0), "none")
    flags = [f"near_anchor:{f}" for f in near_anchors(card)]
    if card["relative_volume"] <= LOW_VOLUME:
        flags.append("low_volume")
    if card["realized_volatility"] >= VOL_ONLY:
        flags.append("high_volatility")
    if any(s[1] == "strong" for s in counted) or len(families) >= 2:
        return "review_worthy", direction, flags, None
    if counted:
        return "ordinary", direction, flags, None
    for flag, why in INSUFFICIENT.items():
        if flag in flags:
            return "ambiguous", direction, flags, why
    return "ordinary", direction, flags, None


_FMT = {"five_day_return": lambda x: f"5d {x * 100:+.2f}%", "sma20_distance": lambda x: f"vs 20d avg {x * 100:+.2f}%",
        "rsi14": lambda x: f"RSI14 {x:.4f}", "macd_direction": lambda x: f"MACD dir {x:+.4f}",
        "relative_volume": lambda x: f"rel vol {x:.4f}x", "realized_volatility": lambda x: f"ann vol {x * 100:.2f}%",
        "range_position_20d": lambda x: f"20d range pos {x * 100:.2f}%",
        "move_vs_own_vol": lambda x: f"5d move {x:+.2f} own-vol sd"}


def render(card: Mapping[str, float]) -> str:
    """Reviewer rendering of the exact 4-decimal state, with units. No identity, date or verdict."""
    s = state(card)
    return " | ".join(_FMT[f](s[f]) for f in NUMERIC_FIELDS)


def rationale(card: Mapping[str, float]) -> str:
    """Tied only to the supplied fields."""
    label, direction, flags, why = rubric_reference(card)
    sig = rubric_signals(card)
    fams = sorted({FAMILY[s[0]] for s in sig if s[1] != "supporting"})
    parts = [f"{s[1]} {FAMILY[s[0]]}: {_FMT[s[0]](s[3])}" for s in sig] or ["no field at a rubric anchor"]
    text = f"{label}; direction {direction}; {len(fams)} counted famil{'y' if len(fams) == 1 else 'ies'}"
    text += f" ({', '.join(fams)})" if fams else ""
    text += "; " + "; ".join(parts)
    near = [f.split(":", 1)[1] for f in flags if f.startswith("near_anchor:")]
    if near:
        text += f"; near an anchor (label follows the anchor): {', '.join(near)}"
    return text + (f"; insufficient evidence: {why}" if why else "")


# ---------------------------------------------------------------- synthetic cards (identity-free)
_BASE = {"five_day_return": 0.0, "sma20_distance": 0.0, "rsi14": 50.0, "macd_direction": 0.0,
         "relative_volume": 1.0, "realized_volatility": 0.25, "range_position_20d": 0.5}


def _s(**kw: float) -> dict[str, float]:
    return {**_BASE, **kw}


# (name, situation, card). Names are for documentation only; the pilot gets opaque IDs.
SYNTHETIC: tuple[tuple[str, str, dict[str, float]], ...] = (
    ("quiet", "ordinary", _s(five_day_return=0.004, sma20_distance=0.003, rsi14=51, macd_direction=0.05,
                             relative_volume=0.98, realized_volatility=0.22, range_position_20d=0.52)),
    ("mild_drift_up", "ordinary", _s(five_day_return=0.021, sma20_distance=0.018, rsi14=58, macd_direction=0.25,
                                     relative_volume=1.1, realized_volatility=0.28, range_position_20d=0.71)),
    ("quiet_high_vol_name", "ordinary", _s(five_day_return=-0.03, sma20_distance=-0.015, rsi14=45,
                                           macd_direction=-0.2, relative_volume=0.9, realized_volatility=0.55,
                                           range_position_20d=0.38)),
    ("breakout_on_volume", "clear", _s(five_day_return=0.09, sma20_distance=0.075, rsi14=74, macd_direction=0.75,
                                       relative_volume=2.8, realized_volatility=0.38, range_position_20d=0.98)),
    ("selloff_on_volume", "clear", _s(five_day_return=-0.11, sma20_distance=-0.09, rsi14=22, macd_direction=-0.8,
                                      relative_volume=3.1, realized_volatility=0.45, range_position_20d=0.03)),
    ("volume_spike_flat_price", "clear", _s(five_day_return=0.008, sma20_distance=0.005, rsi14=53,
                                            macd_direction=0.1, relative_volume=3.4, realized_volatility=0.24,
                                            range_position_20d=0.6)),
    ("steady_coherent_advance", "clear", _s(five_day_return=0.05, sma20_distance=0.045, rsi14=66,
                                            macd_direction=0.62, relative_volume=1.3, realized_volatility=0.3,
                                            range_position_20d=0.93)),
    ("just_below_anchors_combined", "borderline", _s(five_day_return=0.066, sma20_distance=0.058, rsi14=72,
                                                     macd_direction=0.55, relative_volume=1.5,
                                                     realized_volatility=0.42, range_position_20d=0.88)),
    ("pullback_after_runup", "borderline", _s(five_day_return=-0.06, sma20_distance=0.045, rsi14=52,
                                              macd_direction=-0.45, relative_volume=1.2, realized_volatility=0.35,
                                              range_position_20d=0.62)),
    ("very_low_volume_only", "borderline", _s(five_day_return=0.002, sma20_distance=-0.004, rsi14=49,
                                              macd_direction=0.0, relative_volume=0.3, realized_volatility=0.2,
                                              range_position_20d=0.48)),
    ("high_volatility_only", "borderline", _s(five_day_return=0.01, sma20_distance=-0.01, rsi14=48,
                                              macd_direction=-0.1, relative_volume=1.05, realized_volatility=0.75,
                                              range_position_20d=0.45)),
    ("lone_move_near_anchor", "borderline", _s(five_day_return=0.068, sma20_distance=0.03, rsi14=63,
                                               macd_direction=0.4, relative_volume=1.4, realized_volatility=0.3,
                                               range_position_20d=0.85)),
)


# ---------------------------------------------------------------- real sample (method fixed before labelling)
def _key(*parts: str) -> str:
    return hashlib.sha256("|".join((SAMPLE_SALT,) + parts).encode()).hexdigest()


def sample_real(cards_by_session: Mapping[str, Sequence[Any]], per_session: int = REAL_PER_SESSION) -> list[tuple[str, Any]]:
    """Per session (date order), the `per_session` cards with the smallest
    sha256(salt|session|row_id), skipping companies already drawn, so the 12
    are 12 different companies. Uses only session and row ID: never scores,
    ranks, shortlists, outcomes or card values."""
    taken: set[str] = set()
    out = []
    for session in sorted(cards_by_session):
        pool = sorted(cards_by_session[session], key=lambda c: _key(session, c.row_id))
        picked = [c for c in pool if c.row_id not in taken][:per_session]
        taken.update(c.row_id for c in picked)
        out += [(session, c) for c in picked]
    return out


def example_id(*parts: str) -> str:
    return "ex_" + _key("example", *parts)[:10]


# ---------------------------------------------------------------- records, validation, conversion
RECORD_KEYS = {"example_id", "source_type", "card", "rendering", "proposed_label", "direction", "rationale",
               "flags", "ambiguity_reason", "label_status", "provenance", "rubric_version", "previous_label",
               "previous_rubric_version", "change_reason", "partition", "group", "private_source"}
FORBIDDEN_TERMS = ("ticker", "symbol", "company", "name", "laya", "score", "rank", "shortlist", "hermes",
                   "decision", "forward", "outcome", "pnl", "return_after")
# Context the seven fields cannot supply; a rationale must not lean on it.
UNSUPPORTED_CONTEXT = ("usually", "normally", "historically", "for this stock is", "calm stock", "sector",
                       "market-wide", "earnings", "news", "support level", "resistance", "will ")


def make_record(source_type: str, eid: str, card: Mapping[str, float], group: str,
                private_source: Mapping[str, str] | None = None, previous_label: str | None = None,
                previous_rubric_version: str | None = None, change_reason: str | None = None) -> dict[str, Any]:
    """One AI-proposed record under the current rubric. No judgement overrides:
    the label is exactly the rubric rules' output."""
    card = {f: float(card[f]) for f in NUMERIC_FIELDS}
    label, direction, flags, why = rubric_reference(card)
    return {"example_id": eid, "source_type": source_type, "card": card, "rendering": render(card),
            "proposed_label": label, "direction": direction, "rationale": rationale(card), "flags": flags,
            "ambiguity_reason": why, "label_status": "ai_proposed_unreviewed", "provenance": PROVENANCE,
            "rubric_version": RUBRIC_VERSION, "previous_label": previous_label,
            "previous_rubric_version": previous_rubric_version, "change_reason": change_reason,
            "partition": "pilot", "group": group, "private_source": dict(private_source or {})}


def validate_record(rec: Mapping[str, Any]) -> None:
    """Raises ValueError on a malformed, leaky or inconsistent record."""
    extra = set(rec) - RECORD_KEYS
    if extra or RECORD_KEYS - set(rec):
        raise ValueError(f"record keys wrong: extra={sorted(extra)} missing={sorted(RECORD_KEYS - set(rec))}")
    if rec["source_type"] not in ("real", "synthetic"):
        raise ValueError("source_type")
    if rec["proposed_label"] not in LABELS or rec["previous_label"] not in (*LABELS, None):
        raise ValueError("label")
    if set(rec["card"]) != set(NUMERIC_FIELDS):
        raise ValueError("card fields")
    build_card("r_000000000000", rec["card"])                 # range/type checks of the production builder
    if not rec["provenance"] or rec["rubric_version"] != RUBRIC_VERSION:
        raise ValueError("provenance/rubric version")
    if rec["label_status"] != "ai_proposed_unreviewed":
        raise ValueError("an AI-built record cannot claim any review status")
    if (rec["proposed_label"] == "ambiguous") != bool(rec["ambiguity_reason"]):
        raise ValueError("ambiguous needs an insufficient-evidence reason, and only ambiguous has one")
    changed = rec["previous_label"] is not None and rec["previous_label"] != rec["proposed_label"]
    if changed != bool(rec["change_reason"]):
        raise ValueError("a change reason is required exactly when the label changed")
    model_facing = json.dumps([rec["card"], rec["rendering"]]).lower()
    if any(t in model_facing for t in FORBIDDEN_TERMS):
        raise ValueError("forbidden term in model- or reviewer-facing content")
    justification = f"{rec['rationale']} {rec['ambiguity_reason'] or ''}".lower()   # change_reason is history
    if any(t in justification for t in UNSUPPORTED_CONTEXT):
        raise ValueError("rationale relies on context the card does not carry")
    if set(rec["private_source"]) - {"session", "row_id"}:
        raise ValueError("private_source may hold only session and row_id")


def _final_label(rec: Mapping[str, Any], allow_provisional: bool) -> bool:
    if rec["label_status"] != "human_reviewed" and not allow_provisional:
        raise ValueError(f"{rec['example_id']}: label is {rec['label_status']}; refusing to export for training")
    label = rec.get("final_label", rec["proposed_label"])
    if label not in TRAINABLE:
        raise ValueError(f"{rec['example_id']}: {label!r} is not a training label")
    return TRAINABLE[label]


def to_typed_decisions_row(rec: Mapping[str, Any], allow_provisional: bool = False) -> dict[str, str]:
    """One row in the LocalLLaMA/typed-decisions layout the upstream training
    script reads (state/questions/gold as JSON strings; noul gold =
    probabilities over false/true). The question is the production one."""
    true = _final_label(rec, allow_provisional)
    state = {f: round(rec["card"][f], laya_adapter.STATE_DECIMALS) for f in NUMERIC_FIELDS}
    gold = {laya_adapter.ANSWER_FIELD: {"type": "noul", "label": "true" if true else "false",
                                        "noul": 1.0 if true else 0.0,
                                        "probabilities": {"false": 0.0 if true else 1.0, "true": 1.0 if true else 0.0}}}
    return {"id": rec["example_id"], "workflow": WORKFLOW, "split": rec["partition"],
            "state": json.dumps(state), "questions": json.dumps(laya_adapter.REVIEW_QUESTIONS), "gold": json.dumps(gold)}


def to_evals_row(rec: Mapping[str, Any], allow_provisional: bool = False) -> dict[str, Any]:
    """One row in the pinned `laya.evals` JSONL format (state, questions, expected, tags)."""
    true = _final_label(rec, allow_provisional)
    state = {f: round(rec["card"][f], laya_adapter.STATE_DECIMALS) for f in NUMERIC_FIELDS}
    return {"state": state, "questions": laya_adapter.REVIEW_QUESTIONS,
            "expected": {laya_adapter.ANSWER_FIELD: true}, "tags": [rec["source_type"], rec["partition"]]}


def check_partitions(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Leakage checks for a future dataset. Each row: partition, session
    (ISO date), group (company), state (dict), source_type. -> problems."""
    rows = list(rows)
    problems, spans, by_state = [], {}, {}
    for r in rows:
        part = r["partition"]
        if part not in PARTITION_ORDER:
            problems.append(f"unknown partition {part!r}")
            continue
        if r["source_type"] == "synthetic" and part in ("calibration", "test"):
            problems.append(f"synthetic card in {part}")
        if part == "test" and r["session"] in DEV_SESSIONS:
            problems.append(f"explored development session {r['session']} in test")
        d = date.fromisoformat(r["session"])
        lo, hi = spans.get(part, (d, d))
        spans[part] = (min(lo, d), max(hi, d))
        by_state.setdefault(json.dumps(r["state"], sort_keys=True), set()).add(part)
    problems += [f"identical card in {sorted(p)}" for p in by_state.values() if len(p) > 1]
    present = [p for p in PARTITION_ORDER if p in spans]
    for a, b in zip(present, present[1:]):
        gap = (spans[b][0] - spans[a][1]).days
        if gap < EMBARGO_DAYS:
            problems.append(f"{a}->{b} gap {gap} days < {EMBARGO_DAYS}")
    return problems


# ---------------------------------------------------------------- build / revise (private outputs)
BLIND_COLUMNS = ["example_id", "card", *NUMERIC_FIELDS, "your_label", "your_reason",
                 "insufficient_evidence_reason_if_ambiguous", "reviewer", "reviewed_on"]
PROPOSAL_COLUMNS = ["example_id", "rubric_version", "proposed_label", "direction", "rationale",
                    "ambiguity_reason", "near_anchor_fields", "decision_accept_change_ambiguous", "final_label",
                    "reviewer", "reviewed_on"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_cards() -> dict[str, tuple[dict[str, float], dict[str, str]]]:
    """example_id -> (card values, private source) for the pilot's fixed sample:
    real cards re-derived from the hash-verified saved run, synthetic from SYNTHETIC."""
    import laya_framing     # verified loader of the saved cards (sha + per-day input digests)

    cards_by_session, _ = laya_framing.load_saved_cards()     # scores in the file are never read
    out = {example_id(s, c.row_id): (c.model_state(), {"session": s, "row_id": c.row_id})
           for s, c in sample_real(cards_by_session)}
    out.update({example_id("synthetic", n): (dict(card), {}) for n, _, card in SYNTHETIC})
    return out


def _write_private(run: Path, meta: Mapping[str, Any], records: Sequence[Mapping[str, Any]], name: str) -> None:
    run.mkdir(parents=True, exist_ok=False)
    (run / name).write_text(json.dumps({**meta, "records": list(records)}, indent=1), encoding="utf-8")
    with open(run / "review_blind.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(BLIND_COLUMNS)
        for r in records:
            s = state(r["card"])
            w.writerow([r["example_id"], r["rendering"], *(s[k] for k in NUMERIC_FIELDS), "", "", "", "", ""])
    with open(run / "proposals.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PROPOSAL_COLUMNS)
        for r in records:
            near = [x.split(":", 1)[1] for x in r["flags"] if x.startswith("near_anchor:")]
            w.writerow([r["example_id"], r["rubric_version"], r["proposed_label"], r["direction"], r["rationale"],
                        r["ambiguity_reason"] or "", ", ".join(near), "", "", "", ""])


def build_pilot() -> Path:
    src = source_cards()
    records = sorted((make_record("synthetic" if not ps else "real", eid, card,
                                  _key("group", ps["row_id"])[:12] if ps else "syn_" + eid, ps)
                      for eid, (card, ps) in src.items()), key=lambda r: r["example_id"])
    for r in records:
        validate_record(r)
    import laya_framing
    meta = {"PRIVATE": "real-card values; keep out of git", "rubric_version": RUBRIC_VERSION,
            "sample": {"salt": SAMPLE_SALT, "per_session": REAL_PER_SESSION,
                       "source_sha256": laya_framing.SAVED_RESULTS_SHA256,
                       "method": "per session, smallest sha256(salt|session|row_id), distinct companies"},
            "label_status": "ai_proposed_unreviewed", "provenance": PROVENANCE,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    run = OUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _write_private(run, meta, records, "pilot_dataset.json")
    return run


def change_reason(prev: Mapping[str, Any], new_label: str) -> str | None:
    """Why a v0.1 label changed, tied to the v0.1 rule that produced it."""
    if prev["proposed_label"] == new_label:
        return None
    if prev.get("reference_label", prev["proposed_label"]) != prev["proposed_label"]:
        why = ("the v0.1 label was an AI judgement override (citing a 'usually calm stock'); the card carries "
               "only 20-day volatility, so that context is unsupported, and v0.2 allows no overrides")
    elif "conflicting_directions" in prev.get("ambiguity_flags", []):
        why = "v0.1 made conflicting directions ambiguous; in v0.2 indicator disagreement never decides the label"
    elif "near_threshold" in prev.get("ambiguity_flags", []):
        why = "v0.1 made a value near an anchor ambiguous; v0.2 flags it and lets the label follow the anchor"
    else:
        why = "v0.2 rule change"
    return f"{prev['proposed_label']} -> {new_label}: {why}"


def revise(parent_dir: Path, parent_sha256: str,
           source: Mapping[str, tuple[Mapping[str, float], Mapping[str, str]]] | None = None,
           out_dir: Path = OUT_DIR) -> Path:
    """Relabel an existing pilot under the current rubric into a new folder.
    Refuses unless the parent file hash matches and every card value, sample
    member and private source equals its source. The parent is only read."""
    p = Path(parent_dir) / "pilot_dataset.json"
    if _sha(p) != parent_sha256:
        raise SystemExit(f"{p}: SHA-256 differs from the recorded parent hash; refusing")
    parent = json.loads(p.read_text(encoding="utf-8"))
    if parent["rubric_version"] != PREVIOUS_RUBRIC_VERSION:
        raise SystemExit(f"parent rubric is {parent['rubric_version']}, expected {PREVIOUS_RUBRIC_VERSION}")
    source = source_cards() if source is None else source
    if {r["example_id"] for r in parent["records"]} != set(source):
        raise SystemExit("parent examples differ from the fixed sample")
    records = []
    for prev in parent["records"]:
        card, ps = source[prev["example_id"]]
        if {f: float(card[f]) for f in NUMERIC_FIELDS} != prev["card"] or dict(ps) != prev["private_source"]:
            raise SystemExit(f"{prev['example_id']}: card values or source changed since the parent; refusing")
        new = make_record(prev["source_type"], prev["example_id"], prev["card"], prev["group"],
                          prev["private_source"], prev["proposed_label"], prev["rubric_version"])
        new["change_reason"] = change_reason(prev, new["proposed_label"])
        validate_record(new)
        records.append(new)
    meta = {"PRIVATE": "real-card values; keep out of git", "rubric_version": RUBRIC_VERSION,
            "parent": {"folder": Path(parent_dir).name, "file": p.name, "sha256": parent_sha256,
                       "rubric_version": parent["rubric_version"]},
            "sample": parent["sample"], "label_status": "ai_proposed_unreviewed", "provenance": PROVENANCE,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    run = Path(out_dir) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_revision-v0.2")
    _write_private(run, meta, records, "pilot_revision.json")
    return run


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["build", "revise"])
    ap.add_argument("parent", nargs="?", help="revise: the parent pilot folder")
    ap.add_argument("--parent-sha256", help="revise: expected SHA-256 of the parent pilot_dataset.json")
    a = ap.parse_args(argv)
    if a.command == "revise":
        if not (a.parent and a.parent_sha256):
            ap.error("revise needs the parent folder and --parent-sha256")
        print(revise(Path(a.parent), a.parent_sha256))
    else:
        print(build_pilot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
