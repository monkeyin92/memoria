"""Uplink PCM forensic probe: bounded cadence, measured values, no other work."""

from __future__ import annotations

import logging

from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session_input import _trace_uplink_pcm
from services.agent.tests.unit.media_session_support import _device_identity


def _frame(sequence: int, *, value: int = 16) -> AudioFrame:
    identity: SessionIdentity = _device_identity("uplink-trace")
    payload = value.to_bytes(2, "little", signed=True) * 2
    return AudioFrame(
        identity=identity,
        sequence=sequence,
        capture_start_sample=sequence * 2,
        frame_samples=2,
        payload=payload,
    )


def test_uplink_pcm_trace_reports_every_twenty_fifth_frame(caplog) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.voice_core.media_session_input")
    for sequence in (0, 1, 24, 25, 49, 50):
        _trace_uplink_pcm("uplink-trace", _frame(sequence))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("media uplink pcm")]
    assert len(lines) == 3
    assert "seq=0 " in lines[0] and "rms=16.0" in lines[0]
    assert "seq=25 " in lines[1]
    assert "seq=50 " in lines[2]
    assert "bytes=4" in lines[0]
