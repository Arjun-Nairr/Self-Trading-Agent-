"""Standalone eval for the risk checker — every rule in RULES.md's Phase 1
aggressive profile that can be mechanically enforced, fed hand-built
scenarios with no harness involved.

Run: python test_risk_checker.py

The baseline scenario below is a deliberately clean, legal trade under the
Phase 1 numbers (2% risk budget, 20% max position, 1.5:1 base min R:R,
3-12% stop band, cash-only exposure, -30% drawdown as the only breaker).
Each test changes exactly one thing and asserts the checker catches exactly
that. If a test fails, the scenario it describes is a trade the agent could
really have placed.
"""
import dataclasses

from risk_checker import (
    AccountState, Context, MarketClock, Position, ProposedTrade, RiskLimits,
    check_trade,
)

# $100k equity, nothing held, mid-session, AAPL on the watchlist.
# BUY 150 @ $100 = $15,000 = 15% of equity        (cap 20%)
# stop $92 -> 8% below entry                       (bounds 3-12%)
# risk 150 x $8 = $1,200 = 1.2% of equity           (budget 2%)
# target $132 -> reward $32 / risk $8 = 4.0         (minimum 1.5)
BASE_TRADE = dict(
    ticker="AAPL", action="BUY", quantity=150,
    entry_price=100.0, stop_price=92.0, target_price=132.0,
)


def _ctx(**overrides) -> Context:
    account_kw = overrides.pop("account", {})
    clock_kw = overrides.pop("clock", {})
    watchlist = overrides.pop("watchlist", ["AAPL", "MSFT"])
    account = AccountState(**{
        "equity": 100_000.0, "cash": 100_000.0, "daily_pl_pct": 0.0,
        "weekly_pl_pct": 0.0, "drawdown_from_peak_pct": 0.0,
        "trading_blocked": False, **account_kw,
    })
    clock = MarketClock(**{
        "is_open": True, "minutes_since_open": 120,
        "minutes_until_close": 120, **clock_kw,
    })
    return Context(
        account=account, clock=clock, watchlist=watchlist, **overrides
    )


def _trade(**overrides) -> ProposedTrade:
    return ProposedTrade(**{**BASE_TRADE, **overrides})


def _blocked_by(section: str, trade=None, ctx=None) -> None:
    """Assert the trade is rejected, and specifically for `section`."""
    verdict = check_trade(trade or _trade(), ctx or _ctx())
    assert not verdict.allowed, f"expected rejection for {section}, got approval"
    assert any(section in v for v in verdict.violations), (
        f"expected a {section} violation, got: {verdict.violations}"
    )


