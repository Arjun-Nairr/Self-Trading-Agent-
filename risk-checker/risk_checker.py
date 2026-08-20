"""
risk_checker.py — deterministic enforcement of RULES.md, independent of
Hermes' reasoning.

WHY THIS EXISTS: Hermes reads RULES.md every cycle and is instructed to
follow it. That is asking, not enforcing. If its reasoning slips on one
cycle — a misread number, an arithmetic error while sizing, or simply
talking itself into an exception — nothing else in the stack stops the bad
order. This module is the difference between "we told it the rules" and
"breaking them is structurally impossible."

DESIGN: pure functions, no I/O, no network, no clock of its own. Everything
it judges arrives as explicit arguments. That makes it exhaustively testable
against hand-built scenarios, which is the point: a safety layer nobody can
fully test is not a safety layer.

IT VERIFIES, IT DOES NOT SIZE. Hermes decides quantities; this rejects them
when they breach a limit. Handing Hermes the correct size would make the
sizing judgment the checker's rather than the agent's, which cuts against
the whole point of the project (evaluating Hermes' own judgment — see
RULES.md §10's resolved disagreement).

FAIL CLOSED. Anything unknown (equity missing, a circuit-breaker figure that
could not be computed) blocks the trade. An unevaluated safety check has not
passed. This matches get_account's null-not-zero contract and quota_pacer's
corrupt-state behaviour.

EXITS ARE PRIVILEGED. Circuit breakers, timing windows, reward:risk and
exposure caps all restrict *entries only*. A checker that blocked exits
would trap losing positions open during exactly the conditions those limits
exist to protect against — strictly worse than no checker. See
`_check_exit`.

# ponytail: the numbers below mirror RULES.md rather than parsing it.
# Parsing prose markdown for figures would be fragile in a way a safety
# layer cannot afford. The cost is a real (small) drift risk: change a
# number in RULES.md and you MUST change it here. Kept in one block, with
# section references, so the sync point is obvious. RULES.md §11 points back
# here for the same reason.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskLimits:
    """Mirrors RULES.md. The only place numbers live in this module."""
    risk_budget_pct: float = 1.0          # §2a  max % of equity risked per trade
    max_position_pct: float = 10.0        # §2b  max % of equity in one name
    max_exposure_pct: float = 60.0        # §3   max % of equity invested total
    min_cash_pct: float = 5.0             # §3   trade must leave this much cash
    max_open_positions: int = 8           # §3
    daily_halt_pct: float = -3.0          # §4
    weekly_halt_pct: float = -6.0         # §4
    drawdown_halt_pct: float = -10.0      # §4
    streak_losses: int = 3                # §4a
    streak_size_multiplier: float = 0.75  # §4a  cut sizes 25% after a streak
    min_stop_pct: float = 5.0             # §6a  stop no closer than this
    max_stop_pct: float = 10.0            # §6a  stop no further than this
    min_reward_risk: float = 2.0          # §6b
    # Tolerance for §6a/§6b's boundary checks, NOT a loosened rule -- a
    # numerical precision buffer, applied uniformly and symmetrically.
    # Found via a real smoke-test rejection: a stop landed at 4.99% against
    # the 5.00% floor. That is not float noise in this module's math (the
    # comparison itself is exact) -- it is Hermes proposing a real stop
    # price rounded to the cent, which lands a hair under the floor at
    # cheap end of the watchlist. §8's price floor is $10; one cent of
    # rounding there is 0.01/10*100 = 0.10 percentage points -- the exact
    # worst case. stop_tolerance_pct absorbs that; it does not change what
    # RULES.md requires, only what counts as "close enough" to it.
    # reward_risk_tolerance mirrors the same reasoning in ratio terms.
    stop_tolerance_pct: float = 0.10
    reward_risk_tolerance: float = 0.02
    open_buffer_min: int = 15             # §7   no entries just after open
    close_buffer_min: int = 30            # §7   no entries just before close


@dataclass
class AccountState:
    """Straight from alpaca-mcp's get_account. `None` means "could not be
    determined" and is treated as blocking, never as clear."""
    equity: float | None
    cash: float | None
    daily_pl_pct: float | None
    weekly_pl_pct: float | None
    drawdown_from_peak_pct: float | None
    trading_blocked: bool = False


@dataclass
class Position:
    ticker: str
    quantity: float
    market_value: float


