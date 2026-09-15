"""Tests for the read-only voice session timing report.

Fixtures mirror the real capture formats: local +08:00 serial timestamps with an
ESP-IDF uptime counter, docker log timestamps with nanosecond precision, and a
second media session inside one capture.  Every counterexample asserts the exact
positive wording the report must print, so a counterexample cannot pass merely
because some weak substring is absent.  No device, network or production state is
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import voice_session_report as report

SESSION_A = "7d9d3a2f-51ea-4fbf-a427-d1c9f92950b9"
SESSION_B = "e81ff828-0e2b-4795-a3cf-f90b195aaec0"


def _device_line(stamp: str, body: str, uptime: int = 1000) -> str:
    return f"[{stamp}] I ({uptime}) {body}\r\n"


def _vad(stamp: str, edge: str, sample: int = 12800, rms: str = "0.0005") -> str:
    return _device_line(stamp, f"MemoriaProtocol: Device VAD {edge} at sample={sample} rms={rms}")


def _frame(stamp: str, generation: int, seq: int = 0) -> str:
    return _device_line(
        stamp, f"MemoriaProtocol: First playable downlink frame generation={generation} seq={seq}"
    )


def _state(stamp: str, source: str, destination: str, uptime: int = 1000) -> str:
    return _device_line(stamp, f"StateMachine: State: {source} -> {destination}", uptime)


def _supply_wait(
    stamp: str, generation: int = 3, wait_ms: int = 1377, supply_waits: int = 1
) -> str:
    return _device_line(
        stamp,
        "audio_service: media playback supply wait layer=playout_queue "
        "scope=software_queue_wait "
        f"generation={generation} wait_ms={wait_ms} supply_waits={supply_waits}",
    )


def _supply_summary(stamp: str, generation: int = 3, close: str = "generation_switch") -> str:
    return _device_line(
        stamp,
        "audio_service: media playback supply summary layer=playout_queue "
        "scope=software_queue_wait "
        f"generation={generation} close={close} output_frames=91 first_output=yes "
        "first_output_latency_ms=412 supply_waits=2 supply_max_ms=1377 supply_total_ms=1500 "
        "prestart_waits=1 prestart_max_ms=1377 boundary_waits=0 boundary_max_ms=0 "
        "close_dropped_waits=0 close_dropped_ms=0 outside_waits=0 outside_max_ms=0 "
        "exact_confirmed=91 exact_timeouts=0 exact_polls=91",
    )


def _legacy_meter(stamp: str, generation: int = 3, gap_ms: int = 1377, count: int = 1) -> str:
    return _device_line(
        stamp,
        "audio_service: media playback starved "
        f"generation={generation} gap_ms={gap_ms} starved_count={count}",
    )


def _delivery(
    stamp: str,
    session: str,
    event: str,
    *,
    epoch: int = 1,
    turn: int = 1,
    generation: int = 1,
    tool: int = 0,
    terminal: str = "",
    terminal_reason: str = "",
) -> str:
    return (
        f"{stamp} INFO:services.agent.src.voice_core.media_session_output_dispatch:media reply "
        f"delivery session={session} delivery_id={session}/epoch-{epoch}/turn-{turn}/"
        f"generation-{generation}/tool-{tool} event={event} terminal={terminal} "
        f"terminal_reason={terminal_reason} first_frame_sent=True provider_completed=True "
        "playback_ended=True actual_heard=True\n"
    )


def _commit(
    stamp: str,
    session: str,
    *,
    session_epoch: int = 1,
    stream_epoch: int = 1947,
    turn: int = 1,
    generation: int = 1,
    tool: int = 0,
) -> str:
    return (
        f"{stamp} INFO:services.agent.src.voice_core.media_session_commit:media turn committed "
        f"session={session} session_epoch={session_epoch} stream_epoch={stream_epoch} "
        f"turn_id={turn} generation_id={generation} tool_epoch={tool}\n"
    )


def _legacy_phase(
    stamp: str, session: str, *, turn: int = 1, generation: int = 1, cause: str = "turn_committed"
) -> str:
    return (
        f"{stamp} INFO:services.agent.src.duplex_runtime:interaction_phase from=listening "
        f"to=thinking_silent cause={cause} session_id={session} turn_id={turn} "
        f"generation_id={generation}\n"
    )


def _agent_commit(stamp: str, *, turn: int = 1, generation: int = 1, tool_epoch: int = 0) -> str:
    return (
        f"{stamp} INFO:services.agent.src.agent:turn_committed turn_id={turn} "
        f"generation_id={generation} tool_epoch={tool_epoch} text_len=10\n"
    )


def _pacing(
    stamp: str,
    session: str,
    *,
    session_epoch: int | None = 1,
    stream_epoch: int | None = 1950,
    turn: int | None = 2,
    generation: int | None = 3,
    tool: int | None = 0,
    ratio: str = "0.99",
    ratio_key: str = "produced_ratio",
    measurement: bool = False,
    wall_ms: str = "1836",
    extra: str = "",
) -> str:
    parts = []
    if measurement:
        parts.append("measurement=post_pacer_send")
    parts.append(f"session={session}")
    if session_epoch is not None:
        parts.append(f"session_epoch={session_epoch}")
    if stream_epoch is not None:
        parts.append(f"stream_epoch={stream_epoch}")
    if turn is not None:
        parts.append(f"turn_id={turn}")
    if generation is not None:
        parts.append(f"generation_id={generation}")
    if tool is not None:
        parts.append(f"tool_epoch={tool}")
    parts += [
        "reason=final_frame",
        "frames=91",
        "audio_ms=1820",
        f"wall_ms={wall_ms}",
        "max_gap_ms=67",
    ]
    if ratio_key != "absent":
        parts.append(f"{ratio_key}={ratio}")
    parts.append("queue_high_water=1")
    if extra:
        parts.append(extra)
    return (
        f"{stamp} INFO:services.agent.src.voice_core.media_bridge_server:media downlink pacing "
        f"{' '.join(parts)}\n"
    )


def _close(
    stamp: str, session: str, *, epoch: int = 1947, reason: str = "owner_silence_timeout"
) -> str:
    return (
        f"{stamp} 2026/09/14 10:25:49 media edge projected conversation close session={session} "
        f"device=dev_atk_a4cb8fd6095c epoch={epoch} reason={reason} control_sequence=10\n"
    )


def _capture(
    root: Path,
    *,
    serial: str = "",
    bridge: str = "",
    edge: str = "",
    capture_json: dict[str, object] | None = None,
) -> Path:
    run = root / "capture"
    run.mkdir(parents=True)
    if serial:
        (run / "serial.log").write_text(serial, encoding="utf-8")
    if bridge:
        (run / "bridge.log").write_text(bridge, encoding="utf-8")
    if edge:
        (run / "edge.log").write_text(edge, encoding="utf-8")
    if capture_json is not None:
        (run / "capture.json").write_text(json.dumps(capture_json), encoding="utf-8")
    return run


def test_two_sessions_in_one_capture_are_never_merged(tmp_path: Path, capsys) -> None:
    bridge = (
        _commit("2026-09-14T10:25:33.000000000Z", SESSION_A, turn=2, generation=2)
        + _delivery(
            "2026-09-14T10:25:34.000000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
        )
        + _delivery(
            "2026-09-14T10:25:36.000000000Z", SESSION_A, "playback_ended", turn=2, generation=2
        )
        + _delivery(
            "2026-09-14T10:25:37.000000000Z", SESSION_A, "first_frame_sent", turn=2, generation=3
        )
        + _commit("2026-09-14T10:28:44.000000000Z", SESSION_B, turn=2, generation=2)
        + _delivery(
            "2026-09-14T10:28:44.500000000Z", SESSION_B, "first_frame_sent", turn=2, generation=2
        )
        + _delivery(
            "2026-09-14T10:28:47.000000000Z", SESSION_B, "playback_ended", turn=2, generation=2
        )
        + _delivery(
            "2026-09-14T10:28:51.000000000Z", SESSION_B, "first_frame_sent", turn=2, generation=3
        )
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert f"-- session {SESSION_A}" in out
    assert f"-- session {SESSION_B}" in out
    assert "commit->first_frame_sent=1.000s" in out
    assert "commit->first_frame_sent=0.500s" in out
    assert "gen 2->3: 1.000s" in out
    assert "gen 2->3: 4.000s" in out
    assert "11.500s" not in out


def test_legacy_phase_commit_is_never_used_for_timing(tmp_path: Path, capsys) -> None:
    bridge = _legacy_phase(
        "2026-09-14T10:25:33.000000000Z", SESSION_A, turn=2, generation=2
    ) + _delivery(
        "2026-09-14T10:25:34.007000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "commit->first_frame_sent=unknown (no complete matching media turn committed)" in out
    assert (
        "partial commit evidence (listed only, never used for timing or delivery binding): 1" in out
    )
    assert f"source=legacy PHASE session={SESSION_A} turn_id=2 generation_id=2" in out
    assert "commit->first_frame_sent=1.007s" not in out


def test_agent_commit_without_session_identity_is_never_used(tmp_path: Path, capsys) -> None:
    bridge = _agent_commit("2026-09-14T10:25:33.555000000Z", turn=2, generation=2) + _delivery(
        "2026-09-14T10:25:34.007000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "commit->first_frame_sent=unknown (no complete matching media turn committed)" in out
    assert "source=agent turn_committed turn_id=2 generation_id=2 tool_epoch=0 " in out
    assert "commit->first_frame_sent=0.452s" not in out


def test_complete_commit_without_delivery_is_not_observed_not_never_arrived(
    tmp_path: Path, capsys
) -> None:
    bridge = _commit("2026-09-14T10:25:49.358000000Z", SESSION_A, turn=3, generation=4) + _delivery(
        "2026-09-14T10:25:34.007000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        f"session {SESSION_A} epoch 1 turn 3 gen 4 tool 0: committed 10:25:49.358 but no delivery "
        "carries this full key (delivery not observed in this capture; device arrival is unknown)"
    ) in out
    assert "never reached the device" not in out


def test_duplicate_complete_commits_leave_timing_unknown(tmp_path: Path, capsys) -> None:
    bridge = (
        _commit("2026-09-14T10:25:33.000000000Z", SESSION_A, turn=2, generation=2)
        + _commit("2026-09-14T10:25:33.400000000Z", SESSION_A, turn=2, generation=2)
        + _delivery(
            "2026-09-14T10:25:34.000000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
        )
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        "commit->first_frame_sent=unknown (2 complete commits share this full key; duplicate "
        "commit evidence is ambiguous)" in out
    )
    assert "commit->first_frame_sent=1.000s" not in out


def test_same_turn_and_generation_in_two_epochs_stay_separate(tmp_path: Path, capsys) -> None:
    bridge = (
        _commit("2026-09-14T10:25:33.000000000Z", SESSION_A, session_epoch=1, turn=2, generation=2)
        + _delivery(
            "2026-09-14T10:25:34.000000000Z",
            SESSION_A,
            "first_frame_sent",
            epoch=1,
            turn=2,
            generation=2,
        )
        + _delivery(
            "2026-09-14T10:25:44.000000000Z",
            SESSION_A,
            "first_frame_sent",
            epoch=2,
            turn=2,
            generation=2,
        )
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert f"session {SESSION_A} epoch 1 turn 2 gen 2 tool 0" in out
    assert f"session {SESSION_A} epoch 2 turn 2 gen 2 tool 0" in out
    assert "commit->first_frame_sent=1.000s" in out
    assert "commit->first_frame_sent=unknown (no complete matching media turn committed)" in out


def test_non_normal_event_is_not_reported_as_a_missing_log(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T10:25:30.000000000Z",
        SESSION_A,
        "preempted",
        turn=2,
        generation=2,
        terminal="preempted",
        terminal_reason="pre_first_frame",
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        "missing=first_frame_sent,provider_completed,actual_heard,playback_ended "
        "[not a log gap: non-normal event on this delivery]" in out
    )
    assert "non_normal_event_observed=true (preempted)" in out
    assert "event=preempted terminal=preempted terminal_reason=pre_first_frame" in out


def test_missing_receipts_leave_gaps_and_coverage_unknown(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T10:25:34.000000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
    ) + _delivery(
        "2026-09-14T10:25:37.000000000Z", SESSION_A, "first_frame_sent", turn=2, generation=3
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        "gen 2->3: unknown (playback_ended not observed in this capture; "
        "terminal state not recorded)" in out
    )
    assert f"session {SESSION_A} epoch 1 turn 2 tool 0: 2 delivery(ies)" in out


def test_out_of_order_generation_is_not_a_negative_silence_gap(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T10:00:10.000000000Z", SESSION_A, "playback_ended", turn=2, generation=2
    ) + _delivery(
        "2026-09-14T10:00:09.500000000Z", SESSION_A, "first_frame_sent", turn=2, generation=3
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "gen 2->3: out_of_order (-0.500s" in out
    assert "not a silence gap" in out


def test_duplicate_delivery_lines_are_deduplicated(tmp_path: Path, capsys) -> None:
    bridge = (
        _commit("2026-09-14T10:00:00.000000000Z", SESSION_A, turn=2, generation=2)
        + _delivery(
            "2026-09-14T10:00:00.100000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
        )
        + _delivery(
            "2026-09-14T10:00:00.100000000Z", SESSION_A, "first_frame_sent", turn=2, generation=2
        )
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "commit->first_frame_sent=0.100s" in out
    assert "note: ignored 1 duplicate delivery event line(s)" in out


def test_device_reconnect_resets_anchors_instead_of_a_false_latency(tmp_path: Path, capsys) -> None:
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _state("2026-09-14T18:00:02.000+08:00", "connecting", "listening")
        + _vad("2026-09-14T18:00:10.000+08:00", "end")
        + _frame("2026-09-14T18:00:10.500+08:00", 1)
        + _state("2026-09-14T18:00:12.000+08:00", "speaking", "listening")
        + _state("2026-09-14T18:00:20.000+08:00", "listening", "idle")
        + _state("2026-09-14T18:03:00.000+08:00", "idle", "connecting")
        + _state("2026-09-14T18:03:03.000+08:00", "connecting", "listening")
        + _frame("2026-09-14T18:03:03.500+08:00", 1)
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "vad_end->first_received=0.500s" in out
    assert "unattributable (no unconsumed Device VAD end in this segment)" in out
    assert "173." not in out
    assert out.count("device started a new media session") == 2


def test_device_frame_is_receive_side_and_long_gaps_are_not_hidden(tmp_path: Path, capsys) -> None:
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _vad("2026-09-14T18:00:10.000+08:00", "end")
        + _frame("2026-09-14T18:00:14.200+08:00", 1)
        + _vad("2026-09-14T18:00:20.000+08:00", "end")
        + _frame("2026-09-14T18:00:19.500+08:00", 2)
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "vad_end->first_received=4.200s" in out
    assert "over 1.5s receive-side target" in out
    assert "audible=not_measured" in out
    assert (
        "frame accepted at the device media boundary, a receive-side fact, not audible output."
        in out
    )
    assert "vad_end->first_received=-0.500s [negative/out-of-order: not a latency]" in out


def test_unconsumed_device_vad_end_is_retained_not_discarded(tmp_path: Path, capsys) -> None:
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _vad("2026-09-14T18:00:05.000+08:00", "end")
        + _vad("2026-09-14T18:00:09.000+08:00", "end")
        + _frame("2026-09-14T18:00:09.400+08:00", 1)
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "vad_end_count=2 locally_paired=1 unconsumed=1" in out
    assert "(unconsumed VAD ends are retained as unknown, not silently discarded)" in out
    assert (
        "unconsumed vad.end at 18:00:05.000: no first_received observed in this device segment"
        in out
    )


def test_new_supply_wait_and_summary_have_correct_evidence_scope(tmp_path: Path, capsys) -> None:
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _supply_wait("2026-09-14T18:00:11.000+08:00")
        + _supply_summary("2026-09-14T18:00:11.400+08:00", close="channel_flush")
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "supply.supply_wait" in out
    assert "generation=3 wait_ms=1377 supply_waits=1" in out
    assert (
        "producer_format=software_queue_wait_v2 evidence_scope=device software queue only; "
        "not DMA underrun, not audible" in out
    )
    assert "supply.supply_summary" in out
    assert "close=channel_flush" in out
    assert "close=eos" not in out
    assert "close=cancel" not in out
    assert "first_output_latency_ms=412" in out


def test_legacy_meter_fields_are_labelled_with_their_own_scope(tmp_path: Path, capsys) -> None:
    serial = _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting") + _legacy_meter(
        "2026-09-14T18:00:11.000+08:00", gap_ms=5857, count=1
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "legacy_starved" in out
    assert "gap_ms=5857 starved_count=1" in out
    assert (
        "producer_format=legacy_playback_meter evidence_scope=device software queue only; "
        "not DMA underrun, not audible" in out
    )


def test_legacy_pacing_line_is_labelled_as_an_after_pacer_send_ratio(
    tmp_path: Path, capsys
) -> None:
    # The historical producer line carried no session_epoch/stream_epoch/turn_id/
    # generation_id/tool_epoch at all, only the session and the media metrics.
    run = _capture(
        tmp_path,
        bridge=_pacing(
            "2026-09-14T11:11:52.263000000Z",
            SESSION_A,
            session_epoch=None,
            stream_epoch=None,
            turn=None,
            generation=None,
            tool=None,
        ),
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out
    block = out.split("== downlink pacing")[1].split("== delivery ledger receipts")[0]

    assert "after_pacer_send_ratio=0.99" in block
    assert f"session={SESSION_A}" in block
    assert "producer_format=legacy_produced_ratio" in block
    assert "legacy produced_ratio kept as an after-pacer sender field" in block
    assert "bound_to_delivery=unknown (partial identity; missing session_epoch" in block
    assert "no audio/DAC/listener inference" in block


def test_new_pacing_fields_are_bound_by_the_complete_key(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T11:11:52.379000000Z", SESSION_A, "playback_ended", turn=2, generation=3
    ) + _pacing(
        "2026-09-14T11:11:52.263000000Z",
        SESSION_A,
        session_epoch=1,
        stream_epoch=1950,
        turn=2,
        generation=3,
        tool=0,
        ratio="1.00",
        ratio_key="send_audio_ratio",
        measurement=True,
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "measurement=post_pacer_send" in out
    assert "producer_format=post_pacer_send_v2" in out
    assert "session_epoch=1 stream_epoch=1950 turn_id=2 generation_id=3 tool_epoch=0" in out
    assert "after_pacer_send_ratio=1.00" in out
    assert (
        f"bound_to_delivery=session {SESSION_A} epoch 1 turn 2 gen 3 tool 0 "
        "(session_epoch compared exactly)" in out
    )
    assert "legacy_produced_ratio" not in out


def test_pacing_with_a_wrong_session_epoch_does_not_bind(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T11:11:52.379000000Z",
        SESSION_A,
        "playback_ended",
        epoch=1,
        turn=2,
        generation=3,
    ) + _pacing(
        "2026-09-14T11:11:52.263000000Z",
        SESSION_A,
        session_epoch=2,
        stream_epoch=1950,
        turn=2,
        generation=3,
        ratio="1.00",
        ratio_key="send_audio_ratio",
        measurement=True,
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "session_epoch=2" in out
    assert (
        f"bound_to_delivery=unknown (no delivery with complete key session {SESSION_A} epoch 2 "
        "turn 2 gen 3 tool 0)" in out
    )


def test_pacing_complete_key_that_matches_nothing_is_unknown(tmp_path: Path, capsys) -> None:
    run = _capture(
        tmp_path,
        bridge=_pacing(
            "2026-09-14T11:11:52.263000000Z",
            SESSION_A,
            session_epoch=1,
            stream_epoch=1950,
            turn=2,
            generation=9,
            ratio_key="send_audio_ratio",
            measurement=True,
        ),
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        f"bound_to_delivery=unknown (no delivery with complete key session {SESSION_A} epoch 1 "
        "turn 2 gen 9 tool 0)" in out
    )


def test_pacing_ratio_is_unknown_when_a_single_frame_names_no_rate(tmp_path: Path, capsys) -> None:
    run = _capture(
        tmp_path,
        bridge=_pacing(
            "2026-09-14T11:11:52.263000000Z",
            SESSION_A,
            ratio="1.00",
            ratio_key="send_audio_ratio",
            wall_ms="0",
        ),
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "after_pacer_send_ratio=unknown (wall_ms=0: a single frame names no rate)" in out
    assert "wall_ms=0 overrides any ratio field on this line" in out
    assert "after_pacer_send_ratio=1.00" not in out


def test_pacing_nonfinite_ratio_is_unknown(tmp_path: Path, capsys) -> None:
    run = _capture(
        tmp_path,
        bridge=_pacing(
            "2026-09-14T11:11:52.263000000Z",
            SESSION_A,
            ratio="nan",
            ratio_key="send_audio_ratio",
        ),
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "after_pacer_send_ratio=unknown (ratio field absent or not a finite number)" in out


def test_renamed_ratio_wins_and_unknown_fields_are_passed_through(tmp_path: Path, capsys) -> None:
    run = _capture(
        tmp_path,
        bridge=_pacing(
            "2026-09-14T11:11:52.263000000Z",
            SESSION_A,
            ratio="1.00",
            ratio_key="send_audio_ratio",
            extra="produced_ratio=0.50 future_field=7",
        ),
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "after_pacer_send_ratio=1.00" in out
    assert "legacy produced_ratio kept" not in out
    assert "produced_ratio=0.50" in out
    assert "future_field=7" in out


def test_farewell_uses_an_explicit_stream_to_media_binding(tmp_path: Path, capsys) -> None:
    bridge = _commit(
        "2026-09-14T10:25:33.000000000Z",
        SESSION_A,
        session_epoch=1,
        stream_epoch=1947,
        turn=2,
        generation=3,
    ) + _delivery(
        "2026-09-14T10:25:42.000000000Z", SESSION_A, "playback_ended", turn=2, generation=3
    )
    edge = _close("2026-09-14T10:25:49.000000000Z", SESSION_A, epoch=1947)
    run = _capture(tmp_path, bridge=bridge, edge=edge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert f"session {SESSION_A}: 7.000s (reason=owner_silence_timeout" in out
    assert "device_stream_epoch=1947" in out

    other = _capture(tmp_path / "second", bridge=bridge)
    assert report.main([str(other)]) == 0
    out = capsys.readouterr().out
    assert "no conversation close in this capture; farewell gap unknown" in out


def test_close_without_a_binding_is_unknown_and_never_max_paired(tmp_path: Path, capsys) -> None:
    # Two playback_ended receipts exist in the session, but nothing binds the
    # device stream epoch to a media epoch, so neither may be picked.
    bridge = _delivery(
        "2026-09-14T10:25:42.000000000Z",
        SESSION_A,
        "playback_ended",
        epoch=1,
        turn=2,
        generation=3,
    ) + _delivery(
        "2026-09-14T10:29:30.000000000Z",
        SESSION_A,
        "playback_ended",
        epoch=2,
        turn=2,
        generation=3,
    )
    edge = _close("2026-09-14T10:30:00.000000000Z", SESSION_A, epoch=1947)
    run = _capture(tmp_path, bridge=bridge, edge=edge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        "gap=unknown (no explicit device_stream_epoch=1947 -> media epoch binding in this capture "
        "(stream and media epochs are separate namespaces; not paired by proximity))" in out
    )


def test_many_to_many_stream_binding_is_unknown(tmp_path: Path, capsys) -> None:
    # The same device stream epoch is named by two complete keys in two different
    # media epochs, so no single delivery can be chosen for the close.
    bridge = (
        _commit(
            "2026-09-14T10:25:33.000000000Z",
            SESSION_A,
            session_epoch=1,
            stream_epoch=1947,
            turn=2,
            generation=3,
        )
        + _commit(
            "2026-09-14T10:26:33.000000000Z",
            SESSION_A,
            session_epoch=2,
            stream_epoch=1947,
            turn=2,
            generation=3,
        )
        + _delivery(
            "2026-09-14T10:25:42.000000000Z",
            SESSION_A,
            "playback_ended",
            epoch=1,
            turn=2,
            generation=3,
        )
        + _delivery(
            "2026-09-14T10:26:42.000000000Z",
            SESSION_A,
            "playback_ended",
            epoch=2,
            turn=2,
            generation=3,
        )
    )
    edge = _close("2026-09-14T10:30:00.000000000Z", SESSION_A, epoch=1947)
    run = _capture(tmp_path, bridge=bridge, edge=edge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        "gap=unknown (device_stream_epoch=1947 maps to 2 delivery key(s) across 2 media "
        "epoch(s); many-to-many binding, not resolvable)" in out
    )


def test_pacing_tool_epoch_mismatch_is_unknown_not_bound(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T11:11:52.379000000Z",
        SESSION_A,
        "playback_ended",
        epoch=1,
        turn=2,
        generation=3,
        tool=0,
    ) + _pacing(
        "2026-09-14T11:11:52.263000000Z",
        SESSION_A,
        session_epoch=1,
        stream_epoch=1950,
        turn=2,
        generation=3,
        tool=9,
        ratio="1.00",
        ratio_key="send_audio_ratio",
        measurement=True,
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert (
        f"bound_to_delivery=unknown (no delivery with complete key session {SESSION_A} epoch 1 "
        "turn 2 gen 3 tool 9)" in out
    )
    assert f"bound_to_delivery=session {SESSION_A} epoch 1 turn 2 gen 3 tool 0" not in out


def test_legacy_capture_receipt_is_marked_unbound(tmp_path: Path, capsys) -> None:
    receipt_a = {
        "release_head": "e1c6998c7e7c569db91aeeabf2375364d0dbea17",
        "candidate_app_sha256": "b" * 64,
        "candidate_elf_sha256": "c" * 64,
        "verified_at": "2026-09-14T15:06:45+08:00",
    }
    receipt_b = {
        "release_head": "fa54d7de027cccaee35e1721762a5d0bb060d60c",
        "candidate_app_sha256": "f" * 64,
        "candidate_elf_sha256": "e" * 64,
        "verified_at": "2026-09-14T19:08:51+08:00",
    }
    run = _capture(
        tmp_path, capture_json={"git_head": "96f58aed", "flash_readback_verification": receipt_a}
    )
    supplied = tmp_path / "postflash.json"
    supplied.write_text(json.dumps(receipt_b), encoding="utf-8")

    assert report.main([str(run), "--firmware-receipt", str(supplied)]) == 0
    out = capsys.readouterr().out
    assert "capture source revision: git_head=96f58aed" in out
    assert "capture firmware receipt (source flash_readback_verification)" in out
    assert (
        "capture_receipt_binding=unbound_legacy_record (no path/sha256/"
        "read_from_board_this_run; not binding evidence and not a live read)" in out
    )
    assert "MISMATCH: capture.json records a different flash receipt than --firmware-receipt" in out
    assert (
        "release_head: capture=e1c6998c7e7c569db91aeeabf2375364d0dbea17 "
        "supplied=fa54d7de027cccaee35e1721762a5d0bb060d60c" in out
    )
    assert "treat this capture's firmware identity as unverified" in out


def test_capture_receipt_with_path_and_sha_is_reported_as_bound(tmp_path: Path, capsys) -> None:
    receipt = {
        "release_head": "fa54d7de027cccaee35e1721762a5d0bb060d60c",
        "candidate_app_sha256": "f" * 64,
        "candidate_elf_sha256": "e" * 64,
        "identity_sha256": "a" * 64,
        "verified_at": "2026-09-14T19:08:51+08:00",
        "path": "/evidence/postflash.json",
        "sha256": "d" * 64,
        "read_from_board_this_run": False,
    }
    run = _capture(
        tmp_path,
        capture_json={"source_revision": {"git_head": "0" * 40}, "firmware_receipt": receipt},
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "capture_receipt_binding=bound_by_capture_tool" in out
    assert "read of the board's on-chip version" in out


def test_missing_logs_are_noted_and_empty_input_still_exits_zero(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert report.main([str(empty)]) == 0
    out = capsys.readouterr().out
    assert "serial.log has no parseable device lines" in out
    assert "bridge.log is missing or empty" in out
    assert "no media reply delivery events found" in out


def test_non_directory_argument_is_rejected(tmp_path: Path, capsys) -> None:
    target = tmp_path / "bridge.log"
    target.write_text("", encoding="utf-8")
    assert report.main([str(target)]) == 2
    assert "is not a capture directory" in capsys.readouterr().out


def test_capture_without_a_completion_record_is_reported_incomplete(tmp_path: Path, capsys) -> None:
    # The exact shape of a capture whose tool stopped before it could finalize:
    # no completed_at_local, no exit reason, capture_status still in_progress.
    run = _capture(
        tmp_path,
        capture_json={
            "capture_mode": "live",
            "capture_status": "in_progress",
            "started_at_local": "2026-09-15T16:15:43.000000+08:00",
            "serial_opened": True,
            "log_stream_health": "healthy",
        },
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "== capture integrity (capture tool lifecycle record)" in out
    assert "capture_integrity=incomplete" in out
    assert (
        "reason: no completion record (capture_status='in_progress' and completed_at_local=None): "
        "the capture tool did not reach finalization; why it stopped is not recorded and is not "
        "inferred here" in out
    )
    assert "why" in out and "it stopped is unknown here and is not inferred" in out
    assert "capture_status=in_progress capture_mode=live" in out
    assert "completed_at_local=None" in out
    assert "capture_integrity=completed" not in out
    assert "capture_integrity=degraded" not in out


def test_a_capture_whose_record_is_missing_any_completion_field_is_not_completed(
    tmp_path: Path, capsys
) -> None:
    no_timestamp = _capture(
        tmp_path / "a",
        capture_json={"capture_mode": "live", "capture_status": "completed", "serial_opened": True},
    )
    unknown_status = _capture(
        tmp_path / "b",
        capture_json={
            "capture_mode": "live",
            "capture_status": "finishing",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "serial_opened": True,
        },
    )
    requested_but_silent = _capture(
        tmp_path / "c",
        capture_json={
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "serial_opened": True,
            "server_logs_requested": True,
            "log_stream_health": "not_requested",
        },
    )

    assert report.main([str(no_timestamp)]) == 0
    out = capsys.readouterr().out
    assert "capture_integrity=incomplete" in out
    assert "capture_status='completed' and completed_at_local=None" in out
    assert "capture_integrity=completed" not in out

    assert report.main([str(unknown_status)]) == 0
    out = capsys.readouterr().out
    assert "capture_integrity=incomplete" in out
    assert "capture_status='finishing'" in out

    assert report.main([str(requested_but_silent)]) == 0
    out = capsys.readouterr().out
    assert "capture_integrity=degraded" in out
    assert (
        "reason: server logs were requested but the capture did not record all three streams as "
        "healthy (log_stream_health='not_requested')" in out
    )
    assert "capture_integrity=completed" not in out


def test_a_degraded_capture_without_usable_reasons_stays_degraded(tmp_path: Path, capsys) -> None:
    # A missing or empty reason list must not turn a degraded capture back into completed.
    no_reasons = _capture(
        tmp_path / "a",
        capture_json={
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "serial_opened": True,
            "log_stream_health": "degraded",
            "log_stream_health_reasons": None,
        },
    )
    empty_reasons = _capture(
        tmp_path / "b",
        capture_json={
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "serial_opened": True,
            "log_stream_health": "degraded",
            "log_stream_health_reasons": [],
        },
    )

    for run in (no_reasons, empty_reasons):
        assert report.main([str(run)]) == 0
        out = capsys.readouterr().out
        assert "capture_integrity=degraded" in out
        assert "reason: log_stream_health=degraded without a recorded reason" in out
        assert "capture_integrity=completed" not in out


@pytest.mark.parametrize("timestamp", ["", "not-a-date", False, 123, [], {}])
def test_invalid_completion_timestamp_never_proves_completion(timestamp):
    verdict, _ = report._capture_integrity(
        {
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": timestamp,
            "log_stream_health": "not_requested",
        }
    )
    assert verdict == "incomplete"


@pytest.mark.parametrize("defect", ["missing", "failed", "forced", "unknown_exit", "none"])
def test_healthy_label_cannot_hide_missing_or_failed_stream_records(defect):
    streams = {
        label: {"status": "stopped_by_capture", "exit_code": -15, "forced_kill": False}
        for label in ("bridge", "agent", "edge")
    }
    if defect == "missing":
        del streams["bridge"]
    elif defect == "failed":
        streams["bridge"]["status"] = "failed"
    elif defect == "forced":
        streams["bridge"]["forced_kill"] = True
    elif defect == "unknown_exit":
        streams["bridge"]["exit_code"] = None
    else:
        streams = None
    verdict, reasons = report._capture_integrity(
        {
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42+08:00",
            "server_logs_requested": True,
            "log_stream_health": "healthy",
            "log_streams": streams,
        }
    )
    assert verdict == "degraded"
    assert any("bridge" in reason for reason in reasons)


def test_healthy_capture_requires_and_accepts_every_stream_record():
    verdict, reasons = report._capture_integrity(
        {
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42+08:00",
            "server_logs_requested": True,
            "log_stream_health": "healthy",
            "log_streams": {
                label: {"status": "stopped_by_capture", "exit_code": -15, "forced_kill": False}
                for label in ("bridge", "agent", "edge")
            },
        }
    )
    assert (verdict, reasons) == ("completed", [])


def _healthy_stream_records() -> dict[str, object]:
    return {
        label: {"status": "stopped_by_capture", "exit_code": -15, "forced_kill": False}
        for label in ("bridge", "agent", "edge")
    }


def _healthy_completion(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "capture_mode": "live",
        "capture_status": "completed",
        "completed_at_local": "2026-09-15T16:31:42+08:00",
        "server_logs_requested": True,
        "log_stream_health": "healthy",
        "log_streams": _healthy_stream_records(),
    }
    payload.update(overrides)
    return payload


def test_a_completion_timestamp_must_be_a_parseable_non_empty_string() -> None:
    # A blank or unparseable timestamp is a label without a record, and a real one is
    # read as written: nothing here is inferred from capture_status alone.
    def verdict(timestamp: object) -> str:
        return report._capture_integrity(
            _healthy_completion(
                completed_at_local=timestamp,
                server_logs_requested=False,
                log_stream_health="not_requested",
            )
        )[0]

    for timestamp in ("   ", "2026-09-15T16:31:42+08:00 ", "2026-13-45T99:99:99+08:00"):
        assert verdict(timestamp) == "incomplete"
    assert verdict("2026-09-15 16:31:42+08:00") == "completed"


@pytest.mark.parametrize(
    "streams",
    [None, [], "stopped_by_capture", {"bridge": "stopped"}],
)
def test_a_healthy_label_without_usable_per_stream_records_names_the_stream(streams) -> None:
    verdict, reasons = report._capture_integrity(_healthy_completion(log_streams=streams))

    assert verdict == "degraded"
    assert any("bridge" in reason for reason in reasons)


def test_a_healthy_label_is_verified_even_when_the_requested_flag_is_absent() -> None:
    # capture_status=completed + log_stream_health=healthy with no server_logs_requested and
    # no records is still a claim about three streams nobody recorded: the label is never
    # trusted on its own, whichever field carries the request.
    without_flag = _healthy_completion()
    del without_flag["server_logs_requested"]
    del without_flag["log_streams"]

    verdict, reasons = report._capture_integrity(without_flag)
    assert verdict == "degraded"
    assert any("bridge" in reason for reason in reasons)

    no_records_but_claimed = {**_healthy_completion(), "log_streams": None}
    del no_records_but_claimed["server_logs_requested"]
    verdict, reasons = report._capture_integrity(no_records_but_claimed)
    assert verdict == "degraded"
    assert any("bridge" in reason for reason in reasons)

    recorded = _healthy_completion()
    del recorded["server_logs_requested"]
    assert report._capture_integrity(recorded) == ("completed", [])


@pytest.mark.parametrize("health", [None, "not_requested", "degraded"])
def test_requested_streams_are_named_even_without_a_healthy_label(health) -> None:
    verdict, reasons = report._capture_integrity(
        _healthy_completion(log_stream_health=health, log_streams={"agent": {}})
    )
    assert verdict == "degraded"
    for label in ("bridge", "agent", "edge"):
        assert any(f"log stream {label}" in reason for reason in reasons)


@pytest.mark.parametrize("forced", [0, 1, "false"])
def test_invalid_forced_kill_flags_do_not_assert_that_sigkill_happened(forced) -> None:
    streams = _healthy_stream_records()
    streams["bridge"]["forced_kill"] = forced
    verdict, reasons = report._capture_integrity(_healthy_completion(log_streams=streams))
    assert verdict == "degraded"
    assert any(
        "log stream bridge does not record forced_kill=False" in reason for reason in reasons
    )
    assert not any("needed SIGKILL" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("bridge", "expected"),
    [
        (
            {
                "status": "stopped_by_capture",
                "exit_code": -15,
                "forced_kill": True,
                "forced_reason": "SIGTERM timed out",
            },
            "log stream bridge needed SIGKILL to be reclaimed (SIGTERM timed out)",
        ),
        (
            {"status": "stopped_by_capture", "exit_code": -15},
            "log stream bridge does not record forced_kill=False (forced_kill=None)",
        ),
        (
            {"status": "stopped_by_capture", "exit_code": True, "forced_kill": False},
            "log stream bridge stopped_by_capture without a real integer exit code "
            "(exit_code=True)",
        ),
        (
            {"status": "exited_early", "exit_code": 0, "forced_kill": False},
            "log stream bridge exited_early (exit=0)",
        ),
        (
            {"status": "failed", "exit_code": 3, "forced_kill": False, "error": "OSError: gone"},
            "log stream bridge failed (exit=3) error=OSError: gone",
        ),
    ],
)
def test_a_defective_bridge_record_under_a_healthy_label_is_degraded_and_named(
    bridge: dict[str, object], expected: str
) -> None:
    verdict, reasons = report._capture_integrity(
        _healthy_completion(log_streams={**_healthy_stream_records(), "bridge": bridge})
    )

    assert verdict == "degraded"
    assert expected in reasons
    # The two healthy siblings are never blamed for the one defective record.
    assert not any("agent" in reason or "edge" in reason for reason in reasons)


def test_a_healthy_stream_record_needs_no_matching_log_file_on_disk(tmp_path: Path, capsys) -> None:
    # The records are the capture tool's own metadata.  This report never opens
    # bridge.log/agent.log/edge.log to re-derive health, so a complete record set is
    # accepted as such even when no stream file was copied into the directory.
    run = _capture(
        tmp_path,
        capture_json={**_healthy_completion(), "serial_opened": True},
    )
    assert not (run / "bridge.log").exists()

    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "capture_integrity=completed" in out
    assert "capture_integrity=degraded" not in out


def test_a_healthy_lifecycle_record_does_not_vouch_for_the_firmware_receipt(
    tmp_path: Path, capsys
) -> None:
    # The two dimensions are reported independently: an unbound legacy receipt does not
    # degrade a finished capture, and a finished capture does not bind that receipt.
    run = _capture(
        tmp_path,
        capture_json={
            **_healthy_completion(),
            "firmware_receipt": {
                "release_head": "96f58aed",
                "verified_at": "2026-09-14T19:08:51+08:00",
            },
        },
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "capture_integrity=completed" in out
    assert "capture_receipt_binding=unbound_legacy_record" in out
    assert out.index("capture_integrity=completed") < out.index("capture_receipt_binding=")


def test_degraded_capture_is_reported_with_reasons_and_a_bound_receipt_stays_separate(
    tmp_path: Path, capsys
) -> None:
    receipt = {
        "release_head": "fa54d7de027cccaee35e1721762a5d0bb060d60c",
        "candidate_app_sha256": "f" * 64,
        "candidate_elf_sha256": "e" * 64,
        "identity_sha256": "a" * 64,
        "verified_at": "2026-09-14T19:08:51+08:00",
        "path": "/evidence/postflash.json",
        "sha256": "d" * 64,
        "read_from_board_this_run": False,
    }
    run = _capture(
        tmp_path,
        capture_json={
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "exit_reason": "duration_elapsed",
            "serial_opened": True,
            "serial_error": None,
            "cleanup_errors": ["closing the serial port: OSError: port flush failed"],
            "log_stream_health": "degraded",
            "log_stream_health_reasons": [
                "log stream bridge not_started (exit=None)",
                "cleanup closing the serial port: OSError: port flush failed",
            ],
            "firmware_receipt": receipt,
        },
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "capture_integrity=degraded" in out
    assert "reason: log stream bridge not_started (exit=None)" in out
    assert "reason: cleanup closing the serial port: OSError: port flush failed" in out
    # The same cleanup failure is not listed twice, and it never reads as completed.
    assert out.count("reason: cleanup closing the serial port") == 1
    assert "cleanup_errors=1" in out
    assert "capture_integrity=completed" not in out
    # A bound receipt is evidence about the flash, not about this capture's life.
    assert "capture_receipt_binding=bound_by_capture_tool" in out
    assert out.index("capture_integrity=degraded") < out.index(
        "capture_receipt_binding=bound_by_capture_tool"
    )


def test_a_recorded_stop_signal_is_reported_without_claiming_the_session_ended(
    tmp_path: Path, capsys
) -> None:
    run = _capture(
        tmp_path,
        capture_json={
            "capture_mode": "live",
            "capture_status": "completed",
            "completed_at_local": "2026-09-15T16:31:42.000000+08:00",
            "stop_signal": "SIGHUP",
            "stop_signal_number": 1,
            "stop_requested_at_local": "2026-09-15T16:31:41.000000+08:00",
            "stop_signal_handlers": ["SIGHUP", "SIGQUIT", "SIGINT", "SIGTERM"],
            "exit_reason": "stop_signal SIGHUP",
            "cleanup_errors": [],
            "log_stream_health": "not_requested",
        },
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "capture_integrity=completed" in out
    assert "exit_reason=stop_signal SIGHUP stop_signal=SIGHUP stop_signal_number=1" in out
    assert "stop_signal_handlers=SIGHUP,SIGQUIT,SIGINT,SIGTERM" in out
    assert "the first stop signal is recorded verbatim (name, number, time)" in out
    assert "does not interpret who or what sent it" in out
    assert "completed means the tool reached finalization, not that the capture window" in out
    assert "elapsed or that the session was healthy or audible" in out
    assert "terminal or ssh session went away" not in out
    assert "capture_integrity=incomplete" not in out
    assert "capture_integrity=degraded" not in out


def test_legacy_and_preflight_capture_json_are_not_called_completed_blindly(
    tmp_path: Path, capsys
) -> None:
    legacy_unfinished = _capture(tmp_path / "a", capture_json={"git_head": "96f58aed"})
    legacy_finished = _capture(
        tmp_path / "b",
        capture_json={"git_head": "96f58aed", "completed_at_local": "2026-09-14T19:08:51+08:00"},
    )
    preflight = _capture(
        tmp_path / "c", capture_json={"capture_mode": "preflight_only", "serial_opened": False}
    )
    missing = tmp_path / "d"
    missing.mkdir()

    assert report.main([str(legacy_unfinished)]) == 0
    out = capsys.readouterr().out
    assert "capture_integrity=incomplete" in out
    assert "no completion record (capture.json predates capture_status" in out
    assert "completed_at_local=None, which is not a completion record" in out

    assert report.main([str(legacy_finished)]) == 0
    out = capsys.readouterr().out
    # Legacy fields are never promoted: only a tool-written completion record counts.
    assert "capture_integrity=incomplete" in out
    assert "capture_integrity=completed" not in out

    assert report.main([str(preflight)]) == 0
    out = capsys.readouterr().out
    assert "capture_status=None capture_mode=preflight_only" in out
    assert "capture_integrity=completed" in out
    assert "preflight_only never opens the serial port" in out

    assert report.main([str(missing)]) == 0
    out = capsys.readouterr().out
    assert "capture.json: missing" in out
    assert "capture_integrity=incomplete" in out
    assert "no lifecycle record (capture.json is missing, unreadable or not a JSON object)" in out


def test_device_boot_keeps_one_segment_for_startup_transitions(tmp_path: Path, capsys) -> None:
    serial = (
        "[2026-09-14T18:24:59.396+08:00] ESP-ROM:esp32s3-20210327\r\n"
        "[2026-09-14T18:24:59.396+08:00] rst:0x15 (USB_UART_CHIP_RESET),boot:0x2b\r\n"
        + _state("2026-09-14T18:24:59.500+08:00", "unknown", "starting", uptime=53)
        + _state("2026-09-14T18:25:09.562+08:00", "starting", "activating", uptime=10000)
        + _state("2026-09-14T18:25:10.774+08:00", "activating", "idle", uptime=11000)
        + _state("2026-09-14T18:25:22.007+08:00", "idle", "connecting", uptime=22000)
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "-- device segment 1: line 1: device boot" in out
    assert "-- device segment 2: line 6: device started a new media session" in out
    assert "device uptime counter reset" not in out


def test_multiple_supply_summaries_for_one_generation_are_kept_in_order(
    tmp_path: Path, capsys
) -> None:
    # The firmware may summarise one generation more than once after a flush, so
    # every summary must survive with its own close reason and original order.
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _supply_summary("2026-09-14T18:00:11.000+08:00", close="channel_flush")
        + _supply_summary("2026-09-14T18:00:11.400+08:00", close="decoder_reset")
        + _supply_summary("2026-09-14T18:00:12.000+08:00", close="channel_flush")
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert out.count("supply.supply_summary") == 3
    first = out.index("close=channel_flush")
    second = out.index("close=decoder_reset")
    third = out.index("close=channel_flush", first + 1)
    assert first < second < third
    assert (
        "(each episode is kept in log order; a generation flushed more than once keeps every "
        "summary): gen 3=3" in out
    )


def test_a_dropped_supply_line_is_never_counted_as_a_metering_episode(
    tmp_path: Path, capsys
) -> None:
    # The producer's logger can report its own dropped line ("media playback supply
    # summary dropped: buffer too small").  Such a line matches the episode prefix but
    # names no generation, so it must never be counted as a supply/meter episode.
    serial = (
        _state("2026-09-14T18:00:00.000+08:00", "idle", "connecting")
        + _device_line(
            "2026-09-14T18:00:11.000+08:00",
            "audio_service: media playback supply summary dropped: buffer too small pending=9",
        )
        + _device_line(
            "2026-09-14T18:00:11.200+08:00",
            "audio_service: media playback starved dropped: buffer too small pending=9",
        )
        + _supply_summary("2026-09-14T18:00:11.400+08:00", close="channel_flush")
    )
    run = _capture(tmp_path, serial=serial)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert out.count("supply.") == 1
    assert "supply.supply_summary" in out
    assert "gen ?" not in out
    assert "summary): gen 3=1" in out
    timeline = out.split("== device timeline")[1].split("== server deliveries")[0]
    assert timeline.count("metering_notice") == 2
    assert timeline.count("supply.supply_summary") == 1
    # Only the real episode carries a producer format claim.
    assert out.count("producer_format=software_queue_wait_v2") == 1
    assert "producer_format=legacy_playback_meter" not in out
    assert (
        "note: serial.log has 2 'media playback ...' line(s) with no numeric generation= field; "
        "they are shown as metering_notice lines and are never counted as supply or legacy meter "
        "episodes: line 2, line 3" in out
    )


def test_byte_identical_device_lines_are_kept_and_noted(tmp_path: Path, capsys) -> None:
    # Dropping a repeated line could merge two real episodes, so repeats are kept
    # and only reported.
    line = _supply_summary("2026-09-14T18:00:11.000+08:00", close="channel_flush")
    run = _capture(
        tmp_path, serial=_state("2026-09-14T18:00:00.000+08:00", "idle", "connecting") + line + line
    )
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert out.count("supply.supply_summary") == 2
    assert "summary): gen 3=2" in out
    assert (
        "note: serial.log has 1 byte-identical repeated line(s), kept in original order and not "
        "merged: line 3 repeats line 2" in out
    )


def test_producer_skipped_session_closed_terminal_is_reported_verbatim(
    tmp_path: Path, capsys
) -> None:
    bridge = _delivery(
        "2026-09-14T10:25:30.000000000Z",
        SESSION_A,
        "skipped",
        turn=2,
        generation=2,
        terminal="skipped",
        terminal_reason="session_closed",
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "event=skipped terminal=skipped terminal_reason=session_closed" in out
    assert "non_normal_event_observed=true (skipped)" in out
    assert "[not a log gap: non-normal event on this delivery]" in out


def test_producer_error_terminal_is_flagged_non_normal(tmp_path: Path, capsys) -> None:
    bridge = _delivery(
        "2026-09-14T10:25:30.000000000Z",
        SESSION_A,
        "error",
        turn=2,
        generation=2,
        terminal="error",
        terminal_reason="transport_write_failed",
    )
    run = _capture(tmp_path, bridge=bridge)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert "event=error terminal=error terminal_reason=transport_write_failed" in out
    assert "non_normal_event_observed=true (error)" in out


def test_full_fence_timing_matches_the_producer_literals(tmp_path: Path, capsys) -> None:
    session = "99522b56-7bcf-435e-aa3f-98956e1834da"
    commit = (
        f"2026-09-14T11:11:05.000000000Z INFO:services.agent.src.voice_core.media_session_commit:"
        f"media turn committed session={session} session_epoch=1 stream_epoch=1950 turn_id=1 "
        "generation_id=1 tool_epoch=0\n"
    )
    first_frame = (
        f"2026-09-14T11:11:06.084999242Z INFO:services.agent.src.voice_core."
        f"media_session_output_dispatch:media reply delivery session={session} "
        f"delivery_id={session}/epoch-1/turn-1/generation-1/tool-0 event=first_frame_sent "
        "terminal= terminal_reason= first_frame_sent=True provider_completed=False "
        "playback_ended=False actual_heard=False\n"
    )
    pacing = (
        "2026-09-14T11:11:07.920551349Z INFO:services.agent.src.voice_core.media_bridge_server:"
        "media downlink pacing measurement=post_pacer_send "
        f"session={session} session_epoch=1 stream_epoch=1950 turn_id=1 generation_id=1 "
        "tool_epoch=0 reason=final_frame frames=91 audio_ms=1820 wall_ms=1836 max_gap_ms=67 "
        "send_audio_ratio=0.99 queue_high_water=1\n"
    )
    playback_ended = (
        f"2026-09-14T11:11:08.027686999Z INFO:services.agent.src.voice_core."
        f"media_session_output_dispatch:media reply delivery session={session} "
        f"delivery_id={session}/epoch-1/turn-1/generation-1/tool-0 event=playback_ended "
        "terminal=playback_ended terminal_reason=playback_completed first_frame_sent=True "
        "provider_completed=True playback_ended=True actual_heard=True\n"
    )
    close = (
        "2026-09-14T11:11:10.000000000Z 2026/09/14 19:11:10 media edge projected conversation "
        f"close session={session} device=dev_atk_a4cb8fd6095c epoch=1950 "
        "reason=owner_silence_timeout control_sequence=10\n"
    )
    run = _capture(tmp_path, bridge=commit + first_frame + pacing + playback_ended, edge=close)
    assert report.main([str(run)]) == 0
    out = capsys.readouterr().out

    assert f"session {session} epoch 1 turn 1 gen 1 tool 0" in out
    assert "commit->first_frame_sent=1.085s" in out
    assert "producer_format=post_pacer_send_v2" in out
    assert "after_pacer_send_ratio=0.99" in out
    assert (
        f"bound_to_delivery=session {session} epoch 1 turn 1 gen 1 tool 0 "
        "(session_epoch compared exactly)" in out
    )
    assert (
        f"session {session}: 1.972s (reason=owner_silence_timeout, "
        "device=dev_atk_a4cb8fd6095c, device_stream_epoch=1950)" in out
    )
