from __future__ import annotations

import pytest
from services.agent.src.providers.reliability import CircuitBreaker, CircuitOpenError


def test_circuit_breaker_open_half_open_closed() -> None:
    breaker = CircuitBreaker(open_seconds=30.0)
    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_request()

    breaker.before_request(health_probe=True)
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"


def test_half_open_failure_reopens() -> None:
    breaker = CircuitBreaker()
    breaker.state = "half_open"
    breaker.record_failure()
    assert breaker.state == "open"
