"""Tests for the T1-T14 hardware realtime acceptance orchestrator (fail-closed).

The public seam is the CLI: list/run/verify. Receipts, evidence roots and
probes are exercised through tmp_path fixtures; no real hardware, WeChat,
network or production state is touched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts import hardware_realtime_acceptance as hra

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


def _scenario_for(item_id: str) -> dict[str, object]:
    if item_id == "T1":
        return {"playback_count": 3, "playback_ack": True, "watermark": "exact", "played_samples": 24000}
    if item_id == "T2":
        return {"sample_rate": 16000, "channels": 1, "pops": 0, "frame_loss": 0, "clock": "stable"}
    if item_id == "T3":
        return {
            "offline_aec_version": "esp-sr-1.0",
            "capture_session_id": "cap-0001",
            "mic_ref_aligned": True,
            "erle_db": 18.5,
            "residual": "pass",
            "processing": "offline",
        }
    if item_id == "T4":
        return {"asr_engine": "funasr_final", "final_shown": True, "cer": 0.08}
    if item_id == "T5":
        return {"tts_source": "fixed_text", "played": True, "playback_ack": True, "generations_played": 1}
    if item_id == "T6":
        return {"llm_source": "injected_user_text", "tts_played": True, "dac_played": True}
    if item_id == "T7":
        return {
            "mic_vad": True,
            "asr_final": True,
            "llm_reply": True,
            "tts_played": True,
            "dac_played": True,
            "single_turn": "complete",
        }
    if item_id == "T8":
        return {"interrupt": "button", "trials": 12, "stop_p95_ms": 180.0, "old_generation_replay": 0, "actual_heard": "exact"}
    if item_id == "T9":
        return {
            "stop_words": ["停一下", "等等"],
            "distances_m": [0.3, 1.0, 2.0],
            "volumes_pct": [20, 50],
            "noises": ["quiet", "tv"],
            "hit_rate": 0.95,
            "false_trigger_rate": 0.02,
            "stop_p95_ms": 400.0,
        }
    if item_id == "T10":
        return {
            "interrupt": "natural",
            "new_question_full": True,
            "old_reply_cancelled": True,
            "new_turn_established": True,
            "stop_p95_ms": 600.0,
            "aec_matrix": [
                {
                    "mode": "double_talk",
                    "volume_pct": 50,
                    "distance_m": 1.0,
                    "orientation": "front",
                    "environment": "quiet",
                    "voice": "adult_male",
                    "content": "barge_in",
                }
            ],
        }
    if item_id == "T11":
        return {"backchannel_words": ["嗯", "对", "好"], "trials": 25, "false_cancel_rate": 0.08}
    if item_id == "T12":
        return {"echo_sources": ["robot_voice"], "private_memory_written": False, "false_barge_in_count": 0, "memory_checked": True}
    if item_id == "T13":
        return {
            "scope": "full",
            "rounds_merge": 120,
            "rounds_night": 520,
            "panics": 0,
            "watchdogs": 0,
            "heap_samples": [100 + i for i in range(30)],
            "generation_revival": 0,
            "wss_reconnect_checked": True,
        }
    if item_id == "T14":
        return {
            "platform": "ios",
            "steps": {
                "close_after_init": True,
                "clear_background": True,
                "network_switch": True,
                "robot_continues": True,
                "reopen_records": True,
                "reopen_summary": True,
            },
            "records_authoritative": True,
        }
    raise AssertionError(f"no scenario for {item_id}")


def _base_receipt(
    item_id: str = "T7",
    result: str = "pass",
    scenario: dict[str, object] | None = None,
    evidence: list[dict[str, str]] | None = None,
    collected_at: str | None = None,
    ttl_hours: int | None = None,
    device: dict[str, str] | None = None,
    **extra: object,
) -> dict[str, object]:
    item = hra.ITEM_BY_ID.get(item_id)
    category = item.category if item is not None else hra.CATEGORY_REAL_HARDWARE
    receipt: dict[str, object] = {
        "schema_version": hra.SCHEMA_VERSION,
        "receipt_type": hra.RECEIPT_TYPE,
        "item_id": item_id,
        "category": category,
        "result": result,
        "device": device or {
            "device_id": "esp32-s3-0001",
            "board": "atk-dnesp32s3-v1",
            "firmware_sha256": "a" * 64,
            "runtime": "go_media_edge_direct_voice_core",
        },
        "tag": "acceptance-2026-08-13",
        "collected_at": collected_at or (NOW - timedelta(hours=1)).isoformat(),
        "scenario": scenario if scenario is not None else _scenario_for(item_id),
    }
    if evidence is not None:
        receipt["evidence"] = evidence
    if ttl_hours is not None:
        receipt["ttl_hours"] = ttl_hours
    receipt.update(extra)
    return receipt


def _write_evidence(
    root: Path,
    name: str = "metrics.json",
    content: bytes = b'{"ok": true}',
) -> dict[str, str]:
    path = root / name
    path.write_bytes(content)
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": "metrics",
    }


def _verify(
    tmp_path: Path,
    receipts: list[dict[str, object]],
    *,
    now: str = NOW_ISO,
    extra: list[str] | None = None,
    root: Path | None = None,
    coverage: bool = False,
) -> int:
    root = root or tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for index, receipt in enumerate(receipts):
        if receipt.get("result") == "pass" and "evidence" not in receipt:
            receipt["evidence"] = [_write_evidence(root)]
        path = tmp_path / f"receipt-{index}.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        files.append(path)
    argv = ["verify", *(str(p) for p in files), "--evidence-root", str(root), "--now", now]
    if extra:
        argv.extend(extra)
    if coverage:
        argv.append("--coverage")
    return hra.main(argv)


def _ok_outcome(name: str) -> hra._ProbeOutcome:
    return hra._ProbeOutcome(name, ("true",), "/tmp", 0, True, False, "ok")


def _fail_outcome(name: str) -> hra._ProbeOutcome:
    return hra._ProbeOutcome(name, ("false",), "/tmp", 1, False, False, "boom")

# ---------------------------------------------------------------------------
# AEC matrix canonical axes (方案 §9.5)
# ---------------------------------------------------------------------------


def test_aec_matrix_axes_cover_the_required_spec() -> None:
    assert hra.AEC_MODES == ("far_end_only", "near_end_only", "double_talk")
    assert hra.AEC_VOLUMES == (20, 50, 80, 100)
    assert hra.AEC_DISTANCES == (0.3, 1.0, 2.0, 3.0)
    assert hra.AEC_ORIENTATIONS == ("front", "side", "back")
    assert hra.AEC_ENVIRONMENTS == ("quiet", "tv", "music", "fan", "restaurant")
    assert hra.AEC_VOICES == (
        "adult_male", "adult_female", "child", "elder", "soft", "fast"
    )
    assert hra.AEC_CONTENT == ("barge_in", "stop_word", "backchannel", "long_turn")
    assert set(hra.T14_PLATFORMS) == {"ios", "android"}
    assert hra.STOP_P95_MS == {"T8": 250.0, "T9": 500.0, "T10": 700.0}
    assert hra.T13_MERGE_ROUNDS == 100
    assert hra.T13_NIGHT_ROUNDS == 500


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_all_items(capsys: pytest.CaptureFixture[str]) -> None:
    assert hra.main(["list"]) == 0
    out = capsys.readouterr().out
    for item_id in (f"T{index}" for index in range(1, 15)):
        assert item_id in out
    assert "double_talk" in out
    assert "restaurant" in out


def test_list_json_has_full_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert hra.main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "list"
    assert len(payload["items"]) == 14
    categories = {item["category"] for item in payload["items"]}
    assert categories == set(hra.CATEGORIES) - {hra.CATEGORY_REPOSITORY}
    assert payload["aec_matrix"]["volumes_pct"] == [20, 50, 80, 100]


def test_main_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert hra.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_main_without_args_prints_usage() -> None:
    assert hra.main([]) == 2


# ---------------------------------------------------------------------------
# verify: schema
# ---------------------------------------------------------------------------


def test_verify_valid_receipt_passes(tmp_path: Path) -> None:
    assert _verify(tmp_path, [_base_receipt("T7")]) == 0


def test_verify_all_item_scenarios_pass(tmp_path: Path) -> None:
    for item_id in (f"T{index}" for index in range(1, 15)):
        assert _verify(tmp_path, [_base_receipt(item_id)]) == 0, item_id


def test_verify_unknown_top_level_key_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", hacker=1)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_missing_required_key_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7")
    del receipt["device"]
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_bad_schema_version_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", schema_version="9.9")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_unknown_item_fails(tmp_path: Path) -> None:
    receipt = _base_receipt(item_id="T99", scenario=_scenario_for("T7"))
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_category_mismatch_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", category=hra.CATEGORY_LOCAL_DEPENDENCY)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_repository_category_receipt_rejected(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", category=hra.CATEGORY_REPOSITORY)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_invalid_result_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", result="PASS")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_duplicate_json_key_fails(tmp_path: Path) -> None:
    path = tmp_path / "receipt-0.json"
    path.write_text('{"tag": "a", "tag": "b"}', encoding="utf-8")
    (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
    assert hra.main(["verify", str(path), "--evidence-root", str(tmp_path / "evidence"), "--now", NOW_ISO]) == 1


def test_verify_receipt_symlink_fails(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
    assert hra.main(["verify", str(link), "--evidence-root", str(tmp_path / "evidence"), "--now", NOW_ISO]) == 1


def test_verify_missing_receipt_file_fails(tmp_path: Path) -> None:
    (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
    assert hra.main(["verify", str(tmp_path / "nope.json"), "--evidence-root", str(tmp_path / "evidence"), "--now", NOW_ISO]) == 1


def test_verify_oversized_receipt_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="x" * (hra.RECEIPT_MAX_BYTES + 100))
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_pass_requires_evidence(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", evidence=[])
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_declared_failed_result(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", result="failed", evidence=[])
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_declared_blocked_result_is_blocked(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", result="blocked", evidence=[])
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_json_summary_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    receipts = [
        _base_receipt("T7"),
        _base_receipt("T7", result="blocked", evidence=[]),
        _base_receipt("T7", result="failed", evidence=[]),
    ]
    assert _verify(tmp_path, receipts) == 1
    capsys.readouterr()  # discard human output
    # rerun with --json to inspect the machine summary
    root = tmp_path / "evidence"
    files = sorted(tmp_path.glob("receipt-*.json"))
    assert hra.main(["verify", *(str(p) for p in files), "--evidence-root", str(root), "--now", NOW_ISO, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"pass": 1, "blocked": 1, "failed": 1}
    verdicts = {entry["verdict"] for entry in payload["receipts"]}
    assert verdicts == {"pass", "blocked", "failed"}


# ---------------------------------------------------------------------------
# verify: TTL and clock
# ---------------------------------------------------------------------------


def test_verify_expired_receipt_fails(tmp_path: Path) -> None:
    collected = (NOW - timedelta(hours=100)).isoformat()
    receipt = _base_receipt("T7", collected_at=collected)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_ttl_override_accepts_old_receipt(tmp_path: Path) -> None:
    collected = (NOW - timedelta(hours=100)).isoformat()
    receipt = _base_receipt("T7", collected_at=collected)
    assert _verify(tmp_path, [receipt], extra=["--max-age-hours", "200"]) == 0


def test_verify_receipt_ttl_capped_by_category(tmp_path: Path) -> None:
    collected = (NOW - timedelta(hours=10)).isoformat()
    receipt = _base_receipt("T7", collected_at=collected, ttl_hours=999)
    assert _verify(tmp_path, [receipt]) == 0


def test_verify_receipt_ttl_short_expires(tmp_path: Path) -> None:
    collected = (NOW - timedelta(hours=30)).isoformat()
    receipt = _base_receipt("T7", collected_at=collected, ttl_hours=24)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_future_collected_at_fails(tmp_path: Path) -> None:
    collected = (NOW + timedelta(hours=2)).isoformat()
    receipt = _base_receipt("T7", collected_at=collected)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_naive_collected_at_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", collected_at="2026-08-13T11:00:00")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_local_timezone_collected_at_passes(tmp_path: Path) -> None:
    collected = (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat()
    receipt = _base_receipt("T7", collected_at=collected)
    assert _verify(tmp_path, [receipt]) == 0


def test_verify_invalid_iso_collected_at_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", collected_at="not-a-date")
    assert _verify(tmp_path, [receipt]) == 1

# ---------------------------------------------------------------------------
# verify: forbidden content (keys, tokens, WiFi, transcripts, family data)
# ---------------------------------------------------------------------------


def test_verify_api_key_pattern_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="leak sk-abcdefghijklmnopqrstuvwxyz123456")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_jwt_pattern_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123456789")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_wifi_key_segment_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", scenario={"wifi_password": "hunter2"})
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_wifi_value_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="wifi 密码: hunter2")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_phone_number_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="contact 13812345678")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_id_card_number_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="id 110101199003071234")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_family_key_segment_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", scenario={"family_info": {"members": 3}})
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_chinese_family_terms_fail(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="记录家庭住址与真实姓名")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_transcript_key_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", scenario={"full_transcript": "你好，今天天气不错"})
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_data_uri_embedding_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="data:audio/wav;base64,UklGRg==")
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_raw_audio_evidence_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (root / "clip.wav").write_bytes(b"RIFF")
    evidence = [{
        "path": "clip.wav",
        "sha256": hashlib.sha256(b"RIFF").hexdigest(),
        "kind": "log",
    }]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_transcript_evidence_filename_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (root / "transcript.json").write_bytes(b"{}")
    evidence = [{
        "path": "transcript.json",
        "sha256": hashlib.sha256(b"{}").hexdigest(),
        "kind": "metrics",
    }]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_normal_chinese_notes_pass(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="实板验收记录：单轮稳定，双讲通过")
    assert _verify(tmp_path, [receipt]) == 0


def test_verify_oversized_string_value_fails(tmp_path: Path) -> None:
    receipt = _base_receipt("T7", notes="x" * (hra.MAX_STRING_CHARS + 10))
    assert _verify(tmp_path, [receipt]) == 1


# ---------------------------------------------------------------------------
# verify: evidence paths, hashes and sizes
# ---------------------------------------------------------------------------


def test_verify_evidence_hash_mismatch_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (root / "metrics.json").write_bytes(b'{"ok": true}')
    evidence = [{"path": "metrics.json", "sha256": "f" * 64, "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_missing_file_fails(tmp_path: Path) -> None:
    evidence = [{"path": "missing.json", "sha256": "a" * 64, "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_path_traversal_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside.json").write_bytes(b"{}")
    evidence = [{"path": "../outside.json", "sha256": hashlib.sha256(b"{}").hexdigest(), "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_absolute_escape_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    evidence = [{"path": "/etc/hosts", "sha256": "b" * 64, "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_symlink_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside.json").write_bytes(b"{}")
    (root / "evil.json").symlink_to(tmp_path / "outside.json")
    evidence = [{"path": "evil.json", "sha256": hashlib.sha256(b"{}").hexdigest(), "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_self_reference_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    evidence = [{"path": "receipt-0.json", "sha256": "c" * 64, "kind": "metrics"}]
    receipt = _base_receipt("T7", evidence=evidence)
    assert _verify(tmp_path, [receipt], root=tmp_path) == 1


def test_verify_evidence_duplicate_path_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    entry = _write_evidence(root)
    receipt = _base_receipt("T7", evidence=[entry, dict(entry)])
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_evidence_too_large_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    entry = _write_evidence(root, content=b"x" * 100)
    receipt = _base_receipt("T7", evidence=[entry])
    assert _verify(tmp_path, [receipt], extra=["--max-evidence-bytes", "10"]) == 1


def test_verify_evidence_kind_invalid_fails(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    entry = _write_evidence(root)
    entry["kind"] = "raw"
    receipt = _base_receipt("T7", evidence=[entry])
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_dir_scan(tmp_path: Path) -> None:
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / "a.json").write_text(json.dumps(_base_receipt("T7"), ensure_ascii=False), encoding="utf-8")
    (receipts_dir / "b.json").write_text(json.dumps(_base_receipt("T7"), ensure_ascii=False), encoding="utf-8")
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    (root / "metrics.json").write_bytes(b'{"ok": true}')
    digest = hashlib.sha256(b'{"ok": true}').hexdigest()
    for name in ("a.json", "b.json"):
        path = receipts_dir / name
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["evidence"] = [{"path": "metrics.json", "sha256": digest, "kind": "metrics"}]
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    assert hra.main(["verify", "--dir", str(receipts_dir), "--evidence-root", str(root), "--now", NOW_ISO]) == 0


def test_verify_dir_without_json_exits_2(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    assert hra.main(["verify", "--dir", str(empty), "--now", NOW_ISO]) == 2


def test_verify_no_receipts_exits_2(tmp_path: Path) -> None:
    assert hra.main(["verify", "--now", NOW_ISO]) == 2

# ---------------------------------------------------------------------------
# verify: per-item scenario gates
# ---------------------------------------------------------------------------


def test_verify_t8_trials_below_minimum_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T8"), trials=5)
    receipt = _base_receipt("T8", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t8_stop_p95_over_threshold_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T8"), stop_p95_ms=300.0)
    receipt = _base_receipt("T8", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t8_old_generation_replay_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T8"), old_generation_replay=1)
    receipt = _base_receipt("T8", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t9_insufficient_distances_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T9"), distances_m=[0.3])
    receipt = _base_receipt("T9", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t9_non_canonical_volume_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T9"), volumes_pct=[15])
    receipt = _base_receipt("T9", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t9_missing_stop_word_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T9"), stop_words=["停一下"])
    receipt = _base_receipt("T9", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t10_invalid_aec_cell_fails(tmp_path: Path) -> None:
    cells = [dict(_scenario_for("T10")["aec_matrix"][0], mode="quantum")]
    scenario = dict(_scenario_for("T10"), aec_matrix=cells)
    receipt = _base_receipt("T10", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t10_without_double_talk_fails(tmp_path: Path) -> None:
    cell = dict(_scenario_for("T10")["aec_matrix"][0], mode="near_end_only")
    scenario = dict(_scenario_for("T10"), aec_matrix=[cell])
    receipt = _base_receipt("T10", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t10_missing_aec_matrix_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T10"))
    del scenario["aec_matrix"]
    receipt = _base_receipt("T10", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t11_false_cancel_over_cap_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T11"), false_cancel_rate=0.5)
    receipt = _base_receipt("T11", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t11_trials_below_minimum_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T11"), trials=5)
    receipt = _base_receipt("T11", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t12_private_memory_written_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T12"), private_memory_written=True)
    receipt = _base_receipt("T12", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t12_echo_sources_invalid_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T12"), echo_sources=["airplane"])
    receipt = _base_receipt("T12", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t13_merge_scope_rounds_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T13"), scope="merge", rounds_merge=50)
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t13_night_scope_rounds_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T13"), scope="night", rounds_night=400)
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t13_panics_fail(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T13"), panics=1)
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t13_heap_decline_fails(tmp_path: Path) -> None:
    samples = [100 - index for index in range(30)]
    scenario = dict(_scenario_for("T13"), heap_samples=samples)
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t13_heap_trend_string_passes(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T13"))
    del scenario["heap_samples"]
    scenario["heap_trend"] = "non_decreasing"
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 0


def test_verify_t13_generation_revival_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T13"), generation_revival=2)
    receipt = _base_receipt("T13", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t14_step_false_fails(tmp_path: Path) -> None:
    steps = dict(_scenario_for("T14")["steps"], robot_continues=False)
    scenario = dict(_scenario_for("T14"), steps=steps)
    receipt = _base_receipt("T14", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t14_unknown_platform_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T14"), platform="windows")
    receipt = _base_receipt("T14", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t14_unknown_step_fails(tmp_path: Path) -> None:
    steps = dict(_scenario_for("T14")["steps"], extra_step=True)
    scenario = dict(_scenario_for("T14"), steps=steps)
    receipt = _base_receipt("T14", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t14_android_passes(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T14"), platform="android")
    receipt = _base_receipt("T14", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 0


def test_verify_t2_pops_fail(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T2"), pops=2)
    receipt = _base_receipt("T2", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t3_erle_missing_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T3"))
    del scenario["erle_db"]
    receipt = _base_receipt("T3", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1


def test_verify_t1_played_samples_zero_fails(tmp_path: Path) -> None:
    scenario = dict(_scenario_for("T1"), played_samples=0)
    receipt = _base_receipt("T1", scenario=scenario)
    assert _verify(tmp_path, [receipt]) == 1

# ---------------------------------------------------------------------------
# verify: --coverage aggregation
# ---------------------------------------------------------------------------


def _coverage_cells() -> list[dict[str, object]]:
    cells: list[dict[str, object]] = []
    for mode, volume, distance, orientation, environment, voice, content in (
        ("double_talk", 50, 1.0, "front", "quiet", "adult_male", "barge_in"),
        ("double_talk", 80, 2.0, "side", "tv", "adult_female", "stop_word"),
        ("near_end_only", 50, 1.0, "back", "music", "child", "backchannel"),
        ("far_end_only", 50, 1.0, "front", "fan", "elder", "long_turn"),
        ("double_talk", 20, 3.0, "side", "restaurant", "soft", "barge_in"),
        ("far_end_only", 100, 0.3, "back", "quiet", "fast", "backchannel"),
    ):
        cells.append(
            {
                "mode": mode,
                "volume_pct": volume,
                "distance_m": distance,
                "orientation": orientation,
                "environment": environment,
                "voice": voice,
                "content": content,
            }
        )
    return cells


def _full_coverage_receipts() -> list[dict[str, object]]:
    t9_scenario = dict(_scenario_for("T9"))
    t9_scenario["distances_m"] = [0.3, 1.0, 2.0, 3.0]
    t9_scenario["volumes_pct"] = [20, 50, 80, 100]
    t9_scenario["noises"] = ["quiet", "tv", "music", "fan", "restaurant"]
    t10_scenario = dict(_scenario_for("T10"), aec_matrix=_coverage_cells())
    return [
        _base_receipt("T9", scenario=t9_scenario),
        _base_receipt("T10", scenario=t10_scenario),
        _base_receipt("T13"),
        _base_receipt("T14"),
        _base_receipt("T14", scenario=dict(_scenario_for("T14"), platform="android")),
    ]


def test_verify_coverage_full_passes(tmp_path: Path) -> None:
    assert _verify(tmp_path, _full_coverage_receipts(), coverage=True) == 0


def test_verify_coverage_missing_axis_fails(tmp_path: Path) -> None:
    receipts = _full_coverage_receipts()
    cells = dict(_scenario_for("T10"), aec_matrix=_coverage_cells())
    cells["aec_matrix"] = [cell for cell in cells["aec_matrix"] if cell["environment"] != "restaurant"]
    receipts[1]["scenario"] = cells
    assert _verify(tmp_path, receipts, coverage=True) == 1


def test_verify_coverage_t14_needs_both_platforms(tmp_path: Path) -> None:
    receipts = _full_coverage_receipts()[:-1]
    assert _verify(tmp_path, receipts, coverage=True) == 1


def test_verify_coverage_t13_needs_night_rounds(tmp_path: Path) -> None:
    receipts = _full_coverage_receipts()
    scenario = dict(_scenario_for("T13"), scope="merge", rounds_night=None)
    del scenario["rounds_night"]
    receipts[2]["scenario"] = scenario
    assert _verify(tmp_path, receipts, coverage=True) == 1


def test_verify_coverage_no_relevant_receipts(tmp_path: Path) -> None:
    assert _verify(tmp_path, [_base_receipt("T7")], coverage=True) == 1


def test_verify_coverage_reasons_in_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _verify(tmp_path, [_base_receipt("T7")], coverage=True, extra=["--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["ok"] is False
    assert any("T13" in reason for reason in payload["coverage"]["reasons"])


# ---------------------------------------------------------------------------
# run: probes, blocked/failed verdicts and --force rejection
# ---------------------------------------------------------------------------


def test_run_external_item_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hra, "_execute_probe", lambda probe, timeout: _ok_outcome(probe.name))
    assert hra.main(["run", "T1"]) == 2


def test_run_probe_failure_hard_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hra, "_execute_probe", lambda probe, timeout: _fail_outcome(probe.name))
    assert hra.main(["run", "T14"]) == 1


def test_run_repository_item_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = hra._Item("T99", "fake", hra.CATEGORY_REPOSITORY, "chain", "proc", (hra._Probe("p", ("true",), tmp_path),))
    monkeypatch.setattr(hra, "ITEM_BY_ID", {**hra.ITEM_BY_ID, "T99": fake})
    monkeypatch.setattr(hra, "_execute_probe", lambda probe, timeout: _ok_outcome(probe.name))
    assert hra.main(["run", "T99"]) == 0


def test_run_force_rejected() -> None:
    assert hra.main(["run", "--force", "T1"]) == 2


def test_run_unknown_item_exits_2() -> None:
    assert hra.main(["run", "T99"]) == 2


def test_run_category_with_no_items_exits_2() -> None:
    assert hra.main(["run", "--category", hra.CATEGORY_REPOSITORY]) == 2


def test_run_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(hra, "_execute_probe", lambda probe, timeout: _ok_outcome(probe.name))
    assert hra.main(["run", "--json", "T1"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "run"
    assert payload["summary"]["blocked"] == 1
    assert payload["items"][0]["item_id"] == "T1"
    assert payload["items"][0]["receipt_required"] is True


def test_run_default_all_items_are_blocked_or_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hra, "_execute_probe", lambda probe, timeout: _ok_outcome(probe.name))
    assert hra.main(["run"]) == 2


def test_probe_command_uses_offline_uv_and_repo_root() -> None:
    probes = {item.item_id: item.probes for item in hra.ITEMS}
    assert probes["T1"][0].argv[:3] == ("uv", "run", "--offline")
    assert probes["T13"][0].argv == probes["T1"][0].argv
    assert probes["T14"][0].argv == ("npm", "test")
    assert probes["T14"][0].cwd == hra.REPO_ROOT / "apps" / "miniprogram"
    assert hra.ITEM_BY_ID["T3"].probes == ()
