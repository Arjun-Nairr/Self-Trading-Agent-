"""
laya_selector.py -- Phase 1 foundation of the experimental fixed-30 Laya
candidate selector (docs/scopes/LAYA_SELECTOR_V1.md).

EXPERIMENTAL AND UNWIRED. Nothing in run_backtest.py, eval_suite.py, the live
path, the risk checker, or any Hermes prompt imports this module. It exists so
a later, opt-in wiring phase has typed, tested building blocks:

  1. FixedUniverse   -- the static fixed-30 prototype universe, loaded from
                        watchlist/experimental_fixed30.json (never from the
                        baseline watchlist.json) with a stable content hash.
  2. CandidateCard   -- the identity-free numeric card. Only the fields in
                        CARD_FIELDS can exist; there is no place to put a
                        ticker, name, date, headline, fundamental, outcome,
                        or free text, and build_card() rejects any key it does
                        not know instead of silently dropping it.
  3. CardScorer      -- the narrow interface a local scorer (laya_adapter.py,
                        or a deterministic fake in tests) implements.
  4. select_pool     -- a PURE selector: rank by descending review_probability,
                        seeded opaque-row-ID tie-break, up to 10 selected, no
                        exploration picks, every input labelled.
  5. SelectionSnapshot + write_snapshot -- a versioned, byte-stable, atomically
                        written, never-silently-overwritten per-date record.

WHAT review_probability IS NOT. It is Laya's P(true) for one narrow question
("is this identity-free numeric card worth spending the existing full
technical/news review on today?"). It is a review-PRIORITY signal only -- not
a probability of profit, not a probability Hermes succeeds, not a buy signal.
As shipped, Laya's probabilities are over-confident and uncalibrated for this
question, so nothing here thresholds on them (there is no 0.90 gate) and
`answer_confidence` is recorded for audit but is NEVER used to rank or filter.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

REPO = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO / "watchlist" / "experimental_fixed30.json"
BASELINE_WATCHLIST_PATH = REPO / "watchlist" / "watchlist.json"

SELECTOR_VERSION = "laya-selector-v1-phase1"
SNAPSHOT_SCHEMA_VERSION = 1
MAX_SELECTED = 10

STATUS_SELECTED = "selected"
STATUS_NOT_SELECTED = "not_selected"
STATUS_DATA_UNAVAILABLE = "data_unavailable"

FRESHNESS_CURRENT = "current"
FRESHNESS_STALE = "stale"
FRESHNESS_UNVERIFIED = "unverified"


# --------------------------------------------------------------------------
# 1. fixed-30 experimental universe
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    sector: str
    name: str
    group: str  # "baseline" (also in watchlist.json) or "added"
    # Extra masking terms (brands/subsidiaries/shorthand) beyond the name
    # variants derived from `name` -- best-effort, never claimed complete.
    brands: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixedUniverse:
    version: str
    members: tuple[UniverseMember, ...]
    hash: str
    # Tickers whose lowercase/mixed-case form is an ordinary word or name
    # ("dino", "ryan", "cf."): RunMasking matches these case-sensitively so
    # prose is not corrupted. Every other ticker is matched case-insensitively
    # so a lowercase ticker leak is detected.
    word_like_tickers: tuple[str, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(m.symbol for m in self.members)


def universe_hash(
    members: Sequence[UniverseMember], word_like_tickers: Sequence[str] = ()
) -> str:
    """SHA-256 over the canonical (symbol, sector, name, sorted brands) list,
    sorted by symbol, plus the sorted word-like-ticker list -- independent of
    file key order/whitespace and of `group` (a label, not identity). Any
    change to a symbol/sector/name/brand/masking-case rule changes it."""
    canon = json.dumps(
        [
            sorted([m.symbol, m.sector, m.name, sorted(m.brands)] for m in members),
            sorted(word_like_tickers),
        ],
        separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def load_universe(path: Path = UNIVERSE_PATH) -> FixedUniverse:
    """Loads and validates the experimental universe config. Fails loudly (a
    bad universe must never silently proceed) on: not flagged experimental,
    duplicate symbols, a count that disagrees with `expected_count`, or an
    unknown `group`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("experimental") is not True:
        raise ValueError(f"{path}: universe config must set experimental=true")
    members = tuple(
        UniverseMember(
            symbol=str(t["symbol"]), sector=str(t["sector"]),
            name=str(t["name"]), group=str(t["group"]),
            brands=tuple(str(b) for b in t.get("brands", ())),
        )
        for t in raw["tickers"]
    )
    symbols = [m.symbol for m in members]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"{path}: duplicate symbols in universe")
    if len(members) != raw["expected_count"]:
        raise ValueError(
            f"{path}: {len(members)} tickers but expected_count={raw['expected_count']}"
        )
    bad = [m.symbol for m in members if m.group not in ("baseline", "added")]
    if bad:
        raise ValueError(f"{path}: unknown group for {bad}")
    word_like = tuple(str(x) for x in raw.get("word_like_tickers", ()))
    unknown = [x for x in word_like if x not in symbols]
    if unknown:
        raise ValueError(f"{path}: word_like_tickers not in universe: {unknown}")
    return FixedUniverse(version=str(raw["version"]), members=members,
                         hash=universe_hash(members, word_like), word_like_tickers=word_like)


