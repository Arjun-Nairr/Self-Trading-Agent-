#!/usr/bin/env python3
"""One documented, non-mutating entry point for "did the tests pass?" --
correction pass item F (prior audit M6): a repo-wide `pytest` sweep from
the repo root silently covers NONE of the risk checker, fill simulator,
journal field-validation, or SEC layer, because those suites use
`def main()` + `if __name__ == "__main__":` (not `def test_*`), so pytest
collects zero items from them -- someone running "pytest" and reading
"165 passed" has verified none of the safety layer.

This script does not rewrite those suites into pytest-collectable tests
(a much larger, riskier diff for a discoverability problem) -- it just
runs the exact same real, already-passing suites the same way a careful
manual check already would, in one place, so the answer to "did the
safety layer pass" stops depending on which command someone remembers to
type.

Run from the repo root:

    python verify.py

Every suite here is genuinely non-mutating: no Docker container is
started, no network call is made, no Neon row is read or written. Suites
that need a live container, network credentials, or an uninstalled
dependency (trade-journal-mcp's DB-backed pair, alpaca-mcp's
test_historical_news, google-news-rss-mcp's test_news_fetch, and
maverick-mcp's own vendored suite) are deliberately NOT run here -- see
SKIPPED below and hermes_audit_15e2000.md's own "Tests/checks NOT run"
table for why each one is out of bounds for a read-only check.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

# (label, cwd relative to REPO, argv, extra env)
SUITES: list[tuple[str, str, list[str], dict[str, str]]] = [
    ("backtest-harness (pytest)", "backtest-harness",
     [sys.executable, "-m", "pytest", "-q"], {}),
    ("live paper controller (pytest)", "live",
     [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], {}),
    ("risk-checker", "risk-checker",
     [sys.executable, "test_risk_checker.py"], {}),
    ("alpaca-mcp: account_math", "alpaca-mcp",
     [sys.executable, "test_account_math.py"], {"PYTHONPATH": str(REPO / "risk-checker")}),
    ("alpaca-mcp: status_mapping", "alpaca-mcp",
     [sys.executable, "test_status_mapping.py"], {"PYTHONPATH": str(REPO / "risk-checker")}),
    ("alpaca-mcp: historical_news_pagination", "alpaca-mcp",
     [sys.executable, "test_historical_news_pagination.py"], {"PYTHONPATH": str(REPO / "risk-checker")}),
    ("execution-sim-mcp: fill_logic", "execution-sim-mcp",
     [sys.executable, "test_fill_logic.py"], {}),
    ("trade-journal-mcp: decision_fields", "trade-journal-mcp",
     [sys.executable, "test_decision_fields.py"], {}),
    ("trade-journal-mcp: mode_isolation", "trade-journal-mcp",
     [sys.executable, "test_mode_isolation.py"], {}),
    ("trade-journal-mcp: run_kind_validation", "trade-journal-mcp",
     [sys.executable, "test_run_kind_validation.py"], {}),
    ("fundamentals: edgar", "fundamentals",
     [sys.executable, "test_edgar.py"], {}),
    ("fundamentals: sec_client", "fundamentals",
     [sys.executable, "test_sec_client.py"], {}),
    ("dashboard-v2 (node fixtures)", "dashboard-v2",
     ["node", "test/calc.test.mjs"], {}),
]

SKIPPED = [
    "trade-journal-mcp: test_idempotency / test_run_isolation / "
    "test_dashboard_fields / test_run_registry / test_run_status / "
    "test_run_configuration -- INSERT real rows into Neon",
    "alpaca-mcp: test_historical_news -- requires a live ALPACA_API_KEY + network",
    "google-news-rss-mcp: test_news_fetch -- requires feedparser, not installed by default",
    "maverick-mcp's own suite -- vendored third-party code, not this project's source",
]


def main() -> int:
    import os

    results: list[tuple[str, bool, str]] = []
    for label, rel_cwd, argv, extra_env in SUITES:
        env = {**os.environ, **extra_env}
        proc = subprocess.run(argv, cwd=REPO / rel_cwd, env=env,
                              capture_output=True, text=True, timeout=180)
        ok = proc.returncode == 0
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        results.append((label, ok, tail[-1] if tail else ""))
        print(f"{'PASS' if ok else 'FAIL'}  {label:<32} {tail[-1] if tail else ''}")
        if not ok:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])

    print()
    print(f"{sum(ok for _, ok, _ in results)}/{len(results)} suites passed")
    print()
    print("Deliberately NOT run here (real container/network/credential dependency):")
    for line in SKIPPED:
        print(f"  - {line}")

    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
