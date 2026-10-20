"""Fixtures shared by the split media-session test modules."""

from __future__ import annotations

from typing import Any

import pytest
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.tests.unit.media_session_support import (  # noqa: F401 - fixture
    device_media_session,
)


@pytest.fixture(autouse=True)
def _media_voice_registry_without_aigc_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pre-existing media-session fixtures free of the default-on AIGC tone.

    Production defaults the round notice on. These fixtures stream replies, cues
    or wake acknowledgements first, so the tone would claim the generation ahead
    of them. Tests that exercise the hook pass their own config explicitly.
    """

    original = MediaVoiceCoreRegistry.__init__

    def init(self: MediaVoiceCoreRegistry, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("aigc_notice_config", AigcNoticeConfig(enabled=False))
        kwargs.setdefault("aigc_round_logger", None)
        original(self, *args, **kwargs)

    monkeypatch.setattr(MediaVoiceCoreRegistry, "__init__", init)
