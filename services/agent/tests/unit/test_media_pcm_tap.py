"""Unit tests for the env-gated Media ingress PCM diagnostic tap."""

from __future__ import annotations

import os
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from services.agent.src.voice_core.media_audio_ingress import (
    MediaAudioIngress,
    MediaAudioIngressState,
)
from services.agent.src.voice_core.media_pcm_tap import (
    DEFAULT_TAP_DIR_MAX_BYTES,
    DEFAULT_TAP_MAX_BYTES,
    SAMPLE_RATE_HZ,
    MediaPcmTap,
    maybe_create_tap,
    prune_tap_directory,
    tap_dir_max_bytes,
    tap_max_bytes,
)


def _pcm(values: tuple[int, ...]) -> bytes:
    return b"".join(value.to_bytes(2, "little", signed=True) for value in values)


def _read_wav(path: Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(path), "rb") as reader:
        return (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.readframes(reader.getnframes()),
        )


def _fake_context(state: MediaAudioIngressState, stream_epoch: int = 7):
    return SimpleNamespace(
        ingress=state,
        identity=SimpleNamespace(session_id="sess-1"),
        stream_epoch=stream_epoch,
    )


def test_maybe_create_tap_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_PCM_TAP_DIR", raising=False)
    assert maybe_create_tap("sess", 1) is None


def test_maybe_create_tap_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR", str(tmp_path))
    tap = maybe_create_tap("sess-1", 3)
    assert tap is not None
    assert tap.path == str(tmp_path / "media-uplink-sess-1-epoch3.wav")
    tap.close()


def test_tap_writes_valid_wav(tmp_path: Path) -> None:
    tap = MediaPcmTap(directory=str(tmp_path), session_id="sess-1", stream_epoch=2, max_bytes=4096)
    payload = _pcm((1, -1, 32, -32))
    tap.write(payload)
    tap.write(payload)
    tap.close()
    channels, width, rate, frames = _read_wav(Path(tap.path))
    assert (channels, width, rate) == (1, 2, SAMPLE_RATE_HZ)
    assert frames == payload * 2


def test_tap_enforces_byte_cap_frame_aligned(tmp_path: Path) -> None:
    tap = MediaPcmTap(directory=str(tmp_path), session_id="sess-1", stream_epoch=2, max_bytes=5)
    tap.write(_pcm((1, 2, 3, 4)))
    assert tap.disabled
    tap.write(_pcm((9, 9)))
    tap.close()
    _, _, _, frames = _read_wav(Path(tap.path))
    # 5-byte budget truncated to the nearest 16-bit frame boundary.
    assert frames == _pcm((1, 2))


def test_tap_zero_budget_disabled_without_file(tmp_path: Path) -> None:
    tap = MediaPcmTap(directory=str(tmp_path), session_id="sess-1", stream_epoch=2, max_bytes=0)
    tap.write(_pcm((1,)))
    tap.close()
    assert tap.disabled
    assert tap.written_bytes == 0
    assert not Path(tap.path).exists()


def test_tap_fail_open_on_unwritable_directory(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked.wav"
    blocked.write_text("not a directory")
    tap = MediaPcmTap(directory=str(blocked), session_id="sess-1", stream_epoch=2, max_bytes=1024)
    tap.write(_pcm((1, 2)))
    assert tap.disabled
    tap.write(_pcm((3,)))
    tap.close()


def test_tap_max_bytes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_PCM_TAP_MAX_BYTES", raising=False)
    assert tap_max_bytes() == DEFAULT_TAP_MAX_BYTES
    monkeypatch.setenv("MEDIA_PCM_TAP_MAX_BYTES", "2048")
    assert tap_max_bytes() == 2048
    monkeypatch.setenv("MEDIA_PCM_TAP_MAX_BYTES", "not-a-number")
    assert tap_max_bytes() == DEFAULT_TAP_MAX_BYTES
    monkeypatch.setenv("MEDIA_PCM_TAP_MAX_BYTES", "-5")
    assert tap_max_bytes() == 0


def test_ingress_tap_frame_lazy_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR", str(tmp_path))
    state = MediaAudioIngressState.create(max_frames=4)
    context = _fake_context(state)
    frame = SimpleNamespace(payload=_pcm((7, -7)))
    MediaAudioIngress._tap_frame(context, frame)
    MediaAudioIngress._tap_frame(context, frame)
    assert state.pcm_tap is not None
    state.pcm_tap.close()
    _, _, _, frames = _read_wav(Path(state.pcm_tap.path))
    assert frames == frame.payload * 2


def test_ingress_tap_frame_disabled_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEDIA_PCM_TAP_DIR", raising=False)
    state = MediaAudioIngressState.create(max_frames=4)
    context = _fake_context(state)
    MediaAudioIngress._tap_frame(context, SimpleNamespace(payload=_pcm((1,))))
    assert state.pcm_tap_initialized
    assert state.pcm_tap is None


def _old_tap(directory: Path, name: str, size: int, mtime: float) -> Path:
    path = directory / f"media-uplink-{name}.wav"
    path.write_bytes(b"\0" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_opening_a_tap_deletes_the_oldest_files_to_stay_under_the_directory_cap(
    tmp_path: Path,
) -> None:
    # 5 files of 1000 bytes, oldest first; the cap leaves room for 2 of them plus the new 1000.
    files = [_old_tap(tmp_path, f"s{index}-epoch1", 1000, 1_000 + index) for index in range(5)]
    other = tmp_path / "heartbeat.json"
    other.write_text("{}")
    deleted = prune_tap_directory(str(tmp_path), room_for=1000, limit=3000)
    assert deleted == 3
    assert [path.exists() for path in files] == [False, False, False, True, True]
    assert other.exists(), "only the tap's own files are ever deleted"


def test_a_tap_that_opens_prunes_first_and_then_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR_MAX_BYTES", "2500")
    oldest = _old_tap(tmp_path, "a-epoch1", 1000, 100)
    newer = _old_tap(tmp_path, "b-epoch1", 1000, 200)
    tap = MediaPcmTap(directory=str(tmp_path), session_id="c", stream_epoch=1, max_bytes=1000)
    tap.write(_pcm((1, 2, 3, 4)))
    tap.close()
    assert not oldest.exists()
    assert newer.exists()
    assert Path(tap.path).exists()
    assert sum(path.stat().st_size for path in tmp_path.glob("media-uplink-*.wav")) <= 2500 + 64


def test_pruning_never_raises_on_a_missing_directory_or_unremovable_file(tmp_path: Path) -> None:
    assert prune_tap_directory(str(tmp_path / "absent"), room_for=10, limit=10) == 0
    # A directory named like a tap file cannot be unlinked; pruning carries on.
    (tmp_path / "media-uplink-dir.wav").mkdir()
    _old_tap(tmp_path, "x-epoch1", 500, 1)
    assert prune_tap_directory(str(tmp_path), room_for=10, limit=10) >= 0


def test_directory_cap_defaults_and_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_PCM_TAP_DIR_MAX_BYTES", raising=False)
    assert tap_dir_max_bytes() == DEFAULT_TAP_DIR_MAX_BYTES
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR_MAX_BYTES", "not-a-number")
    assert tap_dir_max_bytes() == DEFAULT_TAP_DIR_MAX_BYTES
    monkeypatch.setenv("MEDIA_PCM_TAP_DIR_MAX_BYTES", "-5")
    assert tap_dir_max_bytes() == 0
    # The default leaves real room on the 64 MiB /tmp tmpfs the production bridge gets.
    assert DEFAULT_TAP_DIR_MAX_BYTES <= 48 * 1024 * 1024