# --------------------------------------------------------------------------
# 2. identity-free numeric candidate card
# --------------------------------------------------------------------------

# The ONLY numeric inputs a card carries (besides its opaque row ID and the
# derived data_complete flag). Order is the canonical order.
NUMERIC_FIELDS: tuple[str, ...] = (
    "five_day_return",
    "sma20_distance",
    "rsi14",
    "macd_direction",
    "relative_volume",
    "realized_volatility",
    "range_position_20d",
)
CARD_FIELDS: tuple[str, ...] = ("row_id", *NUMERIC_FIELDS, "data_complete")

# Inclusive valid ranges for fields with one. A value outside its range is a
# producer bug (it would silently skew the model), not "missing data".
_FIELD_RANGES: dict[str, tuple[float | None, float | None]] = {
    "rsi14": (0.0, 100.0),
    "macd_direction": (-1.0, 1.0),
    "relative_volume": (0.0, None),
    "realized_volatility": (0.0, None),
    "range_position_20d": (0.0, 1.0),
}

# Opaque row IDs are generated, never supplied as a name: "r_" + 12 hex.
_ROW_ID_RE = re.compile(r"^r_[0-9a-f]{12}$")


class ForbiddenCardField(ValueError):
    """A prepared-input key outside the allowed numeric set -- e.g. a ticker,
    company name, date, headline, fundamental, outcome, or free text."""


class InvalidCardValue(ValueError):
    """An allowed field carried a non-numeric type or an out-of-range number."""


@dataclass(frozen=True)
class CandidateCard:
    row_id: str
    five_day_return: float | None
    sma20_distance: float | None
    rsi14: float | None
    macd_direction: float | None
    relative_volume: float | None
    realized_volatility: float | None
    range_position_20d: float | None
    data_complete: bool

    def model_state(self) -> dict[str, float]:
        """The dict a scorer is shown: the numeric fields only. The row ID is
        deliberately NOT part of the model's input (it is an opaque join key,
        meaningless to the model and a needless noise channel); only
        data-complete cards are ever scored, so no None can reach the model."""
        return {name: getattr(self, name) for name in NUMERIC_FIELDS}


def make_row_ids(
    symbols: Sequence[str], seed: int, universe_hash_: str
) -> dict[str, str]:
    """symbol -> opaque row ID, deterministic per (seed, universe). The ID is
    a salted hash, so it carries no ticker, name, sector, or ordering hint;
    the symbol<->ID map is private run state (never part of a card)."""
    out = {
        s: "r_" + hashlib.sha256(f"{seed}|{universe_hash_}|{s}".encode()).hexdigest()[:12]
        for s in symbols
    }
    if len(set(out.values())) != len(out):
        raise ValueError("row-ID collision; choose a different seed")
    return out


def build_card(row_id: str, prepared: Mapping[str, Any]) -> CandidateCard:
    """Build a card from PREPARED technical inputs (already computed by the
    caller -- this performs no market-data fetch).

    `prepared` may contain exactly NUMERIC_FIELDS keys; any other key raises
    ForbiddenCardField (rejected, not silently dropped, so a producer that
    starts leaking a field is caught immediately). Each present value must be
    a real number (bool/str/etc. rejected) inside its valid range, or None /
    NaN / inf (= missing). Missing keys count as missing values. A card with
    any missing value is built with data_complete=False and ALL numeric
    fields cleared to None -- a partial card is never scored."""
    if not _ROW_ID_RE.match(row_id):
        raise ForbiddenCardField(
            f"row_id {row_id!r} is not an opaque generated ID (expected r_<12 hex>; "
            "use make_row_ids) -- a ticker or name must never be a row ID"
        )
    extra = sorted(set(prepared) - set(NUMERIC_FIELDS))
    if extra:
        raise ForbiddenCardField(
            f"fields not allowed on a Laya card: {extra}; "
            f"allowed numeric fields are {list(NUMERIC_FIELDS)}"
        )
    values: dict[str, float | None] = {}
    complete = True
    for name in NUMERIC_FIELDS:
        v = prepared.get(name)
        if v is None:
            values[name] = None
            complete = False
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise InvalidCardValue(f"{name} must be a number, got {type(v).__name__}")
        v = float(v)
        if not math.isfinite(v):
            values[name] = None
            complete = False
            continue
        lo, hi = _FIELD_RANGES.get(name, (None, None))
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise InvalidCardValue(f"{name}={v} outside valid range [{lo}, {hi}]")
        values[name] = v
    if not complete:
        values = {name: None for name in NUMERIC_FIELDS}
    return CandidateCard(row_id=row_id, data_complete=complete, **values)


