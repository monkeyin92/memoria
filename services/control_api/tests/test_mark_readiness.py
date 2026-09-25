from __future__ import annotations

from typing import Any

import pytest
from scripts import mark_readiness


def test_mark_and_check_use_control_env_secret_and_release_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "control-auth-material-that-is-long-enough")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-a")
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            (200, {"status": "marked"}),
            (200, {"status": "ready"}),
        ]
    )

    def fake_request(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append({"url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(mark_readiness, "_request_json", fake_request)

    assert mark_readiness.main(["--control-api-url", "http://control-api:8000"]) == 0
    assert calls == [
        {
            "url": "http://control-api:8000/internal/readiness/smokes",
            "method": "POST",
            "authorization": "control-auth-material-that-is-long-enough",
            "payload": {
                "livekit": True,
                "funasr": True,
                "llm": True,
                "llm_provider": "deepseek",
                "release_tag": "release-test-a",
                "tts": {
                    "provider": "doubao",
                    "audio": True,
                    "word_timestamps": True,
                },
            },
        },
        {"url": "http://control-api:8000/health/ready"},
    ]


def test_mark_fails_closed_without_control_auth_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORIA_AUTH_SECRET", raising=False)
    calls: list[str] = []

    def unexpected_request(url: str, **_: Any) -> tuple[int, dict[str, Any]]:
        calls.append(url)
        return 200, {"status": "marked"}

    monkeypatch.setattr(mark_readiness, "_request_json", unexpected_request)

    assert mark_readiness._mark_smokes_passed("http://control-api:8000") is False
    assert calls == []


def test_mark_does_not_run_ready_check_after_mark_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "control-auth-material-that-is-long-enough")
    calls: list[dict[str, Any]] = []

    def failed_mark(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append({"url": url, **kwargs})
        return 503, {}

    monkeypatch.setattr(mark_readiness, "_request_json", failed_mark)

    assert mark_readiness.main([]) == 1
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"


def test_mark_only_defers_ready_check_to_the_release_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "control-auth-material-that-is-long-enough")
    calls: list[dict[str, Any]] = []

    def marked(url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append({"url": url, **kwargs})
        return 200, {"status": "marked"}

    monkeypatch.setattr(mark_readiness, "_request_json", marked)

    assert mark_readiness.main(["--skip-ready-check"]) == 0
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/internal/readiness/smokes")
