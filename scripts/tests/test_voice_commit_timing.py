"""The commit/classifier timing summary (offline: synthetic bridge log lines in the real format)."""

from __future__ import annotations

from pathlib import Path

from scripts import voice_commit_timing as timing

_PREFIX = "2026-10-03T08:00:00.000000000Z INFO:services.agent.src.voice_core.media_session_commit:"


def _commit(session: str, result: str, **fields: int | str) -> str:
    values = {
        "since_endpoint_ms": 1400,
        "final_to_start_ms": 1250,
        "final_to_done_ms": 1500,
        "lock_ms": 0,
        "speaker_ms": 0,
        "close_ms": 2,
        "prepare_ms": 230,
        "commit_ms": 250,
    } | fields
    body = " ".join(f"{key}={value}" for key, value in values.items())
    return f"{_PREFIX}media turn commit timing session={session} stream_epoch=1 result={result} {body}"


def _call(kind: str, outcome: str, ms: int, verdict: str = "False") -> str:
    return (
        "2026-10-03T08:00:00Z INFO:services.agent.src.classifier_inflight:"
        f"classifier call kind={kind} outcome={outcome} verdict={verdict} duration_ms={ms}"
    )


def _wait(kind: str, source: str, ms: int) -> str:
    return (
        "2026-10-03T08:00:00Z INFO:services.agent.src.classifier_inflight:"
        f"classifier verdict wait kind={kind} source={source} waited_ms={ms}"
    )


def test_summary_splits_commits_calls_and_waits() -> None:
    lines = [
        "unrelated line without any marker",
        _commit("aaaa1111", "committed", final_to_done_ms=1200),
        _commit("aaaa1111", "committed", final_to_done_ms=3100, prepare_ms=900),
        _commit("bbbb2222", "assistant_echo", final_to_done_ms=2000, prepare_ms="-"),
        _call("live_lookup", "ok", 180),
        _call("live_lookup", "ok", 650),
        _call("conversation_close", "error", 1210, verdict="None"),
        _wait("conversation_close", "joined", 30),
        _wait("conversation_close", "called", 640),
    ]

    report = timing.summarize(lines)

    assert "== turn commits n=3  results: committed=2, assistant_echo=1" in report
    assert "final_to_done_ms   n=3 p50=2000 p90=3100 max=3100" in report
    assert "prepare_ms         n=2 p50=230 p90=900 max=900" in report  # the "-" row is not a number
    assert "live_lookup" in report and "n=2 p50=180 p90=650 max=650  outcomes: ok=2" in report
    assert "conversation_close" in report and "outcomes: error=1" in report
    assert "conversation_close   joined  n=1 p50=30 p90=30 max=30  waited>=50ms: 0" in report
    assert "conversation_close   called  n=1 p50=640 p90=640 max=640  waited>=50ms: 1" in report


def test_session_filter_and_empty_input() -> None:
    lines = [_commit("aaaa1111", "committed"), _commit("bbbb2222", "committed")]

    assert "turn commits n=1" in timing.summarize(lines, session="bbbb")
    empty = timing.summarize([])
    assert "turn commits n=0" in empty and "(none)" in empty


def test_main_reads_a_log_file(tmp_path: Path, capsys: object) -> None:
    log = tmp_path / "bridge.log"
    log.write_text(_commit("aaaa1111", "committed") + "\n", encoding="utf-8")

    assert timing.main([str(log)]) == 0
    assert "turn commits n=1" in capsys.readouterr().out  # type: ignore[attr-defined]
