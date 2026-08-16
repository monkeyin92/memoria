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
            "playback_watermark_capability": "exact",
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

def test_verify_t4_cer_over_threshold_fails(tmp_path: Path) -> None:
    receipt = _base_receipt(
        "T4",
        scenario={"asr_engine": "funasr_final", "final_shown": True, "cer": 0.5},
    )
    assert _verify(tmp_path, [receipt]) == 1

def test_verify_invalid_watermark_capability_fails(tmp_path: Path) -> None:
    device = {
        "device_id": "esp32-s3-0001",
        "board": "atk-dnesp32s3-v1",
        "firmware_sha256": "a" * 64,
        "runtime": "go_media_edge_direct_voice_core",
        "playback_watermark_capability": "partial",
    }
    receipt = _base_receipt("T7", device=device)
    assert _verify(tmp_path, [receipt]) == 1


EXACT_DAC_ITEMS = ("T1", "T5", "T6", "T7", "T8")


def _device_without_capability() -> dict[str, str]:
    return {
        "device_id": "esp32-s3-0001",
        "board": "atk-dnesp32s3-v1",
        "firmware_sha256": "a" * 64,
        "runtime": "go_media_edge_direct_voice_core",
    }


@pytest.mark.parametrize("item_id", EXACT_DAC_ITEMS)
@pytest.mark.parametrize("capability", [None, "approximate"])
def test_verify_exact_dac_items_require_exact_watermark_capability(
    tmp_path: Path, item_id: str, capability: str | None
) -> None:
    device = _device_without_capability()
    if capability is not None:
        device["playback_watermark_capability"] = capability
    receipt = _base_receipt(item_id, device=device)
    assert _verify(tmp_path, [receipt]) == 1


@pytest.mark.parametrize("item_id", EXACT_DAC_ITEMS)
def test_verify_exact_dac_items_pass_with_exact_watermark_capability(
    tmp_path: Path, item_id: str
) -> None:
    assert _verify(tmp_path, [_base_receipt(item_id)]) == 0


@pytest.mark.parametrize(
    "item_id",
    ["T2", "T3", "T4", "T9", "T10", "T11", "T12", "T13", "T14"],
)
def test_verify_non_dac_items_do_not_require_watermark_capability(
    tmp_path: Path, item_id: str
) -> None:
    receipt = _base_receipt(item_id, device=_device_without_capability())
    assert _verify(tmp_path, [receipt]) == 0


def test_declares_exact_playback_gate_single_decision_point() -> None:
    for item_id in ("T5", "T6", "T7"):
        assert hra._declares_exact_playback_gate(item_id, {}) is True
    assert hra._declares_exact_playback_gate("T1", {"watermark": "exact"}) is True
    assert hra._declares_exact_playback_gate("T8", {"actual_heard": "exact"}) is True
    assert hra._declares_exact_playback_gate("T1", {}) is False
    assert hra._declares_exact_playback_gate("T8", {}) is False
    assert hra._declares_exact_playback_gate("T4", {}) is False
    assert hra._declares_exact_playback_gate("T4", None) is False

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


# ---------------------------------------------------------------------------
# collect: golden trace -> T4-T7 candidate receipts (fail-closed)
# ---------------------------------------------------------------------------

TRACE_DEVICE: dict[str, str] = {
    "device_id": "esp32-s3-0001",
    "board": "atk-dnesp32s3-v1",
    "firmware_sha256": "a" * 64,
    "runtime": "go_media_edge_direct_voice_core",
    "playback_watermark_capability": "exact",
}


def _event(event_type: str, at: str, **fields: object) -> dict[str, object]:
    return {"type": event_type, "at": at, **fields}


