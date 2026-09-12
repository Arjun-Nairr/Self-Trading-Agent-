"""edgar.py — SEC EDGAR Fundamentals V1: the module's one public
operation, `get_fundamentals_snapshot(ticker, as_of_date)`.

Owns ticker->CIK resolution, filing-as-of selection, US-GAAP fact
normalization, and derived-metric calculation. `sec_client.py` owns the
network/cache/rate-limit layer this file calls into.

DATA FOUNDATION ONLY (per docs/research/sec_edgar_v1_data_design.md):
not imported by run_backtest.py, not referenced in AGENTS.md/RULES.md/
SOUL.md, no dashboard page, no MCP tool, no agent-facing wiring. Called
on demand by a human or a future integration milestone; never inside a
trading decision loop.

CLI (for validation only, never a dashboard/agent endpoint):
    python fundamentals/edgar.py --ticker WMG --as-of 2026-04-29
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

# Own directory on sys.path so `import sec_client` resolves both when this
# file is run directly (python fundamentals/edgar.py, the documented CLI
# form) and when imported as fundamentals.edgar from a test running from
# the repo root -- same pattern backtest-harness's own scripts use.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sec_client import SecClient, SecFetchError, UserAgentMissingError  # noqa: E402

# ---------------------------------------------------------------------
# V1 standard US-GAAP tag fallback order, one list per metric. ONLY these
# exact tags are ever read for a given metric -- no custom/company-
# specific tag is ever substituted, and a metric with none of its tags
# present is explicitly unavailable, never guessed. Each list is tried
# in order; the first tag with an as-of-eligible fact wins.
# ---------------------------------------------------------------------
REVENUE_TAGS = [
    "Revenues",  # broadest/generic tag, used across most of the ASC 606 transition
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606 (2018+) primary tag for most filers
    "SalesRevenueNet",  # pre-2018 tag some filers never migrated off
]
NET_INCOME_TAGS = [
    "NetIncomeLoss",  # standard consolidated net income/loss tag
    "ProfitLoss",  # IFRS/foreign-private-issuer alias some filers report under instead
]
OPERATING_CASH_FLOW_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",  # filers that separate discontinued ops
]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",  # ASU 2016-18 combined tag some filers adopted
]
CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
CURRENT_LIABILITIES_TAGS = ["LiabilitiesCurrent"]
# Total debt is a FIXED two-tag composition, not a fallback list: "total
# debt" is only well-defined here when BOTH the noncurrent and current
# portions of long-term debt are present for the SAME balance-sheet
# date. If either tag is missing, this is marked unavailable rather than
# treating the missing piece as zero (silently understates debt) or
# substituting a different tag (mixes incompatible debt definitions) --
# see resolve_total_debt().
TOTAL_DEBT_COMPONENT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebtCurrent")

# Only 10-Q/10-K and their amendments count as eligible source filings.
ELIGIBLE_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}

# Tolerance for matching a "comparable prior period" for YoY growth:
# the prior period's duration (end - start) must be within this many
# days of the latest period's duration (handles calendar-quarter length
# variance), and its end date must fall in this day-count window before
# the latest period's end (handles fiscal calendars that aren't exactly
# 365 days apart).
DURATION_TOLERANCE_DAYS = 10
YOY_WINDOW_DAYS = (330, 400)


@dataclass
class Observation:
    """One normalized, provenance-carrying SEC fact -- exactly the
    contract docs/research/sec_edgar_v1_data_design.md specifies."""
    ticker: str
    cik: str
    metric: str
    value: float
    unit: str
    period_start: str | None
    period_end: str
    filed_at: str
    form: str
    accession_number: str
    source_url: str
    fetched_at: str


@dataclass
class MetricResult:
    status: str  # "available" | "unavailable"
    reason: str | None = None
    value: float | None = None
    observation: Observation | None = None
    derived_from: list[Observation] = field(default_factory=list)


@dataclass
class FundamentalsSnapshot:
    ticker: str
    as_of_date: str
    status: str  # "available" | "unavailable" | "ambiguous"
    reason: str | None
    cik: str | None
    entity_name: str | None
    fetched_at: str
    metrics: dict[str, MetricResult]

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(ticker: str, as_of_date: str, status: str, reason: str | None,
              cik: str | None = None, entity_name: str | None = None,
              metrics: dict[str, MetricResult] | None = None) -> FundamentalsSnapshot:
    return FundamentalsSnapshot(
        ticker=ticker.upper(), as_of_date=as_of_date, status=status, reason=reason,
        cik=cik, entity_name=entity_name, fetched_at=_now_iso(), metrics=metrics or {},
    )


# ---------------------------------------------------------------------
# ticker -> CIK resolution
# ---------------------------------------------------------------------

def resolve_cik(ticker_map: dict, ticker: str) -> tuple[str | None, str | None]:
    """Returns (cik10, error_status) where error_status is None on a
    clean single match, 'unavailable' if the ticker isn't in SEC's map,
    or 'ambiguous' if it maps to more than one distinct CIK."""
    ticker = ticker.upper()
    ciks = {
        str(row["cik_str"]) for row in ticker_map.values()
        if str(row.get("ticker", "")).upper() == ticker
    }
    if not ciks:
        return None, "unavailable"
    if len(ciks) > 1:
        return None, "ambiguous"
    return next(iter(ciks)).zfill(10), None


# ---------------------------------------------------------------------
# filing-as-of selection + fact normalization
# ---------------------------------------------------------------------

def _facts_for_tag(us_gaap: dict, tag: str) -> list[dict]:
    """Only USD facts -- every V1 metric is a monetary quantity. A tag's
    `units` dict can carry other unit keys (shares, per-share, a foreign
    currency); those are never eligible here regardless of date, so a
    non-USD entry can never be selected "because it's latest" -- a tag
    with no USD-unit facts at all behaves exactly like a tag with no
    facts, i.e. unavailable, never a substituted unit."""
    node = us_gaap.get(tag)
    if not node:
        return []
    return [{**e, "unit": "USD"} for e in (node.get("units") or {}).get("USD") or []]


def _select_eligible(entries: list[dict], as_of: date) -> list[dict]:
    """Only 10-Q/10-K/amendments filed on or before as_of -- never a
    later filing, amendment, or restatement just because it's visible
    now."""
    out = []
    for e in entries:
        filed = e.get("filed")
        if e.get("form") not in ELIGIBLE_FORMS or not filed or not e.get("end"):
            continue
        if date.fromisoformat(filed) > as_of:
            continue
        out.append(e)
    return out


def _duration_days(e: dict) -> int:
    """0 for an instant/balance-sheet fact (no `start`) -- a flow-metric
    tiebreaker only; an instant fact is never assigned an invented
    duration."""
    if not e.get("start"):
        return 0
    return (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days


def _selection_key(e: dict) -> tuple:
    """Deterministic reporting-period selection, independent of source-
    list order (SEC's own JSON can carry more than one valid fact for
    the same tag with the same period end and filing date -- e.g. a
    10-Q's quarter-only flow next to its year-to-date flow):

      1. Latest eligible period end wins.
      2. Then latest eligible filing date wins.
      3. Then, for a FLOW fact only (duration > 0), the LONGEST reported
         duration wins -- so a 10-Q's own year-to-date fact is preferred
         over its quarter-only fact when both tie on end+filed, and a
         10-K's full-year fact wins the same way. An instant/balance-
         sheet fact's duration is always 0 here; this step never invents
         one and never lets a flow fact outrank an instant fact through
         it (instant vs. flow are never compared for the same metric).
      4. Final stable tiebreak: the accession number itself -- unique,
         and never affected by dict/list iteration order, unlike relying
         on Python's max() alone over an unordered tie.
    """
    return (e["end"], e["filed"], _duration_days(e), e["accn"])


def _latest(entries: list[dict]) -> dict | None:
    """'Latest eligible' per the deterministic policy in _selection_key."""
    if not entries:
        return None
    return max(entries, key=_selection_key)


def resolve_metric(us_gaap: dict, tags: list[str], as_of: date) -> tuple[str, dict] | tuple[None, None]:
    """First tag (in fixed fallback order) with an eligible fact wins --
    no cross-tag mixing within one metric, no custom tag ever tried."""
    for tag in tags:
        latest = _latest(_select_eligible(_facts_for_tag(us_gaap, tag), as_of))
        if latest is not None:
            return tag, latest
    return None, None


def resolve_total_debt(us_gaap: dict, as_of: date) -> tuple[dict, dict] | None:
    """Both TOTAL_DEBT_COMPONENT_TAGS must resolve to eligible USD facts
    for the SAME balance-sheet date (period end) -- otherwise this
    returns None (unavailable), never a partial or zero-filled sum.
    `_facts_for_tag` already restricts both legs to unit=="USD" before
    this ever runs, so the explicit equality check below is a second,
    independent guarantee that a future change to that filter can't
    silently reintroduce a mixed-unit sum."""
    noncurrent_tag, current_tag = TOTAL_DEBT_COMPONENT_TAGS
    noncurrent_entries = _select_eligible(_facts_for_tag(us_gaap, noncurrent_tag), as_of)
    current_entries = _select_eligible(_facts_for_tag(us_gaap, current_tag), as_of)
    if not noncurrent_entries or not current_entries:
        return None
    noncurrent = _latest(noncurrent_entries)
    matching_current = [
        e for e in current_entries
        if e["end"] == noncurrent["end"] and e["unit"] == noncurrent["unit"]
    ]
    current = _latest(matching_current)
    if current is None:
        return None
    return noncurrent, current


def find_comparable_prior_period(us_gaap: dict, tag: str, latest: dict, as_of: date) -> dict | None:
    """A prior-period fact under the SAME tag (already USD-only via
    _facts_for_tag) whose duration matches latest's (within
    DURATION_TOLERANCE_DAYS) and whose end date falls ~1 year before
    latest's end (within YOY_WINDOW_DAYS) -- and which is itself
    as-of-eligible, so growth never leaks a future-known prior-period
    restatement. Among qualifying candidates, the SAME deterministic
    _selection_key ranking used everywhere else picks the winner --
    never dependent on source-list order. None if no such period exists."""
    if not latest.get("start"):
        return None
    latest_end = date.fromisoformat(latest["end"])
    latest_start = date.fromisoformat(latest["start"])
    latest_duration = (latest_end - latest_start).days

    candidates = []
    for c in _select_eligible(_facts_for_tag(us_gaap, tag), as_of):
        if not c.get("start") or c["end"] == latest["end"]:
            continue
        c_end = date.fromisoformat(c["end"])
        c_start = date.fromisoformat(c["start"])
        days_back = (latest_end - c_end).days
        if not (YOY_WINDOW_DAYS[0] <= days_back <= YOY_WINDOW_DAYS[1]):
            continue
        if abs((c_end - c_start).days - latest_duration) > DURATION_TOLERANCE_DAYS:
            continue
        candidates.append(c)
    return _latest(candidates)


def _observation(ticker: str, cik: str, metric: str, tag_fact: tuple[str, dict], cik_int: str) -> Observation:
    _tag, fact = tag_fact
    return Observation(
        ticker=ticker, cik=cik, metric=metric, value=float(fact["val"]), unit=fact["unit"],
        period_start=fact.get("start"), period_end=fact["end"], filed_at=fact["filed"],
        form=fact["form"], accession_number=fact["accn"],
        source_url=_filing_index_url(cik_int, fact["accn"]), fetched_at=_now_iso(),
    )


def _filing_index_url(cik_int: str, accession_number: str) -> str:
    accn_nodashes = accession_number.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik_int)}/{accn_nodashes}/{accession_number}-index.htm"


# ---------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------

_SIMPLE_METRICS = [
    ("revenue", REVENUE_TAGS),
    ("net_income", NET_INCOME_TAGS),
    ("operating_cash_flow", OPERATING_CASH_FLOW_TAGS),
    ("cash_and_equivalents", CASH_TAGS),
    ("current_assets", CURRENT_ASSETS_TAGS),
    ("current_liabilities", CURRENT_LIABILITIES_TAGS),
]


def _build_metrics(us_gaap: dict, ticker: str, cik: str, as_of: date) -> dict[str, MetricResult]:
    metrics: dict[str, MetricResult] = {}
    resolved_tag_fact: dict[str, tuple[str, dict]] = {}

    for name, tags in _SIMPLE_METRICS:
        tag, fact = resolve_metric(us_gaap, tags, as_of)
        if fact is None:
            metrics[name] = MetricResult(status="unavailable",
                                          reason=f"no eligible fact for any of {tags}")
            continue
        resolved_tag_fact[name] = (tag, fact)
        obs = _observation(ticker, cik, name, (tag, fact), cik)
        metrics[name] = MetricResult(status="available", value=obs.value, observation=obs)

    # total_debt: fixed two-tag composition, never a fallback chain.
    debt = resolve_total_debt(us_gaap, as_of)
    if debt is None:
        metrics["total_debt"] = MetricResult(
            status="unavailable",
            reason=f"compatible {TOTAL_DEBT_COMPONENT_TAGS} composition not available for the same balance-sheet date",
        )
    else:
        noncurrent_fact, current_fact = debt
        noncurrent_obs = _observation(ticker, cik, "long_term_debt_noncurrent",
                                       (TOTAL_DEBT_COMPONENT_TAGS[0], noncurrent_fact), cik)
        current_obs = _observation(ticker, cik, "long_term_debt_current",
                                    (TOTAL_DEBT_COMPONENT_TAGS[1], current_fact), cik)
        metrics["total_debt"] = MetricResult(
            status="available", value=noncurrent_obs.value + current_obs.value,
            derived_from=[noncurrent_obs, current_obs],
        )

    # revenue_growth_yoy: only with a matching comparable prior period.
    if "revenue" in resolved_tag_fact:
        tag, latest_fact = resolved_tag_fact["revenue"]
        prior = find_comparable_prior_period(us_gaap, tag, latest_fact, as_of)
        if prior is None:
            metrics["revenue_growth_yoy"] = MetricResult(
                status="unavailable", reason="no comparable prior-year period available")
        elif prior["val"] == 0:
            metrics["revenue_growth_yoy"] = MetricResult(
                status="unavailable", reason="prior-period revenue is zero, growth undefined")
        else:
            prior_obs = _observation(ticker, cik, "revenue_prior_period", (tag, prior), cik)
            growth = (latest_fact["val"] - prior["val"]) / prior["val"] * 100
            metrics["revenue_growth_yoy"] = MetricResult(
                status="available", value=growth,
                derived_from=[metrics["revenue"].observation, prior_obs],
            )
    else:
        metrics["revenue_growth_yoy"] = MetricResult(status="unavailable", reason="revenue unavailable")

    # net_margin: only when revenue exists for the EXACT same period.
    if "net_income" in resolved_tag_fact and "revenue" in resolved_tag_fact:
        ni_obs, rev_obs = metrics["net_income"].observation, metrics["revenue"].observation
        if ni_obs.period_start == rev_obs.period_start and ni_obs.period_end == rev_obs.period_end:
            if rev_obs.value == 0:
                metrics["net_margin"] = MetricResult(status="unavailable", reason="revenue is zero, margin undefined")
            else:
                metrics["net_margin"] = MetricResult(
                    status="available", value=ni_obs.value / rev_obs.value * 100,
                    derived_from=[ni_obs, rev_obs],
                )
        else:
            metrics["net_margin"] = MetricResult(
                status="unavailable",
                reason="net income and revenue periods do not match (incompatible periods)")
    else:
        metrics["net_margin"] = MetricResult(status="unavailable", reason="net_income or revenue unavailable")

    # current_ratio: only when both share the same balance-sheet date.
    if "current_assets" in resolved_tag_fact and "current_liabilities" in resolved_tag_fact:
        ca_obs, cl_obs = metrics["current_assets"].observation, metrics["current_liabilities"].observation
        if ca_obs.period_end != cl_obs.period_end:
            metrics["current_ratio"] = MetricResult(
                status="unavailable",
                reason="current assets and current liabilities are not from the same balance-sheet date")
        elif cl_obs.value == 0:
            metrics["current_ratio"] = MetricResult(status="unavailable", reason="current liabilities are zero")
        else:
            metrics["current_ratio"] = MetricResult(
                status="available", value=ca_obs.value / cl_obs.value,
                derived_from=[ca_obs, cl_obs],
            )
    else:
        metrics["current_ratio"] = MetricResult(
            status="unavailable", reason="current_assets or current_liabilities unavailable")

    return metrics


def get_fundamentals_snapshot(ticker: str, as_of_date: str, client: SecClient | None = None) -> FundamentalsSnapshot:
    """The module's one public operation. `as_of_date` is 'YYYY-MM-DD'.
    UserAgentMissingError propagates uncaught -- a missing SEC_EDGAR_USER_AGENT
    must fail clearly, never silently degrade to 'unavailable'."""
    as_of = date.fromisoformat(as_of_date)
    client = client or SecClient()  # raises UserAgentMissingError if unset

    try:
        ticker_map = client.get_json(SecClient.TICKER_MAP_URL)
    except SecFetchError as e:
        return _snapshot(ticker, as_of_date, "unavailable", f"could not fetch SEC ticker map: {e}")

    cik10, error_status = resolve_cik(ticker_map, ticker)
    if error_status == "unavailable":
        return _snapshot(ticker, as_of_date, "unavailable", "ticker not found in SEC ticker map")
    if error_status == "ambiguous":
        return _snapshot(ticker, as_of_date, "ambiguous", "ticker resolves to more than one CIK")

    try:
        submissions = client.get_json(SecClient.SUBMISSIONS_URL.format(cik10=cik10))
        companyfacts = client.get_json(SecClient.COMPANYFACTS_URL.format(cik10=cik10))
    except SecFetchError as e:
        return _snapshot(ticker, as_of_date, "unavailable", f"could not fetch SEC filing data: {e}", cik=cik10)

    entity_name = submissions.get("entityName")
    us_gaap = (companyfacts.get("facts") or {}).get("us-gaap") or {}
    if not us_gaap:
        return _snapshot(ticker, as_of_date, "unavailable", "no us-gaap facts available for this company",
                          cik=cik10, entity_name=entity_name)

    metrics = _build_metrics(us_gaap, ticker.upper(), cik10, as_of)
    return _snapshot(ticker, as_of_date, "available", None, cik=cik10, entity_name=entity_name, metrics=metrics)


# ---------------------------------------------------------------------
# CLI (human validation only -- never a dashboard/agent endpoint)
# ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="SEC EDGAR Fundamentals V1 -- read-only, point-in-time-safe "
                     "snapshot for one ticker. Validation CLI only.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    args = parser.parse_args(argv)

    try:
        snapshot = get_fundamentals_snapshot(args.ticker, args.as_of)
    except UserAgentMissingError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(snapshot.to_dict(), indent=2))


if __name__ == "__main__":
    main()
