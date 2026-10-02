"""Proof the no-trained-memory gate actually strips what it claims to.

Every case here is adversarial in the way a real payload is: names nested
inside longer names, mixed date formats, identifiers hiding in fields nobody
thinks about (`symbols`, URLs, metadata). Run: python test_anonymize.py
"""
from datetime import date

from anonymize import LeakDetected, RunMasking

NAMES = {
    "AAPL": ["Apple Inc.", "Apple"],
    "MSFT": ["Microsoft Corporation", "Microsoft"],
}
RUN_START = date(2023, 1, 3)


def _masker(seed: int = 7) -> RunMasking:
    return RunMasking(NAMES, run_start=RUN_START, seed=seed)


def test_alias_identity():
    m = _masker()
    # Stable within a run, and round-trips back to the real ticker so the
    # harness can execute the decision against the real instrument.
    assert m.alias("AAPL") == m.alias("AAPL")
    assert m.real(m.alias("AAPL")) == "AAPL"
    assert m.alias("AAPL") != m.alias("MSFT")
    # Aliases carry no information about the real ticker.
    assert "AAPL" not in m.alias("AAPL")

    # Different runs must not reuse the same mapping, or the mapping itself
    # becomes learnable across runs.
    seeds = {RunMasking(NAMES, RUN_START, seed=s).alias("AAPL") for s in range(12)}
    assert len(seeds) > 1, "alias assignment is not run-dependent"


def test_day_index_replaces_calendar_date():
    m = _masker()
    assert m.day_index(date(2023, 1, 3)) == "Day 1"
    assert m.day_index(date(2023, 1, 13)) == "Day 11"


def test_mask_text_strips_tickers_and_names():
    m = _masker()
    alias = m.alias("AAPL")

    assert m.mask_text("AAPL rallied") == f"{alias} rallied"
    assert m.mask_text("Apple rallied") == f"{alias} rallied"
    # Case-insensitive: real headlines are not consistently cased.
    assert m.mask_text("apple rallied") == f"{alias} rallied"

    # The nesting trap: "Apple" lives inside "Apple Inc.". Longest-first
    # replacement must consume the whole name, not leave a dangling "Inc."
    masked = m.mask_text("Apple Inc. beat estimates")
    assert masked == f"{alias} beat estimates", masked
    assert "Inc." not in masked


def test_mask_text_redacts_every_date_shape():
    m = _masker()
    for raw in (
        "reported on 2023-06-15",
        "reported on 6/15/2023",
        "reported on June 15, 2023",
        "reported on Jun. 15 2023",
    ):
        masked = m.mask_text(raw)
        assert "2023" not in masked, (raw, masked)
        assert "REDACTED" in masked, (raw, masked)

    # Fiscal/quarter references are retrieval cues too.
    assert "2023" not in m.mask_text("Q2 2023 guidance")
    # A bare year with no surrounding date still identifies the period.
    assert "2023" not in m.mask_text("the best year since 2023")


def test_mask_news_strips_identity_but_keeps_signal():
    m = _masker()
    alias = m.alias("AAPL")
    articles = [
        {
            "headline": "Apple (AAPL) beats Q2 2023 estimates",
            "summary": "Apple Inc. reported on 2023-06-15 that revenue rose.",
            "created_at": "2023-06-13T14:30:00Z",
            "symbols": ["AAPL"],
            "source_url": "https://benzinga.com/news/23/06/apple-earnings-beat",
        }
    ]

    masked = m.mask_news(articles, as_of=date(2023, 6, 15))[0]

    # Identity gone from every field, including the ones easy to forget.
    assert "Apple" not in str(masked) and "AAPL" not in str(masked)
    assert masked["symbols"] == [alias]
    # The URL embeds both company slug and date -- must not survive at all.
    assert "source_url" not in masked

    # Signal kept: recency is real information Hermes should weigh, so the
    # absolute date becomes relative age rather than being dropped.
    assert masked["age_days"] == 2
    assert "beats" in masked["headline"]
    assert "revenue rose" in masked["summary"]

    m.assert_clean(masked)


