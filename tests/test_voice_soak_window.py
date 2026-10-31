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


def _write(run: Path, rows: list[dict[str, object]], bridge: str = "", serial: str = "") -> None:
    run.mkdir()
    (run / "timeline.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    if bridge:
        (run / "bridge.log").write_text(bridge, encoding="utf-8")
    if serial:
        (run / "serial.log").write_text(serial, encoding="utf-8")


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


def test_a_run_with_a_confirmed_stop_and_idle_tail_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bridge, serial = _confirmed_stop_logs()
    run = tmp_path / "run-pass"
    _write(run, _passing_rows(), bridge, serial)
    assert voice_soak_verdict.main([str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
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
    _write(run, _passing_rows())
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == ["stop_not_confirmed: t001 unverified"]


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
    _write(run, rows, bridge, serial)
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
    )
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
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
    _write(run, [{"kind": "said", "tag": "t001", "text": "你好"}])
    assert voice_soak_verdict.main([str(run)]) == 1
    assert json.loads(capsys.readouterr().out)["failures"] == [
        "tail_idle: missing",
        "reply_latency_p90_s: missing > 2.0",
        "reply_latency_max_s: missing > 3.0",
    ]


def test_no_reply_zero_passes_and_one_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = tmp_path / "run-noreply-zero"
    _write(ok, [{"kind": "reply", "tag": "t001", "latency_s": 2.0}, _tail()])
    assert voice_soak_verdict.main([str(ok)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
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
    )
    assert voice_soak_verdict.main([str(bad)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["no_reply"] == 1
    assert report["failures"] == ["no_reply: 1 > 0"]
    assert report["gates"]["max_no_reply"] == 0


def _replies(*latencies: float) -> list[dict[str, object]]:
    return [
        {"kind": "reply", "tag": f"t{index:03d}", "latency_s": latency}
        for index, latency in enumerate(latencies, 1)
    ] + [_tail()]


def test_one_slow_reply_under_3s_passes_when_p90_is_within_2s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run-p90-ok"
    _write(run, _replies(*([2.0] * 19), 2.9))
    assert voice_soak_verdict.main([str(run)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reply_latency_p90_s"] == 2.0
    assert report["reply_latency_max_s"] == 2.9
    assert report["failures"] == []


def test_p90_over_2s_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-p90-over"
    _write(run, _replies(*([1.5] * 18), 2.01, 2.01))
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == ["reply_latency_p90_s: 2.01 > 2.0"]
    assert report["gates"]["max_reply_latency_p90_s"] == 2.0


def test_any_reply_over_3s_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-max-over"
    _write(run, _replies(*([1.5] * 19), 3.01))
    assert voice_soak_verdict.main([str(run)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == ["reply_latency_max_s: 3.01 > 3.0"]
    assert report["gates"]["max_reply_latency_max_s"] == 3.0


def test_invalid_timeline_json_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "run-badjson"
    run.mkdir()
    (run / "timeline.jsonl").write_text('{"kind": "start"}\nnot-json\n', encoding="utf-8")
    assert voice_soak_verdict.main([str(run)]) == 1
    assert json.loads(capsys.readouterr().out)["failures"] == ["timeline.jsonl:2: invalid json"]


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
