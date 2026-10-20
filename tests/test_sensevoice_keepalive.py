"""SenseVoice keepalive probe: synthetic audio only, no external calls."""

from __future__ import annotations

import io
import urllib.error

import pytest
from scripts import sensevoice_keepalive as keepalive


def test_silence_pcm_is_one_second_of_16k_mono() -> None:
    pcm = keepalive.silence_pcm(1.0)
    assert len(pcm) == 32_000
    assert pcm == b"\x00\x00" * 16_000


@pytest.mark.parametrize("seconds", [0, -1])
def test_silence_pcm_rejects_non_positive_duration(seconds: float) -> None:
    with pytest.raises(ValueError):
        keepalive.silence_pcm(seconds)


def test_probe_posts_pcm_query_and_reads_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Response(io.BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def fake_urlopen(request: object, timeout: float) -> Response:
        seen["url"] = request.full_url  # type: ignore[attr-defined]
        seen["body"] = request.data  # type: ignore[attr-defined]
        seen["timeout"] = timeout
        return Response(b'{"text": "\xe6\x88\x91\xe3\x80\x82"}')

    monkeypatch.setattr(keepalive.urllib.request, "urlopen", fake_urlopen)
    assert keepalive.probe("http://127.0.0.1:8001/transcribe", timeout_s=15) is True
    assert seen["url"] == (
        "http://127.0.0.1:8001/transcribe?format=pcm&sample_rate=16000&language=zh"
    )
    assert seen["body"] == b"\x00\x00" * 16_000
    assert seen["timeout"] == 15


def test_probe_returns_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> None:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(keepalive.urllib.request, "urlopen", fake_urlopen)
    assert keepalive.probe("http://127.0.0.1:8001/transcribe") is False


def test_main_reports_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(keepalive, "probe", lambda *args, **kwargs: True)
    assert keepalive.main(["--url", "http://127.0.0.1:8001/transcribe"]) == 0
    assert "ok" in capsys.readouterr().out

    monkeypatch.setattr(keepalive, "probe", lambda *args, **kwargs: False)
    assert keepalive.main([]) == 1
    assert "failed" in capsys.readouterr().err
