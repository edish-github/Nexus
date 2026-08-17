"""Agent fixtures.

The loader lives in `scripts/_agents.py` because the local pipeline runner needs
exactly the same thing: every agent ships as its own Lambda code directory with
the same entrypoint file name, so a plain `import app` collides the moment a
second one is loaded.
"""
from __future__ import annotations

from types import ModuleType

import pytest

from _agents import load_agent


@pytest.fixture(scope="session")
def oracle() -> ModuleType:
    return load_agent("oracle")


@pytest.fixture(scope="session")
def sentinel() -> ModuleType:
    return load_agent("sentinel")


@pytest.fixture(scope="session")
def guardian() -> ModuleType:
    return load_agent("guardian")


@pytest.fixture(scope="session")
def diagnostician() -> ModuleType:
    return load_agent("diagnostician")


@pytest.fixture(scope="session")
def chronicler() -> ModuleType:
    return load_agent("chronicler")


@pytest.fixture(autouse=True)
def _no_metrics(monkeypatch):
    """Metrics are best-effort in production and pure noise in tests."""
    monkeypatch.setenv("NEXUS_METRICS", "0")
    from nexus_common import metrics

    monkeypatch.setattr(metrics, "ENABLED", False)
