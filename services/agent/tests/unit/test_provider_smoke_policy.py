from __future__ import annotations

import pytest
from scripts import provider_smoke_test
from services.agent.src.contracts.events import TimedWord


@pytest.mark.asyncio
async def test_provider_smoke_may_skip_in_local_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.delenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", raising=False)

    assert await provider_smoke_test.main() == 0


@pytest.mark.asyncio
async def test_required_provider_smoke_fails_closed_when_not_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", "true")

    assert await provider_smoke_test.main() == 1


def test_doubao_smoke_validates_pcm_and_monotonic_word_timestamps() -> None:
    pcm = b"\x00\x00" * 24_000
    words = (
        TimedWord(text="你", begin_ms=0, end_ms=450),
        TimedWord(text="好", begin_ms=450, end_ms=1_000),
    )

    provider_smoke_test._validate_doubao_result(
        pcm,
        words,
        sample_rate=24_000,
        num_channels=1,
    )

    with pytest.raises(AssertionError, match="24000 Hz mono"):
        provider_smoke_test._validate_doubao_result(
            pcm,
            words,
            sample_rate=16_000,
            num_channels=1,
        )
    with pytest.raises(AssertionError, match="frame-aligned"):
        provider_smoke_test._validate_doubao_result(
            pcm + b"\x00",
            words,
            sample_rate=24_000,
            num_channels=1,
        )
    with pytest.raises(AssertionError, match="not monotonic"):
        provider_smoke_test._validate_doubao_result(
            pcm,
            (
                TimedWord(text="你", begin_ms=500, end_ms=700),
                TimedWord(text="好", begin_ms=400, end_ms=1_000),
            ),
            sample_rate=24_000,
            num_channels=1,
        )
    with pytest.raises(AssertionError, match="alignment is degraded"):
        provider_smoke_test._validate_doubao_result(
            pcm,
            words,
            sample_rate=24_000,
            num_channels=1,
            alignment_status="degraded",
        )


@pytest.mark.asyncio
async def test_provider_smoke_runs_doubao_funasr_and_llm_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "test-doubao-key")
    monkeypatch.setenv("DOUBAO_TTS_STYLE_CONTROL_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "bailian_deepseek")
    calls: list[str] = []

    async def fake_doubao() -> list[tuple[bytes, tuple[str, ...], tuple[str, ...]]]:
        calls.append("doubao")
        return [(b"\x00\x00", ("测试",), ())]

    async def fake_realtime_search(_settings: object) -> None:
        calls.append("realtime-search")

    async def fake_funasr(
        pcm: bytes,
        *,
        expected_markers: tuple[str, ...],
        forbidden_markers: tuple[str, ...],
    ) -> None:
        assert pcm == b"\x00\x00"
        assert expected_markers == ("测试",)
        assert forbidden_markers == ()
        calls.append("funasr")

    async def fake_llm() -> str:
        calls.append("llm")
        return "DeepSeek"

    async def fake_interrupt_semantic() -> None:
        calls.append("interrupt-semantic")

    monkeypatch.setattr(provider_smoke_test, "smoke_doubao", fake_doubao)
    monkeypatch.setattr(provider_smoke_test, "smoke_realtime_search", fake_realtime_search)
    monkeypatch.setattr(provider_smoke_test, "smoke_funasr", fake_funasr)
    monkeypatch.setattr(provider_smoke_test, "smoke_llm", fake_llm)
    monkeypatch.setattr(
        provider_smoke_test,
        "smoke_interrupt_semantic",
        fake_interrupt_semantic,
    )

    assert await provider_smoke_test.main() == 0
    assert calls == ["realtime-search", "doubao", "funasr", "llm", "interrupt-semantic"]
    assert (
        "provider_smoke_test PASS: FunASR, QwenRealtimeSearch, DeepSeek, Doubao, InterruptSemantic"
        in capsys.readouterr().out
    )


@pytest.mark.asyncio
async def test_realtime_search_smoke_uses_isolated_resolver_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    closed = False

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "今天是测试日期。"

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        provider_smoke_test,
        "build_realtime_search_resolver",
        lambda *, settings: Resolver(),
    )

    await provider_smoke_test.smoke_realtime_search(object())

    assert queries == ["请联网查询今天的日期，只用一句中文回答。"]
    assert closed


@pytest.mark.asyncio
async def test_realtime_search_smoke_fails_closed_on_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    class Resolver:
        async def resolve(self, *, query: str) -> None:
            assert query
            return None

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        provider_smoke_test,
        "build_realtime_search_resolver",
        lambda *, settings: Resolver(),
    )

    with pytest.raises(AssertionError, match="returned no public result"):
        await provider_smoke_test.smoke_realtime_search(object())

    assert closed
