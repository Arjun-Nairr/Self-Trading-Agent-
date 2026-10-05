"""
laya_ft_pilot.py -- offline groundwork for a possible Laya review-priority
fine-tune (scope: docs/scopes/LAYA_FINETUNE_PILOT_V0.md). PREPARATION ONLY:
no training, no model load, no inference, no market data, no network.

    python backtest-harness/laya_ft_pilot.py build

Builds a 24-example pilot (12 real cards sampled reproducibly from the saved,
hash-verified Laya-vs-controls cards + 12 synthetic cards), proposes a label
for each from the PROPOSED rubric v0.1, and writes private, git-ignored files
under .laya_ft_pilot/<run time>/: the pilot dataset, a review CSV, and
format-check exports in the two official upstream formats. Labels are
AI-proposed (rubric reference rules, checked by Claude), not ground truth.

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
RUBRIC_VERSION = "review-rubric-v0.1-proposed"
SAMPLE_SALT = "laya-ft-pilot-v1"
REAL_PER_SESSION = 4
LABELS = ("review_worthy", "ordinary", "ambiguous")
TRAINABLE = {"review_worthy": True, "ordinary": False}       # ambiguous is never a training label
DEV_SESSIONS = frozenset({"2026-09-28", "2026-09-29", "2026-10-02"})   # already explored: never test
EMBARGO_DAYS = 30            # calendar-day gap between partitions (> the 20-session feature window)
PARTITION_ORDER = ("train", "validation", "calibration", "test")
WORKFLOW = "laya_review_priority"
PROVENANCE = ("AI-proposed: Claude (claude-opus-5-5) applied the rubric v0.1 reference rules "
              "and checked each result; not human-reviewed, not ground truth")

# ---------------------------------------------------------------- rubric v0.1 (PROPOSED)
STRONG = {"five_day_return": 0.07, "sma20_distance": 0.07, "rsi_low": 25.0, "rsi_high": 75.0,
          "relative_volume": 2.5, "move_vs_own_vol": 2.5}
MODERATE = {"five_day_return": 0.04, "sma20_distance": 0.04, "rsi_low": 32.0, "rsi_high": 68.0,
            "macd_direction": 0.6, "range_low": 0.10, "range_high": 0.90, "relative_volume": 1.6}
NEAR = 0.10                  # a lone signal within 10% of its strong anchor is "near threshold"
VOL_ONLY = 0.60              # annualized volatility that alone makes a quiet card ambiguous
LOW_VOLUME = 0.40            # relative volume this low alone: possible half-session or data issue
# Correlated fields form one family; R needs a strong signal or moderate signals in >= 2 families.
FAMILY = {"five_day_return": "move", "move_vs_own_vol": "move", "sma20_distance": "location",
          "range_position_20d": "location", "rsi14": "oscillator", "relative_volume": "participation",
          "macd_direction": "momentum"}


def move_vs_own_vol(card: Mapping[str, float]) -> float:
    """5-day return in units of the card's own 5-day standard deviation
    (sigma * sqrt(5/252)). Volatility includes the recent move itself."""
    sd5 = card["realized_volatility"] * math.sqrt(5 / 252)
    return card["five_day_return"] / sd5 if sd5 > 0 else 0.0


def rubric_signals(card: Mapping[str, float]) -> list[tuple[str, str, int, float, float]]:
    """-> [(field, strength, direction, value, strong_anchor)]; direction +1/-1/0."""
    r5, d, rsi = card["five_day_return"], card["sma20_distance"], card["rsi14"]
    m, v, p = card["macd_direction"], card["relative_volume"], card["range_position_20d"]
    sgn = lambda x: 1 if x > 0 else -1
    out = []
    for f, x in (("five_day_return", r5), ("sma20_distance", d)):
        if abs(x) >= STRONG[f]:
            out.append((f, "strong", sgn(x), abs(x), STRONG[f]))
        elif abs(x) >= MODERATE[f]:
            out.append((f, "moderate", sgn(x), abs(x), STRONG[f]))
    z = move_vs_own_vol(card)
    if abs(z) >= STRONG["move_vs_own_vol"] and abs(r5) >= MODERATE["five_day_return"] \
            and abs(r5) < STRONG["five_day_return"]:
        out.append(("move_vs_own_vol", "strong", sgn(z), abs(z), STRONG["move_vs_own_vol"]))
    if rsi <= STRONG["rsi_low"] or rsi >= STRONG["rsi_high"]:
        out.append(("rsi14", "strong", 1 if rsi >= 50 else -1, rsi, STRONG["rsi_high" if rsi >= 50 else "rsi_low"]))
    elif rsi <= MODERATE["rsi_low"] or rsi >= MODERATE["rsi_high"]:
        out.append(("rsi14", "moderate", 1 if rsi >= 50 else -1, rsi, STRONG["rsi_high" if rsi >= 50 else "rsi_low"]))
    if abs(m) >= MODERATE["macd_direction"]:     # supporting only: saturates often and echoes the move
        out.append(("macd_direction", "supporting", sgn(m), abs(m), 1.0))
    if p >= MODERATE["range_high"] or p <= MODERATE["range_low"]:
        out.append(("range_position_20d", "moderate", 1 if p >= 0.5 else -1, p, 1.0 if p >= 0.5 else 0.0))
    if v >= STRONG["relative_volume"]:
        out.append(("relative_volume", "strong", 0, v, STRONG["relative_volume"]))
    elif v >= MODERATE["relative_volume"]:
        out.append(("relative_volume", "moderate", 0, v, STRONG["relative_volume"]))
    return out


def _near_anchor(field: str, value: float, anchor: float) -> bool:
    if field in ("macd_direction", "range_position_20d"):
        return False          # moderate-only signals have no strong anchor
    centre = 50.0 if field == "rsi14" else 0.0                # RSI distance is measured from neutral 50
    return abs(value - anchor) <= NEAR * abs(anchor - centre)


def rubric_reference(card: Mapping[str, float]) -> tuple[str, str, list[str]]:
    """Rubric v0.1 as deterministic rules -> (label, direction, flags).
    This IS a formula; a model trained only on its output distils it."""
    sig = rubric_signals(card)
    counted = [s for s in sig if s[1] != "supporting"]
    families = {FAMILY[s[0]] for s in counted}
    dirs = {s[2] for s in counted if s[2] != 0}
    direction = "mixed" if len(dirs) > 1 else {1: "up", -1: "down"}.get(next(iter(dirs), 0), "none")
    flags = [f"{s[1]}:{s[0]}" for s in sig]
    if card["relative_volume"] <= LOW_VOLUME:
        flags.append("low_volume")
    if card["realized_volatility"] >= VOL_ONLY:
        flags.append("high_volatility")
    near = any(_near_anchor(s[0], s[3], s[4]) for s in counted)
    if direction == "mixed":
        return "ambiguous", direction, flags + ["conflicting_directions"]
    if any(s[1] == "strong" for s in counted):
        if len(counted) == 1 and near:
            return "ambiguous", direction, flags + ["near_threshold"]
        return "review_worthy", direction, flags
    if len(families) >= 2:
        return "review_worthy", direction, flags
    if counted:
        return ("ambiguous", direction, flags + ["near_threshold"]) if near else ("ordinary", direction, flags)
    if "low_volume" in flags or "high_volatility" in flags:
        return "ambiguous", direction, flags + ["single_unexplained_extreme"]
    return "ordinary", direction, flags


def render(card: Mapping[str, float]) -> str:
    """Concise reviewer rendering with units. No identity, date or verdict."""
    return (f"5d {card['five_day_return'] * 100:+.2f}% | vs 20d avg {card['sma20_distance'] * 100:+.2f}% | "
            f"RSI14 {card['rsi14']:.1f} | MACD dir {card['macd_direction']:+.2f} | "
            f"rel vol {card['relative_volume']:.2f}x | ann vol {card['realized_volatility'] * 100:.0f}% | "
            f"20d range pos {card['range_position_20d'] * 100:.0f}%")


def rationale(card: Mapping[str, float], label: str, direction: str, flags: Sequence[str]) -> str:
    sig = rubric_signals(card)
    parts = [f"{s[1]} {s[0]}" for s in sig] or ["no signal at a rubric anchor"]
    extra = [f for f in flags if ":" not in f]
    return f"{label}; direction {direction}; " + ", ".join(parts) + (f"; flags: {', '.join(extra)}" if extra else "")


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
RECORD_KEYS = {"example_id", "source_type", "card", "rendering", "proposed_label", "reference_label",
               "direction", "rationale",
               "ambiguity_flags", "label_status", "provenance", "rubric_version", "partition", "group",
               "private_source"}
FORBIDDEN_TERMS = ("ticker", "symbol", "company", "name", "laya", "score", "rank", "shortlist", "hermes",
                   "decision", "forward", "outcome", "pnl", "return_after")


def make_record(source_type: str, eid: str, card: Mapping[str, float], group: str,
                private_source: Mapping[str, str] | None = None,
                override: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`override` = {"label", "reason"}: Claude's judgement departing from the
    reference rules; recorded beside the rule output, never silently."""
    card = {f: float(card[f]) for f in NUMERIC_FIELDS}
    ref, direction, flags = rubric_reference(card)
    label, why = ref, rationale(card, ref, direction, flags)
    if override:
        label = override["label"]
        why = f"{label} (AI judgement; rules said {ref}: {override['reason']}); " + why.split("; ", 1)[1]
    return {"example_id": eid, "source_type": source_type, "card": card, "rendering": render(card),
            "proposed_label": label, "reference_label": ref, "direction": direction, "rationale": why,
            "ambiguity_flags": [f for f in flags if ":" not in f], "label_status": "ai_proposed_unreviewed",
            "provenance": PROVENANCE, "rubric_version": RUBRIC_VERSION, "partition": "pilot", "group": group,
            "private_source": dict(private_source or {})}