# --------------------------------------------------------------------------
# 3. scorer interface
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreRecord:
    """One card's score. `review_probability` is a review-PRIORITY signal
    only (see module docstring). `answer_confidence` is logged, never ranked
    or filtered on."""
    row_id: str
    review_probability: float
    answer_confidence: float


@dataclass(frozen=True)
class ScorerMetadata:
    provider: str                 # e.g. "laya", "fake"
    package_version: str | None   # e.g. laya's __version__
    checkpoint: str               # model repo / checkpoint name
    revision: str | None          # pinned immutable revision (commit sha)
    question: str                 # the exact question asked of every card
    settings: str = ""            # canonical JSON of run-affecting settings (batch size, device, ...)


class CardScorer(Protocol):
    metadata: ScorerMetadata

    def score(self, cards: Sequence[CandidateCard]) -> list[ScoreRecord]:
        """Score data-complete cards independently. Must return exactly one
        ScoreRecord per input card, in input order, with finite
        probabilities in [0, 1]. Must be local: no network at score time."""
        ...


class ScorerError(RuntimeError):
    """A scorer returned something the selector cannot trust (wrong count,
    wrong order, unknown row, non-finite or out-of-range probability)."""


def score_complete_cards(
    scorer: CardScorer, cards: Sequence[CandidateCard]
) -> dict[str, ScoreRecord]:
    """Score ONLY data-complete cards (an incomplete card has no numbers to
    show a model and is labelled data_unavailable by select_pool) and return
    row_id -> ScoreRecord. Validates the scorer's contract -- one record per
    card, same order, same row IDs, finite probability in [0, 1], finite
    confidence -- and fails closed (ScorerError) on any violation rather than
    letting a malformed batch influence selection."""
    complete = [c for c in cards if c.data_complete]
    ids = [c.row_id for c in complete]
    if len(set(ids)) != len(ids):
        raise ScorerError("duplicate row_id among the cards to score")
    if not complete:
        return {}
    records = scorer.score(complete)
    if len(records) != len(complete):
        raise ScorerError(f"scorer returned {len(records)} records for {len(complete)} cards")
    out: dict[str, ScoreRecord] = {}
    for card, rec in zip(complete, records):
        if rec.row_id != card.row_id:
            raise ScorerError(f"scorer returned {rec.row_id!r} where {card.row_id!r} was expected")
        if not (math.isfinite(rec.review_probability) and 0.0 <= rec.review_probability <= 1.0):
            raise ScorerError(f"{rec.row_id}: review_probability {rec.review_probability!r} not in [0,1]")
        if not (math.isfinite(rec.answer_confidence) and 0.0 <= rec.answer_confidence <= 1.0):
            raise ScorerError(f"{rec.row_id}: answer_confidence {rec.answer_confidence!r} not in [0,1]")
        out[rec.row_id] = rec
    return out


# --------------------------------------------------------------------------
# 4. pure deterministic selection
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectionRow:
    row_id: str
    status: str                       # selected | not_selected | data_unavailable
    reason: str
    rank: int | None                  # 1-based among scored cards; None if unscored
    review_probability: float | None
    answer_confidence: float | None
    data_complete: bool
    freshness: str                    # current | stale | unverified
    tie_break_key: str                # sha256(seed|row_id), recorded for every row


def _tie_break_key(seed: int, row_id: str) -> str:
    return hashlib.sha256(f"{seed}|{row_id}".encode("utf-8")).hexdigest()