def test_mask_technicals_keeps_every_number_untouched():
    """The research warning made concrete: masking identity must not touch
    prices/indicators, or position sizing and support/resistance reasoning
    lose the data they depend on."""
    m = _masker()
    payload = {
        "ticker": "AAPL",
        "status": "success",
        "current_price": 187.42,
        "rsi": 28.6,
        "levels": {"support": [180.15, 176.4], "resistance": [192.8]},
        "analysis_metadata": {"bars_analyzed": 247, "as_of": "2023-06-15"},
    }

    masked = m.mask_technicals(payload, as_of=date(2023, 6, 15))

    assert masked["ticker"] == m.alias("AAPL")
    assert masked["analysis_metadata"]["as_of"] == "Day 164"
    # Every number survives exactly.
    assert masked["current_price"] == 187.42
    assert masked["rsi"] == 28.6
    assert masked["levels"] == {"support": [180.15, 176.4], "resistance": [192.8]}
    assert masked["analysis_metadata"]["bars_analyzed"] == 247
    # Input not mutated in place -- the harness still needs the real payload
    # to record the true ticker in the trade journal afterwards.
    assert payload["ticker"] == "AAPL"

    m.assert_clean(masked)


def test_assert_clean_catches_real_leaks():
    m = _masker()

    # A field that skipped the masker entirely -- the realistic failure mode
    # when a new tool gets wired in.
    try:
        m.assert_clean({"headline": "ALPHA rose", "note": "per Apple's filing"})
        raise AssertionError("leaked company name was not detected")
    except LeakDetected:
        pass

    try:
        m.assert_clean({"headline": "AAPL rose"})
        raise AssertionError("leaked ticker was not detected")
    except LeakDetected:
        pass

    try:
        m.assert_clean({"note": "strongest since 2023"})
        raise AssertionError("leaked year was not detected")
    except LeakDetected:
        pass


def test_brand_and_counterparty_names_are_maskable_via_config():
    """Empirical finding from real Benzinga headlines: for well-known names,
    brands and deal counterparties leak identity harder than the company
    name itself -- "Xbox's Matt Booty" and "Microsoft, Activision Ask U.S.
    Judge..." both identify the company AND pin the period, while the word
    "Microsoft" is the easy part.

    The fix is config, not code: the masker already accepts arbitrary
    variant lists, so brands/subsidiaries/counterparties belong in `names`.
    This test locks that in -- and, by needing to be extended by hand for
    every new brand, documents exactly why this gate is necessary-but-not-
    sufficient and needs the detection layer behind it.
    """
    m = RunMasking(
        {"MSFT": ["Microsoft Corporation", "Microsoft", "Xbox", "Activision"]},
        run_start=RUN_START,
        seed=3,
    )
    alias = m.alias("MSFT")

    masked = m.mask_text("Xbox's Matt Booty assures Microsoft's brand turned the corner")
    assert "Xbox" not in masked and "Microsoft" not in masked, masked
    assert masked.count(alias) == 2, masked

    masked = m.mask_text("Microsoft, Activision ask U.S. judge to expedite FTC challenge")
    assert "Activision" not in masked, masked
    m.assert_clean(masked)


def test_assert_clean_does_not_false_alarm_on_prices():
    """Regression: a share price near $2023 must not be mistaken for a
    leaked calendar year and halt a legitimate cycle."""
    m = _masker()
    m.assert_clean({"ticker": "ALPHA", "current_price": 2023.40, "volume": 1995})


def test_real_watchlist_headline_forms_are_masked():
    """Regression for a real leak found by running actual watchlist names
    through actual headline text: the old one-shot suffix strip
    (split(",")[0].split(" Inc")[0]) never produced the bare brand form at
    all, so 7 of 10 names -- exactly the form real headlines use -- sailed
    through unmasked while assert_clean blessed them. Locks in
    run_backtest._name_variants's iterative strip + brand-override fix."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import run_backtest as rb

    watchlist = [
        {"symbol": "OLN", "name": "Olin Corporation"},
        {"symbol": "WMG", "name": "Warner Music Group Corp."},
        {"symbol": "SFM", "name": "Sprouts Farmers Market, Inc."},
        {"symbol": "DINO", "name": "HF Sinclair Corporation"},
        {"symbol": "RYAN", "name": "Ryan Specialty Holdings, Inc."},
        {"symbol": "BRKR", "name": "Bruker Corporation"},
        {"symbol": "BRX", "name": "Brixmor Property Group Inc."},
        {"symbol": "ALGM", "name": "Allegro MicroSystems, Inc."},
    ]
    headlines = {
        "OLN": "Olin Announces Q2 Results, Beats Estimates",
        "WMG": "Warner Music Group Signs New Streaming Deal",
        "SFM": "Sprouts Same-Store Sales Up 4%",
        "DINO": "HF Sinclair Completes Refinery Turnaround",
        "RYAN": "Ryan Specialty Acquires Rival Brokerage",
        "BRKR": "Bruker Shares Fall After Guidance Cut",
        "BRX": "Brixmor Raises Dividend",
        "ALGM": "Allegro Wins Auto Chip Contract",
    }
    names = {t["symbol"]: rb._name_variants(t["name"], t["symbol"]) for t in watchlist}
    m = RunMasking(names, run_start=RUN_START, seed=42)
    for symbol, headline in headlines.items():
        masked = m.mask_text(headline)
        m.assert_clean(masked)  # raises LeakDetected if anything real survived
        assert symbol not in masked, (symbol, masked)


def test_mapping_is_deterministic_for_same_seed_and_snapshot():
    """Correction pass item D (freeform red-team R2), property 2: the SAME
    seed + the SAME frozen ticker/name snapshot always reproduces the
    identical alias mapping -- required so a resume can freeze the mapping
    once and trust it's reproducible from (seed, snapshot) alone."""
    m1 = RunMasking(NAMES, run_start=RUN_START, seed=42)
    m2 = RunMasking(dict(NAMES), run_start=RUN_START, seed=42)  # fresh dict, same content
    assert m1.mapping() == m2.mapping()
    assert m1.mapping() == {"AAPL": m1.alias("AAPL"), "MSFT": m1.alias("MSFT")}


