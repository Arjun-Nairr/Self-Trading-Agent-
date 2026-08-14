"""Smallest proof the fill logic does what it claims. Run:
    docker exec hermes-trading-agent-execution-sim python test_fill_logic.py
"""
from server import _simulate_fill


def main():
    # MARKET BUY slips price UP (you pay slightly more than reference)
    status, fill_price, reason = _simulate_fill("BUY", "MARKET", 100.0, None)
    assert status == "filled" and fill_price > 100.0, (status, fill_price)

    # MARKET SELL slips price DOWN (you receive slightly less than reference)
    status, fill_price, reason = _simulate_fill("SELL", "MARKET", 100.0, None)
    assert status == "filled" and fill_price < 100.0, (status, fill_price)

    # LIMIT BUY crosses when reference <= limit
    status, fill_price, reason = _simulate_fill("BUY", "LIMIT", 95.0, 100.0)
    assert status == "filled" and fill_price == 100.0, (status, fill_price)

    # LIMIT BUY does NOT cross when reference > limit
    status, fill_price, reason = _simulate_fill("BUY", "LIMIT", 105.0, 100.0)
    assert status == "rejected" and fill_price is None, (status, fill_price)

    # LIMIT SELL crosses when reference >= limit
    status, fill_price, reason = _simulate_fill("SELL", "LIMIT", 105.0, 100.0)
    assert status == "filled" and fill_price == 100.0, (status, fill_price)

    # LIMIT SELL does NOT cross when reference < limit
    status, fill_price, reason = _simulate_fill("SELL", "LIMIT", 95.0, 100.0)
    assert status == "rejected", (status, fill_price)

    # LIMIT without a limit_price is rejected, not silently treated as MARKET
    status, fill_price, reason = _simulate_fill("BUY", "LIMIT", 100.0, None)
    assert status == "rejected" and "limit_price" in reason, (status, reason)

    print("PASS: all fill-logic branches behave as claimed")


if __name__ == "__main__":
    main()