def main():
    # -- the baseline must pass, or every other test is meaningless -------
    verdict = check_trade(_trade(), _ctx())
    assert verdict.allowed, f"clean trade was rejected: {verdict.violations}"
    assert bool(verdict) is True
    print("  ok  a clean, legal trade is approved")

    # -- §1 scope ---------------------------------------------------------
    _blocked_by("§1", trade=_trade(ticker="TSLA"))          # off-watchlist
    _blocked_by("§1", trade=_trade(action="SELL"))           # short (not held)
    print("  ok  §1  off-watchlist and short selling blocked")

    # -- §1 watchlist fail-closed -------------------------------------------
    _blocked_by("§1", ctx=_ctx(watchlist=[]))
    print("  ok  §1  empty/unavailable watchlist blocks every BUY (fail closed)")

    verdict = check_trade(
        _trade(action="SELL", stop_price=None, target_price=None),
        _ctx(watchlist=[], positions=[Position(ticker="AAPL", quantity=150, market_value=15_000.0)]),
    )
    assert verdict.allowed, f"exit wrongly blocked by empty watchlist: {verdict.violations}"
    print("  ok  §1  empty watchlist does not block an exit of an already-held position")

    # -- §9 prohibitions (also: one open position per ticker) -------------
    _blocked_by("§9.1", ctx=_ctx(
        positions=[Position(ticker="AAPL", quantity=10, market_value=1_000.0)]
    ))
    _blocked_by("§9.4", ctx=_ctx(stopped_out_today=["AAPL"]))
    print("  ok  §9  one-position-per-ticker and same-day re-entry blocked")

    # =======================================================================
    # PHASE 1 AGGRESSIVE PROFILE
    # =======================================================================

    # -- item 1 / item 2: exact-boundary risk (2%) and position (20%) -------
    # entry=$100, stop=$90 (10% distance, mid-band) -- chosen so BOTH land
    # exactly on their own cap at once: qty=200 -> notional=$20,000=20% of
    # equity (cap 20%), risk=200x$10=$2,000=2.0% of equity (budget 2%).
    # Both must PASS exactly at the boundary; qty=201 breaches BOTH and
    # must FAIL, reporting both.
    boundary_trade = _trade(quantity=200, stop_price=90.0, target_price=125.0)
    verdict = check_trade(boundary_trade, _ctx())
    assert verdict.allowed, (
        f"exactly 2% risk / exactly 20% position should pass: {verdict.violations}"
    )
    print("  ok  §2a/§2b  exactly 2% risk and exactly 20% position both pass")

    verdict = check_trade(_trade(quantity=201, stop_price=90.0, target_price=125.0), _ctx())
    assert not verdict.allowed
    assert any("§2a" in v for v in verdict.violations), verdict.violations
    assert any("§2b" in v for v in verdict.violations), verdict.violations
    print("  ok  §2a/§2b  above 2% risk / above 20% position both fail")

    # -- item 3: cash-only exposure -----------------------------------------
    # Cash is the binding constraint here (not the 20% position cap):
    # equity $100k (cap would allow $20k), but only $5k cash available.
    # Using ALL of it (qty=50 @ $100 = $5,000) must pass; one dollar more
    # must fail, and specifically for §3, not §2b (position is only 5%).
    cash_ctx = _ctx(account={"cash": 5_000.0})
    verdict = check_trade(
        _trade(quantity=50, stop_price=92.0, target_price=132.0), cash_ctx
    )
    assert verdict.allowed, f"spending exactly all available cash should pass: {verdict.violations}"
    print("  ok  §3  a trade may use all available cash")

    verdict = check_trade(
        _trade(quantity=51, stop_price=92.0, target_price=132.0), cash_ctx
    )
    assert not verdict.allowed
    assert any("§3" in v for v in verdict.violations), verdict.violations
    assert not any("§2b" in v for v in verdict.violations), (
        "this should fail on cash alone, not the position cap too: "
        f"{verdict.violations}"
    )
    print("  ok  §3  exceeding available cash by even $1 fails, cash cannot go negative")

    # -- item 4: no position-count limit -------------------------------------
    # 15 other tickers already held (small size each) must never by itself
    # block a new, otherwise-eligible ticker -- there is no cap left to hit.
    many_positions = [
        Position(ticker=f"T{i}", quantity=1, market_value=100.0) for i in range(15)
    ]
    verdict = check_trade(_trade(), _ctx(positions=many_positions))
    assert verdict.allowed, (
        f"existing position count alone must never block a new eligible ticker: "
        f"{verdict.violations}"
    )
    print("  ok  §3  no position-count limit -- 15 open positions do not block a 16th")

    # -- item 5: §6a stop distance 3-12% ------------------------------------
    verdict = check_trade(_trade(stop_price=97.0, target_price=115.5), _ctx())  # exactly 3%
    assert verdict.allowed, f"exactly 3% stop distance should pass: {verdict.violations}"
    verdict = check_trade(_trade(stop_price=88.0, target_price=132.0), _ctx())  # exactly 12%
    assert verdict.allowed, f"exactly 12% stop distance should pass: {verdict.violations}"
    print("  ok  §6a  exactly 3% and exactly 12% stop distances both pass")

    _blocked_by("§6a", trade=_trade(stop_price=97.5))   # 2.5% -- too close
    _blocked_by("§6a", trade=_trade(stop_price=87.0))   # 13% -- too far
    _blocked_by("§6a", trade=_trade(stop_price=None))   # missing
    _blocked_by("§6a", trade=_trade(stop_price=105.0))  # above entry
    print("  ok  §6a  outside 3-12%, missing, or inverted stops all blocked")

    # Rounding-tolerance regression at the NEW floor: a stop landing at
    # 2.99% (a cent-rounded miss, same shape as the original 4.99%-vs-5.00%
    # smoke-test failure) must still pass; a real 2.5% miss must not.
    verdict = check_trade(_trade(quantity=150, stop_price=97.01, target_price=115.5), _ctx())
    assert verdict.allowed, (
        f"a cent-rounded stop just inside the 0.10pp tolerance should pass: "
        f"{verdict.violations}"
    )
    print("  ok  §6a  0.10pp rounding tolerance still applies at the new 3% floor")

    # -- item 6: base tier reward:risk (1.5:1) -------------------------------
    verdict = check_trade(_trade(stop_price=92.0, target_price=112.0), _ctx())  # ratio 1.5 exactly
    assert verdict.allowed, f"exactly 1.5:1 should pass at the base tier: {verdict.violations}"
    print("  ok  §6b  base tier: exactly 1.5:1 passes")
    _blocked_by("§6b", trade=_trade(stop_price=92.0, target_price=105.0))  # ratio ~0.63
    _blocked_by("§6b", trade=_trade(target_price=None))
    print("  ok  §6b  base tier: a materially low ratio still fails")

    # -- item 7: escalated tier reward:risk (1.25:1) -------------------------
    # risk_checker doesn't know about blocks or escalation -- it only ever
    # sees whatever RiskLimits it's handed. Proving it honors a 1.25 floor
    # when given one is this suite's job; run_backtest's own tests prove
    # the harness actually SWITCHES to it after a slow block (see
    # test_execution_semantics.py).
    escalated = _ctx(limits=dataclasses.replace(RiskLimits(), min_reward_risk=1.25))
    verdict = check_trade(_trade(stop_price=92.0, target_price=110.0), escalated)  # ratio 1.25
    assert verdict.allowed, f"exactly 1.25:1 should pass once escalated: {verdict.violations}"
    verdict = check_trade(_trade(stop_price=92.0, target_price=108.0), escalated)  # ratio 1.0
    assert not verdict.allowed and any("§6b" in v for v in verdict.violations)
    print("  ok  §6b  escalated tier: 1.25:1 passes, below it still fails")

    # -- item 8: daily/weekly P&L no longer block --------------------------
    # These fields still exist on AccountState (harmless, unread) -- Phase 1
    # removed the daily/weekly circuit breakers entirely, not just widened
    # them.
    for bad in ({"daily_pl_pct": -50.0}, {"weekly_pl_pct": -80.0},
                {"daily_pl_pct": None}, {"weekly_pl_pct": None}):
        verdict = check_trade(_trade(), _ctx(account=bad))
        assert verdict.allowed, f"daily/weekly P&L must no longer block entries: {verdict.violations}"
    print("  ok  §4  daily/weekly loss and null values no longer block entries")

    # -- item 9: only the -30% drawdown breaker remains ---------------------
    verdict = check_trade(_trade(), _ctx(account={"drawdown_from_peak_pct": -29.0}))
    assert verdict.allowed, f"-29% drawdown should not trip the breaker: {verdict.violations}"
    _blocked_by("§4", ctx=_ctx(account={"drawdown_from_peak_pct": -30.0}))   # exactly the halt
    _blocked_by("§4", ctx=_ctx(account={"drawdown_from_peak_pct": -35.0}))
    _blocked_by("§4", ctx=_ctx(account={"drawdown_from_peak_pct": None}))    # fail closed
    print("  ok  §4  only -30% peak drawdown blocks; unknown drawdown fails closed too")

    _blocked_by("trading_blocked", ctx=_ctx(account={"trading_blocked": True}))
    print("  ok      broker-side trading_blocked is respected")

    # -- item 10: losing streaks no longer resize anything -------------------
    # A trade at the exact 20%/2% boundary above passed with NO losses; it
    # must pass IDENTICALLY after 3 (or any number of) straight losses --
    # RULES.md §4a is gone, not merely widened.
    streak = _ctx(recent_losses=[True, True, True, True, True])
    verdict = check_trade(boundary_trade, streak)
    assert verdict.allowed, (
        f"losing streaks must no longer tighten any cap: {verdict.violations}"
    )
    print("  ok  §4a (removed) a losing streak no longer resizes or blocks anything")

    # -- item 11: any open-market minute permits entry -----------------------
    verdict = check_trade(_trade(), _ctx(clock={"minutes_since_open": 0}))
    assert verdict.allowed, f"the old opening buffer must be gone: {verdict.violations}"
    verdict = check_trade(_trade(), _ctx(clock={"minutes_until_close": 0}))
    assert verdict.allowed, f"the old closing buffer must be gone: {verdict.violations}"
    verdict = check_trade(_trade(), _ctx(clock={"minutes_since_open": None, "minutes_until_close": None}))
    assert verdict.allowed, f"unknown minute values no longer matter at all: {verdict.violations}"
    print("  ok  §7  any minute the market is open permits an entry")

    _blocked_by("§7", ctx=_ctx(clock={"is_open": False}))
    print("  ok  §7  a closed market still blocks entries")

    # -- EXITS ARE PRIVILEGED (the most important behaviour here) ----------
    held = [Position(ticker="AAPL", quantity=150, market_value=15_000.0)]
    exit_trade = _trade(action="SELL", stop_price=None, target_price=None)

    for label, ctx in (
        ("tripped drawdown", _ctx(positions=held, account={"drawdown_from_peak_pct": -80.0})),
        ("unknown drawdown", _ctx(positions=held, account={"drawdown_from_peak_pct": None})),
        ("closing window", _ctx(positions=held, clock={"minutes_until_close": 0})),
        ("opening window", _ctx(positions=held, clock={"minutes_since_open": 0})),
        ("no cash at all", _ctx(positions=held, account={"cash": 0.0})),
    ):
        verdict = check_trade(exit_trade, ctx)
        assert verdict.allowed, f"exit wrongly blocked during {label}: {verdict.violations}"
    print("  ok      EXITS still allowed under the breaker and every timing scenario")

    verdict = check_trade(exit_trade, _ctx(positions=held, clock={"is_open": False}))
    assert not verdict.allowed and any("§7" in v for v in verdict.violations)
    verdict = check_trade(
        _trade(action="SELL", quantity=500, stop_price=None, target_price=None),
        _ctx(positions=held),
    )
    assert not verdict.allowed and any("§1" in v for v in verdict.violations)
    print("  ok      exits blocked only by a closed market or overselling")

    # -- misc sanity ------------------------------------------------------
    assert not check_trade(_trade(quantity=0), _ctx()).allowed
    assert not check_trade(_trade(action="HOLD"), _ctx()).allowed
    print("  ok      zero quantity and unknown actions rejected")

    verdict = check_trade(
        _trade(ticker="NVDA", quantity=2000, stop_price=99.5, target_price=100.5),
        _ctx(account={"drawdown_from_peak_pct": -50.0}),
    )
    assert len(verdict.violations) >= 3, verdict.violations
    print(f"  ok      multiple violations all reported ({len(verdict.violations)} at once)")

    # -- one-lot model, fail-closed on bad numbers, timing separation -------
    held150 = [Position(ticker="AAPL", quantity=150, market_value=15_000.0)]

    verdict = check_trade(
        _trade(action="SELL", quantity=30, stop_price=None, target_price=None),
        _ctx(positions=held150),
    )
    assert not verdict.allowed and any("§9.1" in v for v in verdict.violations)
    verdict = check_trade(
        _trade(action="SELL", quantity=150, stop_price=None, target_price=None),
        _ctx(positions=held150),
    )
    assert verdict.allowed, f"a full exit must still be allowed: {verdict.violations}"
    print("  ok      §9.1 partial discretionary SELL rejected; full exit still allowed")

    nan, inf = float("nan"), float("inf")
    for bad_equity in (0.0, -100.0, nan, inf):
        v = check_trade(_trade(), _ctx(account={"equity": bad_equity}))
        assert not v.allowed, f"equity={bad_equity} should fail closed"
    for bad_cash in (nan, inf, -inf):
        v = check_trade(_trade(), _ctx(account={"cash": bad_cash}))
        assert not v.allowed, f"cash={bad_cash} should fail closed"
    for bad_price in (0.0, -50.0, nan, inf):
        v = check_trade(_trade(entry_price=bad_price), _ctx())
        assert not v.allowed, f"entry_price={bad_price} should fail closed"
    for bad_stop in (0.0, -1.0, nan, inf):
        v = check_trade(_trade(stop_price=bad_stop), _ctx())
        assert not v.allowed, f"stop_price={bad_stop} should fail closed"
    for bad_target in (0.0, -1.0, nan, inf):
        v = check_trade(_trade(target_price=bad_target), _ctx())
        assert not v.allowed, f"target_price={bad_target} should fail closed"
    v = check_trade(_trade(quantity=nan), _ctx())
    assert not v.allowed, "NaN quantity should fail closed"
    print("  ok      non-finite/non-positive equity, cash, prices, and quantity all fail closed")

    # validate_timing=False (decision-time proposal validation): skips §7
    # entirely -- a trade that would be blocked ONLY by §7 (closed market)
    # is approved; every OTHER rule still applies in full, including exits.
    v = check_trade(_trade(), _ctx(clock={"is_open": False}), validate_timing=False)
    assert v.allowed, f"validate_timing=False must skip §7 entirely: {v.violations}"
    v = check_trade(
        _trade(action="SELL", stop_price=None, target_price=None),
        _ctx(positions=held150, clock={"is_open": False}), validate_timing=False,
    )
    assert v.allowed, f"validate_timing=False must skip §7 on exits too: {v.violations}"
    v = check_trade(
        _trade(ticker="TSLA"), _ctx(clock={"is_open": False}), validate_timing=False,
    )
    assert not v.allowed and any("§1" in x for x in v.violations)
    print("  ok      validate_timing=False skips only §7, every other rule still enforced")

    print("PASS: risk checker enforces the Phase 1 aggressive profile and keeps exits open")


if __name__ == "__main__":
    main()
