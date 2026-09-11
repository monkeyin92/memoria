"""User-facing copy for a refused voice sample.

The reasons are already measured (``sample_validation.py``); this is the only
place that turns them into Chinese the user can act on. Kept next to the
measurement so a new reason cannot ship without copy.
"""

from __future__ import annotations

from services.voice_profile.sample_validation import SampleRejection

_REASON_COPY: dict[SampleRejection, str] = {
    "audio_decode_failed": "这个文件不像是一段能用的录音，请换一段人声试试。",
    "audio_too_short": "这段声音太短了，请录 10 秒以上、60 秒以内。",
    "audio_too_long": "这段声音太长了，请录 10 秒以上、60 秒以内。",
    "audio_low_sample_rate": "这段录音的音质太低了，请换个设备或重新录一段。",
    "audio_silent": "这段声音太安静了，请在安静的环境里重录一遍。",
    "speech_too_short": "这段里说话的时间太短，请连续说 10 秒以上。",
    "audio_clipped": "这段声音有破音，请离麦克风远一点再录一遍。",
}

#: The first reason decides the headline: it is the most actionable one.
_REASON_ORDER: tuple[SampleRejection, ...] = (
    "audio_decode_failed",
    "audio_too_short",
    "audio_too_long",
    "audio_silent",
    "speech_too_short",
    "audio_clipped",
    "audio_low_sample_rate",
)


def sample_rejection_message(reasons: tuple[SampleRejection, ...]) -> str:
    """One friendly Chinese sentence naming the most actionable problem."""
    for reason in _REASON_ORDER:
        if reason in reasons:
            return _REASON_COPY[reason]
    return "这段声音暂时用不了，请换一段 10–60 秒的清晰人声再试。"
