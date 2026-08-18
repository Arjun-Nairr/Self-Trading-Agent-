"""Standalone eval for the risk checker — every rule in RULES.md that can be
mechanically enforced, fed hand-built scenarios with no harness involved.

Run: python test_risk_checker.py

The baseline scenario below is a deliberately clean, legal trade. Each test
changes exactly one thing and asserts the checker catches exactly that. If a
test fails, the scenario it describes is a trade the agent could really have
placed.
"""
from risk_checker import (
    AccountState, Context, MarketClock, Position, ProposedTrade, check_trade,
)

# $100k equity, nothing held, mid-session, AAPL on the watchlist.
# BUY 90 @ $100 = $9,000 = 9% of equity        (cap 10%)
# stop $93 -> 7% below entry                    (bounds 5-10%)
# risk 90 x $7 = $630 = 0.63% of equity         (budget 1%)
# target $115 -> reward $15 / risk $7 = 2.14    (minimum 2.0)
BASE_TRADE = dict(
    ticker="AAPL", action="BUY", quantity=90,
    entry_price=100.0, stop_price=93.0, target_price=115.0,
)


def _ctx(**overrides) -> Context:
    account_kw = overrides.pop("account", {})
    clock_kw = overrides.pop("clock", {})
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
        account=account, clock=clock, watchlist=["AAPL", "MSFT"], **overrides
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

    # -- §9 prohibitions --------------------------------------------------
    _blocked_by("§9.1", ctx=_ctx(
        positions=[Position(ticker="AAPL", quantity=10, market_value=1_000.0)]
    ))
    _blocked_by("§9.4", ctx=_ctx(stopped_out_today=["AAPL"]))
    print("  ok  §9  averaging down and same-day re-entry blocked")

    # -- §4 circuit breakers ----------------------------------------------
    _blocked_by("§4", ctx=_ctx(account={"daily_pl_pct": -3.0}))
    _blocked_by("§4", ctx=_ctx(account={"weekly_pl_pct": -6.5}))
    _blocked_by("§4", ctx=_ctx(account={"drawdown_from_peak_pct": -10.1}))
    print("  ok  §4  daily / weekly / drawdown breakers all block entries")

    # THE FAIL-CLOSED CASE: an unknown safety figure must block, never pass.
    # get_account returns null when a figure can't be computed honestly; if
    # that read as "clear", the breaker would silently not exist.
    _blocked_by("§4", ctx=_ctx(account={"weekly_pl_pct": None}))
    _blocked_by("§4", ctx=_ctx(account={"daily_pl_pct": None}))
    _blocked_by("§2", ctx=_ctx(account={"equity": None}))
    print("  ok  §4  unknown P&L / equity fails CLOSED, not open")

    _blocked_by("trading_blocked", ctx=_ctx(account={"trading_blocked": True}))
    print("  ok      broker-side trading_blocked is respected")

    # -- §2b concentration cap --------------------------------------------
    # 110 x $100 = $11,000 = 11% of equity, over the 10% cap.
    _blocked_by("§2b", trade=_trade(quantity=110))
    print("  ok  §2b concentration cap enforced")

    # -- §2a risk budget --------------------------------------------------
    # NOTE: §2a cannot be breached in isolation. risk% = position% x stop%,
    # so with position <=10% and stop <=10%, risk can never exceed 1% — the
    # constraints meet exactly at the corner. Breaching the budget therefore
    # always co-occurs with a §2b or §6a breach, and this asserts the budget
    # violation is reported too rather than being masked by the other.
    verdict = check_trade(_trade(quantity=200, stop_price=88.0), _ctx())
    assert not verdict.allowed
    assert any("§2a" in v for v in verdict.violations), verdict.violations
    print("  ok  §2a risk budget reported (co-fires by construction — see note)")

    # -- §6a stop-loss ----------------------------------------------------
    _blocked_by("§6a", trade=_trade(stop_price=None))         # missing
    _blocked_by("§6a", trade=_trade(stop_price=97.0))         # 3% — too close
    _blocked_by("§6a", trade=_trade(stop_price=85.0))         # 15% — too far
    _blocked_by("§6a", trade=_trade(stop_price=105.0))        # above entry
    print("  ok  §6a stop missing / too close / too far / inverted all blocked")

    # -- §6b reward:risk --------------------------------------------------
    # target $105 -> reward $5 / risk $7 = 0.71, well under 2.0
    _blocked_by("§6b", trade=_trade(target_price=105.0))
    _blocked_by("§6b", trade=_trade(target_price=None))
    print("  ok  §6b reward:risk minimum enforced")

    # -- §3 exposure / cash / count ---------------------------------------
    _blocked_by("§3", ctx=_ctx(positions=[
        Position(ticker="MSFT", quantity=1, market_value=55_000.0)
    ]))  # 55% held + 9% new = 64% > 60%
    _blocked_by("§3", ctx=_ctx(account={"cash": 9_200.0}))
    # 8 positions already open (none of them AAPL, so §9.1 doesn't mask it)
    _blocked_by("§3", ctx=_ctx(positions=[
        Position(ticker=f"T{i}", quantity=1, market_value=100.0) for i in range(8)
    ]))
    print("  ok  §3  exposure cap / cash floor / max positions enforced")

    # -- §7 market hours --------------------------------------------------
    _blocked_by("§7", ctx=_ctx(clock={"is_open": False}))
    _blocked_by("§7", ctx=_ctx(clock={"minutes_since_open": 5}))
    _blocked_by("§7", ctx=_ctx(clock={"minutes_until_close": 10}))
    print("  ok  §7  closed market / opening / closing windows block entries")

    # -- §4a losing-streak de-risking -------------------------------------
    # The baseline 9% position is legal normally; after 3 straight losses
    # the cap tightens to 7.5%, so the identical trade must now fail.
    streak = _ctx(recent_losses=[True, True, True])
    verdict = check_trade(_trade(), streak)
    assert not verdict.allowed, "losing streak did not tighten the caps"
    assert any("§4a" in v or "streak" in v for v in verdict.violations), verdict.violations
    # A 3rd loss further back must NOT trigger it — the streak is the most
    # recent three, not any three.
    assert check_trade(_trade(), _ctx(recent_losses=[False, True, True, True])).allowed
    # And a smaller position still fits under the tightened cap.
    assert check_trade(_trade(quantity=70), streak).allowed
    print("  ok  §4a losing streak tightens caps, and only on a real streak")

    # -- EXITS ARE PRIVILEGED (the most important behaviour here) ----------
    # A checker that blocked exits would trap losing positions open during
    # exactly the conditions these limits exist to protect against.
    held = [Position(ticker="AAPL", quantity=90, market_value=9_000.0)]
    exit_trade = _trade(action="SELL", stop_price=None, target_price=None)

    for label, ctx in (
        ("tripped daily breaker", _ctx(positions=held, account={"daily_pl_pct": -20.0})),
        ("tripped drawdown", _ctx(positions=held, account={"drawdown_from_peak_pct": -50.0})),
        ("unknown P&L", _ctx(positions=held, account={"weekly_pl_pct": None})),
        ("closing window", _ctx(positions=held, clock={"minutes_until_close": 2})),
        ("opening window", _ctx(positions=held, clock={"minutes_since_open": 1})),
        ("full exposure", _ctx(positions=held, account={"cash": 0.0})),
    ):
        verdict = check_trade(exit_trade, ctx)
        assert verdict.allowed, f"exit wrongly blocked during {label}: {verdict.violations}"
    print("  ok      EXITS still allowed under every breaker and timing window")

    # But an exit genuinely cannot happen with the market shut...
    verdict = check_trade(exit_trade, _ctx(positions=held, clock={"is_open": False}))
    assert not verdict.allowed and any("§7" in v for v in verdict.violations)
    # ...and selling more than is held is a disguised short, not an exit.
    verdict = check_trade(
        _trade(action="SELL", quantity=200, stop_price=None, target_price=None),
        _ctx(positions=held),
    )
    assert not verdict.allowed and any("§1" in v for v in verdict.violations)
    print("  ok      exits blocked only by a closed market or overselling")

    # -- misc sanity ------------------------------------------------------
    assert not check_trade(_trade(quantity=0), _ctx()).allowed
    assert not check_trade(_trade(action="HOLD"), _ctx()).allowed
    print("  ok      zero quantity and unknown actions rejected")

    # A violating trade reports every rule it breaks, not just the first —
    # so one rejection doesn't hide three other problems.
    verdict = check_trade(
        _trade(ticker="NVDA", quantity=500, stop_price=99.5, target_price=100.5),
        _ctx(account={"daily_pl_pct": -5.0}),
    )
    assert len(verdict.violations) >= 4, verdict.violations
    print(f"  ok      multiple violations all reported ({len(verdict.violations)} at once)")

    print("PASS: risk checker enforces RULES.md and keeps exits open")


if __name__ == "__main__":
    main()
