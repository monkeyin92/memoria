from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.first_packet_latency import analyze_lines, analyze_paths


def log_line(timestamp: str, event: str) -> str:
    if event == "first_frame_sent":
        return (
            f"{timestamp} INFO:services.agent.src.voice_core.media_session_output_dispatch: "
            "media reply delivery event=first_frame_sent\n"
        )
    return (
        f"{timestamp} INFO:services.agent.src.runtime_publication:agent_audio_trace "
        f"name={event} status=ok turn_id=None generation_id=None\n"
    )


def test_normal_pairing_reports_all_four_segments() -> None:
    lines = [
        log_line("2026-11-10T00:00:00.000000000Z", "first_phrase_ready"),
        log_line("2026-11-10T00:00:00.100000000Z", "doubao_session_started"),
        log_line("2026-11-10T00:00:00.400000000Z", "doubao_first_pcm"),
        log_line("2026-11-10T00:00:00.450000000Z", "first_frame_sent"),
    ]

    report = analyze_lines(lines)
    segments = report["segments"]

    assert segments["first_phrase_ready->doubao_session_started"]["samples"] == 1
    assert segments["doubao_session_started->doubao_first_pcm"]["p50"] == 0.3
    assert segments["doubao_first_pcm->first_frame_sent"]["mean"] == 0.05
    assert segments["first_phrase_ready->first_frame_sent"]["max"] == 0.45


def test_missing_first_pcm_is_visible_and_not_silently_paired() -> None:
    lines = [
        log_line("2026-11-10T00:00:00.000000000Z", "first_phrase_ready"),
        log_line("2026-11-10T00:00:00.100000000Z", "doubao_session_started"),
        log_line("2026-11-10T00:00:00.200000000Z", "first_frame_sent"),
    ]

    report = analyze_lines(lines)
    tts = report["segments"]["doubao_session_started->doubao_first_pcm"]
    pcm_to_frame = report["segments"]["doubao_first_pcm->first_frame_sent"]

    assert tts["samples"] == 0
    assert tts["discarded_rounds"] == 1
    assert tts["missing_end"] == 1
    assert pcm_to_frame["orphan_end"] == 1


def test_overlapping_starts_consume_ambiguous_ends_without_samples() -> None:
    lines = [
        log_line("2026-11-10T00:00:00.000000000Z", "doubao_session_started"),
        log_line("2026-11-10T00:00:00.100000000Z", "doubao_session_started"),
        log_line("2026-11-10T00:00:00.400000000Z", "doubao_first_pcm"),
        log_line("2026-11-10T00:00:00.500000000Z", "doubao_first_pcm"),
    ]

    report = analyze_lines(lines)
    tts = report["segments"]["doubao_session_started->doubao_first_pcm"]

    assert tts["samples"] == 0
    assert tts["ambiguous"] == 2
    assert tts["missing_end"] == 0
    assert tts["orphan_end"] == 0
    assert report["_segment_values"]["doubao_session_started->doubao_first_pcm"] == ()
    assert tts["p50"] is None
    assert tts["p90"] is None
    assert tts["max"] is None


def test_timestamp_out_of_order_is_counted_and_discarded() -> None:
    lines = [
        log_line("2026-11-10T00:00:00.000000000Z", "first_phrase_ready"),
        log_line("2026-11-10T00:00:00.200000000Z", "doubao_session_started"),
        log_line("2026-11-10T00:00:00.100000000Z", "doubao_first_pcm"),
    ]

    report = analyze_lines(lines)
    tts = report["segments"]["doubao_session_started->doubao_first_pcm"]

    assert tts["samples"] == 0
    assert tts["out_of_order"] == 1
    assert tts["discarded_rounds"] == 1


def test_cross_run_aggregation_and_missing_log(tmp_path: Path) -> None:
    first = tmp_path / "run-one"
    second = tmp_path / "run-two"
    first.mkdir()
    second.mkdir()
    (first / "bridge.log").write_text(
        "".join(
            [
                log_line("2026-11-10T00:00:00.000000000Z", "first_phrase_ready"),
                log_line("2026-11-10T00:00:00.100000000Z", "doubao_session_started"),
                log_line("2026-11-10T00:00:00.400000000Z", "doubao_first_pcm"),
                log_line("2026-11-10T00:00:00.450000000Z", "first_frame_sent"),
            ]
        ),
        encoding="utf-8",
    )
    (second / "bridge.log").write_text(
        "".join(
            [
                log_line("2026-11-10T00:01:00.000000000Z", "first_phrase_ready"),
                log_line("2026-11-10T00:01:00.200000000Z", "doubao_session_started"),
                log_line("2026-11-10T00:01:00.500000000Z", "doubao_first_pcm"),
                log_line("2026-11-10T00:01:00.600000000Z", "first_frame_sent"),
            ]
        ),
        encoding="utf-8",
    )

    report = analyze_paths([first, second, tmp_path / "missing-run"])

    assert report["overall"]["run_count"] == 2
    assert report["overall"]["segments"][
        "doubao_session_started->doubao_first_pcm"
    ]["samples"] == 2
    assert len(report["skipped"]) == 1


def test_json_cli_does_not_require_devicetest_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/first_packet_latency.py",
            "--json",
            str(tmp_path / "missing-run"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["overall"]["run_count"] == 0
    assert payload["skipped"][0]["reason"] == "missing log file"