def select_pool(
    cards: Sequence[CandidateCard],
    scores: Mapping[str, ScoreRecord],
    *,
    as_of: date,
    seed: int,
    latest_bar_dates: Mapping[str, date] | None = None,
    k: int = MAX_SELECTED,
) -> tuple[SelectionRow, ...]:
    """Rank by DESCENDING review_probability; break exact ties by ascending
    seeded hash of the opaque row ID; select up to `k` (default 10) -- fewer
    when fewer scored cards exist. No exploration picks. Every input card is
    returned with an explicit label. `answer_confidence` is carried through
    but never read by the ordering.

    A card is `data_unavailable` (never scored, never selectable) when it is
    incomplete, has no ScoreRecord, or its latest bar predates `as_of`
    (stale). `latest_bar_dates` (row_id -> date of the last bar the card was
    built from) is optional; without it freshness is "unverified" and not
    gated on. A bar dated AFTER `as_of` is lookahead and raises."""
    if not 0 < k <= MAX_SELECTED:
        raise ValueError(f"k must be in 1..{MAX_SELECTED}, got {k}")
    ids = [c.row_id for c in cards]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate row_id among cards")
    unknown = sorted(set(scores) - set(ids))
    if unknown:
        raise ValueError(f"scores for row_ids with no card: {unknown}")
    for rid, rec in scores.items():
        if rec.row_id != rid:
            raise ValueError(f"score keyed {rid!r} carries row_id {rec.row_id!r}")
        if not (math.isfinite(rec.review_probability) and 0.0 <= rec.review_probability <= 1.0):
            raise ValueError(f"{rid}: review_probability must be finite in [0,1]")

    unavailable: list[SelectionRow] = []
    scored: list[tuple[CandidateCard, ScoreRecord, str]] = []
    for card in cards:
        freshness = FRESHNESS_UNVERIFIED
        if latest_bar_dates is not None and card.row_id in latest_bar_dates:
            bar = latest_bar_dates[card.row_id]
            if bar > as_of:
                raise ValueError(f"{card.row_id}: latest bar {bar} is after as_of {as_of} (lookahead)")
            freshness = FRESHNESS_CURRENT if bar == as_of else FRESHNESS_STALE
        key = _tie_break_key(seed, card.row_id)
        if not card.data_complete:
            reason = "incomplete card: a technical input is missing or non-finite"
        elif freshness == FRESHNESS_STALE:
            reason = "stale inputs: latest bar predates the as-of date"
        elif card.row_id not in scores:
            reason = "no review score returned for this card"
        else:
            scored.append((card, scores[card.row_id], freshness))
            continue
        unavailable.append(SelectionRow(
            row_id=card.row_id, status=STATUS_DATA_UNAVAILABLE, reason=reason,
            rank=None, review_probability=None, answer_confidence=None,
            data_complete=card.data_complete, freshness=freshness, tie_break_key=key,
        ))

    scored.sort(key=lambda t: (-t[1].review_probability, _tie_break_key(seed, t[0].row_id)))
    rows: list[SelectionRow] = []
    for rank, (card, rec, freshness) in enumerate(scored, start=1):
        picked = rank <= k
        rows.append(SelectionRow(
            row_id=card.row_id,
            status=STATUS_SELECTED if picked else STATUS_NOT_SELECTED,
            reason=(f"rank {rank} <= {k} by review_probability" if picked
                    else f"rank {rank} > {k} by review_probability"),
            rank=rank, review_probability=rec.review_probability,
            answer_confidence=rec.answer_confidence, data_complete=True,
            freshness=freshness, tie_break_key=_tie_break_key(seed, card.row_id),
        ))
    unavailable.sort(key=lambda r: r.row_id)
    return tuple(rows) + tuple(unavailable)


# --------------------------------------------------------------------------
# 5. versioned snapshot + atomic, never-overwritten persistence
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectionSnapshot:
    schema_version: int
    selector_version: str
    scorer: ScorerMetadata
    universe_version: str
    universe_hash: str
    as_of: str                 # ISO trading date (private evidence; never shown to Laya)
    seed: int
    k: int
    inputs_digest: str         # hash of every card's numeric content + freshness
    rows: tuple[SelectionRow, ...]