def validate_record(rec: Mapping[str, Any]) -> None:
    """Raises ValueError on a malformed or leaky record."""
    extra = set(rec) - RECORD_KEYS
    if extra or RECORD_KEYS - set(rec):
        raise ValueError(f"record keys wrong: extra={sorted(extra)} missing={sorted(RECORD_KEYS - set(rec))}")
    if rec["source_type"] not in ("real", "synthetic"):
        raise ValueError("source_type")
    if rec["proposed_label"] not in LABELS or rec["reference_label"] not in LABELS:
        raise ValueError("label")
    if set(rec["card"]) != set(NUMERIC_FIELDS):
        raise ValueError("card fields")
    build_card("r_000000000000", rec["card"])                 # range/type checks of the production builder
    if not rec["provenance"] or rec["rubric_version"] != RUBRIC_VERSION:
        raise ValueError("provenance/rubric version")
    model_facing = json.dumps([rec["card"], rec["rendering"]]).lower()
    if any(t in model_facing for t in FORBIDDEN_TERMS):
        raise ValueError("forbidden term in model- or reviewer-facing content")
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


# ---------------------------------------------------------------- build (private outputs)
def build_pilot(overrides: Mapping[str, Mapping[str, str]] | None = None) -> Path:
    import laya_framing     # verified loader of the saved cards (sha + per-day input digests)

    overrides = dict(overrides or {})
    cards_by_session, _ = laya_framing.load_saved_cards()     # scores in the file are never read
    records = [make_record("real", eid, c.model_state(), _key("group", c.row_id)[:12],
                           {"session": s, "row_id": c.row_id}, overrides.pop(eid, None))
               for s, c in sample_real(cards_by_session) for eid in [example_id(s, c.row_id)]]
    records += [make_record("synthetic", eid, card, "syn_" + n, None, overrides.pop(eid, None))
                for n, _, card in SYNTHETIC for eid in [example_id("synthetic", n)]]
    if overrides:
        raise SystemExit(f"overrides name unknown examples: {sorted(overrides)}")
    records.sort(key=lambda r: r["example_id"])                # review order carries no source hint
    for r in records:
        validate_record(r)
    run = OUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=False)
    meta = {"PRIVATE": "real-card values; keep out of git", "rubric_version": RUBRIC_VERSION,
            "sample": {"salt": SAMPLE_SALT, "per_session": REAL_PER_SESSION,
                       "source_sha256": laya_framing.SAVED_RESULTS_SHA256,
                       "method": "per session, smallest sha256(salt|session|row_id), distinct companies"},
            "label_status": "ai_proposed_unreviewed", "provenance": PROVENANCE,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (run / "pilot_dataset.json").write_text(json.dumps({**meta, "records": records}, indent=1), encoding="utf-8")
    with open(run / "review_template.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["example_id", "card", *NUMERIC_FIELDS, "your_label_before_reading_proposal", "your_note",
                    "proposed_label", "proposed_rationale", "decision_accept_change_ambiguous", "final_label",
                    "reviewer", "reviewed_on"])
        for r in records:
            w.writerow([r["example_id"], r["rendering"], *(round(r["card"][k], 4) for k in NUMERIC_FIELDS),
                        "", "", r["proposed_label"], r["rationale"], "", "", "", ""])
    trainable = [r for r in records if r["proposed_label"] in TRAINABLE]
    for name, conv in (("format_check_typed_decisions.jsonl", to_typed_decisions_row),
                       ("format_check_evals.jsonl", to_evals_row)):
        (run / name).write_text("".join(json.dumps(conv(r, allow_provisional=True)) + "\n" for r in trainable),
                                encoding="utf-8")
    return run


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["build"])
    ap.add_argument("--overrides", help="private JSON {example_id: {label, reason}} of AI judgement overrides")
    a = ap.parse_args(argv)
    print(build_pilot(json.loads(Path(a.overrides).read_text(encoding="utf-8")) if a.overrides else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
