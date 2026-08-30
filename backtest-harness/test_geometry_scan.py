"""Plain-python tests for geometry_scan.py's pure classification logic.
No network, no Neon, no Hermes, no Docker -- synthetic support/resistance
lists only. Run: python test_geometry_scan.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_scan import classify, nearest_resistance, valid_stop  # noqa: E402

checks = 0


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    assert got == want, f"{label}: got {got!r}, want {want!r}"


# -- nearest_resistance --------------------------------------------------
check("nearest_resistance picks the closest level above close",
      nearest_resistance([105.0, 110.0, 120.0], 100.0), 105.0)
check("nearest_resistance ignores levels at/below close",
      nearest_resistance([95.0, 100.0], 100.0), None)
check("nearest_resistance empty list", nearest_resistance([], 100.0), None)

# -- valid_stop ------------------------------------------------------------
# close=100: 5% floor = 95.00, 10% ceiling = 90.00 (with 0.10pt tolerance
# each side, so 95.10 down to 89.90 all count).
check("valid_stop picks the shallowest level inside the 5-10% band",
      valid_stop([90.0, 93.0, 80.0], 100.0), 93.0)
check("valid_stop rejects a level above close (not a stop at all)",
      valid_stop([101.0], 100.0), None)
check("valid_stop rejects too-shallow (2% below, ordinary noise)",
      valid_stop([98.0], 100.0), None)
check("valid_stop rejects too-deep (20% below)",
      valid_stop([80.0], 100.0), None)
check("valid_stop accepts exactly the 5.00% floor",
      valid_stop([95.0], 100.0), 95.0)
check("valid_stop accepts exactly the 10.00% ceiling",
      valid_stop([90.0], 100.0), 90.0)
check("valid_stop honors the 0.10pt tolerance just past the floor",
      valid_stop([94.95], 100.0), 94.95)  # 5.05% below -> within tolerance
check("valid_stop rejects just past the floor beyond tolerance",
      valid_stop([95.2], 100.0), None)  # 4.80% below -> outside tolerance
check("valid_stop no candidates in band", valid_stop([50.0], 100.0), None)

# -- classify ----------------------------------------------------------
# stop=95 (5% risk=5), only resistance=105 (5% reward=5) -> ratio 1.0, no
# candidate clears 2:1 -> INVALID, reported against the (only, so also
# farthest) candidate.
klass, ratio, target, stop = classify([95.0], [105.0], 100.0)
check("classify 1:1 is INVALID", klass, "INVALID")
check("classify 1:1 ratio value", ratio, 1.0)

# stop=95 (risk=5), only resistance=107.5 (reward=7.5) -> ratio 1.5 -> CLOSE
klass, ratio, target, stop = classify([95.0], [107.5], 100.0)
check("classify 1.5:1 is CLOSE", klass, "CLOSE")

# stop=95 (risk=5), resistance=110 (reward=10) -> ratio 2.0 -> GEOMETRY-VALID
klass, ratio, target, stop = classify([95.0], [110.0], 100.0)
check("classify 2:1 is GEOMETRY-VALID", klass, "GEOMETRY-VALID")
check("classify picks the resistance used", target, 110.0)
check("classify picks the stop used", stop, 95.0)

# Multiple resistance candidates: the NEAREST one that clears 2:1 is used,
# not the single farthest available (105 clears at 2:1 too -- 120 should
# never be picked when 110 already works).
klass, ratio, target, stop = classify([95.0], [110.0, 120.0, 200.0], 100.0)
check("classify picks the nearest candidate that clears 2:1, not the farthest",
      target, 110.0)

# Nearest candidate (105, ratio 1.0) fails, but a farther one (110, ratio
# 2.0) clears it -- must still find and use 110, not stop at the first
# (failing) candidate.
klass, ratio, target, stop = classify([95.0], [105.0, 110.0], 100.0)
check("classify skips a too-close candidate to find one that clears 2:1",
      klass, "GEOMETRY-VALID")
check("classify uses the qualifying candidate, not the nearest failing one",
      target, 110.0)

# Ground-truth regression: run 1's real SFM row (2026-03-17), reproduced
# from maverick's real support_resistance output for that exact day --
# stop 77.02, target 89.18, ratio 2.00, matching the real journaled trade.
klass, ratio, target, stop = classify(
    [64.75, 72.96, 77.02], [84.08, 85.12, 89.18], 81.07)
check("SFM ground truth: classify is GEOMETRY-VALID", klass, "GEOMETRY-VALID")
check("SFM ground truth: stop matches the real journaled stop", stop, 77.02)
check("SFM ground truth: target matches the real journaled target", target, 89.18)
check("SFM ground truth: ratio matches the real journaled 2.00 (to 2dp)",
      round(ratio, 2), 2.00)

# no valid stop at all -> INVALID regardless of resistance
klass, ratio, target, stop = classify([50.0], [110.0], 100.0)
check("classify with no valid stop is INVALID", klass, "INVALID")
check("classify with no valid stop has no ratio", ratio, None)

# no resistance above close -> INVALID
klass, ratio, target, stop = classify([95.0], [99.0], 100.0)
check("classify with no resistance above close is INVALID", klass, "INVALID")

print(f"OK — {checks} checks passed")