def _trace_events(*, dac_exact: bool = False, displayed: bool = True) -> list[dict[str, object]]:
    """One session: fixed-text TTS generation 1 + one complete speech turn generation 2."""
    t0 = NOW - timedelta(minutes=5)
    precision = "exact" if dac_exact else "approximate"
    return [
        _event("session.accepted", (t0 + timedelta(seconds=0)).isoformat(), session_id="sess-0001", stream_epoch=7),
        _event("tts.started", (t0 + timedelta(seconds=1)).isoformat(), generation_id=1, source="fixed_text"),
        _event("playback.started", (t0 + timedelta(seconds=2)).isoformat(), generation_id=1, dac_verified=dac_exact),
        _event("playback.ended", (t0 + timedelta(seconds=3)).isoformat(), generation_id=1, ack=True, dac_verified=dac_exact, watermark_precision=precision),
        _event("vad.start", (t0 + timedelta(seconds=4)).isoformat(), sample=0),
        _event("uplink.audio", (t0 + timedelta(seconds=5)).isoformat(), frames=24),
        _event("vad.end", (t0 + timedelta(seconds=6)).isoformat(), sample=19200),
        _event("asr.final", (t0 + timedelta(seconds=7)).isoformat(), engine="funasr", recognized_text="你好你好", reference_text="你好你好", displayed=displayed),
        _event("turn.committed", (t0 + timedelta(seconds=8)).isoformat(), turn_id=1, generation_id=2, tool_epoch=0),
        _event(
            "user.text.injected",
            (t0 + timedelta(seconds=9)).isoformat(),
            text="今天星期几",
            generation_id=2,
        ),
        _event(
            "llm.reply",
            (t0 + timedelta(seconds=10)).isoformat(),
            text="今天是星期五",
            generation_id=2,
        ),
        _event("tts.started", (t0 + timedelta(seconds=11)).isoformat(), generation_id=2, source="llm"),
        _event("playback.started", (t0 + timedelta(seconds=12)).isoformat(), generation_id=2, dac_verified=dac_exact),
        _event("playback.ended", (t0 + timedelta(seconds=13)).isoformat(), generation_id=2, ack=True, dac_verified=dac_exact, watermark_precision=precision),
    ]


def _speech_events(*, dac_exact: bool = False, displayed: bool = True) -> list[dict[str, object]]:
    return [
        event
        for event in _trace_events(dac_exact=dac_exact, displayed=displayed)
        if event["type"] not in {"tts.started", "playback.started", "playback.ended"}
    ]


def _golden_trace(
    events: list[dict[str, object]] | None = None,
    *,
    origin: str = "real_device",
    collected_at: str | None = None,
    **extra: object,
) -> dict[str, object]:
    trace: dict[str, object] = {
        "schema_version": hra.TRACE_SCHEMA_VERSION,
        "trace_type": hra.TRACE_TYPE,
        "origin": origin,
        "session_id": "sess-0001",
        "stream_epoch": 7,
        "collected_at": collected_at or (NOW - timedelta(minutes=5)).isoformat(),
        "device": dict(TRACE_DEVICE),
        "events": events if events is not None else _trace_events(),
    }
    trace.update(extra)
    return trace


def _collect(
    tmp_path: Path,
    trace: dict[str, object],
    *,
    tag: str = "collect-test",
    items: list[str] | None = None,
    extra: list[str] | None = None,
) -> tuple[int, Path]:
    trace_path = tmp_path / "golden-trace.json"
    trace_path.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "collected"
    argv = [
        "collect",
        "--trace",
        str(trace_path),
        "--tag",
        tag,
        "--output",
        str(out),
        "--now",
        NOW_ISO,
        "--json",
    ]
    if items is not None:
        argv.extend(["--items", *items])
    if extra:
        argv.extend(extra)
    return hra.main(argv), out


def test_collect_requires_trace_argument() -> None:
    assert hra.main(["collect"]) == 2


