"""
identity_cues.py -- an INDEPENDENT known-cue check for model-facing text.

RunMasking.assert_clean searches for the masker's own replacement
dictionary, so it can never see a variant that dictionary lacks
(docs/evaluations/laya_vs_random_2022-07_2026-10-06.md §11.1). This module
keeps a separately maintained reference list of identifying cues verified in
stored backtest prompts (former names, abbreviations, labels, subsidiaries,
affiliates, products, counterparties, facilities) and a structural check for
a masked alias left next to a word of its own legal name ("<alias>
Bancshares"). It deliberately imports nothing from the masker: its job is to
catch the masker's misses.

Finite known-cue protection only. It cannot detect an identifying cue nobody
has listed, a paraphrase, or recognition from the numbers themselves -- see
anonymize.py's KNOWN CEILING.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# cue -> category. Verified in stored prompts (2026-10-07 review) or in the
# project integrity audit's H4 finding; add new cues only with evidence.
KNOWN_IDENTITY_CUES: dict[str, str] = {
    "Allegheny Technologies": "former name",
    "HollyFrontier": "former name",
    "Holly Energy Partners": "affiliate",
    "Warner Records": "label",
    "Warner Chappell": "subsidiary",
    "American Intl Gr": "abbreviation",
    "American Intl Group": "abbreviation",
    "Charles Schwab": "partial-name residue",
    "Huntington Bancshares": "partial-name residue",
    "Marathon Los Angeles Refinery": "facility",
    "Wordle": "product",
    "PF Black Card": "product",
    "AspenTech": "subsidiary",
    "Aspen Technology": "subsidiary",
    "Great Wolf": "counterparty",
    "Berkshire": "counterparty",
    "Buffett": "counterparty",
    "Charlie Munger": "counterparty",
}

# Legal-name words that carry no identity on their own (corporate form,
# articles) or that ordinary headlines put next to a ticker anyway.
_GENERIC = {"inc", "corp", "corporation", "company", "co", "holdings", "holding", "group",
            "incorporated", "plc", "ltd", "llc", "the", "and", "n.v", "nv",
            "market", "shares", "stock", "international", "industries", "services"}


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)


_CUE_PATTERNS = [(cue, _pattern(cue)) for cue in KNOWN_IDENTITY_CUES]


def find_known_cues(text: str) -> list[str]:
    """Every listed cue present in `text` (case-insensitive, word-bounded)."""
    return [cue for cue, pat in _CUE_PATTERNS if pat.search(text or "")]


def find_alias_residues(text: str, masking: Any, watchlist: Iterable[Mapping[str, Any]]) -> list[str]:
    """`<alias> Word` / `Word <alias>` where Word is a distinctive word of
    that company's own legal name -- what a partially masked name leaves
    behind. Built from the watchlist's legal names, not the masker's
    replacement dictionary."""
    hits = []
    for entry in watchlist:
        try:
            alias = masking.alias(entry["symbol"])
        except (KeyError, TypeError):
            continue
        words = {w for w in re.findall(r"[A-Za-z][A-Za-z.&'-]*", str(entry.get("name") or ""))
                 if len(w) >= 4 and w.lower().strip(".") not in _GENERIC}
        for w in words:
            # Exact-case alias, name word as written (capitalized, as in a name).
            if re.search(rf"\b{re.escape(alias)}\s+{re.escape(w)}\b|\b{re.escape(w)}\s+{re.escape(alias)}\b",
                         text or ""):
                hits.append(f"{alias}~{w}")
    return hits


def assert_prompt_clean(text: str, masking: Any = None, watchlist: Iterable[Mapping[str, Any]] | None = None,
                        where: str = "model prompt") -> None:
    """Fail closed (LeakDetected, the masker's own fatal type) if a listed
    cue or an alias-adjacent legal-name residue reaches the final rendered
    text. Imported lazily to keep this module independent of the masker's
    dictionary."""
    found = find_known_cues(text)
    if masking is not None and watchlist is not None:
        found += find_alias_residues(text, masking, watchlist)
    if found:
        raise IdentityCueDetected(f"identity cue(s) {sorted(set(found))[:5]} reached the {where}; refusing to send it")


class IdentityCueDetected(AssertionError):
    """Fatal, like anonymize.LeakDetected (same AssertionError family): the
    cycle halts before the text reaches Hermes. Defined here so this module
    shares nothing with the masker."""
