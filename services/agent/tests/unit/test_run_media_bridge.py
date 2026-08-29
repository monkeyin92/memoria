from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from scripts import run_media_bridge


class _SessionFactory:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_error = close_error
        self.closed = False

    def __call__(self, _identity: object) -> object:
        return object()

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Server:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.start_error = start_error
        self.stop_error = stop_error
        self.stopped = False

    async def start(self, _address: str, *, tls: object) -> int:
        if self.start_error is not None:
            raise self.start_error
        return 50051

    async def stop(self) -> None:
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        environment="production",
        log_level="INFO",
        media_bridge_grpc_enabled=True,
        media_bridge_max_pending_audio_frames=8,
        media_bridge_max_pending_messages=8,
        media_bridge_go_shadow_enabled=False,
        media_output_generation_timeout_s=45.0,
        media_owner_silence_timeout_s=10.0,
        media_max_user_speech_duration_s=60.0,
        media_bridge_grpc_addr="127.0.0.1:50051",
        prometheus_port=0,
    )


@pytest.mark.asyncio
async def test_run_wires_max_user_speech_duration_to_session_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server()
    session_factory = _SessionFactory()
    settings = _settings()
    settings.media_max_user_speech_duration_s = 37.5
    _patch_run(monkeypatch, server=server, session_factory=session_factory)
    monkeypatch.setattr(run_media_bridge, "load_settings", lambda **_kwargs: settings)

    captured: list[dict[str, object]] = []

    class _Registry:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

        def install(self) -> None:
            return None

    monkeypatch.setattr(run_media_bridge, "MediaVoiceCoreRegistry", _Registry)

    await run_media_bridge.run()

    assert len(captured) == 1
    assert captured[0]["output_generation_timeout_s"] == 45.0
    assert captured[0]["owner_silence_timeout_s"] == 10.0
    assert captured[0]["max_user_speech_duration_s"] == 37.5


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_factory", [None, object()])
async def test_run_wires_watchdog_in_provider_registry_variants(
    monkeypatch: pytest.MonkeyPatch,
    runtime_factory: object | None,
) -> None:
    """The provider-only fallback must not silently drop watchdog settings."""

    server = _Server()
    settings = _settings()
    settings.media_max_user_speech_duration_s = 41.0
    _patch_run(monkeypatch, server=server, session_factory=_SessionFactory())
    monkeypatch.setattr(run_media_bridge, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(run_media_bridge, "_load_session_factory", lambda _settings: None)
    monkeypatch.setattr(
        run_media_bridge,
        "_load_provider_factory",
        lambda _settings: (lambda _identity: object()),
    )
    monkeypatch.setattr(run_media_bridge, "_load_runtime_factory", lambda _settings: runtime_factory)

    captured: list[dict[str, object]] = []

    class _Registry:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

        def install(self) -> None:
            return None

    monkeypatch.setattr(run_media_bridge, "MediaVoiceCoreRegistry", _Registry)

    await run_media_bridge.run()

    assert len(captured) == 1
    assert captured[0]["owner_silence_timeout_s"] == 10.0
    assert captured[0]["max_user_speech_duration_s"] == 41.0
    if runtime_factory is None:
        assert "runtime_factory" not in captured[0]
    else:
        assert captured[0]["runtime_factory"] is runtime_factory


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    server: _Server,
    session_factory: _SessionFactory,
    wait_error: BaseException | None = None,
) -> None:
    class _StopEvent:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            if wait_error is not None:
                raise wait_error

    class _Loop:
        def add_signal_handler(self, _signum: int, callback: Any) -> None:
            callback()

    monkeypatch.setattr(run_media_bridge, "load_settings", lambda **_kwargs: _settings())
    monkeypatch.setattr(run_media_bridge, "MediaBridgeGrpcServer", lambda **_kwargs: server)
    monkeypatch.setattr(
        run_media_bridge,
        "_load_session_factory",
        lambda _settings: session_factory,
    )
    monkeypatch.setattr(run_media_bridge, "_tls_for_settings", lambda _settings: None)
    monkeypatch.setattr(
        run_media_bridge,
        "asyncio",
        SimpleNamespace(Event=_StopEvent, get_running_loop=lambda: _Loop()),
    )


@pytest.mark.asyncio
async def test_run_closes_session_factory_when_server_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_error = RuntimeError("start failed")
    server = _Server(start_error=start_error)
    session_factory = _SessionFactory(close_error=RuntimeError("close failed"))
    _patch_run(monkeypatch, server=server, session_factory=session_factory)

    with pytest.raises(RuntimeError, match="start failed") as caught:
        await run_media_bridge.run()

    assert caught.value is start_error
    assert session_factory.closed


@pytest.mark.asyncio
async def test_run_closes_session_factory_and_preserves_server_stop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_error = RuntimeError("stop failed")
    server = _Server(stop_error=stop_error)
    session_factory = _SessionFactory(close_error=RuntimeError("close failed"))
    _patch_run(monkeypatch, server=server, session_factory=session_factory)

    with pytest.raises(RuntimeError, match="stop failed") as caught:
        await run_media_bridge.run()

    assert caught.value is stop_error
    assert server.stopped
    assert session_factory.closed


@pytest.mark.asyncio
async def test_run_preserves_wait_error_when_all_cleanup_steps_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_error = RuntimeError("wait failed")
    server = _Server(stop_error=RuntimeError("stop failed"))
    session_factory = _SessionFactory(close_error=RuntimeError("close failed"))
    _patch_run(
        monkeypatch,
        server=server,
        session_factory=session_factory,
        wait_error=wait_error,
    )

    with pytest.raises(RuntimeError, match="wait failed") as caught:
        await run_media_bridge.run()

    assert caught.value is wait_error
    assert server.stopped
    assert session_factory.closed


@pytest.mark.asyncio
async def test_run_propagates_factory_close_error_after_clean_server_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = RuntimeError("close failed")
    server = _Server()
    session_factory = _SessionFactory(close_error=close_error)
    _patch_run(monkeypatch, server=server, session_factory=session_factory)

    with pytest.raises(RuntimeError, match="close failed") as caught:
        await run_media_bridge.run()

    assert caught.value is close_error
    assert server.stopped
    assert session_factory.closed
