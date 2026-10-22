"""StepFun circuit windows: auth/quota versus ordinary failures.

Authentication and balance errors still open the circuit on the first failure,
but only for a configurable five minutes by default. Ordinary transport
failures keep the pre-existing three-failure, thirty-second window.
"""

from __future__ import annotations

from services.agent.src.providers.tts_failover import (
    StepFunFailoverConfig,
    StepFunPrimaryTTS,
    TTSFailoverBreaker,
)

AUTH_OPEN_S = 300.0
ORDINARY_OPEN_S = 30.0


def test_auth_breaker_defaults_to_300_seconds() -> None:
    assert StepFunFailoverConfig().auth_open_s == AUTH_OPEN_S
    assert StepFunFailoverConfig.from_env({}).auth_open_s == AUTH_OPEN_S
    assert TTSFailoverBreaker().auth_open_s == AUTH_OPEN_S

    wrapper = StepFunPrimaryTTS(
        primary=object(),
        backup=object(),
        config=StepFunFailoverConfig.from_env({}),
    )
    assert wrapper.breaker.auth_open_s == AUTH_OPEN_S


def test_auth_breaker_window_can_be_configured() -> None:
    now = [100.0]
    config = StepFunFailoverConfig.from_env(
        {"STEPFUN_TTS_AUTH_BREAKER_OPEN_S": "45"}
    )
    assert config.auth_open_s == 45.0

    breaker = TTSFailoverBreaker(clock=lambda: now[0])
    breaker.configure(config)
    breaker.record_failure(auth=True)

    now[0] += 44
    assert breaker.allow() is False
    now[0] += 1
    assert breaker.allow() is True
    assert breaker.state == "half_open"


def test_auth_breaker_half_opens_and_recovers_after_expiry() -> None:
    now = [1000.0]
    breaker = TTSFailoverBreaker(clock=lambda: now[0])

    breaker.record_failure(auth=True)
    assert breaker.state == "open"

    now[0] += AUTH_OPEN_S - 1
    assert breaker.allow() is False
    now[0] += 1
    assert breaker.available() is True
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    assert breaker.allow() is False

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_ordinary_failures_keep_the_30_second_window() -> None:
    now = [500.0]
    breaker = TTSFailoverBreaker(clock=lambda: now[0])
    assert breaker.open_s == ORDINARY_OPEN_S

    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open"

    now[0] += ORDINARY_OPEN_S - 1
    assert breaker.allow() is False
    now[0] += 1
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    assert breaker.allow() is False

    breaker.record_success()
    assert breaker.state == "closed"
