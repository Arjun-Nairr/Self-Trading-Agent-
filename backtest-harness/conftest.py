"""TEST ONLY: no test may pick up the user's real ~/.hermes-trading-agent-backtest/tracing.env and export to the
hosted Langfuse project. Every test sees an empty tracing dir (tracing off) unless it passes its own trace_dir."""
import pytest


@pytest.fixture(autouse=True)
def _no_home_backtest_tracing(tmp_path_factory, monkeypatch):
    import backtest_tracing
    monkeypatch.setattr(backtest_tracing, "DEFAULT_DIR", tmp_path_factory.mktemp("no-backtest-tracing"))
