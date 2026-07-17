from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.agent.src import main as main_module


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