@dataclass
class MarketClock:
    is_open: bool
    minutes_since_open: int | None = None
    minutes_until_close: int | None = None


@dataclass
class ProposedTrade:
    ticker: str
    action: str                      # "BUY" or "SELL"
    quantity: int
    entry_price: float               # price the decision was sized against
    stop_price: float | None = None  # required for entries
    target_price: float | None = None


@dataclass
class Context:
    account: AccountState
    clock: MarketClock
    positions: list[Position] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    # Most recent CLOSED trades, newest first, True = loss. Drives §4a.
    recent_losses: list[bool] = field(default_factory=list)
    # Tickers stopped out earlier today — §9.4 blocks same-day re-entry.
    stopped_out_today: list[str] = field(default_factory=list)
    limits: RiskLimits = field(default_factory=RiskLimits)


@dataclass
class Verdict:
    allowed: bool
    violations: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


def _held(ctx: Context, ticker: str) -> Position | None:
    return next((p for p in ctx.positions if p.ticker.upper() == ticker.upper()), None)


def _check_exit(trade: ProposedTrade, ctx: Context, v: list[str]) -> None:
    """Exits are deliberately almost unrestricted — see module docstring.

    Only two things can block an exit, and neither is a risk limit: a closed
    market (the order physically cannot fill at the assumed price), and
    trying to sell more than is actually held (which would open a short,
    banned outright by §1).
    """
    if not ctx.clock.is_open:
        v.append("§7: market is closed")

    position = _held(ctx, trade.ticker)
    if position is not None and trade.quantity > position.quantity:
        v.append(
            f"§1: selling {trade.quantity} exceeds the {position.quantity} held "
            "— would open a short position"
        )


