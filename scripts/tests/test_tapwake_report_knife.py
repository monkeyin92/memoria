"""Card 3 knife aggregates: ASR segment is asr_final→commit, compared to soak-232237."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "devicetest"))
from tapwake_report import (  # noqa: E402
    BASELINE_232237_ASR_FINAL_TO_COMMIT_S,
    segment_aggregates,
)


def test_segment_aggregates_report_asr_final_to_commit_against_232237_baseline() -> None:
    turns = [
        {
            "kind": "reply",
            "segments_s": {"收音": 0.4, "ASR": 0.91, "LLM": 0.1, "TTS": 0.4, "下发播放": 0.2},
            "卡在哪段": "ASR",
        },
        {
            "kind": "reply",
            "segments_s": {"收音": 0.5, "ASR": 1.18, "LLM": 0.2, "TTS": 0.4, "下发播放": 0.2},
            "卡在哪段": "ASR",
        },
        {
            "kind": "reply",
            "segments_s": {"收音": 0.3, "ASR": 1.31, "LLM": 0.5, "TTS": 0.4, "下发播放": 0.2},
            "卡在哪段": "LLM",
        },
        {
            "kind": "no_reply",
            "segments_s": {"收音": "缺", "ASR": "缺", "LLM": "缺", "TTS": "缺", "下发播放": "缺"},
            "卡在哪段": "缺",
        },
        {
            "kind": "reply",
            "segments_s": {"收音": 0.2, "ASR": "缺", "LLM": "缺", "TTS": "缺", "下发播放": "缺"},
            "卡在哪段": "收音",
        },
    ]

    knife = segment_aggregates(turns)

    assert knife["asr_final_to_commit_field"] == "ASR"
    assert knife["asr_final_to_commit_means"] == "asr_final→commit via final_to_done_ms"
    assert knife["baseline_232237_asr_final_to_commit_s"] == BASELINE_232237_ASR_FINAL_TO_COMMIT_S
    asr = knife["this_run_asr_final_to_commit_s"]
    assert asr["n"] == 3
    assert asr["p50_s"] == 1.18
    assert asr["p90_s"] == 1.31
    assert asr["max_s"] == 1.31
    assert knife["worst_segment_counts"] == {"ASR": 2, "LLM": 1, "收音": 1}
    assert knife["segments_s"]["收音"]["n"] == 4
