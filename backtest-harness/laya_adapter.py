"""
laya_adapter.py -- the narrow, local Laya adapter for the experimental fixed-30
selector (docs/scopes/LAYA_SELECTOR_V1.md). UNWIRED: nothing in the default
backtest, live path, risk checker, or Hermes prompt imports this module.

What it does: asks Laya ONE typed question of every identity-free numeric card
in one batched call (`laya.decide_batch(..., questions=REVIEW_QUESTIONS,
return_details=True)`), and turns
each answer into a ScoreRecord(row_id, review_probability, answer_confidence).

The question (a Laya `noul` with explicit criteria -- QUESTION_FORMAT v2):

    "Is this identity-free numeric card worth spending the existing full
     technical/news review on today?"

`review_probability` is Laya's P(true) for that question, read from the raw
answer (`DecisionResult.answers[field]["noul"]`) exactly as the provider
reports it, with no further rounding or transformation here. (Laya itself
reports noul to 4 decimals, so exact ties are possible -- the selector's
seeded row-ID tie-break exists for that.) It is a review-PRIORITY signal only -- not a probability of profit, not the
chance Hermes succeeds, not a buy signal. Laya ships over-confident and
uncalibrated for this question, so nothing here thresholds on it, and
`answer_confidence` is captured for audit only. No Laya-generated prose
exists anywhere in the path: the output is two numbers per card.

Local and isolated: scoring takes a LOCAL checkpoint directory (`laya.load`
never contacts the network for an existing path), the weights are verified
against a pinned SHA-256 before anything is parsed, no `min_confidence` is
passed (so no card is silently dropped), no server is started, and the only
network step -- `provision_checkpoint` -- is a separate, explicit function the
scorer never calls. `laya` itself is imported lazily, so this module imports
(and its unit tests run) without torch or any model weights.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Sequence

from laya_selector import (
    NUMERIC_FIELDS, CandidateCard, ScoreRecord, ScorerError, ScorerMetadata,
)

# Pins -- the tightest the provider supports: an exact PyPI version, the HF
# repo's immutable commit, and the SHA-256 of the weight file. (Same commit as
# laya.PINNED_REVISIONS["convaiinnovations/laya"]; mirrored here so a provider
# bump cannot silently move this project's pin.)
LAYA_PACKAGE_VERSION = "0.3.23"
LAYA_MODEL_ID = "convaiinnovations/laya"
LAYA_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
LAYA_SHA256 = {
    "model.safetensors": "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
}

REVIEW_QUESTION = (
    "Is this identity-free numeric card worth spending the existing full "
    "technical/news review on today?"
)
ANSWER_FIELD = "worth_review"

# Question format v2 (docs/evaluations/laya_selector_demo_2026-10-03.md). v1 sent
# a JSON-schema boolean, which laya turns into a `noul` with NO criteria. The
# pinned package's own README says that on the English `laya` checkpoint such a
# question renders one generic option pair ("no, the statement does not hold" /
# "yes, the statement holds") that "carries the whole decision, so it answers
# 'no' whatever the state". Our synthetic diagnostic confirmed it (every card
# below 0.5, active cards ranked BELOW quiet ones). v2 asks the same question
# as an explicit `noul` whose two options describe the card, same package,
# checkpoint, and P(true) meaning. It discriminates only weakly (see the report).
QUESTION_FORMAT = "noul-criteria-v2"
REVIEW_CRITERIA = {
    "true": ("yes: the numbers show a notable technical situation today -- a large recent move, "
             "price stretched well away from its 20-day average or range, an extreme RSI, a strong "
             "MACD swing, or unusual volume -- worth a full technical and news review"),
    "false": ("no: the numbers look ordinary today -- a small recent move, price near its 20-day "
              "average and mid-range, RSI near 50, a flat MACD, normal volume -- not worth a full review"),
}
REVIEW_QUESTIONS = {
    ANSWER_FIELD: {"type": "noul", "instructions": REVIEW_QUESTION, "criteria": REVIEW_CRITERIA},
}

BATCH_SIZE = 32       # fixed: batch shape can nudge float results; >= universe size
STATE_DECIMALS = 4    # model input is rounded to a fixed width: short, deterministic text
DEVICE = "cpu"


def card_to_state(card: CandidateCard) -> dict[str, float]:
    """The exact dict shown to Laya: NUMERIC_FIELDS only, canonical order,
    fixed rounding. No row ID, no label, no text. Incomplete cards never get
    here (their numbers are None)."""
    if not card.data_complete:
        raise ValueError(f"{card.row_id}: incomplete card must not be scored")
    return {name: round(getattr(card, name), STATE_DECIMALS) for name in NUMERIC_FIELDS}


def _settings_json() -> str:
    return json.dumps(
        {"batch_size": BATCH_SIZE, "device": DEVICE, "min_confidence": None,
         "state_decimals": STATE_DECIMALS, "answer_field": ANSWER_FIELD,
         "question_format": QUESTION_FORMAT, "criteria": REVIEW_CRITERIA},
        sort_keys=True, separators=(",", ":"),
    )


def expected_metadata() -> ScorerMetadata:
    """The scorer identity every selection snapshot must carry, built from the
    pins alone (no model load) -- so a controller can validate a stored
    snapshot, or the worker's handshake, without importing laya or torch."""
    return ScorerMetadata(
        provider="laya", package_version=LAYA_PACKAGE_VERSION, checkpoint=LAYA_MODEL_ID,
        revision=LAYA_REVISION, question=REVIEW_QUESTION, settings=_settings_json(),
    )


