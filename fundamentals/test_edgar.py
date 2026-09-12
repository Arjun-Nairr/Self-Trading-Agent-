"""Plain-python tests for edgar.py -- no live network access. A fake
SecClient (get_json returns from an in-memory fixture dict, no HTTP at
all) drives get_fundamentals_snapshot end to end; resolve_cik/
resolve_metric/resolve_total_debt/find_comparable_prior_period are also
exercised directly.
Run: python test_edgar.py
"""
import os
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "test-suite contact@example.com")

from datetime import date  # noqa: E402

import edgar  # noqa: E402
from edgar import (  # noqa: E402
    Observation, find_comparable_prior_period, get_fundamentals_snapshot,
    resolve_cik, resolve_metric, resolve_total_debt,
)
from sec_client import SecClient, UserAgentMissingError  # noqa: E402

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def check_true(label: str, cond) -> None:
    global checks
    checks += 1
    assert cond, f"{label}: expected truthy"


def fact(val, start, end, filed, form="10-Q", accn="0000320193-24-000001", fy=2024, fp="Q1"):
    return {"val": val, "start": start, "end": end, "filed": filed, "form": form,
            "accn": accn, "fy": fy, "fp": fp, "unit": "USD"}


def instant_fact(val, end, filed, form="10-K", accn="0000320193-24-000002"):
    """Balance-sheet ('instant') fact -- no start date."""
    return {"val": val, "start": None, "end": end, "filed": filed, "form": form,
            "accn": accn, "fy": 2024, "fp": "FY", "unit": "USD"}


def us_gaap_node(*facts):
    return {"units": {"USD": list(facts)}}


