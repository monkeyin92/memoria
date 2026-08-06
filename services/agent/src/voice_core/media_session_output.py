"""Compatibility facade for fenced Media Voice output coordination.

The concrete responsibilities live in focused modules so output dispatch,
streaming and lease ownership can evolve independently while the registry keeps
its established mixin contract.
"""

from services.agent.src.voice_core.media_session_output_dispatch import (
    _CONVERSATION_REPLY_TTL_MS,
    _STREAMCORE_EXECUTABLE_OUTPUT_KINDS,
    MediaOutputDispatchMixin,
)
from services.agent.src.voice_core.media_session_output_owner import MediaOutputOwnerMixin
from services.agent.src.voice_core.media_session_output_stream import MediaOutputStreamMixin


class MediaOutputMixin(
    MediaOutputDispatchMixin,
    MediaOutputStreamMixin,
    MediaOutputOwnerMixin,
):
    """Compose one output coordinator without duplicating its session state."""


__all__ = [
    "MediaOutputMixin",
    "_CONVERSATION_REPLY_TTL_MS",
    "_STREAMCORE_EXECUTABLE_OUTPUT_KINDS",
]
