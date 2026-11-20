"""Fixtures shared by the tracing test files (test_tracing*.py)."""
import pytest

from test_tracing import PK, SK, Collector


@pytest.fixture(autouse=True)
def _no_real_hermes_home(monkeypatch):
    """The plugin spools under $HERMES_HOME; a developer machine may have a real one. Tests never touch it."""
    monkeypatch.delenv("HERMES_HOME", raising=False)


@pytest.fixture
def collector():
    """A local OTLP/JSON capture server (offline transport evidence only)."""
    c = Collector()
    yield c
    c.stop()


@pytest.fixture
def plugin_env(monkeypatch, collector):
    monkeypatch.setenv("HERMES_TRACE_PUBLIC_KEY", PK)
    monkeypatch.setenv("HERMES_TRACE_SECRET_KEY", SK)
    monkeypatch.setenv("HERMES_TRACE_BASE_URL", collector.url)
    return collector
