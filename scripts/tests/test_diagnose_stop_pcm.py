"""Tests for the local-only stop PCM diagnosis."""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
from scripts import diagnose_stop_pcm as diagnosis

BASE = 1_700_000_000.0


def write_wav(path: Path, samples: np.ndarray) -> None:
    """Write a test 16 kHz mono PCM WAV."""

    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.astype("<i2").tobytes())


def write_wav_rate(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write a test 16-bit mono PCM WAV at an explicit rate."""

    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.astype("<i2").tobytes())


def test_acoustic_window_only_reports_energy_not_semantics() -> None:
    """A voiced interval is signal evidence, never a recognized stop word."""

    silence = np.zeros(16_000, dtype=np.int16)
    tone = (3_000 * np.sin(2 * np.pi * 220 * np.arange(16_000) / 16_000)).astype(np.int16)
    samples = np.concatenate((silence, tone, silence))
    window = diagnosis.acoustic_window(samples, 1.0, 2.0)

    assert window.available
    assert window.speech_like_energy is True
    assert window.rms is not None and window.rms > 1_000
    assert not hasattr(window, "transcript")


def test_clipping_handles_negative_int16_full_scale() -> None:
    """The -32768 value must not overflow when computing absolute peaks."""

    samples = np.full(16_000, -32768, dtype=np.int16)
    window = diagnosis.acoustic_window(samples, 0.0, 1.0)
    assert window.peak_abs == 32768
    assert window.clipped_samples == 16_000


def test_normalized_cross_correlation_finds_scaled_template() -> None:
    """NCC locates a source after a gain change without exceeding its bound."""

    rng = np.random.default_rng(12)
    template = rng.integers(-20_000, 20_000, 2_000).astype(np.float64)
    capture = rng.normal(0.0, 2.0, 12_000)
    capture[4_321 : 4_321 + len(template)] += 0.35 * template

    ncc = diagnosis.normalized_cross_correlation(capture, template)

    assert int(np.argmax(ncc)) == 4_321
    assert ncc[4_321] > 0.99
    assert float(np.max(ncc)) <= 1.0 + 1e-9


def test_stimulus_alignment_uses_receipt_assets_and_reports_offset(tmp_path: Path) -> None:
    """The receipt asset selected by a trial is matched at its capture lag."""

    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    rng = np.random.default_rng(7)
    source = rng.integers(-15_000, 15_000, 3_000).astype(np.int16)
    asset_path = asset_dir / "stop.wav"
    write_wav_rate(asset_path, source, 24_000)
    stimulus, info = diagnosis.read_stimulus_wav(asset_path)
    capture = np.zeros(16_000, dtype=np.int16)
    capture[6_000 : 6_000 + len(stimulus)] = stimulus.astype(np.int16)
    tag = "t001-story-stop-a"
    rows = [
        {
            "kind": "said",
            "tag": tag,
            "start": "00:00:09.000",
            "end": "00:00:10.000",
            "ts": BASE + 1.375,
        },
        {
            "kind": "reply",
            "tag": tag,
            "interrupted": True,
            "interrupt_start_ts": BASE + 0.375,
            "interrupt_end_ts": BASE + 0.875,
        },
    ]
    receipts = {
        "stimuli": {
            "assets": [
                {
                    "id": "story-stop-a",
                    "file": str(asset_path),
                    "sha256": info["sha256"],
                },
                {
                    "id": "story-stop-a-stop",
                    "file": str(asset_path),
                    "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
                },
            ]
        }
    }

    result = diagnosis.stimulus_alignment(rows, capture, BASE, receipts)

    assert result["status"] == "available"
    assert result["question_offsets_s"] == [0.0]
    assert result["mean_offset_s"] == 0.0
    assert [item["role"] for item in result["matches"]] == ["question", "interrupt"]
    assert all(item["sha256_verified"] is True for item in result["matches"])
    assert all(item["peak_abs_ncc"] > 0.99 for item in result["matches"])
    assert all(item["match_lag_s"] == 6_000 / 16_000 for item in result["matches"])


def test_diagnose_is_offline_and_fail_closed_without_model(tmp_path: Path) -> None:
    """Missing local model is reported, while acoustic/log evidence remains usable."""

    run = tmp_path / "run"
    run.mkdir()
    samples = np.concatenate(
        (
            np.zeros(8_000, dtype=np.int16),
            (2_000 * np.sin(2 * np.pi * 220 * np.arange(16_000) / 16_000)).astype(np.int16),
        )
    )
    write_wav(run / "admitted-uplink.wav", samples)
    (run / "timeline.jsonl").write_text(
        json.dumps(
            {
                "kind": "reply",
                "tag": "t001-story-stop-a",
                "reply_delivery_id": "s/epoch-1/turn-2/generation-2/tool-0",
                "interrupted": True,
                "interrupt_text": "停。",
                "interrupt_start_ts": BASE + 0.5,
                "interrupt_end_ts": BASE + 1.0,
                "interrupt_deadline_ts": BASE + 2.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run / "bridge.log").write_text(
        "2023-11-14T22:13:20.000000000Z INFO media PCM tap opened session=s stream_epoch=1\n"
        "2023-11-14T22:13:20.500000000Z INFO FunASR realtime final task_id=x sentence_id=1 text_len=2 begin_ms=0 end_ms=500\n"
        "2023-11-14T22:13:20.600000000Z INFO media playback-stop not taken session=s reason=not_stop_word stop_word=False\n",
        encoding="utf-8",
    )
    report = diagnosis.diagnose(run, model_dir=tmp_path / "missing-model")

    assert report["offline_only"] is True
    assert report["pcm_wall_clock_alignment"]["verified"] is False
    trial = report["trials"][0]
    assert trial["acoustic_window"]["speech_like_energy"] is True
    assert trial["offline_asr"]["status"] == "unavailable_missing_model"
    assert trial["log_evidence"]["stop_route_invocations"] == 0
    assert trial["log_evidence"]["stop_route_rejections"] == 1
    assert trial["log_evidence"]["asr_result_text_available"] is False


def test_identity_keeps_unique_session_rejections_and_excludes_other_sessions() -> None:
    """Session-only stop logs are attributable when only one tap epoch exists."""

    events = [
        diagnosis.LogEvent(BASE, "tap_open", {"session": "s", "stream_epoch": "1"}, 1),
        diagnosis.LogEvent(BASE + 1, "stop_rejected", {"session": "s"}, 2),
        diagnosis.LogEvent(BASE + 1, "stop_route", {"session": "other"}, 3),
        diagnosis.LogEvent(BASE + 1, "asr_final", {"text_len": 2}, 4),
    ]
    tap_start, scoped, identity = diagnosis.scope_events(events, "s", "1")
    assert tap_start == BASE
    assert [e.kind for e in scoped] == ["stop_rejected"]
    assert identity["stop_rejections_total"] == 1
    assert identity["stop_execution_total"] == 0


def test_multiple_epochs_do_not_borrow_session_only_stop_success() -> None:
    """A session-only execution is unassignable across multiple tap epochs."""

    events = [
        diagnosis.LogEvent(BASE, "tap_open", {"session": "s", "stream_epoch": "1"}, 1),
        diagnosis.LogEvent(BASE + 2, "tap_open", {"session": "s", "stream_epoch": "2"}, 2),
        diagnosis.LogEvent(BASE + 3, "stop_route", {"session": "s"}, 3),
        diagnosis.LogEvent(BASE + 3, "stop_rejected", {"session": "s", "stream_epoch": "1"}, 4),
    ]
    _, scoped, identity = diagnosis.scope_events(events, "s", "1")
    assert [e.kind for e in scoped] == ["stop_rejected"]
    assert identity["session_only_attribution"] == "unverified"
    assert identity["stop_execution_total"] == 0


def test_receipt_hash_and_size_are_verified(tmp_path: Path) -> None:
    """A capture only passes receipt verification when both fields match."""

    run = tmp_path / "run"
    run.mkdir()
    write_wav(run / "admitted-uplink.wav", np.zeros(16_000, dtype=np.int16))
    _, info = diagnosis.read_wav(run / "admitted-uplink.wav")
    receipts = tmp_path / "receipts.json"
    receipts.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "file": "outputs/acceptance/run/admitted-uplink.wav",
                        "size_bytes": (run / "admitted-uplink.wav").stat().st_size,
                        "sha256": info["sha256"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = diagnosis._receipt_check(run, receipts, info)

    assert result["status"] == "match"


def test_acoustic_window_fails_closed_on_negative_or_out_of_range_interval() -> None:
    """Negative start, out-of-range end, or reversed intervals must not report available=True."""

    samples = np.full(16_000, 1_000, dtype=np.int16)

    negative_start = diagnosis.acoustic_window(samples, -0.5, 0.5)
    assert negative_start.available is False
    assert negative_start.rms is None

    past_end = diagnosis.acoustic_window(samples, 0.5, 2.5)
    assert past_end.available is False
    assert past_end.rms is None

    reversed_interval = diagnosis.acoustic_window(samples, 0.8, 0.2)
    assert reversed_interval.available is False
    assert reversed_interval.rms is None


def test_read_wav_rejects_truncated_file(tmp_path: Path) -> None:
    """A truncated WAV whose raw byte count is less than declared frames must raise ValueError."""

    wav_path = tmp_path / "truncated.wav"
    samples = np.full(1_000, 500, dtype=np.int16)
    write_wav(wav_path, samples)
    data = wav_path.read_bytes()
    truncated_data = data[:-200]
    wav_path.write_bytes(truncated_data)

    import pytest

    with pytest.raises(ValueError, match="truncated.*expected.*frames"):
        diagnosis.read_wav(wav_path)


def test_diagnose_rejects_mixed_timeline_session(tmp_path: Path) -> None:
    """A timeline with multiple distinct session IDs must be rejected."""

    run = tmp_path / "run"
    run.mkdir()
    write_wav(run / "admitted-uplink.wav", np.zeros(16_000, dtype=np.int16))
    (run / "timeline.jsonl").write_text(
        json.dumps({"kind": "reply", "tag": "t1", "reply_delivery_id": "session-1/epoch-1/turn-1"})
        + "\n"
        + json.dumps({"kind": "reply", "tag": "t2", "reply_delivery_id": "session-2/epoch-1/turn-2"})
        + "\n",
        encoding="utf-8",
    )
    (run / "bridge.log").write_text(
        "2023-11-14T22:13:20.000000000Z INFO media PCM tap opened session=session-1 stream_epoch=1\n",
        encoding="utf-8",
    )

    import pytest

    with pytest.raises(ValueError, match="mixed timeline: multiple session IDs"):
        diagnosis.diagnose(run)


def test_diagnose_rejects_session_identity_conflict_with_receipt(tmp_path: Path) -> None:
    """A conflict between receipts.json session and timeline session must be rejected."""

    run = tmp_path / "run"
    run.mkdir()
    write_wav(run / "admitted-uplink.wav", np.zeros(16_000, dtype=np.int16))
    (run / "timeline.jsonl").write_text(
        json.dumps({"kind": "reply", "tag": "t1", "reply_delivery_id": "session-timeline/epoch-1/turn-1"})
        + "\n",
        encoding="utf-8",
    )
    (run / "bridge.log").write_text(
        "2023-11-14T22:13:20.000000000Z INFO media PCM tap opened session=session-receipt stream_epoch=1\n",
        encoding="utf-8",
    )
    receipts = tmp_path / "receipts.json"
    receipts.write_text(json.dumps({"session_id": "session-receipt", "stream_epoch": "1"}), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="session conflict"):
        diagnosis.diagnose(run, receipts=receipts)


def test_diagnose_rejects_epoch_conflict_with_receipt(tmp_path: Path) -> None:
    """A conflict between receipts.json stream_epoch and bridge.log tap_open must be rejected."""

    run = tmp_path / "run"
    run.mkdir()
    write_wav(run / "admitted-uplink.wav", np.zeros(16_000, dtype=np.int16))
    (run / "timeline.jsonl").write_text(
        json.dumps({"kind": "reply", "tag": "t1", "reply_delivery_id": "session-1/epoch-1/turn-1"}) + "\n",
        encoding="utf-8",
    )
    (run / "bridge.log").write_text(
        "2023-11-14T22:13:20.000000000Z INFO media PCM tap opened session=session-1 stream_epoch=1\n",
        encoding="utf-8",
    )
    receipts = tmp_path / "receipts.json"
    receipts.write_text(json.dumps({"session_id": "session-1", "stream_epoch": "99"}), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="epoch conflict"):
        diagnosis.diagnose(run, receipts=receipts)
