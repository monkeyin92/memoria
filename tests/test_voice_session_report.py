"""Tests for the read-only voice session timing report.

Fixtures mirror the real capture formats: local +08:00 serial timestamps and
Docker log timestamps with nanosecond precision.  No device, network or
production state is touched.
"""

from __future__ import annotations

from pathlib import Path

from scripts import voice_session_report as report

SERIAL = """\
[2026-09-14T15:11:05.362+08:00] I (232457) StateMachine: State: connecting -> listening
[2026-09-14T15:11:06.093+08:00] I (233197) MemoriaProtocol: Device VAD start at sample=0 rms=0.0000
[2026-09-14T15:11:06.872+08:00] I (233967) MemoriaProtocol: Device VAD end at sample=12800 rms=0.0005
[2026-09-14T15:11:07.568+08:00] I (234667) MemoriaProtocol: First playable downlink frame generation=1 seq=0
[2026-09-14T15:11:07.568+08:00] I (234667) StateMachine: State: listening -> speaking
[2026-09-14T15:11:09.112+08:00] I (236207) StateMachine: State: speaking -> listening
[2026-09-14T15:11:14.927+08:00] I (242027) MemoriaProtocol: First playable downlink frame generation=2 seq=0
"""

BRIDGE = """\
2026-09-14T07:11:14.538227599Z INFO:services.agent.src.agent:turn_committed turn_id=2 generation_id=2 tool_epoch=0 text_len=10
2026-09-14T07:11:14.912395194Z INFO:services.agent.src.voice_core.media_session_output_dispatch:media reply delivery session=s delivery_id=s/epoch-1/turn-2/generation-2/tool-0 event=first_frame_sent terminal= terminal_reason= first_frame_sent=True provider_completed=False playback_ended=False actual_heard=False
2026-09-14T07:11:17.868562019Z INFO:services.agent.src.voice_core.media_session_output_dispatch:media reply delivery session=s delivery_id=s/epoch-1/turn-2/generation-2/tool-0 event=playback_ended terminal=playback_ended terminal_reason=playback_completed first_frame_sent=True provider_completed=True playback_ended=True actual_heard=True
2026-09-14T07:11:18.270652584Z INFO:services.agent.src.voice_core.media_session_output_dispatch:media reply delivery session=s delivery_id=s/epoch-1/turn-2/generation-3/tool-0 event=first_frame_sent terminal= terminal_reason= first_frame_sent=True provider_completed=False playback_ended=False actual_heard=False
"""


def _capture(root: Path, *, bridge: str = BRIDGE) -> Path:
    run = root / "capture"
    run.mkdir()
    (run / "serial.log").write_text(SERIAL, encoding="utf-8")
    (run / "bridge.log").write_text(bridge, encoding="utf-8")
    return run


def test_device_gap_uses_a_fresh_vad_end_and_marks_stale_ones(tmp_path: Path, capsys) -> None:
    run = _capture(tmp_path)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    # generation 1 follows the VAD end by 696ms and is a real measurement
    assert "said->audible=0.696s" in out
    # generation 2 is 8s after that same VAD end: an unrelated earlier event, not a latency
    assert "said->audible=stale(8.055s)" in out
    assert "said->audible=8.055s" not in out


def test_server_chain_reports_commit_to_frame_and_generation_gap(tmp_path: Path, capsys) -> None:
    run = _capture(tmp_path)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    # nanosecond timestamps must survive parsing
    assert "turn 2 gen 2: turn_committed first_frame_sent" in out
    assert "commit->frame=0.374s" in out
    assert "turn 2 gen 2->3: 0.402s" in out
    assert "turn 2: 2 generation(s)" in out


def test_missing_actual_heard_and_absent_stage_logs_are_reported(tmp_path: Path, capsys) -> None:
    run = _capture(tmp_path, bridge=BRIDGE.splitlines(keepends=True)[1])
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out
    assert "turn 2 gen 2" in out
    assert "turn 2 gen 2->3" not in out

    empty = tmp_path / "empty"
    empty.mkdir()
    assert report.main([str(empty)]) == 0
    out = capsys.readouterr().out
    assert "note: serial.log has no parseable events" in out
    assert "note: no media reply delivery events found" in out
