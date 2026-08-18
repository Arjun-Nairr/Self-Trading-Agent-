"""
anonymize.py — the no-trained-memory gate for backtest cycles.

THE PROBLEM THIS SOLVES (distinct from the no-future-data gate):
The no-future-data gate (maverick-mcp's `as_of`, alpaca-mcp's
`get_historical_news`) stops future data reaching Hermes through the *tools*.
It does nothing about the other leak path: the model's own pretraining. If
Hermes sees "AAPL, June 2023", it may not reason from the technicals at all --
it may simply recall that Apple rallied through H2 2023 and 'decide'
accordingly. The backtest then measures memory, not judgment, and the
strategy collapses the moment it meets a genuinely unseen market.

This is a real, measured effect, not a hypothetical: published work finds
Sharpe-ratio decay of 51-62% when LLM trading agents move from
pre-training-cutoff periods to genuinely unseen ones ("Profit Mirage",
arXiv:2510.07920; see also Look-Ahead-Bench, arXiv:2601.13770).

THE APPROACH — data-side masking applied consistently across every tool
output, mirroring the KTD-Fin protocol (arXiv:2605.28359): strip the
retrieval cues the model would need to identify *which* company at *which*
point in history it is looking at. No ticker, no company name, no absolute
date -> nothing to look up in memory. The technical and sentiment *signal*
is untouched, so genuine reasoning is unaffected.

DELIBERATELY NOT MASKED — prices, volumes, and ratios. Masking those is a
real temptation (a $187 share price is a weak fingerprint) but the research
is explicit that it "can destroy more signal than it removes bias": position
sizing, stop placement, and support/resistance reasoning all depend on real
numbers. Leaving them real is a deliberate call, not an oversight.

KNOWN CEILING (the honest part, and it is not theoretical): masking removes
explicit retrieval cues but "leaves subtler memorization channels intact".
Running this gate over real Benzinga headlines for 2023-06-15 showed exactly
how, and it is worse than the obvious "the iPhone maker" paraphrase case:

    "Xbox's Matt Booty ... assures Microsoft's brand ..."
    "Microsoft, Activision Ask U.S. Judge To Expedite FTC Challenge"

`Microsoft` masks cleanly. `Xbox` and `Activision` do not -- and they leak
harder, because the Microsoft/Activision antitrust fight identifies both the
company AND pins the calendar period to within weeks. For well-known names,
BRANDS, SUBSIDIARIES, EXECUTIVES, AND DEAL COUNTERPARTIES are the real leak
surface; the company name is the easy part.

Three consequences, all real:
  1. `names` must carry brands/subsidiaries/counterparties, not just the
     legal name. That is config, not code -- but it is inherently
     incomplete, since nobody can enumerate every brand and counterparty in
     advance.
  2. This gate is therefore necessary but NOT sufficient on its own. It
     needs the detection layer (comparing performance on pre-cutoff vs
     genuinely post-cutoff windows -- Look-Ahead-Bench's own methodology,
     arXiv:2601.13770) to know whether it actually worked.
  3. Less-famous tickers leak dramatically less: a mid-cap industrial has no
     Xbox, no Buffett, no landmark antitrust case attached to it. This is a
     real, concrete input to the still-open watchlist criteria in RULES.md,
     not a stylistic preference.

# ponytail: `names` is passed in, not fetched -- keeps this module pure
# stdlib and trivially testable. The harness is responsible for supplying
# real name variants per ticker (yfinance longName + common shorthands).
"""

import random
import re
from datetime import date
from typing import Any, Iterable

# Phonetic aliases: meaningless by construction, so they carry no sector,
# size, or nationality hint the model could use as a retrieval cue.
_ALIAS_POOL = [
    "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF",
    "HOTEL", "INDIA", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER",
]

# Date shapes that actually show up in financial news text.
_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                      # 2023-06-15
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                # 6/15/2023
    re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
        r"\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
        re.IGNORECASE,
    ),                                                          # June 15, 2023
    re.compile(
        r"\b(?:Q[1-4]|FY)\s*'?\d{2,4}\b", re.IGNORECASE
    ),                                                          # Q2 2023, FY24
]
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")

_DATE_REDACTED = "[DATE REDACTED]"
_YEAR_REDACTED = "[YEAR REDACTED]"


def _term_pattern(term: str) -> re.Pattern[str]:
    r"""Word-boundary-anchored matcher for a company name or ticker.

    `\b` only anchors next to a word character, so a term ending in
    punctuation ("Apple Inc.") never matches with a naive `\b...\b` -- the
    boundary between "." and the following space is not a word boundary.
    That silently left the longest name variant unmatched, so "Apple Inc."
    masked to "<ALIAS> Inc.", leaking the corporate suffix. Anchor only the
    ends that are actually alphanumeric.
    """
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return re.compile(rf"{prefix}{re.escape(term)}{suffix}", re.IGNORECASE)


class LeakDetected(AssertionError):
    """Raised when masked output still contains a real identifier.

    Deliberately fatal. A silent leak corrupts every downstream result in a
    way that looks like success, so a backtest cycle must die loudly here
    rather than quietly produce a memorization-contaminated decision.
    """