class LayaScorer:
    """CardScorer backed by Laya's batched structured decisions.

    `agent` is a loaded laya Agent; `decide_batch` is laya.decide_batch.
    Both are injectable so tests can exercise this class with deterministic
    fakes and no model weights; use `LayaScorer.load(...)` for the real thing.
    """

    def __init__(self, agent: Any, decide_batch: Callable[..., list], metadata: ScorerMetadata):
        self._agent = agent
        self._decide_batch = decide_batch
        self.metadata = metadata

    @classmethod
    def load(cls, checkpoint_dir: str) -> "LayaScorer":
        """Load the pinned English checkpoint from an existing LOCAL directory
        (see provision_checkpoint). Refuses a wrong-commit snapshot and any
        weight file whose SHA-256 differs from the pin. Never touches the
        network: `checkpoint_dir` exists locally, so laya skips the Hub."""
        import os

        if not os.path.isdir(checkpoint_dir):
            raise FileNotFoundError(
                f"{checkpoint_dir!r} is not a directory; run provision_checkpoint() once "
                "(the only network step) and pass the directory it returns"
            )
        import laya
        from laya.revisions import snapshot_revision

        if laya.__version__ != LAYA_PACKAGE_VERSION:
            raise RuntimeError(
                f"laya {laya.__version__} installed; this adapter is pinned to {LAYA_PACKAGE_VERSION}"
            )
        revision = snapshot_revision(checkpoint_dir)
        if revision is not None and revision != LAYA_REVISION:
            raise RuntimeError(f"checkpoint snapshot {revision} is not the pinned {LAYA_REVISION}")
        agent = laya.load(checkpoint_dir, device=DEVICE, expected_sha256=LAYA_SHA256)
        meta = ScorerMetadata(
            provider="laya", package_version=laya.__version__, checkpoint=LAYA_MODEL_ID,
            revision=revision, question=REVIEW_QUESTION, settings=_settings_json(),
        )
        return cls(agent, laya.decide_batch, meta)

    def score(self, cards: Sequence[CandidateCard]) -> list[ScoreRecord]:
        if not cards:
            return []
        states = [card_to_state(c) for c in cards]
        results = self._decide_batch(
            self._agent, states, questions=REVIEW_QUESTIONS, return_details=True,
            batch_size=BATCH_SIZE,
        )
        if len(results) != len(cards):
            raise ScorerError(f"laya returned {len(results)} results for {len(cards)} cards")
        out = []
        for card, res in zip(cards, results):
            answer = (res.answers or {}).get(ANSWER_FIELD)
            if not isinstance(answer, dict) or "noul" not in answer:
                raise ScorerError(f"{card.row_id}: no noul answer for {ANSWER_FIELD!r}")
            p = float(answer["noul"])
            conf = (res.answer_confidence or {}).get(ANSWER_FIELD)
            if conf is None:
                raise ScorerError(f"{card.row_id}: laya reported no answer_confidence")
            conf = float(conf)
            if not (math.isfinite(p) and 0.0 <= p <= 1.0 and math.isfinite(conf) and 0.0 <= conf <= 1.0):
                raise ScorerError(f"{card.row_id}: non-finite or out-of-range laya output p={p} conf={conf}")
            out.append(ScoreRecord(card.row_id, p, conf))
        return out


def provision_checkpoint(cache_dir: str | None = None) -> str:
    """The ONE network step: download the pinned English checkpoint (about
    843 MB, official `convaiinnovations/laya` repo at commit LAYA_REVISION)
    from the Hugging Face Hub and return its local snapshot directory. Run it
    once, explicitly, by a person; the scorer never calls it."""
    from huggingface_hub import snapshot_download

    return snapshot_download(
        LAYA_MODEL_ID, revision=LAYA_REVISION, cache_dir=cache_dir,
        allow_patterns=["rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*"],
    )
