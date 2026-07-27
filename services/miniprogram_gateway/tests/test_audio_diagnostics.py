from __future__ import annotations

import json
import stat
import wave
from pathlib import Path

from services.miniprogram_gateway.audio_diagnostics import AecPcmCapture


def test_aec_capture_is_exact_session_only_bounded_and_private(tmp_path: Path) -> None:
    assert (
        AecPcmCapture.try_create(
            directory=tmp_path / "ignored",
            session_id="session-a",
            capture_session_id="session-b",
            sample_rate=16_000,
            max_ms=40,
        )
        is None
    )
    assert not (tmp_path / "ignored").exists()

    directory = tmp_path / "capture"
    capture = AecPcmCapture.try_create(
        directory=directory,
        session_id="session-a",
        capture_session_id="session-a",
        sample_rate=16_000,
        max_ms=40,
    )
    assert capture is not None
    capture.write(b"\x01\x00" * 800, b"\x02\x00" * 800)

    pre_path = next(directory.glob("*-pre.wav"))
    post_path = next(directory.glob("*-post.wav"))
    manifest_path = next(directory.glob("*.json"))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(pre_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(post_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    with wave.open(str(pre_path), "rb") as pre:
        assert pre.getframerate() == 16_000
        assert pre.getnchannels() == 1
        assert pre.getsampwidth() == 2
        assert pre.getnframes() == 640
        assert pre.readframes(640) == b"\x01\x00" * 640
    with wave.open(str(post_path), "rb") as post:
        assert post.getnframes() == 640
        assert post.readframes(640) == b"\x02\x00" * 640

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["captured_ms"] == 40
    assert manifest["session_hash"] != "session-a"