class RunMasking:
    """Per-run, stable ticker<->alias mapping plus text/payload scrubbing.

    Stable *within* a run so Hermes can track its own positions across
    cycles ("still holding BRAVO"); reshuffled *between* runs (different
    `seed`) so a mapping can't be learned across runs.
    """

    def __init__(
        self,
        names: dict[str, Iterable[str]],
        run_start: date,
        seed: int = 0,
    ) -> None:
        """`names` maps each real ticker to its name variants to scrub, e.g.
        {"AAPL": ["Apple Inc.", "Apple"]}. `run_start` is day 1 of the
        simulated window (what relative day indices count from)."""
        tickers = sorted(names)
        if len(tickers) > len(_ALIAS_POOL):
            raise ValueError(
                f"{len(tickers)} tickers exceeds the {len(_ALIAS_POOL)}-alias "
                "pool; extend _ALIAS_POOL"
            )
        pool = list(_ALIAS_POOL)
        random.Random(seed).shuffle(pool)

        self._names = {t: list(v) for t, v in names.items()}
        self._to_alias = dict(zip(tickers, pool))
        self._to_real = {a: t for t, a in self._to_alias.items()}
        self._run_start = run_start

        # Longest-first so "Apple Inc." is consumed before the bare "Apple"
        # inside it, which would otherwise leave a dangling " Inc." behind.
        self._patterns = [
            (_term_pattern(term), ticker)
            for ticker in tickers
            for term in sorted(
                [ticker, *self._names[ticker]], key=len, reverse=True
            )
        ]

    # -- identity ---------------------------------------------------------

    def alias(self, ticker: str) -> str:
        return self._to_alias[ticker.upper()]

    def real(self, alias: str) -> str:
        """Translate a decision about an alias back to the real ticker --
        used by the harness *after* Hermes decides, so the fill simulator
        and trade journal record the real instrument."""
        return self._to_real[alias.upper()]

    def day_index(self, as_of: date) -> str:
        """Absolute date -> 'Day N'. Hermes still needs a sense of elapsed
        time (how long a position has been held); it does not need to know
        the calendar date, which is a direct retrieval cue."""
        return f"Day {(as_of - self._run_start).days + 1}"

    # -- scrubbing --------------------------------------------------------

    def mask_text(self, text: str) -> str:
        """Replace every known ticker/company name with its alias and redact
        absolute dates and years."""
        for pattern, ticker in self._patterns:
            text = pattern.sub(self._to_alias[ticker], text)
        for pattern in _DATE_PATTERNS:
            text = pattern.sub(_DATE_REDACTED, text)
        return _YEAR_PATTERN.sub(_YEAR_REDACTED, text)

    def mask_news(
        self, articles: list[dict[str, Any]], as_of: date
    ) -> list[dict[str, Any]]:
        """Mask alpaca-mcp's `get_historical_news` output.

        `created_at` becomes relative age in days rather than being dropped:
        recency is real signal Hermes should weigh (a headline from this
        morning matters more than one from last week), while the absolute
        publication date is pure retrieval cue.

        `source_url` is dropped entirely -- Benzinga URLs embed both the
        company slug and the publication date, and Hermes cannot browse
        anyway, so there is nothing to trade off.
        """
        masked = []
        for a in articles:
            published = date.fromisoformat(a["created_at"][:10])
            masked.append(
                {
                    "headline": self.mask_text(a["headline"]),
                    "summary": self.mask_text(a.get("summary", "")),
                    "age_days": (as_of - published).days,
                    "symbols": [
                        self._to_alias[s.upper()]
                        for s in a.get("symbols", [])
                        if s.upper() in self._to_alias
                    ],
                }
            )
        return masked

    def mask_technicals(
        self, payload: dict[str, Any], as_of: date
    ) -> dict[str, Any]:
        """Mask maverick-mcp's technical payloads.

        Only identity fields are touched. Every indicator value, price, and
        level passes through untouched and unrounded -- that is the signal
        Hermes is supposed to reason from, and the research warning about
        anonymization destroying more signal than bias applies squarely
        here.
        """
        masked = dict(payload)
        if "ticker" in masked:
            masked["ticker"] = self.alias(masked["ticker"])
        # maverick's own `analysis_metadata.as_of` is a real calendar date.
        meta = masked.get("analysis_metadata")
        if isinstance(meta, dict):
            meta = dict(meta)
            if "as_of" in meta:
                meta["as_of"] = self.day_index(as_of)
            masked["analysis_metadata"] = meta
        return masked

    # -- self-check -------------------------------------------------------

    def assert_clean(self, payload: Any) -> None:
        """Fail loudly if any real identifier survived into masked output.

        Catches the realistic failure mode: a new field or a new tool whose
        output nobody remembered to route through the masker. It cannot
        catch unknown paraphrases ("the iPhone maker") -- see this module's
        KNOWN CEILING note; that is what the detection layer is for.

        Only *string* values are scanned, never numbers. Scanning the whole
        payload blob would flag a share price of $2023.40 as a leaked year --
        a false alarm that would halt a legitimate backtest cycle on any
        high-priced instrument.
        """
        blob = "\n".join(_strings(payload))
        for ticker, variants in self._names.items():
            for term in (ticker, *variants):
                if _term_pattern(term).search(blob):
                    raise LeakDetected(
                        f"real identifier {term!r} survived masking in: "
                        f"{blob[:200]}"
                    )
        if _YEAR_PATTERN.search(blob):
            raise LeakDetected(f"absolute year survived masking in: {blob[:200]}")


def _strings(payload: Any) -> Iterable[str]:
    """Every string value reachable in a nested dict/list payload."""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _strings(item)