def test_mapping_reshuffles_when_watchlist_membership_changes():
    """Correction pass item D, the actual defect this closes: RunMasking's
    alias assignment is a POSITIONAL zip against sorted(tickers), so adding
    even one ticker (same seed) reassigns EVERY existing ticker's alias --
    not just the new one's. This is exactly why the watchlist snapshot must
    be frozen per run rather than re-read live on every resume."""
    before = RunMasking(NAMES, run_start=RUN_START, seed=42).mapping()
    grown = dict(NAMES, GOOG=["Alphabet Inc.", "Google"])
    after = RunMasking(grown, run_start=RUN_START, seed=42).mapping()
    moved = [t for t in before if after[t] != before[t]]
    assert moved, "expected adding one ticker to reshuffle at least one existing alias"


def test_frozen_snapshot_prompt_text_stays_alias_masked():
    """Correction pass item D, property 4: text built from a frozen
    watchlist snapshot never carries a real ticker/company name to the
    agent -- only its alias, exactly as an unfrozen snapshot would."""
    m = RunMasking(NAMES, run_start=RUN_START, seed=42)
    for ticker, variants in NAMES.items():
        masked = m.mask_text(f"{ticker} ({variants[0]}) reported strong earnings")
        m.assert_clean(masked)
        assert ticker not in masked and variants[0] not in masked
        assert m.alias(ticker) in masked


# -- fixed-30 / "opaque" alias scheme (Laya selector prototype, Phase 1) ------

def _fixed30_names():
    """The real experimental universe, with the real name-variant derivation."""
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import run_backtest as rb

    raw = json.loads((Path(__file__).parent.parent / "watchlist" / "experimental_fixed30.json")
                     .read_text(encoding="utf-8"))
    return {t["symbol"]: rb._name_variants(t["name"], t["symbol"]) for t in raw["tickers"]}


def test_default_nato_scheme_is_unchanged_for_the_fixed_10_baseline():
    """Compat guard: every 10-name run (and every resumed run's frozen alias
    map) must keep reproducing exactly the aliases the original code gave.
    Literals computed from the pre-change anonymize.py (HEAD e5ec811+), which
    was also diffed against this implementation over 200 seeds."""
    baseline = ["OLN", "WMG", "PLNT", "SFM", "DINO", "RYAN", "BRKR", "ATI", "BRX", "ALGM"]
    m = RunMasking({t: [] for t in baseline}, run_start=RUN_START, seed=42)
    assert m.mapping() == {
        "ALGM": "INDIA", "ATI": "MIKE", "BRKR": "HOTEL", "BRX": "GOLF", "DINO": "NOVEMBER",
        "OLN": "LIMA", "PLNT": "FOXTROT", "RYAN": "CHARLIE", "SFM": "JULIET", "WMG": "DELTA",
    }
    assert RunMasking(NAMES, RUN_START, seed=7).mapping() == {"AAPL": "DELTA", "MSFT": "LIMA"}


def test_default_scheme_still_caps_at_14_and_points_at_the_opaque_scheme():
    names = {f"T{i:02d}": [] for i in range(15)}
    try:
        RunMasking(names, run_start=RUN_START, seed=1)
        raise AssertionError("expected the legacy 14-alias cap to still raise")
    except ValueError as e:
        assert "opaque" in str(e)
    try:
        RunMasking(NAMES, run_start=RUN_START, seed=1, alias_scheme="bogus")
        raise AssertionError("unknown scheme must be rejected")
    except ValueError as e:
        assert "alias_scheme" in str(e)


