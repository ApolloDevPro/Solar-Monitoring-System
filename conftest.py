"""Shared pytest configuration for the project."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_rate_limiting():
    """Disable slowapi rate limits for the whole test session.

    The limiter keys requests on the client address, which is the constant
    ``testclient`` under FastAPI's TestClient, so counts accumulate across
    every test in the process and eventually trip 429s.  Real limits are
    exercised separately (see tests/test_backend.py::test_rate_limiting).
    """
    import backend.app as app_module

    limiter = app_module.limiter
    previous = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = previous
