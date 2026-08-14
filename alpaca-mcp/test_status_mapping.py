"""Smallest proof the Alpaca-status -> journal-status mapping is correct.
Run: docker exec hermes-trading-agent-alpaca-mcp python test_status_mapping.py
"""
from server import _to_journal_status, _parse_fill


def main():
    # Regression: filled_qty comes back as the string "0" even on an
    # unfilled order (confirmed against a real paper order placed while
    # the market was closed) — must report None, not a fake 0-share fill.
    unfilled_order = {"filled_avg_price": None, "filled_qty": "0"}
    assert _parse_fill(unfilled_order) == (None, None), _parse_fill(unfilled_order)

    filled_order = {"filled_avg_price": "150.25", "filled_qty": "3"}
    assert _parse_fill(filled_order) == (150.25, 3), _parse_fill(filled_order)

    assert _to_journal_status("filled") == "filled"
    assert _to_journal_status("partially_filled") == "filled"
    assert _to_journal_status("rejected") == "rejected"
    assert _to_journal_status("canceled") == "rejected"
    assert _to_journal_status("expired") == "rejected"
    # Anything mid-flight (new, accepted, pending_new, etc.) must map to
    # 'pending', not be mistaken for a final state — this is exactly what
    # the §6b restart-reconciliation flow depends on being honest.
    for mid_flight in ("new", "accepted", "pending_new", "accepted_for_bidding"):
        assert _to_journal_status(mid_flight) == "pending", mid_flight

    print("PASS: status mapping is correct for all known Alpaca order states")


if __name__ == "__main__":
    main()
