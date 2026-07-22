from __future__ import annotations

import asyncio
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from livekit import agents
from services.agent.src import heartbeat as heartbeat_module
from services.agent.src import main as main_module


def test_livekit_1_6_5_registration_probe_tracks_reconnect_state() -> None:
    assert version("livekit-agents") == "1.6.5"
    server = agents.AgentServer()
    assert server._id == "unregistered"
    assert server._closed is True
    assert server._connecting is False
    assert server._connection_failed is False
    assert main_module._livekit_server_is_registered(server) is False

    server._closed = False
    server._id = "worker-registered"
    assert main_module._livekit_server_is_registered(server) is True

    server._closed = True
    assert main_module._livekit_server_is_registered(server) is False

    server._closed = False
    server._connecting = True
    assert main_module._livekit_server_is_registered(server) is False

    server._connecting = False
    server._id = "worker-reregistered"
    assert main_module._livekit_server_is_registered(server) is True

    server._connection_failed = True
    assert main_module._livekit_server_is_registered(server) is False

    server._connection_failed = False
    server._id = None
    assert main_module._livekit_server_is_registered(server) is False


def test_online_start_validates_required_keys_first(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[bool] = []

    def fail_fast(*, require_keys: bool = False) -> None:
        requested.append(require_keys)
        raise RuntimeError("missing provider keys")

    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setattr(main_module, "load_settings", fail_fast)

    with pytest.raises(RuntimeError, match="missing provider keys"):
        main_module.main()

    assert requested == [True]


def test_offline_import_health_skips_livekit_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setattr("sys.argv", ["agent"])
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(offline_mock=True),
    )

    main_module.main()

    assert capsys.readouterr().out == "agent offline mode: not starting LiveKit worker\n"


@pytest.mark.asyncio
async def test_production_worker_events_drive_agent_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: dict[str, object] = {}
    captured: dict[str, object] = {}
    sent = asyncio.Event()

    class FakeServer:
        def __init__(self, **_: object) -> None:
            self._id = "unregistered"
            self._closed = True
            self._connecting = False
            self._connection_failed = False

        def rtc_session(self, *_: object, **__: object) -> None:
            pass

        def on(self, name: str, callback: object) -> None:
            events[name] = callback

    class FakeAsyncClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def get(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(is_success=True)

        async def post(self, endpoint: str, **kwargs: object) -> None:
            captured.update(endpoint=endpoint, **kwargs)
            sent.set()
            raise asyncio.CancelledError

    settings = SimpleNamespace(
        offline_mock=False,
        environment="production",
        livekit_agent_name="duplex-zh-agent",
        archive_session_events_url="http://control-api:8000/v1/archive/session-events",
        internal_token=lambda capability: (
            "agent-heartbeat-token" if capability == "agent_heartbeat" else ""
        ),
    )
    monkeypatch.setattr(main_module, "load_settings", lambda **_: settings)
    monkeypatch.setattr(agents, "AgentServer", FakeServer)
    monkeypatch.setattr(heartbeat_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-heartbeat-test")

    def run_app(server: FakeServer) -> None:
        server._id = "worker-id"
        server._closed = False
        events["worker_started"]()

    monkeypatch.setattr(agents.cli, "run_app", run_app)

    main_module.main()
    await sent.wait()

    assert captured["endpoint"] == (
        "http://control-api:8000/internal/readiness/agent-heartbeat"
    )
    assert captured["headers"] == {
        "X-Memoria-Internal-Token": "agent-heartbeat-token",
    }
    payload = captured["json"]
    assert payload["release_tag"] == "release-heartbeat-test"
    assert payload["worker_ready"] is True
    assert payload["livekit_ready"] is True
