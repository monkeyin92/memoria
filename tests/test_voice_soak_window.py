"""Offline checks for a device-window soak: the existing sentence sheet, the log verdict, and the log pack.

No serial port, speaker, SSH, or archive database.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts import voice_soak_pack, voice_soak_verdict

ROOT = Path(__file__).parents[1]
BASE = datetime(2026, 10, 10, 0, 5, tzinfo=UTC).timestamp()
DELIVERY = "session-a/epoch-1/turn-2/generation-3/tool-0"


def test_child_scenario_is_the_dial_sheet_and_already_covers_twenty() -> None:
    steps = json.loads(
        (ROOT / "scripts/voice_soak_scenarios/child.json").read_text(encoding="utf-8")
    )
    by_tag = {step["tag"]: step for step in steps}
    assert len(steps) >= 20
    assert {"hello", "why-sky", "dino", "story-stop", "long-stop", "silence", "stranger", "bad-word", "bullied"} <= set(by_tag)
    for tag in ("story-stop", "long-stop"):
        assert by_tag[tag]["interrupt"]["say"]
    assert "say" not in by_tag["silence"]
    assert by_tag["silence"]["expect_idle_wait"] > 0
    for tag in ("hello", "why-sky", "dino", "stranger", "bad-word", "bullied"):
        assert isinstance(by_tag[tag]["say"], str) and by_tag[tag]["say"].strip()


def _write(
    run: Path,
    rows: list[dict[str, object]],
    bridge: str = "",
    serial: str = "",
    *,
    manifest: dict[str, object] | None = None,
) -> None:
    run.mkdir()
    (run / "timeline.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    if bridge:
        (run / "bridge.log").write_text(bridge, encoding="utf-8")
    if serial:
        (run / "serial.log").write_text(serial, encoding="utf-8")
    if manifest is not None:
        (run / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _manifest(
    *tags: str,
    required_logs: tuple[str, ...] = (),
    min_valid_interrupt_samples: int = 0,
    scenario_hash: str = "test-fixture",
) -> dict[str, object]:
    return voice_soak_verdict.manifest_dict(
        scenario_hash=scenario_hash,
        expected_step_tags=tags,
        required_logs=required_logs,
        min_valid_interrupt_samples=min_valid_interrupt_samples,
    )


def _tail(**extra: object) -> dict[str, object]:
    return {"kind": "post_scenario_observation", "verdict": "verified_idle", **extra}


def _delivery(seconds: float, event: str, terminal: str = "", reason: str = "") -> str:
    at = datetime.fromtimestamp(BASE + seconds, UTC).isoformat().replace("+00:00", "Z")
    return (
        f"{at} INFO:media reply delivery session=session-a delivery_id={DELIVERY} "
        f"event={event} terminal={terminal} terminal_reason={reason} "
        "first_frame_sent=True provider_completed=True playback_ended=True actual_heard=True\n"
    )


def _serial(seconds: float, message: str) -> str:
    clock = datetime.fromtimestamp(BASE + seconds, timezone(timedelta(hours=8)))
    return f"{clock:%H:%M:%S.%f} I (100) {message}\n"


def _confirmed_stop_logs() -> tuple[str, str]:
    at = datetime.fromtimestamp(BASE + 5.01, UTC).isoformat().replace("+00:00", "Z")
    bridge = (
        _delivery(-0.1, "first_frame_sent")
        + _delivery(5, "preempted", "preempted", "superseded")
        + (
            f"{at} INFO:media spoken stop interrupted reply session=session-a "
            "turn=2 generation=3 replacement_generation=4 flush=True\n"
        )
    )
    serial = _serial(
        0, "MemoriaProtocol: First playable downlink frame generation=3 seq=0"
    ) + _serial(
        5.1,
        "AudioService: media playback supply summary generation=3 close=generation_switch "
        "output_frames=250 first_output=yes",
    )
    return bridge, serial


def _passing_rows() -> list[dict[str, object]]:
    return [
        {"kind": "said", "tag": "t001", "text": "停", "ts": BASE - 1},
        {
            "kind": "reply",
            "tag": "t001",
            "latency_s": 1.25,
            "interrupted": True,
            "stopped": False,
            "reply_started_ts": BASE,
            "interrupt_start_ts": BASE + 3,
            "interrupt_end_ts": BASE + 4,
            "interrupt_deadline_ts": BASE + 12,
            "device_returned_ts": BASE + 5.1,
            "serial_utc_offset_s": 8 * 3600,
        },
        {"kind": "silence", "tag": "t002", "went_idle": True},
        _tail(),
    ]


def _passing_manifest() -> dict[str, object]:
    return _manifest(
        "t001",
        "t002",
        required_logs=("bridge.log", "serial.log"),
        min_valid_interrupt_samples=1,
    )


def test_a_run_with_a_confirmed_stop_and_idle_tail_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bridge, serial = _confirmed_stop_logs()
    run = tmp_path / "run-pass"
    _write(run, _passing_rows(), bridge, serial, manifest=_passing_manifest())
    assert voice_soak_verdict.main([str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert report["status"] == "pass"
    assert report["failures"] == []
    assert report["no_reply"] == 0
    assert report["reply_latency_p90_s"] == 1.25
    assert report["gates"] == {
        "max_no_reply": 0,
        "max_reply_latency_p90_s": 2.0,
        "max_reply_latency_max_s": 3.0,
    }


def test_a_claimed_stop_without_log_evidence_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-unverified"
    _write(run, _passing_rows(), manifest=_manifest("t001", "t002"))
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == ["stop_not_confirmed: t001 unverified"]


def test_required_logs_missing_is_incomplete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-missing-logs"
    _write(
        run,
        _passing_rows(),
        manifest=_manifest(
            "t001",
            "t002",
            required_logs=("bridge.log", "serial.log"),
            min_valid_interrupt_samples=1,
        ),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["status"] == "incomplete"
    assert "incomplete: missing_log: bridge.log" in report["failures"]
    assert "incomplete: missing_log: serial.log" in report["failures"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("{", id="broken-json"),
        pytest.param('{"scenario_hash": "abc"}', id="partial"),
    ],
)
def test_a00_one_reply_idle_without_a_usable_manifest_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str | None
) -> None:
    run = tmp_path / "run-a00-incomplete"
    _write(run, [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()])
    if body is not None:
        (run / "manifest.json").write_text(body, encoding="utf-8")
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["status"] == "incomplete"
    assert report["failures"]
    assert all(item.startswith("incomplete:") for item in report["failures"])
    assert not any(
        item.startswith(("reply_latency", "tail_idle", "no_reply:", "stop_not"))
        for item in report["failures"]
    )
    if body is None:
        assert report["failures"] == ["incomplete: manifest missing"]


def test_manifest_missing_expected_step_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-missing-step"
    _write(
        run,
        [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()],
        manifest=_manifest("t001", "t002"),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: missing_step: t002"]


def test_manifest_requiring_interrupt_sample_without_one_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-no-interrupt"
    _write(
        run,
        [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()],
        manifest=_manifest("t001", min_valid_interrupt_samples=1),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: valid_interrupt_samples: 0 < 1"]


@pytest.mark.parametrize("content", [None, "", "\n", "   "])
def test_required_bridge_log_missing_or_blank_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    run = tmp_path / "run-blank-log"
    rows = [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()]
    _write(run, rows)
    voice_soak_verdict.write_manifest(
        run,
        scenario_hash="test-fixture",
        expected_step_tags=("t001",),
        required_logs=("bridge.log",),
    )
    if content is not None:
        (run / "bridge.log").write_text(content, encoding="utf-8")
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: missing_log: bridge.log"]


def test_log_path_outside_the_run_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "bridge.log"
    outside.write_text("bridge\n", encoding="utf-8")
    run = tmp_path / "run-escape"
    _write(run, [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()])
    voice_soak_verdict.write_manifest(
        run,
        scenario_hash="test-fixture",
        expected_step_tags=("t001",),
        required_logs=("../bridge.log",),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["passed"] is False
    assert report["failures"] == ["incomplete: missing_log: ../bridge.log"]


def test_only_unverifiable_interrupt_misses_the_sample_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, serial = _confirmed_stop_logs()
    bridge = _delivery(-0.1, "first_frame_sent") + _delivery(
        5, "playback_ended", "playback_ended", "playback_completed"
    )
    rows = _passing_rows()
    rows[1]["interrupt_start_ts"] = BASE + 5 - 2.0
    run = tmp_path / "run-unverifiable-interrupt"
    _write(
        run,
        rows,
        bridge,
        serial,
        manifest=_manifest(
            "t001",
            "t002",
            required_logs=("bridge.log", "serial.log"),
            min_valid_interrupt_samples=1,
        ),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: valid_interrupt_samples: 0 < 1"]


def test_step_failed_without_speech_or_silence_is_not_started(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-not-started"
    _write(
        run,
        [
            {"kind": "step_failed", "tag": "t001", "reason": "could not wake the device"},
            _tail(),
        ],
        manifest=_manifest("t001"),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: run_not_started"]


def test_missing_completion_skips_the_tail_metric(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-not-completed"
    _write(
        run,
        [{"kind": "reply", "tag": "t001", "latency_s": 1.0}],
        manifest=_manifest("t001"),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: run_not_completed"]


def test_scenario_done_counts_as_completed_and_the_tail_metric_still_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-scenario-done"
    _write(
        run,
        [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, {"kind": "scenario_done"}],
        manifest=_manifest("t001"),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert "tail_idle: missing" in report["failures"]
    assert all(not item.startswith("incomplete:") for item in report["failures"])


def test_short_duration_and_missing_timestamps_are_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short = tmp_path / "run-short"
    _write(
        short,
        [
            {"kind": "reply", "tag": "t001", "latency_s": 1.0, "ts": 10.0},
            {**_tail(), "ts": 12.0},
        ],
        manifest=voice_soak_verdict.manifest_dict(
            scenario_hash="test-fixture",
            expected_step_tags=["t001"],
            min_duration_s=5,
        ),
    )
    assert voice_soak_verdict.main([str(short)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: duration_s: 2.0 < 5.0"]

    unmeasured = tmp_path / "run-unmeasured"
    _write(
        unmeasured,
        [{"kind": "reply", "tag": "t001", "latency_s": 1.0}, _tail()],
        manifest=voice_soak_verdict.manifest_dict(
            scenario_hash="test-fixture",
            expected_step_tags=["t001"],
            min_duration_s=5,
        ),
    )
    assert voice_soak_verdict.main([str(unmeasured)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "incomplete"
    assert report["failures"] == ["incomplete: duration_unmeasurable"]


@pytest.mark.parametrize("left_s,failures", [
    (2.0, []),
    (3.0, ["stop_not_confirmed: t001 not_stopped"]),
])
def test_a_stop_clip_too_late_to_win_does_not_fail_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], left_s: float, failures: list[str]
) -> None:
    _, serial = _confirmed_stop_logs()
    bridge = _delivery(-0.1, "first_frame_sent") + _delivery(
        5, "playback_ended", "playback_ended", "playback_completed"
    )
    rows = _passing_rows()
    rows[1]["interrupt_start_ts"] = BASE + 5 - left_s
    run = tmp_path / "run-late-clip"
    _write(
        run,
        rows,
        bridge,
        serial,
        manifest=_manifest("t001", "t002", required_logs=("bridge.log", "serial.log")),
    )
    voice_soak_verdict.main([str(run)])
    assert json.loads(capsys.readouterr().out)["failures"] == failures


def test_abort_silence_and_tail_each_fail_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-bad"
    _write(
        run,
        [
            {"kind": "abort", "reason": "device never reached idle after the port open"},
            {"kind": "step_failed", "tag": "t001", "reason": "could not wake the device"},
            {"kind": "silence", "tag": "t002", "went_idle": False},
            {"kind": "post_scenario_observation", "verdict": "timeout"},
        ],
        manifest=_manifest("t001", "t002"),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == [
        "abort: device never reached idle after the port open",
        "step_failed: t001 could not wake the device",
        "silence_not_idle: t002",
        "tail_idle: timeout",
        "reply_latency_p90_s: missing > 2.0",
        "reply_latency_max_s: missing > 3.0",
    ]


def test_missing_tail_observation_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-notail"
    _write(
        run,
        [
            {"kind": "said", "tag": "t001", "text": "你好"},
            {"kind": "silence", "tag": "t001", "went_idle": True},
        ],
        manifest=voice_soak_verdict.manifest_dict(
            scenario_hash="test-fixture",
            expected_step_tags=["t001"],
            require_completed=False,
        ),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == [
        "tail_idle: missing",
        "reply_latency_p90_s: missing > 2.0",
        "reply_latency_max_s: missing > 3.0",
    ]


def test_no_reply_zero_passes_and_one_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = tmp_path / "run-noreply-zero"
    _write(
        ok,
        [{"kind": "reply", "tag": "t001", "latency_s": 2.0}, _tail()],
        manifest=_manifest("t001"),
    )
    assert voice_soak_verdict.main([str(ok)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert report["status"] == "pass"
    assert report["no_reply"] == 0
    assert report["failures"] == []

    bad = tmp_path / "run-noreply-one"
    _write(
        bad,
        [
            {"kind": "no_reply", "tag": "t001"},
            {"kind": "reply", "tag": "t002", "latency_s": 2.0},
            _tail(),
        ],
        manifest=_manifest("t001", "t002"),
    )
    assert voice_soak_verdict.main([str(bad)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["no_reply"] == 1
    assert report["failures"] == ["no_reply: 1 > 0"]
    assert report["gates"]["max_no_reply"] == 0


def _replies(*latencies: float) -> list[dict[str, object]]:
    return [
        {"kind": "reply", "tag": f"t{index:03d}", "latency_s": latency}
        for index, latency in enumerate(latencies, 1)
    ] + [_tail()]


def _reply_tags(*latencies: float) -> tuple[str, ...]:
    return tuple(f"t{index:03d}" for index, _ in enumerate(latencies, 1))


def test_one_slow_reply_under_3s_passes_when_p90_is_within_2s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    latencies = (*([2.0] * 19), 2.9)
    run = tmp_path / "run-p90-ok"
    _write(run, _replies(*latencies), manifest=_manifest(*_reply_tags(*latencies)))
    assert voice_soak_verdict.main([str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reply_latency_p90_s"] == 2.0
    assert report["reply_latency_max_s"] == 2.9
    assert report["failures"] == []
    assert report["status"] == "pass"


def test_p90_over_2s_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    latencies = (*([1.5] * 18), 2.01, 2.01)
    run = tmp_path / "run-p90-over"
    _write(run, _replies(*latencies), manifest=_manifest(*_reply_tags(*latencies)))
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == ["reply_latency_p90_s: 2.01 > 2.0"]
    assert report["gates"]["max_reply_latency_p90_s"] == 2.0


def test_any_reply_over_3s_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    latencies = (*([1.5] * 19), 3.01)
    run = tmp_path / "run-max-over"
    _write(run, _replies(*latencies), manifest=_manifest(*_reply_tags(*latencies)))
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == ["reply_latency_max_s: 3.01 > 3.0"]
    assert report["gates"]["max_reply_latency_max_s"] == 3.0


def test_invalid_timeline_json_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-badjson"
    run.mkdir()
    (run / "timeline.jsonl").write_text('{"kind": "start"}\nnot-json\n', encoding="utf-8")
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == ["timeline.jsonl:2: invalid json"]


def test_missing_timeline_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "empty"
    run.mkdir()
    assert voice_soak_verdict.main([str(run)]) == 2
    assert "timeline.jsonl" in capsys.readouterr().err


def test_pack_includes_only_present_logs_and_writes_a_matching_sha256(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-pack"
    run.mkdir()
    (run / "timeline.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "serial.log").write_bytes(b"serial\n")
    (run / "bridge.log").write_bytes(b"bridge\n")
    (run / "secret.aiff").write_bytes(b"not-a-log")
    assert voice_soak_pack.main([str(run)]) == 0
    dest = Path(capsys.readouterr().out.strip())
    assert dest == tmp_path / "run-pack.tar.gz"
    with tarfile.open(dest, "r:gz") as archive:
        assert archive.getnames() == [
            "run-pack/timeline.jsonl",
            "run-pack/serial.log",
            "run-pack/bridge.log",
        ]
    sidecar = dest.with_name(dest.name + ".sha256").read_text(encoding="utf-8")
    digest, _, name = sidecar.partition("  ")
    assert name == f"{dest.name}\n"
    assert digest == hashlib.sha256(dest.read_bytes()).hexdigest()


def test_pack_refuses_a_missing_timeline_and_a_dest_inside_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-empty"
    run.mkdir()
    assert voice_soak_pack.main([str(run)]) == 2
    assert "timeline.jsonl" in capsys.readouterr().err
    (run / "timeline.jsonl").write_text("{}\n", encoding="utf-8")
    assert voice_soak_pack.main([str(run), "--dest", str(run / "inside.tar.gz")]) == 2
    assert "outside" in capsys.readouterr().err
    assert not (run / "inside.tar.gz").exists()


def _backchannel_rows() -> list[dict[str, object]]:
    rows = _passing_rows()
    rows[0] = {**rows[0], "text": "嗯"}
    rows[1] = {**rows[1], "interrupt_kind": "backchannel"}
    return rows


def test_a_backchannel_that_keeps_the_reply_playing_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-backchannel-continues"
    _write(run, _backchannel_rows(), manifest=_manifest("t001", "t002"))
    assert voice_soak_verdict.main([str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == []
    assert report["status"] == "pass"


def test_a_backchannel_that_stops_the_reply_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bridge, serial = _confirmed_stop_logs()
    run = tmp_path / "run-backchannel-stopped"
    _write(
        run,
        _backchannel_rows(),
        bridge,
        serial,
        manifest=_manifest("t001", "t002", required_logs=("bridge.log", "serial.log")),
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["failures"] == ["backchannel_stopped: t001"]