def test_collect_unknown_item_exits_2(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("{}", encoding="utf-8")
    assert hra.main(["collect", "--trace", str(trace_path), "--items", "T99"]) == 2


def test_collect_rejects_bad_tag(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(_golden_trace(), ensure_ascii=False), encoding="utf-8")
    assert hra.main(["collect", "--trace", str(trace_path), "--tag", "a/b", "--now", NOW_ISO]) == 2


def test_collect_rejects_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("{not json", encoding="utf-8")
    assert hra.main(["collect", "--trace", str(trace_path), "--now", NOW_ISO]) == 1
    assert "error:" in capsys.readouterr().err


def test_collect_rejects_mock_origin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace = _golden_trace(origin=hra.TRACE_ORIGIN_MOCK)
    assert _collect(tmp_path, trace)[0] == 1
    assert "cannot be upgraded" in capsys.readouterr().err


def test_collect_rejects_repository_origin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace = _golden_trace(origin=hra.TRACE_ORIGIN_REPOSITORY)
    assert _collect(tmp_path, trace)[0] == 1
    assert "cannot be upgraded" in capsys.readouterr().err


def test_collect_rejects_unknown_event_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = [_event("teleport", (NOW - timedelta(minutes=4)).isoformat())]
    trace = _golden_trace(events=events)
    assert _collect(tmp_path, trace)[0] == 1
    assert "type must be one of" in capsys.readouterr().err


def test_collect_rejects_backwards_timestamps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    t0 = NOW - timedelta(minutes=4)
    events = [
        _event("session.accepted", (t0 + timedelta(seconds=1)).isoformat(), session_id="sess-0001", stream_epoch=7),
        _event("vad.start", t0.isoformat(), sample=0),
    ]
    trace = _golden_trace(events=events)
    assert _collect(tmp_path, trace)[0] == 1
    assert "moves backwards" in capsys.readouterr().err


def test_collect_rejects_session_epoch_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    t0 = NOW - timedelta(minutes=4)
    events = [_event("session.accepted", t0.isoformat(), session_id="sess-0001", stream_epoch=9)]
    trace = _golden_trace(events=events)
    assert _collect(tmp_path, trace)[0] == 1
    assert "stream_epoch" in capsys.readouterr().err


def test_collect_rejects_session_id_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    t0 = NOW - timedelta(minutes=4)
    events = [_event("session.accepted", t0.isoformat(), session_id="other-session", stream_epoch=7)]
    trace = _golden_trace(events=events)
    assert _collect(tmp_path, trace)[0] == 1
    assert "session_id" in capsys.readouterr().err


def test_collect_rejects_naive_collected_at(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace = _golden_trace(collected_at="2026-08-13T11:00:00")
    assert _collect(tmp_path, trace)[0] == 1
    assert "timezone" in capsys.readouterr().err


def test_collect_rejects_missing_watermark_precision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = _trace_events()
    for event in events:
        if event["type"] == "playback.ended":
            del event["watermark_precision"]
    trace = _golden_trace(events=events)
    assert _collect(tmp_path, trace)[0] == 1
    assert "watermark_precision" in capsys.readouterr().err


def test_collect_minimal_trace_t4_pass_others_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(events=_speech_events(dac_exact=False))
    code, out = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T4"]["result"] == "pass"
    assert by_id["T5"]["result"] == "blocked"
    assert by_id["T6"]["result"] == "blocked"
    assert by_id["T7"]["result"] == "blocked"
    assert any("DAC/actual-heard" in reason for reason in by_id["T7"]["reasons"])
    # The emitted pass receipt must roundtrip through verify unchanged.
    assert (
        hra.main(
            [
                "verify",
                str(out / "T4-collect-test.json"),
                "--evidence-root",
                str(out / "evidence"),
                "--now",
                NOW_ISO,
            ]
        )
        == 0
    )


def test_collect_missing_reference_blocks_t4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _speech_events()
    for event in events:
        if event["type"] == "asr.final":
            del event["reference_text"]
    trace = _golden_trace(events=events)
    code, out = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    t4 = next(entry for entry in payload["receipts"] if entry["item_id"] == "T4")
    assert t4["result"] == "blocked"
    assert any("cannot compute CER" in reason for reason in t4["reasons"])
    receipt = json.loads((out / "T4-collect-test.json").read_text(encoding="utf-8"))
    assert "cannot compute CER" in receipt["notes"]


def test_collect_displayed_false_blocks_t4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(events=_speech_events(displayed=False))
    code, _ = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    t4 = next(entry for entry in payload["receipts"] if entry["item_id"] == "T4")
    assert t4["result"] == "blocked"
    assert any("displayed" in reason for reason in t4["reasons"])


def test_collect_approximate_watermark_blocks_dac_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(events=_trace_events(dac_exact=False))
    code, _ = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T4"]["result"] == "pass"
    for item_id in ("T5", "T6", "T7"):
        assert by_id[item_id]["result"] == "blocked"
        assert any(
            "DAC/actual-heard" in reason for reason in by_id[item_id]["reasons"]
        ), item_id


def test_collect_approximate_capability_blocks_exact_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(
        events=_trace_events(dac_exact=True),
        device={**TRACE_DEVICE, "playback_watermark_capability": "approximate"},
    )
    code, _ = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T4"]["result"] == "pass"
    for item_id in ("T5", "T6", "T7"):
        assert by_id[item_id]["result"] == "blocked"
        assert any(
            "playback_watermark_capability" in reason
            for reason in by_id[item_id]["reasons"]
        ), item_id


def test_collect_missing_capability_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    device = {
        key: value
        for key, value in TRACE_DEVICE.items()
        if key != "playback_watermark_capability"
    }
    trace = _golden_trace(events=_trace_events(dac_exact=True), device=device)
    code, _ = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T4"]["result"] == "pass"
    for item_id in ("T5", "T6", "T7"):
        assert by_id[item_id]["result"] == "blocked"
        assert any(
            "missing" in reason and "playback_watermark_capability" in reason
            for reason in by_id[item_id]["reasons"]
        ), item_id


def test_collect_t4_short_reference_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _speech_events(dac_exact=True)
    for event in events:
        if event["type"] == "asr.final":
            event["reference_text"] = "你好"
            event["recognized_text"] = "你好"
    code, _ = _collect(tmp_path, _golden_trace(events=events))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    t4 = next(entry for entry in payload["receipts"] if entry["item_id"] == "T4")
    assert t4["result"] == "blocked"
    assert any("reference_text too short" in reason for reason in t4["reasons"])


def test_collect_t4_high_cer_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _speech_events(dac_exact=True)
    for event in events:
        if event["type"] == "asr.final":
            event["reference_text"] = "今天天气很好"
            event["recognized_text"] = "不知道不知道"
    code, _ = _collect(tmp_path, _golden_trace(events=events))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    t4 = next(entry for entry in payload["receipts"] if entry["item_id"] == "T4")
    assert t4["result"] == "blocked"
    assert any("CER" in reason and "0.2" in reason for reason in t4["reasons"])


def test_collect_t4_normalizes_whitespace_before_cer(
    tmp_path: Path,
) -> None:
    events = _speech_events(dac_exact=True)
    for event in events:
        if event["type"] == "asr.final":
            event["reference_text"] = "今 天 天 气 很 好"
            event["recognized_text"] = "今天天气很好"
    code, out = _collect(tmp_path, _golden_trace(events=events), items=["T4"])
    assert code == 0
    receipt = json.loads((out / "T4-collect-test.json").read_text(encoding="utf-8"))
    assert receipt["result"] == "pass"
    assert receipt["scenario"]["cer"] == 0.0


def test_collect_t4_cer_uses_same_normalization_as_length_gate(
    tmp_path: Path,
) -> None:
    events = _speech_events(dac_exact=True)
    for event in events:
        if event["type"] == "asr.final":
            event["reference_text"] = "你 好 世 界"
            event["recognized_text"] = "你好 世界"
    code, out = _collect(tmp_path, _golden_trace(events=events), items=["T4"])
    assert code == 0
    receipt = json.loads((out / "T4-collect-test.json").read_text(encoding="utf-8"))
    assert receipt["result"] == "pass"
    assert receipt["scenario"]["cer"] == 0.0


def test_collect_rejects_invalid_watermark_capability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(
        events=_trace_events(dac_exact=True),
        device={**TRACE_DEVICE, "playback_watermark_capability": "partial"},
    )
    code, _ = _collect(tmp_path, trace)
    assert code == 1
    assert "playback_watermark_capability" in capsys.readouterr().err


def test_collect_tts_only_trace_t5_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    t0 = NOW - timedelta(minutes=5)
    events = [
        _event("session.accepted", t0.isoformat(), session_id="sess-0001", stream_epoch=7),
        _event("tts.started", (t0 + timedelta(seconds=1)).isoformat(), generation_id=1, source="fixed_text"),
        _event("playback.started", (t0 + timedelta(seconds=2)).isoformat(), generation_id=1, dac_verified=True),
        _event("playback.ended", (t0 + timedelta(seconds=3)).isoformat(), generation_id=1, ack=True, dac_verified=True, watermark_precision="exact"),
    ]
    trace = _golden_trace(events=events)
    code, out = _collect(tmp_path, trace)
    assert code == 2  # T5 passes; T4/T6/T7 stay blocked without speech evidence
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T5"]["result"] == "pass"
    assert by_id["T4"]["result"] == "blocked"
    assert by_id["T6"]["result"] == "blocked"
    assert by_id["T7"]["result"] == "blocked"
    assert (
        hra.main(
            [
                "verify",
                str(out / "T5-collect-test.json"),
                "--evidence-root",
                str(out / "evidence"),
                "--now",
                NOW_ISO,
            ]
        )
        == 0
    )


def test_collect_full_trace_all_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    trace = _golden_trace(events=_trace_events(dac_exact=True))
    code, out = _collect(tmp_path, trace)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"pass": 4, "blocked": 0, "failed": 0}
    assert {entry["item_id"] for entry in payload["receipts"]} == {"T4", "T5", "T6", "T7"}
    evidence_path = out / "evidence" / "golden-trace-collect-test.json"
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    serialized_evidence = json.dumps(evidence, ensure_ascii=False)
    assert "你好你好" not in serialized_evidence
    assert "今天星期几" not in serialized_evidence
    assert "今天是星期五" not in serialized_evidence
    assert serialized_evidence.count("[REDACTED]") >= 4
    for item_id in ("T4", "T5", "T6", "T7"):
        receipt = json.loads((out / f"{item_id}-collect-test.json").read_text(encoding="utf-8"))
        assert receipt["result"] == "pass"
        assert receipt["evidence"] == [{"path": "golden-trace-collect-test.json", "sha256": digest, "kind": "log"}]
        assert (
            hra.main(
                [
                    "verify",
                    str(out / f"{item_id}-collect-test.json"),
                    "--evidence-root",
                    str(out / "evidence"),
                    "--now",
                    NOW_ISO,
                ]
            )
            == 0
        ), item_id


def test_collect_rejects_cross_generation_playback_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _trace_events(dac_exact=True)
    for event in events:
        if event["type"] == "playback.started" and event["generation_id"] == 1:
            event["generation_id"] = 99
    code, _ = _collect(tmp_path, _golden_trace(events=events))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T5"]["result"] == "blocked"
    assert any(
        "playback.started for generation 1" in reason
        for reason in by_id["T5"]["reasons"]
    )


def test_collect_rejects_cross_generation_llm_reply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _trace_events(dac_exact=True)
    for event in events:
        if event["type"] == "llm.reply":
            event["generation_id"] = 99
    code, _ = _collect(tmp_path, _golden_trace(events=events))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T6"]["result"] == "blocked"
    assert by_id["T7"]["result"] == "blocked"
    assert any(
        "llm.reply" in reason and "generation" in reason
        for reason in by_id["T7"]["reasons"]
    )


def test_collect_rejects_noncausal_speech_event_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _speech_events()
    vad_start = next(event for event in events if event["type"] == "vad.start")
    events.remove(vad_start)
    vad_end_index = next(
        index for index, event in enumerate(events) if event["type"] == "vad.end"
    )
    events.insert(vad_end_index + 1, vad_start)
    t0 = NOW - timedelta(minutes=5)
    for index, event in enumerate(events):
        event["at"] = (t0 + timedelta(seconds=index)).isoformat()
    code, _ = _collect(tmp_path, _golden_trace(events=events))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    assert by_id["T4"]["result"] == "blocked"
    assert by_id["T7"]["result"] == "blocked"
    assert any(
        "causal order" in reason for reason in by_id["T4"]["reasons"]
    )


def test_collect_error_event_blocks_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = _trace_events(dac_exact=True)
    events.append(
        _event(
            "error",
            (NOW - timedelta(minutes=4) + timedelta(seconds=20)).isoformat(),
            message="media bridge request loop failed",
        )
    )
    trace = _golden_trace(events=events)
    code, out = _collect(tmp_path, trace)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    by_id = {entry["item_id"]: entry for entry in payload["receipts"]}
    for item_id in ("T4", "T5", "T6", "T7"):
        assert by_id[item_id]["result"] == "blocked"
        assert any("error event" in reason for reason in by_id[item_id]["reasons"])
    receipt = json.loads((out / "T4-collect-test.json").read_text(encoding="utf-8"))
    assert receipt["evidence"] == []


def test_collect_blocked_receipt_roundtrips_as_blocked_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trace = _golden_trace(events=_speech_events(dac_exact=False))
    code, out = _collect(tmp_path, trace)
    assert code == 2
    capsys.readouterr()
    assert (
        hra.main(
            [
                "verify",
                str(out / "T7-collect-test.json"),
                "--evidence-root",
                str(out / "evidence"),
                "--now",
                NOW_ISO,
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["receipts"][0]["verdict"] == "blocked"
    assert payload["receipts"][0]["result"] == "blocked"


def test_collect_cer_computation() -> None:
    assert hra._cer("你好你好", "你好你好") == 0.0
    assert hra._cer("你好你好", "你好") == 0.5
    assert hra._cer("abcd", "axcd") == 0.25
    assert hra._cer("abcd", "") == 1.0


def test_normalize_asr_text() -> None:
    assert hra._normalize_asr_text("今 天 天 气 很 好") == "今天天气很好"
    assert hra._normalize_asr_text(" \t你好 世界\n ") == "你好世界"


def test_collect_emitted_receipt_keeps_trace_collected_at(tmp_path: Path) -> None:
    trace = _golden_trace(events=_trace_events(dac_exact=True))
    _, out = _collect(tmp_path, trace)
    receipt = json.loads((out / "T4-collect-test.json").read_text(encoding="utf-8"))
    assert receipt["collected_at"] == trace["collected_at"]
    assert receipt["device"] == TRACE_DEVICE
