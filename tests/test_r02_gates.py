from __future__ import annotations

import json
from pathlib import Path

from devicetest import r02_gates


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    run = tmp_path / "run"
    run.mkdir(parents=True)
    (run / "timeline.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return run


def _stop_rows(delay: float = 0.4) -> list[dict]:
    rows = []
    for index, (slug, _word) in enumerate(r02_gates.STOP_WORDS, 1):
        for trial in range(1, 11):
            rows.append(
                {
                    "kind": "reply",
                    "tag": f"stop{index:02d}-{slug}-{trial:02d}",
                    "stop_verdict": "stopped",
                    "stop_delay_from_speech_end_s": delay,
                    "stop_reason": "spoken_stop_and_device_playout",
                }
            )
    return rows


def _back_rows() -> list[dict]:
    rows = []
    for index, (slug, _word) in enumerate(r02_gates.BACKCHANNEL_WORDS, 1):
        for trial in range(1, 11):
            rows.append(
                {
                    "kind": "reply",
                    "tag": f"back{index:02d}-{slug}-{trial:02d}",
                    "stop_verdict": "not_stopped",
                    "still_speaking_after_interrupt": True,
                    "stop_reason": "playback_continued",
                }
            )
    return rows


def _content_rows(stopped: int = 10, stop_delay: float = 0.6, answer_delay: float = 1.2) -> list[dict]:
    rows = []
    for trial in range(1, 11):
        is_stopped = trial <= stopped
        rows.append(
            {
                "kind": "reply",
                "tag": f"content-{trial:02d}",
                "stop_verdict": "stopped" if is_stopped else "not_stopped",
                "stop_delay_from_speech_end_s": stop_delay if is_stopped else None,
                "next_first_syllable_s": answer_delay if is_stopped else None,
                "stop_reason": "spoken_stop_and_device_playout" if is_stopped else "playback_continued",
            }
        )
    return rows


def test_stop_suite_passes_at_ten_of_ten_under_the_p90(tmp_path: Path) -> None:
    summary = r02_gates.reduce_stop(r02_gates._rows(_write(tmp_path, _stop_rows(0.4))))
    assert summary["pass"] is True
    assert all(row["stopped"] == 10 and row["scorable"] == 10 for row in summary["rows"])


def test_stop_suite_fails_when_a_word_lands_a_stop_too_late(tmp_path: Path) -> None:
    rows = _stop_rows(0.4)
    rows[0]["stop_delay_from_speech_end_s"] = 0.95
    rows[1]["stop_delay_from_speech_end_s"] = 0.95
    summary = r02_gates.reduce_stop(r02_gates._rows(_write(tmp_path, rows)))
    assert summary["pass"] is False
    assert summary["rows"][0]["p90_speech_end_s"] > r02_gates.STOP_P90_LIMIT_S


def test_stop_suite_reports_unscorable_attempts_instead_of_passing(tmp_path: Path) -> None:
    rows = _stop_rows(0.4)
    rows[0]["stop_reason"] = "interrupt_too_late_to_stop"
    rows[0]["stop_verdict"] = "unverified"
    summary = r02_gates.reduce_stop(r02_gates._rows(_write(tmp_path, rows)))
    assert summary["pass"] is False
    assert summary["rows"][0]["too_late"] == 1
    assert summary["rows"][0]["scorable"] == 9


def test_backchannel_suite_fails_on_any_false_stop(tmp_path: Path) -> None:
    rows = _back_rows()
    rows[3]["stop_verdict"] = "stopped"
    rows[3]["still_speaking_after_interrupt"] = False
    summary = r02_gates.reduce_backchannel(r02_gates._rows(_write(tmp_path, rows)))
    assert summary["pass"] is False
    assert summary["rows"][0]["false_stops"] == 1


def test_backchannel_suite_passes_when_all_words_keep_playing(tmp_path: Path) -> None:
    summary = r02_gates.reduce_backchannel(r02_gates._rows(_write(tmp_path, _back_rows())))
    assert summary["pass"] is True


def test_content_suite_needs_nine_stops_and_both_latency_lines(tmp_path: Path) -> None:
    passing = r02_gates.reduce_content(r02_gates._rows(_write(tmp_path, _content_rows(stopped=9))))
    assert passing["pass"] is True
    slow = r02_gates.reduce_content(r02_gates._rows(_write(tmp_path / "slow", _content_rows(stopped=10, answer_delay=2.4))))
    assert slow["pass"] is False
    short = r02_gates.reduce_content(r02_gates._rows(_write(tmp_path / "short", _content_rows(stopped=8))))
    assert short["pass"] is False


def _crisis_rows(stopped: int = 0, quiet: int = 5) -> list[dict]:
    rows = []
    for trial in range(1, 6):
        is_stopped = trial <= stopped
        rows.append(
            {
                "kind": "reply",
                "tag": f"crisis-{trial:02d}",
                "stop_verdict": "stopped" if is_stopped else "not_stopped",
                "still_speaking_after_interrupt": not is_stopped,
                "quiet_after_reply": trial <= quiet,
                "stop_reason": "playback_continued" if not is_stopped else "spoken_stop_and_device_playout",
            }
        )
    return rows


def test_crisis_suite_keeps_the_crisis_reply_playing(tmp_path: Path) -> None:
    passing = r02_gates.reduce_crisis(r02_gates._rows(_write(tmp_path, _crisis_rows())))
    assert passing["pass"] is True
    stopped = r02_gates.reduce_crisis(r02_gates._rows(_write(tmp_path / "stopped", _crisis_rows(stopped=1))))
    assert stopped["pass"] is False
    noisy = r02_gates.reduce_crisis(r02_gates._rows(_write(tmp_path / "noisy", _crisis_rows(quiet=4))))
    assert noisy["pass"] is False