def inputs_digest(
    cards: Sequence[CandidateCard],
    latest_bar_dates: Mapping[str, date] | None = None,
) -> str:
    """Digest of the card CONTENT (not just IDs). Same IDs with different
    numbers produce a different digest, so a changed input is visible in the
    snapshot rather than looking like the same one."""
    bars = latest_bar_dates or {}
    payload = [
        [c.row_id, *(getattr(c, n) for n in NUMERIC_FIELDS), c.data_complete,
         bars[c.row_id].isoformat() if c.row_id in bars else None]
        for c in sorted(cards, key=lambda c: c.row_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def build_snapshot(
    *, universe: FixedUniverse, scorer: ScorerMetadata, as_of: date, seed: int,
    cards: Sequence[CandidateCard], scores: Mapping[str, ScoreRecord],
    latest_bar_dates: Mapping[str, date] | None = None, k: int = MAX_SELECTED,
) -> SelectionSnapshot:
    rows = select_pool(cards, scores, as_of=as_of, seed=seed,
                       latest_bar_dates=latest_bar_dates, k=k)
    return SelectionSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION, selector_version=SELECTOR_VERSION,
        scorer=scorer, universe_version=universe.version, universe_hash=universe.hash,
        as_of=as_of.isoformat(), seed=seed, k=k,
        inputs_digest=inputs_digest(cards, latest_bar_dates), rows=rows,
    )


def snapshot_to_dict(snap: SelectionSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snap.schema_version,
        "selector_version": snap.selector_version,
        "scorer": {
            "provider": snap.scorer.provider, "package_version": snap.scorer.package_version,
            "checkpoint": snap.scorer.checkpoint, "revision": snap.scorer.revision,
            "question": snap.scorer.question, "settings": snap.scorer.settings,
        },
        "universe_version": snap.universe_version,
        "universe_hash": snap.universe_hash,
        "as_of": snap.as_of, "seed": snap.seed, "k": snap.k,
        "inputs_digest": snap.inputs_digest,
        "rows": [
            {
                "row_id": r.row_id, "status": r.status, "reason": r.reason,
                "rank": r.rank, "review_probability": r.review_probability,
                "answer_confidence": r.answer_confidence,
                "data_complete": r.data_complete, "freshness": r.freshness,
                "tie_break_key": r.tie_break_key,
            }
            for r in snap.rows
        ],
    }


def snapshot_to_json(snap: SelectionSnapshot) -> str:
    """Canonical, byte-stable serialization: sorted keys, fixed separators,
    ASCII only, no NaN, trailing newline. Same snapshot -> same bytes."""
    return json.dumps(snapshot_to_dict(snap), sort_keys=True, indent=1,
                      ensure_ascii=True, allow_nan=False) + "\n"


def snapshot_from_json(text: str) -> SelectionSnapshot:
    d = json.loads(text)
    if d.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported snapshot schema_version {d.get('schema_version')!r} "
            f"(this code reads {SNAPSHOT_SCHEMA_VERSION})"
        )
    s = d["scorer"]
    return SelectionSnapshot(
        schema_version=d["schema_version"], selector_version=d["selector_version"],
        scorer=ScorerMetadata(s["provider"], s["package_version"], s["checkpoint"],
                              s["revision"], s["question"], s["settings"]),
        universe_version=d["universe_version"], universe_hash=d["universe_hash"],
        as_of=d["as_of"], seed=d["seed"], k=d["k"], inputs_digest=d["inputs_digest"],
        rows=tuple(SelectionRow(**r) for r in d["rows"]),
    )


class SnapshotConflict(RuntimeError):
    """A snapshot already exists for this (run, as-of date) with DIFFERENT
    content. Snapshots are immutable per date: never silently overwritten."""


def snapshot_path(base_dir: Path, run_id: str, as_of: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError(f"run_id {run_id!r} is not path-safe")
    return Path(base_dir) / run_id / f"{as_of}.json"


def write_snapshot(base_dir: Path, run_id: str, snap: SelectionSnapshot) -> Path:
    """Atomic AND exclusive: the first writer for a (run_id, as_of) wins and
    nobody can ever replace it. The content is written to a temp file, then
    published with `os.link` -- an atomic create-if-absent that fails with
    FileExistsError if another writer got there first (os.replace would
    silently overwrite, which is the race this closes). A retry that rebuilds
    the IDENTICAL snapshot is a no-op returning the existing path; a
    DIFFERENT snapshot for the same date -- changed inputs, scores, seed,
    universe, or scorer, whether from a later retry or a concurrent writer --
    raises SnapshotConflict and leaves the stored file untouched, so the
    original selection stays the record. A reader never sees a partial
    file: the link exposes only fully written content."""
    path = snapshot_path(base_dir, run_id, snap.as_of)
    text = snapshot_to_json(snap)

    def _check_existing() -> Path:
        existing = path.read_text(encoding="utf-8")
        if existing == text:
            return path
        old = snapshot_from_json(existing)
        raise SnapshotConflict(
            f"{path} already holds a different snapshot for {snap.as_of} "
            f"(stored inputs_digest {old.inputs_digest[:12]}, new {snap.inputs_digest[:12]}); "
            "snapshots are immutable per date"
        )

    if path.exists():
        return _check_existing()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".selection_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _check_existing()
        return path
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_snapshot(base_dir: Path, run_id: str, as_of: str) -> SelectionSnapshot | None:
    path = snapshot_path(base_dir, run_id, as_of)
    return snapshot_from_json(path.read_text(encoding="utf-8")) if path.exists() else None