class FakeClient:
    """Drop-in for SecClient: get_json reads from a fixed dict of
    url -> body, never touches the network."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def get_json(self, url, use_cache=True):
        self.calls.append(url)
        return self.responses[url]


TICKER_MAP = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 200406, "ticker": "JNJ", "title": "Johnson & Johnson"},
    # Simulated ambiguous ticker: two distinct CIKs claim the same symbol.
    "2": {"cik_str": 111111, "ticker": "DUPX", "title": "Dup Co A"},
    "3": {"cik_str": 222222, "ticker": "DUPX", "title": "Dup Co B"},
}


def client_for(us_gaap: dict, entity_name="Apple Inc.", cik10="0000320193") -> FakeClient:
    return FakeClient({
        SecClient.TICKER_MAP_URL: TICKER_MAP,
        SecClient.SUBMISSIONS_URL.format(cik10=cik10): {"entityName": entity_name},
        SecClient.COMPANYFACTS_URL.format(cik10=cik10): {"facts": {"us-gaap": us_gaap}},
    })


AS_OF = date(2026, 4, 29)

# -- resolve_cik --------------------------------------------------------
check("resolves a known ticker", resolve_cik(TICKER_MAP, "aapl"), ("0000320193", None))
check("unknown ticker is unavailable, not ambiguous", resolve_cik(TICKER_MAP, "ZZZNOPE"), (None, "unavailable"))
check("ticker mapping to two distinct CIKs is ambiguous", resolve_cik(TICKER_MAP, "DUPX"), (None, "ambiguous"))

# -- required User-Agent enforcement (propagates uncaught, fails clearly) --
os.environ.pop("SEC_EDGAR_USER_AGENT", None)
try:
    get_fundamentals_snapshot("AAPL", "2026-04-29")
    check_true("missing SEC_EDGAR_USER_AGENT must fail clearly", False)
except UserAgentMissingError:
    check_true("missing SEC_EDGAR_USER_AGENT raises UserAgentMissingError", True)
os.environ["SEC_EDGAR_USER_AGENT"] = "test-suite contact@example.com"

# -- ticker resolution end to end (ambiguous / unavailable) ---------------
snap = get_fundamentals_snapshot("DUPX", "2026-04-29",
                                  client=FakeClient({SecClient.TICKER_MAP_URL: TICKER_MAP}))
check("ambiguous ticker -> ambiguous status", snap.status, "ambiguous")
check("ambiguous snapshot has no metrics", snap.metrics, {})

snap = get_fundamentals_snapshot("ZZZNOPE", "2026-04-29",
                                  client=FakeClient({SecClient.TICKER_MAP_URL: TICKER_MAP}))
check("unresolved ticker -> unavailable status", snap.status, "unavailable")

# -- as-of rejection of a filing published after the simulated date ------
us_gaap = {
    "Revenues": us_gaap_node(
        fact(100, "2026-01-01", "2026-03-31", filed="2026-04-25", accn="0001-24-000001"),  # eligible
        fact(999, "2026-01-01", "2026-03-31", filed="2026-05-15", accn="0001-24-000002"),  # filed AFTER as_of
    ),
}
tag, f = resolve_metric(us_gaap, edgar.REVENUE_TAGS, AS_OF)
check("later-filed fact (after as_of) is excluded even though it's the 'newer' looking value",
      f["val"], 100)
check("the excluded fact really is later-filed", f["filed"], "2026-04-25")

# -- period/unit compatibility: a fact missing 'end' or wrong form is skipped
us_gaap_bad_form = {
    "Revenues": us_gaap_node(fact(500, "2026-01-01", "2026-03-31", filed="2026-04-01", form="8-K")),
}
tag, f = resolve_metric(us_gaap_bad_form, edgar.REVENUE_TAGS, AS_OF)
check("a non-10-Q/10-K form is never eligible, even if filed in time", f, None)

# -- every V1 metric's normal path + full provenance -----------------------
full_us_gaap = {
    "Revenues": us_gaap_node(
        fact(1_000_000, "2025-01-01", "2025-03-31", filed="2025-04-20", accn="0001-25-000001"),
        fact(1_200_000, "2026-01-01", "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "NetIncomeLoss": us_gaap_node(
        fact(120_000, "2026-01-01", "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "NetCashProvidedByUsedInOperatingActivities": us_gaap_node(
        fact(150_000, "2026-01-01", "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "CashAndCashEquivalentsAtCarryingValue": us_gaap_node(
        instant_fact(500_000, "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "AssetsCurrent": us_gaap_node(
        instant_fact(900_000, "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "LiabilitiesCurrent": us_gaap_node(
        instant_fact(300_000, "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "LongTermDebtNoncurrent": us_gaap_node(
        instant_fact(400_000, "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
    "LongTermDebtCurrent": us_gaap_node(
        instant_fact(50_000, "2026-03-31", filed="2026-04-20", accn="0001-26-000001"),
    ),
}
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(full_us_gaap))
check("full snapshot status is available", snap.status, "available")
check("entity name carried through from submissions", snap.entity_name, "Apple Inc.")

m = snap.metrics
check("revenue value", m["revenue"].value, 1_200_000)
check("net_income value", m["net_income"].value, 120_000)
check("operating_cash_flow value", m["operating_cash_flow"].value, 150_000)
check("cash_and_equivalents value", m["cash_and_equivalents"].value, 500_000)
check("current_assets value", m["current_assets"].value, 900_000)
check("current_liabilities value", m["current_liabilities"].value, 300_000)
check("total_debt = noncurrent + current", m["total_debt"].value, 450_000)
check("current_ratio = assets / liabilities", m["current_ratio"].value, 3.0)
check("net_margin = net_income / revenue * 100", round(m["net_margin"].value, 4), round(120_000 / 1_200_000 * 100, 4))
check("revenue_growth_yoy = (1.2M - 1.0M) / 1.0M * 100", round(m["revenue_growth_yoy"].value, 4), 20.0)

# every available metric's observation carries complete provenance
required_fields = {f.name for f in fields(Observation)}
for name, mr in m.items():
    if mr.status != "available":
        continue
    obs_list = [mr.observation] if mr.observation else mr.derived_from
    for obs in obs_list:
        check_true(f"{name} observation has every provenance field",
                   required_fields <= set(vars(obs).keys()) and all(
                       getattr(obs, fname) not in ("", None) or fname == "period_start"
                       for fname in required_fields
                   ))
        check_true(f"{name} source_url points at the real filing index", obs.source_url.startswith(
            "https://www.sec.gov/Archives/edgar/data/"))

# -- missing/ambiguous concepts remain unavailable, no custom-tag guessing --
sparse_us_gaap = {"SomeCompanyCustomMetric": us_gaap_node(fact(42, "2026-01-01", "2026-03-31", filed="2026-04-01"))}
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(sparse_us_gaap))
check("revenue unavailable when none of the standard tags are present", snap.metrics["revenue"].status, "unavailable")
check_true("unavailable revenue names which standard tags were tried",
           "Revenues" in snap.metrics["revenue"].reason)
check("total_debt unavailable with no debt tags at all", snap.metrics["total_debt"].status, "unavailable")
check_true("no metric ever reads the custom tag", snap.metrics["revenue"].observation is None)

# -- total_debt: partial composition (only one leg) stays unavailable ------
partial_debt = {
    "LongTermDebtNoncurrent": us_gaap_node(instant_fact(400_000, "2026-03-31", filed="2026-04-01")),
    # LongTermDebtCurrent deliberately absent
}
result = resolve_total_debt(partial_debt, AS_OF)
check_true("total_debt with only one leg present is None (unavailable), never a partial sum", result is None)

mismatched_dates_debt = {
    "LongTermDebtNoncurrent": us_gaap_node(instant_fact(400_000, "2026-03-31", filed="2026-04-01")),
    "LongTermDebtCurrent": us_gaap_node(instant_fact(50_000, "2025-12-31", filed="2026-01-15")),
}
result = resolve_total_debt(mismatched_dates_debt, AS_OF)
check_true("total_debt with mismatched balance-sheet dates stays unavailable", result is None)

# -- revenue_growth_yoy: unavailable without a comparable prior period -----
single_period_revenue = {
    "Revenues": us_gaap_node(fact(1_200_000, "2026-01-01", "2026-03-31", filed="2026-04-20")),
}
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(single_period_revenue))
check("revenue available with a single period", snap.metrics["revenue"].status, "available")
check("revenue_growth_yoy unavailable without a comparable prior period",
      snap.metrics["revenue_growth_yoy"].status, "unavailable")

# find_comparable_prior_period directly: mismatched duration is rejected
mismatched_duration = {
    "Revenues": us_gaap_node(
        fact(1_200_000, "2026-01-01", "2026-03-31", filed="2026-04-20"),  # ~90-day quarter
        fact(4_000_000, "2025-01-01", "2025-12-31", filed="2025-04-20"),  # a FULL YEAR, not comparable
    ),
}
latest = fact(1_200_000, "2026-01-01", "2026-03-31", filed="2026-04-20")
prior = find_comparable_prior_period(mismatched_duration, "Revenues", latest, AS_OF)
check_true("a full-year prior fact is not a comparable prior period for a quarter", prior is None)

# -- net_margin: unavailable when net_income/revenue periods don't match --
mismatched_periods = {
    "Revenues": us_gaap_node(fact(1_200_000, "2026-01-01", "2026-03-31", filed="2026-04-20")),
    "NetIncomeLoss": us_gaap_node(fact(90_000, "2025-10-01", "2025-12-31", filed="2026-01-20")),
}
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(mismatched_periods))
check("net_margin unavailable when periods are incompatible", snap.metrics["net_margin"].status, "unavailable")

# -- current_ratio: unavailable when balance-sheet dates don't match -------
mismatched_balance_dates = {
    "AssetsCurrent": us_gaap_node(instant_fact(900_000, "2026-03-31", filed="2026-04-20")),
    "LiabilitiesCurrent": us_gaap_node(instant_fact(300_000, "2025-12-31", filed="2026-01-20")),
}
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(mismatched_balance_dates))
check("current_ratio unavailable when balance-sheet dates mismatch", snap.metrics["current_ratio"].status,
      "unavailable")

# -- CLI never prints credentials ------------------------------------------
os.environ["SEC_EDGAR_USER_AGENT"] = "secret-looking-agent-string contact@example.com"
snap = get_fundamentals_snapshot("AAPL", "2026-04-29", client=client_for(full_us_gaap))
out = str(snap.to_dict())
check_true("snapshot output never includes the User-Agent value", "secret-looking-agent-string" not in out)

print(f"OK — {checks} checks passed")