def test_opaque_scheme_supports_30_names_deterministically_with_unique_aliases():
    names = _fixed30_names()
    assert len(names) == 30
    m1 = RunMasking(names, run_start=RUN_START, seed=42, alias_scheme="opaque")
    m2 = RunMasking(dict(names), run_start=RUN_START, seed=42, alias_scheme="opaque")
    assert m1.mapping() == m2.mapping(), "same seed + universe must reproduce the mapping"
    aliases = list(m1.mapping().values())
    assert len(aliases) == 30 and len(set(aliases)) == 30, "no duplicate aliases"
    assert m1.mapping() != RunMasking(names, RUN_START, seed=43, alias_scheme="opaque").mapping()
    # Reversible through the stored map.
    for ticker, alias in m1.mapping().items():
        assert m1.real(alias) == ticker and m1.alias(ticker) == alias
    # Round-trips through a plain JSON-style dict copy (what a persisted map is).
    stored = dict(m1.mapping())
    assert {a: t for t, a in stored.items()} == {m1.alias(t): t for t in names}


def test_opaque_aliases_are_not_words_and_survive_mask_text_untouched():
    import re
    names = _fixed30_names()
    for seed in range(100):
        m = RunMasking(names, run_start=RUN_START, seed=seed, alias_scheme="opaque")
        for ticker, alias in m.mapping().items():
            assert re.fullmatch(r"[BCDGHJKLMNPRSTVWXZ]{2}[1-9][0-9]", alias), alias
            # Never equal to any ticker/name term the masker strips...
            assert alias not in {ticker.upper(), *(v.upper() for v in names[ticker])}
            # ...and the date/year redaction pass must not mangle an alias.
            assert m.mask_text(f"{alias} rose") == f"{alias} rose", alias


def test_opaque_aliases_are_stable_when_universe_membership_changes():
    """Unlike the positional-zip legacy scheme: removing/adding a ticker must
    not reshuffle everyone else's alias (needed once the pool can rotate)."""
    names = _fixed30_names()
    full = RunMasking(names, RUN_START, seed=42, alias_scheme="opaque").mapping()
    fewer = {t: v for t, v in names.items() if t not in ("OLN", "NUE", "ZBRA")}
    part = RunMasking(fewer, RUN_START, seed=42, alias_scheme="opaque").mapping()
    assert all(part[t] == full[t] for t in part)


def test_opaque_scheme_does_not_corrupt_ordinary_prose_with_word_like_tickers():
    """The collision regression: tickers that are also ordinary English words
    ("ON", "IT", "ALL", "KEY") used to be substituted inside plain prose and
    then trip assert_clean on innocent text."""
    names = {
        "ON": ["ON Semiconductor Corporation", "ON Semiconductor"],
        "IT": ["Gartner, Inc.", "Gartner"],
        "ALL": ["Allstate Corporation", "Allstate"],
        "KEY": ["KeyCorp"],
    }
    m = RunMasking(names, run_start=RUN_START, seed=5, alias_scheme="opaque")
    prose = "Stop placed on support; it holds all week. All in all, a key day -- on balance, fine."
    assert m.mask_text(prose) == prose, "ordinary lowercase/capitalized prose must be untouched"
    m.assert_clean(prose)

    # A real, capitalized ticker mention is still masked and still detected.
    masked = m.mask_text("Shares of ON (NYSE: ON) rose; ALL and KEY lagged")
    assert not any(_term in masked.split() for _term in ("ON", "ALL", "KEY")), masked
    assert m.alias("ON") in masked
    try:
        m.assert_clean("ON rose")
        raise AssertionError("an uppercase ticker leak must still be detected")
    except LeakDetected:
        pass
    # Company names stay case-insensitive: headlines are not consistently cased.
    assert m.alias("ON") in m.mask_text("on semiconductor raised guidance")
    assert m.alias("ALL") in m.mask_text("ALLSTATE posts results")

    # Contrast: the unchanged legacy scheme DOES corrupt this prose -- the
    # documented limitation that is why the fixed-30 prototype opts in.
    legacy = RunMasking({"ON": ["ON Semiconductor"]}, run_start=RUN_START, seed=5)
    assert legacy.mask_text("Stop placed on support") != "Stop placed on support"


def test_opaque_masking_keeps_the_real_headline_forms_masked_for_all_30():
    names = _fixed30_names()
    m = RunMasking(names, run_start=RUN_START, seed=42, alias_scheme="opaque")
    for ticker, variants in names.items():
        text = f"{variants[0]} ({ticker}) reported strong earnings"
        masked = m.mask_text(text)
        m.assert_clean(masked)
        assert m.alias(ticker) in masked and ticker not in masked.split(), (ticker, masked)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("PASS: no-trained-memory gate strips identity, keeps signal, "
          "and fails loudly on leaks")


if __name__ == "__main__":
    main()