def _check_entry(trade: ProposedTrade, ctx: Context, v: list[str]) -> None:
    lim, acct = ctx.limits, ctx.account

    # -- §1 scope ---------------------------------------------------------
    if ctx.watchlist and trade.ticker.upper() not in {t.upper() for t in ctx.watchlist}:
        v.append(f"§1/§9.3: {trade.ticker} is not on the watchlist")

    # -- §9 prohibitions --------------------------------------------------
    if _held(ctx, trade.ticker) is not None:
        v.append(
            f"§9.1: already holding {trade.ticker} — adding to an open position "
            "is averaging down"
        )
    if trade.ticker.upper() in {t.upper() for t in ctx.stopped_out_today}:
        v.append(f"§9.4: {trade.ticker} stopped out earlier today — no same-day re-entry")

    # -- §4 circuit breakers (fail closed on unknowns) --------------------
    if acct.trading_blocked:
        v.append("broker reports trading_blocked")
    for label, value, limit in (
        ("daily", acct.daily_pl_pct, lim.daily_halt_pct),
        ("weekly", acct.weekly_pl_pct, lim.weekly_halt_pct),
        ("drawdown", acct.drawdown_from_peak_pct, lim.drawdown_halt_pct),
    ):
        if value is None:
            v.append(f"§4: {label} P&L could not be determined — treated as tripped")
        elif value <= limit:
            v.append(f"§4: {label} circuit breaker tripped ({value}% <= {limit}%)")

    if acct.equity is None or acct.cash is None:
        v.append("§2: equity/cash unknown — cannot size or verify any limit")
        return  # every remaining check divides by equity

    equity = acct.equity
    notional = trade.quantity * trade.entry_price

    # -- §4a losing-streak de-risking -------------------------------------
    # Expressed as tightened caps rather than a separate rule: after a
    # streak, the ceilings themselves drop 25%.
    streak = (
        len(ctx.recent_losses) >= lim.streak_losses
        and all(ctx.recent_losses[: lim.streak_losses])
    )
    multiplier = lim.streak_size_multiplier if streak else 1.0
    max_position_pct = lim.max_position_pct * multiplier
    risk_budget_pct = lim.risk_budget_pct * multiplier

    # -- §2b concentration cap --------------------------------------------
    position_pct = notional / equity * 100
    if position_pct > max_position_pct:
        suffix = " (reduced 25% by the §4a losing streak)" if streak else ""
        v.append(
            f"§2b: position is {position_pct:.2f}% of equity, cap is "
            f"{max_position_pct:.2f}%{suffix}"
        )

    # -- §6a stop-loss --------------------------------------------------
    if trade.stop_price is None:
        v.append("§6a: no stop price — every entry must have one set at entry time")
    else:
        stop_distance_pct = (trade.entry_price - trade.stop_price) / trade.entry_price * 100
        if stop_distance_pct <= 0:
            v.append("§6a: stop must sit below the entry price for a long")
        elif stop_distance_pct < lim.min_stop_pct - lim.stop_tolerance_pct:
            v.append(
                f"§6a: stop is {stop_distance_pct:.2f}% below entry, minimum is "
                f"{lim.min_stop_pct}% — ordinary noise would trigger it"
            )
        elif stop_distance_pct > lim.max_stop_pct + lim.stop_tolerance_pct:
            v.append(
                f"§6a: stop is {stop_distance_pct:.2f}% below entry, maximum is "
                f"{lim.max_stop_pct}% — too volatile for the sizing model"
            )

        # -- §2a risk budget ---------------------------------------------
        risk = trade.quantity * (trade.entry_price - trade.stop_price)
        risk_pct = risk / equity * 100
        if risk_pct > risk_budget_pct:
            suffix = " (reduced 25% by the §4a losing streak)" if streak else ""
            v.append(
                f"§2a: risks {risk_pct:.2f}% of equity, budget is "
                f"{risk_budget_pct:.2f}%{suffix}"
            )

        # -- §6b reward:risk ----------------------------------------------
        if trade.target_price is None:
            v.append("§6b: no target price — reward:risk cannot be verified")
        else:
            reward = trade.target_price - trade.entry_price
            risk_per_share = trade.entry_price - trade.stop_price
            if risk_per_share > 0:
                ratio = reward / risk_per_share
                if ratio < lim.min_reward_risk - lim.reward_risk_tolerance:
                    v.append(
                        f"§6b: reward:risk is {ratio:.2f}, minimum is "
                        f"{lim.min_reward_risk}"
                    )

    # -- §3 exposure, cash floor, position count --------------------------
    invested = sum(p.market_value for p in ctx.positions)
    exposure_pct = (invested + notional) / equity * 100
    if exposure_pct > lim.max_exposure_pct:
        v.append(
            f"§3: total exposure would be {exposure_pct:.2f}%, cap is "
            f"{lim.max_exposure_pct}%"
        )

    remaining_cash_pct = (acct.cash - notional) / equity * 100
    if remaining_cash_pct < lim.min_cash_pct:
        v.append(
            f"§3: would leave {remaining_cash_pct:.2f}% cash, minimum is "
            f"{lim.min_cash_pct}%"
        )

    if len(ctx.positions) >= lim.max_open_positions:
        v.append(
            f"§3: already holding {len(ctx.positions)} positions, max is "
            f"{lim.max_open_positions}"
        )

    # -- §7 market hours ---------------------------------------------------
    if not ctx.clock.is_open:
        v.append("§7: market is closed — a queued order fills at an unknown price")
    else:
        since = ctx.clock.minutes_since_open
        until = ctx.clock.minutes_until_close
        if since is not None and since < lim.open_buffer_min:
            v.append(
                f"§7: {since} min after open, no entries for the first "
                f"{lim.open_buffer_min}"
            )
        if until is not None and until < lim.close_buffer_min:
            v.append(
                f"§7: {until} min before close, no entries in the final "
                f"{lim.close_buffer_min}"
            )


def check_trade(trade: ProposedTrade, ctx: Context) -> Verdict:
    """Approve or reject a proposed trade against RULES.md.

    An exit is a SELL of something currently held; anything else is an entry.
    Deriving it rather than taking a flag removes a way for the caller to get
    it wrong — and makes a SELL of an unheld ticker fall through to the entry
    path, where §1's long-only rule correctly rejects it as a short.
    """
    violations: list[str] = []

    position = _held(ctx, trade.ticker)
    is_exit = trade.action.upper() == "SELL" and position is not None

    if trade.quantity <= 0:
        violations.append("quantity must be positive")
    if trade.action.upper() not in ("BUY", "SELL"):
        violations.append(f"§1: unsupported action {trade.action!r}")
        return Verdict(allowed=False, violations=violations)

    if is_exit:
        _check_exit(trade, ctx, violations)
    else:
        if trade.action.upper() == "SELL":
            violations.append(
                f"§1: SELL of {trade.ticker}, which is not held — short selling "
                "is prohibited"
            )
        _check_entry(trade, ctx, violations)

    return Verdict(allowed=not violations, violations=violations)
