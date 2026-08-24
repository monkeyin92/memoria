#!/usr/bin/env python3
"""T1-T14 hardware realtime acceptance orchestrator (fail-closed).

Source of truth: HANDOFF.md (T1-T14, SLO/REJECT, code/wired/enabled/verified,
external evidence policy and real-device evidence boundaries).

Semantics
---------
- list:    show the T1-T14 registry, the canonical AEC matrix and thresholds.
- run:     execute only safe in-repo commands ("probes"). External items never
           execute on this machine: they are reported blocked and require a
           structured receipt verified by 'verify'.
- collect: turn one real-device golden trace into T4-T7 candidate receipts.
           Fail-closed: repository/mock traces are refused, and any receipt
           without device DAC/actual-heard evidence is emitted as blocked.
           Collected receipts still must pass 'verify' before they count.
- verify:  validate structured receipt JSON. Fail-closed: schema, TTL,
           path-traversal, forbidden content, evidence hashes and per-item
           scenario gates must all hold before a receipt is accepted.

Categories
----------
- repository:          safe in-repo commands only; no receipt, no real evidence.
- local_dependency:    offline/local processing of board-captured fixtures.
- real_hardware:       physical ESP32 board evidence.
- real_wechat:         real WeChat Mini Program on a real phone (iOS/Android).
- production_security: production privacy/security property evidence.

This tool never flashes firmware, never changes production state, and never
uploads anything. ``run`` cannot write real-device receipts; ``collect`` only
writes candidate receipts from an operator-supplied trace and privacy-redacts
copied transcript text. Repository simulation cannot upgrade real evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "outputs" / "acceptance"

SCHEMA_VERSION = "1.0"
RECEIPT_TYPE = "memoria_hardware_realtime_acceptance"
RECEIPT_MAX_BYTES = 1_000_000
DEFAULT_EVIDENCE_MAX_BYTES = 5_000_000
MAX_STRING_CHARS = 4000
MAX_NOTES_CHARS = 2000

CATEGORY_REPOSITORY = "repository"
CATEGORY_LOCAL_DEPENDENCY = "local_dependency"
CATEGORY_REAL_HARDWARE = "real_hardware"
CATEGORY_REAL_WECHAT = "real_wechat"
CATEGORY_PRODUCTION_SECURITY = "production_security"
CATEGORIES: tuple[str, ...] = (
    CATEGORY_REPOSITORY,
    CATEGORY_LOCAL_DEPENDENCY,
    CATEGORY_REAL_HARDWARE,
    CATEGORY_REAL_WECHAT,
    CATEGORY_PRODUCTION_SECURITY,
)

RESULTS: tuple[str, ...] = ("pass", "blocked", "failed")
EVIDENCE_KINDS: tuple[str, ...] = ("metrics", "log", "screenshot", "photo", "report")
RUN_TIMES: tuple[str, ...] = (
    "go_media_edge_direct_voice_core",
    "python_device_gateway_livekit_compat",
    "livekit",
)

DEFAULT_TTL_HOURS: dict[str, int] = {
    CATEGORY_LOCAL_DEPENDENCY: 168,
    CATEGORY_REAL_HARDWARE: 72,
    CATEGORY_REAL_WECHAT: 72,
    CATEGORY_PRODUCTION_SECURITY: 48,
}
DEFAULT_MAX_FUTURE_MINUTES = 60
STOP_P95_MS: dict[str, float] = {"T8": 250.0, "T9": 500.0, "T10": 700.0}
BACKCHANNEL_FALSE_CANCEL_CAP = 0.2
T13_MERGE_ROUNDS = 100
T13_NIGHT_ROUNDS = 500
T14_PLATFORMS: tuple[str, ...] = ("ios", "android")
BACKCHANNEL_WORDS: tuple[str, ...] = ("嗯", "对", "好")
STOP_WORDS: tuple[str, ...] = ("停一下", "等等")
T4_CER_MAX = 0.2
T4_REFERENCE_MIN_CHARS = 4

PLAYBACK_WATERMARK_CAPABILITY_EXACT = "exact"
PLAYBACK_WATERMARK_CAPABILITY_APPROXIMATE = "approximate"
PLAYBACK_WATERMARK_CAPABILITIES: frozenset[str] = frozenset(
    {PLAYBACK_WATERMARK_CAPABILITY_EXACT, PLAYBACK_WATERMARK_CAPABILITY_APPROXIMATE}
)

AEC_MODES: tuple[str, ...] = ("far_end_only", "near_end_only", "double_talk")
AEC_VOLUMES: tuple[int, ...] = (20, 50, 80, 100)
AEC_DISTANCES: tuple[float, ...] = (0.3, 1.0, 2.0, 3.0)
AEC_ORIENTATIONS: tuple[str, ...] = ("front", "side", "back")
AEC_ENVIRONMENTS: tuple[str, ...] = ("quiet", "tv", "music", "fan", "restaurant")
AEC_VOICES: tuple[str, ...] = ("adult_male", "adult_female", "child", "elder", "soft", "fast")
AEC_CONTENT: tuple[str, ...] = ("barge_in", "stop_word", "backchannel", "long_turn")
AEC_CELL_KEYS: tuple[str, ...] = (
    "mode",
    "volume_pct",
    "distance_m",
    "orientation",
    "environment",
    "voice",
    "content",
)
FORBIDDEN_KEY_SEGMENTS: frozenset[str] = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "pwd",
        "credential",
        "wifi",
        "wlan",
        "ssid",
        "psk",
        "transcript",
        "transcription",
        "family",
        "address",
        "idcard",
        "id_card",
        "phone",
        "mobile",
        "tel",
        "openid",
        "realname",
        "real_name",
        "birthday",
        "cookie",
        "household",
    }
)
FORBIDDEN_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)(wifi|wlan|ssid)\s*(password|pass|pwd|密钥|密码|ssid)?\s*[:=]\s*\S{4,}"),
    re.compile(r"(?i)\bssid\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"家庭住址|门牌号|家庭信息|家庭成员|家庭WiFi|家庭wifi|身份证号|手机号码|真实姓名|银行卡号"),
    re.compile(r"^data:[a-z0-9+/]+;base64,"),
)
FORBIDDEN_EVIDENCE_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".pcm", ".flac", ".opus", ".ogg", ".mp3", ".m4a", ".aac", ".raw", ".pcap"}
)

TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "receipt_type",
        "item_id",
        "category",
        "result",
        "device",
        "tag",
        "collected_at",
        "ttl_hours",
        "scenario",
        "evidence",
        "notes",
    }
)
REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "receipt_type",
        "item_id",
        "category",
        "result",
        "device",
        "tag",
        "collected_at",
        "scenario",
        "evidence",
    }
)
DEVICE_KEYS: frozenset[str] = frozenset(
    {
        "device_id",
        "board",
        "firmware_sha256",
        "runtime",
        "runtime_version",
        "playback_watermark_capability",
    }
)
REQUIRED_DEVICE_KEYS: frozenset[str] = frozenset(
    {"device_id", "board", "firmware_sha256", "runtime"}
)
EVIDENCE_ITEM_KEYS: frozenset[str] = frozenset({"path", "sha256", "kind"})
T14_STEP_KEYS: tuple[str, ...] = (
    "close_after_init",
    "clear_background",
    "network_switch",
    "robot_continues",
    "reopen_records",
    "reopen_summary",
)

TRACE_SCHEMA_VERSION = "1.0"
TRACE_TYPE = "memoria_golden_trace"
TRACE_ORIGIN_REAL_DEVICE = "real_device"
TRACE_ORIGIN_REPOSITORY = "repository"
TRACE_ORIGIN_MOCK = "mock"
TRACE_ORIGINS: tuple[str, ...] = (
    TRACE_ORIGIN_REAL_DEVICE,
    TRACE_ORIGIN_REPOSITORY,
    TRACE_ORIGIN_MOCK,
)
COLLECT_ITEMS: tuple[str, ...] = ("T4", "T5", "T6", "T7")

TRACE_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "trace_type",
        "origin",
        "session_id",
        "stream_epoch",
        "collected_at",
        "device",
        "events",
    }
)
TRACE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "trace_type",
        "origin",
        "session_id",
        "stream_epoch",
        "collected_at",
        "device",
        "events",
    }
)
TRACE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session.accepted",
        "vad.start",
        "vad.end",
        "uplink.audio",
        "asr.final",
        "turn.committed",
        "user.text.injected",
        "llm.reply",
        "tts.started",
        "playback.started",
        "playback.ended",
        "error",
    }
)
TRACE_EVENT_KEYS: dict[str, frozenset[str]] = {
    "session.accepted": frozenset({"type", "at", "session_id", "stream_epoch"}),
    "vad.start": frozenset({"type", "at", "sample"}),
    "vad.end": frozenset({"type", "at", "sample"}),
    "uplink.audio": frozenset({"type", "at", "frames"}),
    "asr.final": frozenset(
        {"type", "at", "engine", "recognized_text", "reference_text", "displayed"}
    ),
    "turn.committed": frozenset({"type", "at", "turn_id", "generation_id", "tool_epoch"}),
    "user.text.injected": frozenset({"type", "at", "text", "generation_id"}),
    "llm.reply": frozenset({"type", "at", "text", "generation_id"}),
    "tts.started": frozenset({"type", "at", "generation_id", "source"}),
    "playback.started": frozenset({"type", "at", "generation_id", "dac_verified"}),
    "playback.ended": frozenset(
        {"type", "at", "generation_id", "ack", "dac_verified", "watermark_precision"}
    ),
    "error": frozenset({"type", "at", "message"}),
}
TRACE_TTS_SOURCES: frozenset[str] = frozenset({"fixed_text", "llm"})
TRACE_WATERMARK_PRECISIONS: frozenset[str] = frozenset({"exact", "approximate"})
TRACE_MAX_TEXT_CHARS = 512
TRACE_MAX_EVENTS = 100_000

DAC_EVIDENCE_NOTE = (
    "missing device DAC/actual-heard evidence: playback.ended requires ack=true, "
    "dac_verified=true and watermark_precision=exact"
)


@dataclass(frozen=True)
class _Probe:
    name: str
    argv: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class _Item:
    item_id: str
    name: str
    category: str
    chain: str
    procedure: str
    probes: tuple[_Probe, ...] = ()


ITEMS: tuple[_Item, ...] = (
    _Item(
        "T1",
        "设备播放 Only",
        CATEGORY_REAL_HARDWARE,
        "固定 Opus → Edge → ESP32 → DAC → Playback ACK",
        "播放固定 Opus 流：验证 DAC 出声、Playback ACK 与播放水位 exact，无旧代次；不经过 ASR/LLM/TTS。",
        (
            _Probe(
                "软件播放水位/代次契约（发布前门禁，不替代 DAC 证据）",
                ("uv", "run", "--offline", "python", "scripts/media_runtime_replay.py"),
                REPO_ROOT,
            ),
        ),
    ),
    _Item(
        "T2",
        "设备录音 Only",
        CATEGORY_REAL_HARDWARE,
        "Mic → AFE bypass → Opus → Edge → 临时 WAV",
        "检查采样率(16/24kHz)、单声道、无爆音、无丢帧、时钟稳定。",
    ),
    _Item(
        "T3",
        "AEC Offline",
        CATEGORY_LOCAL_DEPENDENCY,
        "保存 Mic + Reference → 离线 AEC → 对比输出",
        "用实板导出的 Mic+Reference（capture_session_id 绑定）离线跑 AEC：记录 ERLE、残余回声、版本；不替代真机双讲。",
    ),
    _Item(
        "T4",
        "ASR Only",
        CATEGORY_REAL_HARDWARE,
        "ESP32 → Edge → Voice Core → FunASR final → 屏幕/日志",
        "固定实板录音得到一致 FunASR final 并显示；记录 CER。",
    ),
    _Item(
        "T5",
        "TTS Only",
        CATEGORY_REAL_HARDWARE,
        "固定文本 → TTS → Edge → ESP32",
        "固定文本播出一代：无乱序/爆音，Playback ACK 完整。",
    ),
    _Item(
        "T6",
        "LLM + TTS",
        CATEGORY_REAL_HARDWARE,
        "服务端注入用户文本 → LLM → TTS → ESP32",
        "服务端注入用户文本完成 LLM→TTS→DAC 播放。",
    ),
    _Item(
        "T7",
        "完整单轮",
        CATEGORY_REAL_HARDWARE,
        "Mic → VAD → ASR → Turn → LLM → TTS → DAC",
        "完整单轮不经过 LiveKit 仍能完成：VAD/ASR final/LLM 回复/TTS/DAC 全链路。",
    ),
    _Item(
        "T8",
        "按钮打断",
        CATEGORY_REAL_HARDWARE,
        "播放中随机时刻按键 → 停止水位/Generation/Actual Heard",
        "≥10 次随机按键：P95 可听停止 ≤ 250 ms（初期门槛），旧 Generation 误播 0，Actual Heard exact。",
    ),
    _Item(
        "T9",
        "停止词",
        CATEGORY_REAL_HARDWARE,
        "不同距离/音量/噪声下说 停一下/等等",
        "≥3 距离 × ≥2 音量 × ≥2 环境：命中率/误触发率上报，P95 ≤ 500 ms（初期门槛）。",
    ),
    _Item(
        "T10",
        "自然抢话",
        CATEGORY_REAL_HARDWARE,
        "播放时完整新问题 → 旧回复取消、新话轮建立",
        "P95 ≤ 700 ms（初期门槛）；附 AEC 矩阵单元（含 double_talk），旧回复取消且新话轮建立。",
    ),
    _Item(
        "T11",
        "Backchannel",
        CATEGORY_REAL_HARDWARE,
        "播放中说 嗯/对/好",
        "≥20 次：误取消率 ≤ 0.20，不普遍误打断。",
    ),
    _Item(
        "T12",
        "回声与电视",
        CATEGORY_PRODUCTION_SECURITY,
        "播放机器人声音/电视人声 → 不得建立错误私人记忆",
        "生产隐私门禁：echo_sources 任一来源下 private_memory_written 必须为 false，误打断 0。",
    ),
    _Item(
        "T13",
        "连续稳定性",
        CATEGORY_REAL_HARDWARE,
        "100 轮合入门禁 + 500 轮夜测",
        "scope=merge 需 ≥100 轮、scope=night 需 ≥500 轮、scope=full 两者：无 panic/watchdog，堆不持续下降，WSS 重连不复活旧 Generation。",
        (
            _Probe(
                "软件混沌契约：edge_restart/asr_disconnect/late_final 不复活旧代次（发布前门禁，不替代 500 轮真机）",
                ("uv", "run", "--offline", "python", "scripts/media_runtime_replay.py"),
                REPO_ROOT,
            ),
        ),
    ),
    _Item(
        "T14",
        "小程序独立性",
        CATEGORY_REAL_WECHAT,
        "小程序控制面化（无实时媒体）→ 设备独立对话 → 重开恢复记录/摘要",
        "iOS+Android：初始化后关闭小程序、清微信后台、手机换网，机器人仍能对话；重开可见新记录与摘要。",
        (
            _Probe(
                "小程序无实时媒体静态门禁（RecorderManager/媒体 WSS/网关）",
                ("npm", "test"),
                REPO_ROOT / "apps" / "miniprogram",
            ),
        ),
    ),
)
ITEM_BY_ID: dict[str, _Item] = {item.item_id: item for item in ITEMS}


def _plain_str(value: object, max_len: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_len and "\x00" not in value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _read_receipt(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"receipt is not a regular file: {path}")
    if path.stat().st_size > RECEIPT_MAX_BYTES:
        raise ValueError(f"receipt exceeds {RECEIPT_MAX_BYTES} bytes: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"receipt is not readable UTF-8 text: {exc}") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"receipt is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("receipt root must be a JSON object")
    return value


def _expect(
    container: dict[str, object],
    key: str,
    typ: type[Any],
    problems: list[str],
    *,
    prefix: str = "",
) -> None:
    if not isinstance(container.get(key), typ):
        problems.append(f"{prefix}{key} must be {typ.__name__}")

def _validate_schema(receipt: dict[str, object]) -> list[str]:
    problems: list[str] = []
    unknown = sorted(set(receipt) - TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"unknown top-level keys: {', '.join(unknown)}")
    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - set(receipt))
    if missing:
        problems.append(f"missing required keys: {', '.join(missing)}")
    _expect(receipt, "schema_version", str, problems)
    _expect(receipt, "receipt_type", str, problems)
    _expect(receipt, "item_id", str, problems)
    _expect(receipt, "category", str, problems)
    _expect(receipt, "result", str, problems)
    _expect(receipt, "tag", str, problems)
    _expect(receipt, "collected_at", str, problems)
    _expect(receipt, "device", dict, problems)
    _expect(receipt, "scenario", dict, problems)
    _expect(receipt, "evidence", list, problems)
    if receipt.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if receipt.get("receipt_type") != RECEIPT_TYPE:
        problems.append(f"receipt_type must be {RECEIPT_TYPE!r}")
    if not _plain_str(receipt.get("tag"), 128):
        problems.append("tag must be a non-empty string of at most 128 chars")
    notes = receipt.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS):
        problems.append(f"notes must be a string of at most {MAX_NOTES_CHARS} chars")
    ttl = receipt.get("ttl_hours")
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1):
        problems.append("ttl_hours must be a positive integer")

    device = receipt.get("device")
    if isinstance(device, dict):
        d_unknown = sorted(set(device) - DEVICE_KEYS)
        if d_unknown:
            problems.append(f"unknown device keys: {', '.join(d_unknown)}")
        d_missing = sorted(REQUIRED_DEVICE_KEYS - set(device))
        if d_missing:
            problems.append(f"missing device keys: {', '.join(d_missing)}")
        _expect(device, "device_id", str, problems, prefix="device.")
        _expect(device, "board", str, problems, prefix="device.")
        _expect(device, "firmware_sha256", str, problems, prefix="device.")
        _expect(device, "runtime", str, problems, prefix="device.")
        device_id = device.get("device_id")
        if not _plain_str(device_id, 128):
            problems.append("device.device_id must be a non-empty string of at most 128 chars")
        elif "/" in cast(str, device_id) or "\\" in cast(str, device_id):
            problems.append("device.device_id must not contain path separators")
        board = device.get("board")
        if not _plain_str(board, 128):
            problems.append("device.board must be a non-empty string of at most 128 chars")
        elif "/" in cast(str, board) or "\\" in cast(str, board):
            problems.append("device.board must not contain path separators")
        firmware = device.get("firmware_sha256")
        if not isinstance(firmware, str) or re.fullmatch(r"[0-9a-fA-F]{40,64}", firmware) is None:
            problems.append("device.firmware_sha256 must be 40-64 hex chars")
        runtime = device.get("runtime")
        if runtime not in RUN_TIMES:
            problems.append(f"device.runtime must be one of: {', '.join(RUN_TIMES)}")
        runtime_version = device.get("runtime_version")
        if runtime_version is not None and not _plain_str(runtime_version, 128):
            problems.append("device.runtime_version must be a non-empty string of at most 128 chars")
        capability = device.get("playback_watermark_capability")
        if capability is not None and capability not in PLAYBACK_WATERMARK_CAPABILITIES:
            problems.append(
                "device.playback_watermark_capability must be one of: "
                f"{', '.join(sorted(PLAYBACK_WATERMARK_CAPABILITIES))}"
            )

    evidence = receipt.get("evidence")
    if isinstance(evidence, list):
        seen_paths: set[str] = set()
        for index, entry in enumerate(evidence):
            if not isinstance(entry, dict):
                problems.append(f"evidence[{index}] must be an object")
                continue
            e_unknown = sorted(set(entry) - EVIDENCE_ITEM_KEYS)
            if e_unknown:
                problems.append(f"evidence[{index}] unknown keys: {', '.join(e_unknown)}")
            e_missing = sorted(EVIDENCE_ITEM_KEYS - set(entry))
            if e_missing:
                problems.append(f"evidence[{index}] missing keys: {', '.join(e_missing)}")
            e_path = entry.get("path")
            e_digest = entry.get("sha256")
            e_kind = entry.get("kind")
            if not _plain_str(e_path, 512):
                problems.append(f"evidence[{index}].path must be a non-empty string of at most 512 chars")
            if not isinstance(e_digest, str) or re.fullmatch(r"[0-9a-f]{64}", e_digest) is None:
                problems.append(f"evidence[{index}].sha256 must be 64 lowercase hex chars")
            if e_kind not in EVIDENCE_KINDS:
                problems.append(f"evidence[{index}].kind must be one of: {', '.join(EVIDENCE_KINDS)}")
            if isinstance(e_path, str):
                if e_path in seen_paths:
                    problems.append(f"duplicate evidence path {e_path!r}")
                seen_paths.add(e_path)
    return problems


def _receipt_ttl(receipt: dict[str, object], category: str) -> int:
    default = DEFAULT_TTL_HOURS.get(category)
    if default is None:
        return 0
    ttl = receipt.get("ttl_hours")
    if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 1:
        return min(ttl, default)
    return default


def _validate_ttl(
    receipt: dict[str, object], *, now: datetime, max_age_hours: int | None
) -> list[str]:
    collected = receipt.get("collected_at")
    if not isinstance(collected, str):
        return ["collected_at must be an ISO-8601 string with timezone"]
    try:
        parsed = datetime.fromisoformat(collected)
    except ValueError as exc:
        return [f"collected_at is not ISO-8601: {exc}"]
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ["collected_at must include a timezone"]
    category = cast(str, receipt.get("category"))
    ttl_hours = max_age_hours if max_age_hours is not None else _receipt_ttl(receipt, category)
    age = now - parsed.astimezone(UTC)
    if age < -timedelta(minutes=DEFAULT_MAX_FUTURE_MINUTES):
        return [f"collected_at is in the future ({age})"]
    if age > timedelta(hours=ttl_hours):
        return [f"receipt is expired: age {age} exceeds ttl {ttl_hours}h"]
    return []

def _matches_forbidden(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in FORBIDDEN_VALUE_PATTERNS)


def _walk_forbidden(value: object, where: str, problems: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{where}.{key}"
            for segment in re.split(r"[_.\-\s]+", key.lower()):
                if segment in FORBIDDEN_KEY_SEGMENTS:
                    problems.append(f"{path}: forbidden key segment {segment!r}")
            if isinstance(child, str) and _matches_forbidden(child):
                problems.append(f"{path}: forbidden content pattern")
            _walk_forbidden(child, path, problems)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{where}[{index}]", problems)
    elif isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            problems.append(f"{where}: string exceeds {MAX_STRING_CHARS} chars (possible embedded audio/transcript)")
        if _matches_forbidden(value):
            problems.append(f"{where}: forbidden content pattern")


def _scan_forbidden(receipt: dict[str, object]) -> list[str]:
    problems: list[str] = []
    _walk_forbidden(receipt, "$", problems)
    return problems


def _resolve_within_root(root: Path, raw: str) -> tuple[Path | None, list[str]]:
    if not raw or "\x00" in raw:
        return None, [f"evidence path is empty or contains NUL: {raw!r}"]
    candidate = Path(raw)
    if candidate.is_absolute():
        if not candidate.is_relative_to(root):
            return None, [f"evidence path escapes evidence root: {raw!r}"]
        rel = candidate.relative_to(root)
    else:
        if ".." in candidate.parts:
            return None, [f"evidence path must stay inside the evidence root: {raw!r}"]
        rel = candidate
    current = root
    for part in rel.parts:
        if part in ("..", ""):
            return None, [f"evidence path must stay inside the evidence root: {raw!r}"]
        current = current / part
        if current.is_symlink():
            return None, [f"evidence path contains a symlink: {raw!r}"]
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        return None, [f"evidence path escapes evidence root: {raw!r}"]
    return resolved, []


def _evidence_filename_forbidden(raw: str) -> bool:
    if Path(raw).suffix.lower() in FORBIDDEN_EVIDENCE_EXTENSIONS:
        return True
    name = Path(raw).name.lower()
    return "transcript" in name or "转写" in name or "原始音频" in name


def _validate_evidence(
    receipt: dict[str, object],
    *,
    receipt_path: Path,
    evidence_root: Path,
    max_bytes: int,
) -> list[str]:
    problems: list[str] = []
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list):
        return problems
    root = evidence_root.resolve()
    resolved_receipt = receipt_path.resolve()
    for index, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(raw, str) or not isinstance(digest, str):
            continue
        if _evidence_filename_forbidden(raw):
            problems.append(f"evidence[{index}].path {raw!r}: raw audio/transcript artifacts are forbidden")
        resolved, resolve_problems = _resolve_within_root(root, raw)
        problems.extend(resolve_problems)
        if resolved is None:
            continue
        if resolved == resolved_receipt:
            problems.append(f"evidence[{index}] must not reference the receipt itself")
            continue
        if not resolved.is_file():
            problems.append(f"evidence[{index}] is not a regular file: {raw!r}")
            continue
        if resolved.stat().st_size > max_bytes:
            problems.append(f"evidence[{index}] exceeds {max_bytes} bytes: {raw!r}")
            continue
        if _sha256_file(resolved) != digest.lower():
            problems.append(f"evidence[{index}] sha256 mismatch for {raw!r}")
    return problems


def _flag(container: dict[str, object], key: str, problems: list[str], *, expected: bool) -> None:
    if container.get(key) is not expected:
        problems.append(f"scenario.{key} must be {expected!r}")


def _enum(container: dict[str, object], key: str, allowed: frozenset[object], problems: list[str]) -> None:
    value = container.get(key)
    if isinstance(value, (dict, list)) or value not in allowed:
        shown = ", ".join(repr(item) for item in sorted(allowed, key=repr))
        problems.append(f"scenario.{key} must be one of: {shown}")


def _nonempty(container: dict[str, object], key: str, problems: list[str]) -> None:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        problems.append(f"scenario.{key} must be a non-empty string")


def _int(
    container: dict[str, object],
    key: str,
    problems: list[str],
    *,
    min_value: int | None = None,
    max_value: int | None = None,
    exact: int | None = None,
) -> None:
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        problems.append(f"scenario.{key} must be an integer")
        return
    if exact is not None:
        if value != exact:
            problems.append(f"scenario.{key} must equal {exact}")
        return
    if min_value is not None and value < min_value:
        problems.append(f"scenario.{key} must be >= {min_value}")
    if max_value is not None and value > max_value:
        problems.append(f"scenario.{key} must be <= {max_value}")


def _num(
    container: dict[str, object],
    key: str,
    problems: list[str],
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> None:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"scenario.{key} must be a number")
        return
    number = float(value)
    if min_value is not None and number < min_value:
        problems.append(f"scenario.{key} must be >= {min_value}")
    if max_value is not None and number > max_value:
        problems.append(f"scenario.{key} must be <= {max_value}")


def _words(
    container: dict[str, object],
    key: str,
    canonical: Sequence[str],
    problems: list[str],
    *,
    min_count: int | None = None,
    require_all: bool = True,
) -> None:
    value = container.get(key)
    if not isinstance(value, list) or not value:
        problems.append(f"scenario.{key} must be a non-empty list")
        return
    seen: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            problems.append(f"scenario.{key} entries must be strings")
            continue
        if entry not in canonical:
            problems.append(f"scenario.{key} contains non-canonical value {entry!r}")
        else:
            seen.append(entry)
    if require_all:
        missing = [word for word in canonical if word not in seen]
        if missing:
            problems.append(f"scenario.{key} must include all of: {', '.join(missing)}")
    if min_count is not None and len(set(seen)) < min_count:
        problems.append(f"scenario.{key} must cover at least {min_count} distinct canonical values")


def _numbers(
    container: dict[str, object],
    key: str,
    canonical: Sequence[float],
    problems: list[str],
    *,
    min_count: int,
) -> None:
    value = container.get(key)
    if not isinstance(value, list) or not value:
        problems.append(f"scenario.{key} must be a non-empty list")
        return
    covered: set[float] = set()
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            problems.append(f"scenario.{key} entries must be numbers")
            continue
        if not any(abs(float(entry) - float(item)) < 1e-9 for item in canonical):
            problems.append(f"scenario.{key} contains non-canonical value {entry!r}")
        else:
            covered.add(float(entry))
    if len(covered) < min_count:
        problems.append(f"scenario.{key} must cover at least {min_count} distinct canonical values")

def _cell_problems(cell: object, index: int) -> list[str]:
    problems: list[str] = []
    where = f"scenario.aec_matrix[{index}]"
    if not isinstance(cell, dict):
        return [f"{where} must be an object"]
    if not cell:
        return [f"{where} must not be empty"]
    unknown = sorted(set(cell) - set(AEC_CELL_KEYS))
    if unknown:
        problems.append(f"{where} unknown keys: {', '.join(unknown)}")
    missing = sorted(set(AEC_CELL_KEYS) - set(cell))
    if missing:
        problems.append(f"{where} missing keys: {', '.join(missing)}")
    if cell.get("mode") not in AEC_MODES:
        problems.append(f"{where}.mode must be one of: {', '.join(AEC_MODES)}")
    volume = cell.get("volume_pct")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not any(
        abs(float(volume) - item) < 1e-9 for item in AEC_VOLUMES
    ):
        problems.append(f"{where}.volume_pct must be one of: 20/50/80/100")
    distance = cell.get("distance_m")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not any(
        abs(float(distance) - item) < 1e-9 for item in AEC_DISTANCES
    ):
        problems.append(f"{where}.distance_m must be one of: 0.3/1/2/3")
    if cell.get("orientation") not in AEC_ORIENTATIONS:
        problems.append(f"{where}.orientation must be one of: {', '.join(AEC_ORIENTATIONS)}")
    if cell.get("environment") not in AEC_ENVIRONMENTS:
        problems.append(f"{where}.environment must be one of: {', '.join(AEC_ENVIRONMENTS)}")
    if cell.get("voice") not in AEC_VOICES:
        problems.append(f"{where}.voice must be one of: {', '.join(AEC_VOICES)}")
    if cell.get("content") not in AEC_CONTENT:
        problems.append(f"{where}.content must be one of: {', '.join(AEC_CONTENT)}")
    return problems


def _heap_problems(scenario: dict[str, object], problems: list[str]) -> None:
    samples = scenario.get("heap_samples")
    if isinstance(samples, list) and samples:
        values: list[float] = []
        for entry in samples:
            if isinstance(entry, bool) or not isinstance(entry, (int, float)):
                problems.append("scenario.heap_samples entries must be numbers")
                return
            values.append(float(entry))
        if len(values) < 20:
            problems.append("scenario.heap_samples must contain at least 20 samples")
            return
        if values[-1] < values[0]:
            problems.append("scenario.heap_samples shows sustained heap decline (last < first)")
        return
    _enum(scenario, "heap_trend", frozenset({"non_decreasing"}), problems)


def _declares_exact_playback_gate(item_id: str, scenario: object) -> bool:
    """Whether a pass scenario claims an exact DAC/actual-heard watermark gate.

    T5-T7 are playback-to-DAC items by definition; T1/T8 declare their exact
    watermark/actual-heard claim in the scenario. This is the single decision
    point for the capability gate: any scenario declaring "watermark=exact"
    or "actual_heard=exact" must be backed by a device that declares
    "playback_watermark_capability=exact".
    """

    if item_id in {"T5", "T6", "T7"}:
        return True
    if not isinstance(scenario, dict):
        return False
    return (
        scenario.get("watermark") == PLAYBACK_WATERMARK_CAPABILITY_EXACT
        or scenario.get("actual_heard") == PLAYBACK_WATERMARK_CAPABILITY_EXACT
    )


def _validate_scenario(item_id: str, scenario: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(scenario, dict):
        return ["scenario must be an object"]
    if item_id == "T1":
        _int(scenario, "playback_count", problems, min_value=1)
        _flag(scenario, "playback_ack", problems, expected=True)
        _enum(scenario, "watermark", frozenset({"exact"}), problems)
        _int(scenario, "played_samples", problems, min_value=1)
    elif item_id == "T2":
        _enum(scenario, "sample_rate", frozenset({16000, 24000}), problems)
        _int(scenario, "channels", problems, exact=1)
        _int(scenario, "pops", problems, exact=0)
        _int(scenario, "frame_loss", problems, exact=0)
        _enum(scenario, "clock", frozenset({"stable"}), problems)
    elif item_id == "T3":
        _nonempty(scenario, "offline_aec_version", problems)
        _nonempty(scenario, "capture_session_id", problems)
        _flag(scenario, "mic_ref_aligned", problems, expected=True)
        _num(scenario, "erle_db", problems, min_value=0.0)
        _enum(scenario, "residual", frozenset({"pass"}), problems)
        _enum(scenario, "processing", frozenset({"offline"}), problems)
    elif item_id == "T4":
        _enum(scenario, "asr_engine", frozenset({"funasr_final"}), problems)
        _flag(scenario, "final_shown", problems, expected=True)
        _num(scenario, "cer", problems, min_value=0.0, max_value=T4_CER_MAX)
    elif item_id == "T5":
        _enum(scenario, "tts_source", frozenset({"fixed_text"}), problems)
        _flag(scenario, "played", problems, expected=True)
        _flag(scenario, "playback_ack", problems, expected=True)
        _int(scenario, "generations_played", problems, exact=1)
    elif item_id == "T6":
        _enum(scenario, "llm_source", frozenset({"injected_user_text"}), problems)
        _flag(scenario, "tts_played", problems, expected=True)
        _flag(scenario, "dac_played", problems, expected=True)
    elif item_id == "T7":
        for key in ("mic_vad", "asr_final", "llm_reply", "tts_played", "dac_played"):
            _flag(scenario, key, problems, expected=True)
        _enum(scenario, "single_turn", frozenset({"complete"}), problems)
    elif item_id == "T8":
        _enum(scenario, "interrupt", frozenset({"button"}), problems)
        _int(scenario, "trials", problems, min_value=10)
        _num(scenario, "stop_p95_ms", problems, max_value=STOP_P95_MS["T8"])
        _int(scenario, "old_generation_replay", problems, exact=0)
        _enum(scenario, "actual_heard", frozenset({"exact"}), problems)
    elif item_id == "T9":
        _words(scenario, "stop_words", STOP_WORDS, problems)
        _numbers(scenario, "distances_m", AEC_DISTANCES, problems, min_count=3)
        _numbers(scenario, "volumes_pct", AEC_VOLUMES, problems, min_count=2)
        _words(scenario, "noises", AEC_ENVIRONMENTS, problems, min_count=2, require_all=False)
        _num(scenario, "hit_rate", problems, min_value=0.0, max_value=1.0)
        _num(scenario, "false_trigger_rate", problems, min_value=0.0, max_value=1.0)
        _num(scenario, "stop_p95_ms", problems, max_value=STOP_P95_MS["T9"])
    elif item_id == "T10":
        _enum(scenario, "interrupt", frozenset({"natural"}), problems)
        _flag(scenario, "new_question_full", problems, expected=True)
        _flag(scenario, "old_reply_cancelled", problems, expected=True)
        _flag(scenario, "new_turn_established", problems, expected=True)
        _num(scenario, "stop_p95_ms", problems, max_value=STOP_P95_MS["T10"])
        cells = scenario.get("aec_matrix")
        if not isinstance(cells, list) or not cells:
            problems.append("scenario.aec_matrix must be a non-empty list of canonical cells")
        else:
            if not any(
                isinstance(cell, dict)
                and _cell_problems(cell, -1) == []
                and cell.get("mode") == "double_talk"
                for cell in cells
            ):
                problems.append("scenario.aec_matrix must contain at least one valid double_talk cell")
            for index, cell in enumerate(cells):
                problems.extend(_cell_problems(cell, index))
    elif item_id == "T11":
        _words(scenario, "backchannel_words", BACKCHANNEL_WORDS, problems)
        _int(scenario, "trials", problems, min_value=20)
        _num(scenario, "false_cancel_rate", problems, min_value=0.0, max_value=BACKCHANNEL_FALSE_CANCEL_CAP)
    elif item_id == "T12":
        _words(scenario, "echo_sources", ("robot_voice", "tv_voice"), problems, min_count=1, require_all=False)
        _flag(scenario, "private_memory_written", problems, expected=False)
        _int(scenario, "false_barge_in_count", problems, exact=0)
        _flag(scenario, "memory_checked", problems, expected=True)
    elif item_id == "T13":
        _enum(scenario, "scope", frozenset({"merge", "night", "full"}), problems)
        scope = scenario.get("scope")
        if scope in ("merge", "full"):
            _int(scenario, "rounds_merge", problems, min_value=T13_MERGE_ROUNDS)
        if scope in ("night", "full"):
            _int(scenario, "rounds_night", problems, min_value=T13_NIGHT_ROUNDS)
        _int(scenario, "panics", problems, exact=0)
        _int(scenario, "watchdogs", problems, exact=0)
        _heap_problems(scenario, problems)
        _int(scenario, "generation_revival", problems, exact=0)
        _flag(scenario, "wss_reconnect_checked", problems, expected=True)
    elif item_id == "T14":
        _enum(scenario, "platform", frozenset(T14_PLATFORMS), problems)
        steps = scenario.get("steps")
        if not isinstance(steps, dict):
            problems.append("scenario.steps must be an object")
        else:
            unknown_steps = sorted(set(steps) - set(T14_STEP_KEYS))
            if unknown_steps:
                problems.append(f"unknown scenario.steps keys: {', '.join(unknown_steps)}")
            for key in T14_STEP_KEYS:
                if steps.get(key) is not True:
                    problems.append(f"scenario.steps.{key} must be true")
        _flag(scenario, "records_authoritative", problems, expected=True)
    else:
        problems.append(f"unknown item {item_id}")
    return problems


# ---------------------------------------------------------------------------
# collect: golden trace -> T4-T7 candidate receipts (fail-closed)
# ---------------------------------------------------------------------------


def _parse_trace_time(raw: object, where: str, problems: list[str]) -> datetime | None:
    if not isinstance(raw, str):
        problems.append(f"{where} must be an ISO-8601 string with timezone")
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        problems.append(f"{where} is not ISO-8601: {exc}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        problems.append(f"{where} must include a timezone")
        return None
    return parsed.astimezone(UTC)


def _trace_device_problems(device: object) -> list[str]:
    """Mirror the receipt device block so a trace cannot smuggle an invalid device."""

    problems: list[str] = []
    if not isinstance(device, dict):
        return ["device must be an object"]
    unknown = sorted(set(device) - DEVICE_KEYS)
    if unknown:
        problems.append(f"device unknown keys: {', '.join(unknown)}")
    missing = sorted(REQUIRED_DEVICE_KEYS - set(device))
    if missing:
        problems.append(f"device missing keys: {', '.join(missing)}")
    _expect(device, "device_id", str, problems, prefix="device.")
    _expect(device, "board", str, problems, prefix="device.")
    _expect(device, "firmware_sha256", str, problems, prefix="device.")
    _expect(device, "runtime", str, problems, prefix="device.")
    device_id = device.get("device_id")
    if not _plain_str(device_id, 128):
        problems.append("device.device_id must be a non-empty string of at most 128 chars")
    elif "/" in cast(str, device_id) or "\\" in cast(str, device_id):
        problems.append("device.device_id must not contain path separators")
    board = device.get("board")
    if not _plain_str(board, 128):
        problems.append("device.board must be a non-empty string of at most 128 chars")
    elif "/" in cast(str, board) or "\\" in cast(str, board):
        problems.append("device.board must not contain path separators")
    firmware = device.get("firmware_sha256")
    if not isinstance(firmware, str) or re.fullmatch(r"[0-9a-fA-F]{40,64}", firmware) is None:
        problems.append("device.firmware_sha256 must be 40-64 hex chars")
    runtime = device.get("runtime")
    if runtime not in RUN_TIMES:
        problems.append(f"device.runtime must be one of: {', '.join(RUN_TIMES)}")
    runtime_version = device.get("runtime_version")
    if runtime_version is not None and not _plain_str(runtime_version, 128):
        problems.append("device.runtime_version must be a non-empty string of at most 128 chars")
    capability = device.get("playback_watermark_capability")
    if capability is not None and capability not in PLAYBACK_WATERMARK_CAPABILITIES:
        problems.append(
            "device.playback_watermark_capability must be one of: "
            f"{', '.join(sorted(PLAYBACK_WATERMARK_CAPABILITIES))}"
        )
    return problems


def _validate_trace(value: object) -> list[str]:
    """Strict golden-trace schema. Type errors are rejected; missing or weak
    hardware evidence is handled later by the per-item analyzers (blocked)."""

    problems: list[str] = []
    if not isinstance(value, dict):
        return ["trace root must be a JSON object"]
    unknown = sorted(set(value) - TRACE_TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"trace unknown keys: {', '.join(unknown)}")
    missing = sorted(TRACE_REQUIRED_KEYS - set(value))
    if missing:
        problems.append(f"trace missing keys: {', '.join(missing)}")
    if value.get("schema_version") != TRACE_SCHEMA_VERSION:
        problems.append(f"trace schema_version must be {TRACE_SCHEMA_VERSION!r}")
    if value.get("trace_type") != TRACE_TYPE:
        problems.append(f"trace trace_type must be {TRACE_TYPE!r}")
    if value.get("origin") not in TRACE_ORIGINS:
        problems.append(f"trace origin must be one of: {', '.join(TRACE_ORIGINS)}")
    session_id = value.get("session_id")
    if not _plain_str(session_id, 128):
        problems.append("trace session_id must be a non-empty string of at most 128 chars")
    elif "/" in cast(str, session_id) or "\\" in cast(str, session_id):
        problems.append("trace session_id must not contain path separators")
    stream_epoch = value.get("stream_epoch")
    if not isinstance(stream_epoch, int) or isinstance(stream_epoch, bool) or stream_epoch < 1:
        problems.append("trace stream_epoch must be a positive integer")
    problems.extend(_trace_device_problems(value.get("device")))
    _parse_trace_time(value.get("collected_at"), "trace.collected_at", problems)
    events = value.get("events")
    if not isinstance(events, list) or not events:
        problems.append("trace events must be a non-empty list")
        return problems
    if len(events) > TRACE_MAX_EVENTS:
        problems.append(f"trace events must not exceed {TRACE_MAX_EVENTS}")
        return problems
    previous: datetime | None = None
    for index, event in enumerate(events):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            problems.append(f"{prefix} must be an object")
            continue
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in TRACE_EVENT_TYPES:
            problems.append(
                f"{prefix}.type must be one of: {', '.join(sorted(TRACE_EVENT_TYPES))}"
            )
            continue
        e_unknown = sorted(set(event) - TRACE_EVENT_KEYS[event_type])
        if e_unknown:
            problems.append(f"{prefix} unknown keys: {', '.join(e_unknown)}")
        at = _parse_trace_time(event.get("at"), f"{prefix}.at", problems)
        if at is not None:
            if previous is not None and at < previous:
                problems.append(
                    f"{prefix}.at moves backwards ({at.isoformat()} < {previous.isoformat()})"
                )
            previous = at
        if index == 0 and event_type != "session.accepted":
            problems.append(f"{prefix} first event must be session.accepted")
        if event_type == "session.accepted":
            if event.get("session_id") != session_id:
                problems.append(f"{prefix}.session_id does not match trace session_id")
            if event.get("stream_epoch") != stream_epoch:
                problems.append(f"{prefix}.stream_epoch does not match trace stream_epoch")
        elif event_type in {"vad.start", "vad.end"}:
            sample = event.get("sample")
            if not isinstance(sample, int) or isinstance(sample, bool) or sample < 0:
                problems.append(f"{prefix}.sample must be a non-negative integer")
        elif event_type == "uplink.audio":
            frames = event.get("frames")
            if not isinstance(frames, int) or isinstance(frames, bool) or frames < 1:
                problems.append(f"{prefix}.frames must be a positive integer")
        elif event_type == "asr.final":
            if event.get("engine") != "funasr":
                problems.append(f"{prefix}.engine must be 'funasr'")
            for key in ("recognized_text", "reference_text"):
                text = event.get(key)
                if text is not None and (
                    not isinstance(text, str) or len(text) > TRACE_MAX_TEXT_CHARS
                ):
                    problems.append(
                        f"{prefix}.{key} must be a string of at most {TRACE_MAX_TEXT_CHARS} chars"
                    )
            if not isinstance(event.get("displayed"), bool):
                problems.append(f"{prefix}.displayed must be a boolean")
        elif event_type == "turn.committed":
            for key in ("turn_id", "generation_id"):
                number = event.get(key)
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    problems.append(f"{prefix}.{key} must be a positive integer")
            tool_epoch = event.get("tool_epoch")
            if not isinstance(tool_epoch, int) or isinstance(tool_epoch, bool) or tool_epoch < 0:
                problems.append(f"{prefix}.tool_epoch must be a non-negative integer")
        elif event_type in {"user.text.injected", "llm.reply"}:
            text = event.get("text")
            if text is not None and (
                not isinstance(text, str) or len(text) > TRACE_MAX_TEXT_CHARS
            ):
                problems.append(
                    f"{prefix}.text must be a string of at most {TRACE_MAX_TEXT_CHARS} chars"
                )
            generation = event.get("generation_id")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                problems.append(f"{prefix}.generation_id must be a positive integer")
        elif event_type == "tts.started":
            generation = event.get("generation_id")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                problems.append(f"{prefix}.generation_id must be a positive integer")
            if event.get("source") not in TRACE_TTS_SOURCES:
                problems.append(
                    f"{prefix}.source must be one of: {', '.join(sorted(TRACE_TTS_SOURCES))}"
                )
        elif event_type == "playback.started":
            generation = event.get("generation_id")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                problems.append(f"{prefix}.generation_id must be a positive integer")
            if not isinstance(event.get("dac_verified"), bool):
                problems.append(f"{prefix}.dac_verified must be a boolean")
        elif event_type == "playback.ended":
            generation = event.get("generation_id")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                problems.append(f"{prefix}.generation_id must be a positive integer")
            for key in ("ack", "dac_verified"):
                if not isinstance(event.get(key), bool):
                    problems.append(f"{prefix}.{key} must be a boolean")
            if event.get("watermark_precision") not in TRACE_WATERMARK_PRECISIONS:
                problems.append(
                    f"{prefix}.watermark_precision must be one of: "
                    f"{', '.join(sorted(TRACE_WATERMARK_PRECISIONS))}"
                )
        elif event_type == "error":
            message = event.get("message")
            if not isinstance(message, str) or not message.strip():
                problems.append(f"{prefix}.message must be a non-empty string")
    return problems


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        current = [index]
        for jindex, right_char in enumerate(right, 1):
            current.append(
                min(
                    previous[jindex] + 1,
                    current[jindex - 1] + 1,
                    previous[jindex - 1] + int(left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _cer(reference: str, recognized: str) -> float:
    distance = _edit_distance(reference, recognized)
    return round(min(1.0, distance / max(1, len(reference))), 4)


def _normalize_asr_text(text: str) -> str:
    """Normalize reference/ASR text before the CER and length gates.

    Existing normalization: drop all whitespace so the reference-length gate
    and the CER inputs measure the same characters.
    """

    return "".join(text.split())


@dataclass(frozen=True)
class _CollectOutcome:
    item_id: str
    result: str
    scenario: dict[str, object]
    reasons: tuple[str, ...]


def _events_of(trace: dict[str, object], event_type: str) -> list[dict[str, object]]:
    return [
        event
        for event in cast(list[dict[str, object]], trace["events"])
        if isinstance(event, dict) and event.get("type") == event_type
    ]


def _generations(events: list[dict[str, object]]) -> set[int]:
    found: set[int] = set()
    for event in events:
        generation = event.get("generation_id")
        if isinstance(generation, int) and not isinstance(generation, bool):
            found.add(generation)
    return found


def _device_watermark_capability(trace: dict[str, object]) -> str | None:
    """Return the trace device's declared playback watermark capability, or
    None when it is missing. Missing is fail-closed for DAC-gated items."""

    device = trace.get("device")
    if not isinstance(device, dict):
        return None
    capability = device.get("playback_watermark_capability")
    if capability in PLAYBACK_WATERMARK_CAPABILITIES:
        return cast(str, capability)
    return None


def _has_text(events: list[dict[str, object]]) -> bool:
    return any(
        isinstance(event.get("text"), str) and bool(event["text"].strip())
        for event in events
    )


def _event_indices(
    trace: dict[str, object], event_type: str, **expected: object
) -> list[int]:
    events = cast(list[dict[str, object]], trace["events"])
    return [
        index
        for index, event in enumerate(events)
        if event.get("type") == event_type
        and all(event.get(key) == value for key, value in expected.items())
    ]


def _has_ordered_indices(*groups: list[int]) -> bool:
    if not groups or any(not group for group in groups):
        return False
    cursor = -1
    for group in groups:
        next_index = next((index for index in group if index > cursor), None)
        if next_index is None:
            return False
        cursor = next_index
    return True


def _playback_chain_reasons(
    trace: dict[str, object], generation: int, *, after_indices: list[int]
) -> list[str]:
    reasons: list[str] = []
    capability = _device_watermark_capability(trace)
    if capability != PLAYBACK_WATERMARK_CAPABILITY_EXACT:
        if capability is None:
            reasons.append(
                "device.playback_watermark_capability is missing (fail-closed): "
                "the exact DAC/actual-heard gate cannot pass without capability=exact"
            )
        else:
            reasons.append(
                f"device.playback_watermark_capability={capability!r} forbids the exact "
                "DAC/actual-heard gate (fail-closed)"
            )
        return reasons
    events = cast(list[dict[str, object]], trace["events"])
    started = _event_indices(trace, "playback.started", generation_id=generation)
    ended = _event_indices(trace, "playback.ended", generation_id=generation)
    if not started:
        reasons.append(f"missing playback.started for generation {generation}")
    if not ended:
        reasons.append(f"missing playback.ended for generation {generation}")
    exact_ended = [
        index
        for index in ended
        if events[index].get("ack") is True
        and events[index].get("dac_verified") is True
        and events[index].get("watermark_precision") == "exact"
    ]
    if not _has_ordered_indices(after_indices, started, exact_ended):
        reasons.append(DAC_EVIDENCE_NOTE)
    return reasons


def _privacy_redacted_trace(trace: dict[str, object]) -> dict[str, object]:
    redacted = cast(dict[str, object], json.loads(json.dumps(trace, ensure_ascii=False)))
    for event in cast(list[dict[str, object]], redacted["events"]):
        for key in ("recognized_text", "reference_text", "text", "message"):
            if isinstance(event.get(key), str):
                event[key] = "[REDACTED]"
    return redacted


def _collect_t4(trace: dict[str, object]) -> _CollectOutcome:
    reasons: list[str] = []
    vad_started = _event_indices(trace, "vad.start")
    vad_ended = _event_indices(trace, "vad.end")
    uplink = _event_indices(trace, "uplink.audio")
    final_indices = _event_indices(trace, "asr.final", engine="funasr")
    if not vad_started or not vad_ended:
        reasons.append("missing vad.start/vad.end (device VAD speech epoch)")
    if not uplink:
        reasons.append("missing uplink.audio (device mic -> Edge -> Voice Core)")
    finals = [
        event
        for event in _events_of(trace, "asr.final")
        if event.get("engine") == "funasr"
    ]
    cer: float | None = None
    if not finals:
        reasons.append("missing asr.final with engine=funasr (Voice Core FunASR final)")
    else:
        final = finals[-1]
        if final.get("displayed") is not True:
            reasons.append(
                "asr.final displayed is not true (final not confirmed on device screen/log)"
            )
        reference = final.get("reference_text")
        recognized = final.get("recognized_text")
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or not isinstance(recognized, str)
            or not recognized.strip()
        ):
            reasons.append(
                "asr.final missing reference_text/recognized_text (cannot compute CER)"
            )
        else:
            normalized_reference = _normalize_asr_text(reference)
            normalized_recognized = _normalize_asr_text(recognized)
            cer = _cer(normalized_reference, normalized_recognized)
            if len(normalized_reference) < T4_REFERENCE_MIN_CHARS:
                reasons.append(
                    "asr.final reference_text too short "
                    f"({len(normalized_reference)} normalized chars < "
                    f"{T4_REFERENCE_MIN_CHARS}): cannot produce meaningful CER"
                )
            if cer > T4_CER_MAX:
                reasons.append(f"asr.final CER {cer} exceeds threshold {T4_CER_MAX}")
    if (
        vad_started
        and vad_ended
        and uplink
        and final_indices
        and not _has_ordered_indices(vad_started, uplink, vad_ended, final_indices)
    ):
        reasons.append("VAD/uplink/FunASR final events are not in causal order")
    if reasons:
        return _CollectOutcome("T4", "blocked", {}, tuple(reasons))
    return _CollectOutcome(
        "T4",
        "pass",
        {"asr_engine": "funasr_final", "final_shown": True, "cer": cast(float, cer)},
        (),
    )


def _collect_t5(trace: dict[str, object]) -> _CollectOutcome:
    reasons: list[str] = []
    fixed_tts = [
        event
        for event in _events_of(trace, "tts.started")
        if event.get("source") == "fixed_text"
    ]
    generations = _generations(fixed_tts)
    if not fixed_tts:
        reasons.append("missing tts.started with source=fixed_text (TTS-only fixed text)")
    elif len(generations) != 1:
        reasons.append("fixed-text TTS must span exactly one generation")
    if len(generations) == 1:
        generation = next(iter(generations))
        fixed_tts_indices = _event_indices(
            trace, "tts.started", source="fixed_text", generation_id=generation
        )
        reasons.extend(
            _playback_chain_reasons(
                trace, generation, after_indices=fixed_tts_indices
            )
        )
    if reasons:
        return _CollectOutcome("T5", "blocked", {}, tuple(reasons))
    return _CollectOutcome(
        "T5",
        "pass",
        {
            "tts_source": "fixed_text",
            "played": True,
            "playback_ack": True,
            "generations_played": 1,
        },
        (),
    )


def _collect_t6(trace: dict[str, object]) -> _CollectOutcome:
    reasons: list[str] = []
    injected = _events_of(trace, "user.text.injected")
    replies = _events_of(trace, "llm.reply")
    if not _has_text(injected):
        reasons.append("missing user.text.injected (server-injected user text)")
    if not _has_text(replies):
        reasons.append("missing llm.reply (LLM reply)")
    llm_tts = [
        event
        for event in _events_of(trace, "tts.started")
        if event.get("source") == "llm"
    ]
    generations = _generations(llm_tts)
    if not llm_tts:
        reasons.append("missing tts.started with source=llm")
    elif len(generations) != 1:
        reasons.append("LLM TTS must span exactly one generation")
    if len(generations) == 1:
        generation = next(iter(generations))
        events = cast(list[dict[str, object]], trace["events"])
        injected_indices = [
            index
            for index in _event_indices(
                trace, "user.text.injected", generation_id=generation
            )
            if isinstance(events[index].get("text"), str)
            and bool(cast(str, events[index]["text"]).strip())
        ]
        reply_indices = [
            index
            for index in _event_indices(trace, "llm.reply", generation_id=generation)
            if isinstance(events[index].get("text"), str)
            and bool(cast(str, events[index]["text"]).strip())
        ]
        tts_indices = _event_indices(
            trace, "tts.started", source="llm", generation_id=generation
        )
        if injected and not injected_indices:
            reasons.append("user.text.injected does not match the LLM TTS generation")
        if replies and not reply_indices:
            reasons.append("llm.reply does not match the LLM TTS generation")
        if (
            injected_indices
            and reply_indices
            and tts_indices
            and not _has_ordered_indices(injected_indices, reply_indices, tts_indices)
        ):
            reasons.append("injected text/LLM reply/TTS events are not in causal order")
        reasons.extend(
            _playback_chain_reasons(trace, generation, after_indices=tts_indices)
        )
    if reasons:
        return _CollectOutcome("T6", "blocked", {}, tuple(reasons))
    return _CollectOutcome(
        "T6",
        "pass",
        {
            "llm_source": "injected_user_text",
            "tts_played": True,
            "dac_played": True,
        },
        (),
    )


def _collect_t7(trace: dict[str, object]) -> _CollectOutcome:
    reasons: list[str] = []
    vad_started = _event_indices(trace, "vad.start")
    vad_ended = _event_indices(trace, "vad.end")
    uplink = _event_indices(trace, "uplink.audio")
    final_indices = _event_indices(trace, "asr.final", engine="funasr")
    if not vad_started or not vad_ended:
        reasons.append("missing vad.start/vad.end (device VAD speech epoch)")
    if not uplink:
        reasons.append("missing uplink.audio (device mic -> Edge -> Voice Core)")
    finals = [
        event
        for event in _events_of(trace, "asr.final")
        if event.get("engine") == "funasr"
    ]
    if not finals:
        reasons.append("missing asr.final with engine=funasr (Voice Core FunASR final)")
    elif finals[-1].get("displayed") is not True:
        reasons.append(
            "asr.final displayed is not true (final not confirmed on device screen/log)"
        )
    committed = _events_of(trace, "turn.committed")
    if not committed:
        reasons.append("missing turn.committed (authoritative turn commit)")
    elif len(committed) != 1:
        reasons.append("single_turn requires exactly one turn.committed")
    replies = _events_of(trace, "llm.reply")
    if not _has_text(replies):
        reasons.append("missing llm.reply (LLM reply)")
    llm_tts = [
        event
        for event in _events_of(trace, "tts.started")
        if event.get("source") == "llm"
    ]
    generations = _generations(llm_tts)
    if not llm_tts:
        reasons.append("missing tts.started with source=llm")
    elif len(generations) != 1:
        reasons.append("LLM TTS must span exactly one generation")
    if len(committed) == 1:
        committed_generation = committed[0].get("generation_id")
        if isinstance(committed_generation, int) and not isinstance(
            committed_generation, bool
        ):
            generation = committed_generation
            committed_indices = _event_indices(
                trace, "turn.committed", generation_id=generation
            )
            reply_indices = _event_indices(
                trace, "llm.reply", generation_id=generation
            )
            tts_indices = _event_indices(
                trace, "tts.started", source="llm", generation_id=generation
            )
            if generations and generations != {generation}:
                reasons.append("LLM TTS generation does not match turn.committed")
            if replies and not reply_indices:
                reasons.append("llm.reply generation does not match turn.committed")
            if (
                vad_started
                and uplink
                and vad_ended
                and final_indices
                and committed_indices
                and reply_indices
                and tts_indices
                and not _has_ordered_indices(
                    vad_started,
                    uplink,
                    vad_ended,
                    final_indices,
                    committed_indices,
                    reply_indices,
                    tts_indices,
                )
            ):
                reasons.append("speech/turn/LLM/TTS events are not in causal order")
            reasons.extend(
                _playback_chain_reasons(
                    trace, generation, after_indices=tts_indices
                )
            )
    if reasons:
        return _CollectOutcome("T7", "blocked", {}, tuple(reasons))
    return _CollectOutcome(
        "T7",
        "pass",
        {
            "mic_vad": True,
            "asr_final": True,
            "llm_reply": True,
            "tts_played": True,
            "dac_played": True,
            "single_turn": "complete",
        },
        (),
    )


def _collect_outcomes(trace: dict[str, object]) -> dict[str, _CollectOutcome]:
    """Map one validated trace to T4-T7 outcomes. Any error event blocks all."""

    errors = _events_of(trace, "error")
    if errors:
        messages = "; ".join(
            str(event.get("message", ""))[:200]
            for event in errors
            if isinstance(event.get("message"), str) and event["message"].strip()
        )
        note = (
            f"trace contains error event(s): {messages[:2000]}"
            if messages
            else "trace contains error event(s)"
        )
        return {
            item_id: _CollectOutcome(item_id, "blocked", {}, (note,))
            for item_id in COLLECT_ITEMS
        }
    return {
        "T4": _collect_t4(trace),
        "T5": _collect_t5(trace),
        "T6": _collect_t6(trace),
        "T7": _collect_t7(trace),
    }


@dataclass(frozen=True)
class _ReceiptResult:
    path: str
    item_id: str | None
    result: str | None
    verdict: str
    reasons: tuple[str, ...]
    receipt: dict[str, object] | None


def _check_receipt(
    path: Path,
    *,
    now: datetime,
    max_age_hours: int | None,
    evidence_root: Path,
    max_evidence_bytes: int,
) -> _ReceiptResult:
    receipt: dict[str, object] | None = None
    item_id: str | None = None
    result: str | None = None
    try:
        receipt = _read_receipt(path)
        problems: list[str] = _validate_schema(receipt)
        if not problems:
            item_id = cast(str, receipt["item_id"])
            category = cast(str, receipt["category"])
            result = cast(str, receipt["result"])
            item = ITEM_BY_ID.get(item_id)
            if item is None:
                problems.append(f"unknown item_id {item_id!r}")
            else:
                if category != item.category:
                    problems.append(
                        f"category {category!r} does not match {item_id} (expected {item.category!r})"
                    )
                if category == CATEGORY_REPOSITORY:
                    problems.append("repository items need no receipt and cannot produce real evidence")
            if result not in RESULTS:
                problems.append(f"invalid result {result!r}")
            problems.extend(_validate_ttl(receipt, now=now, max_age_hours=max_age_hours))
            problems.extend(_scan_forbidden(receipt))
            problems.extend(
                _validate_evidence(
                    receipt,
                    receipt_path=path,
                    evidence_root=evidence_root,
                    max_bytes=max_evidence_bytes,
                )
            )
            if result == "pass":
                problems.extend(_validate_scenario(item_id, receipt.get("scenario")))
                if _declares_exact_playback_gate(item_id, receipt.get("scenario")):
                    device = cast(dict[str, object], receipt["device"])
                    capability = device.get("playback_watermark_capability")
                    if capability != PLAYBACK_WATERMARK_CAPABILITY_EXACT:
                        problems.append(
                            f"{item_id} pass requires "
                            "device.playback_watermark_capability='exact'; "
                            f"got {capability!r} (fail-closed)"
                        )
                evidence = receipt.get("evidence")
                if not isinstance(evidence, list) or not evidence:
                    problems.append("result pass requires at least one evidence artifact")
        if problems:
            return _ReceiptResult(str(path), item_id, result, "failed", tuple(problems), receipt)
        reasons: tuple[str, ...] = ()
        if result != "pass":
            reasons = (f"receipt declares result={result}",)
        return _ReceiptResult(str(path), item_id, result, cast(str, result), reasons, receipt)
    except ValueError as exc:
        return _ReceiptResult(str(path), item_id, result, "failed", (str(exc),), receipt)


def _collect_numbers(value: object, into: set[float]) -> None:
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, (int, float)) and not isinstance(entry, bool):
                into.add(float(entry))


def _collect_strings(value: object, into: set[str]) -> None:
    if isinstance(value, str):
        into.add(value)
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, str):
                into.add(entry)


def _collect_max(value: object, current: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(current, value)
    return current


def _coverage_reasons(results: Sequence[_ReceiptResult]) -> list[str]:
    reasons: list[str] = []
    modes: set[str] = set()
    volumes: set[float] = set()
    distances: set[float] = set()
    orientations: set[str] = set()
    environments: set[str] = set()
    voices: set[str] = set()
    contents: set[str] = set()
    platforms: set[str] = set()
    t13_merge = 0
    t13_night = 0
    saw_t9 = False
    saw_t10 = False
    saw_t13 = False
    saw_t14 = False
    for result in results:
        if result.item_id is None or result.receipt is None:
            continue
        scenario = result.receipt.get("scenario")
        if not isinstance(scenario, dict):
            continue
        if result.item_id == "T9":
            saw_t9 = True
            _collect_numbers(scenario.get("distances_m"), distances)
            _collect_numbers(scenario.get("volumes_pct"), volumes)
            _collect_strings(scenario.get("noises"), environments)
        elif result.item_id == "T10":
            saw_t10 = True
            cells = scenario.get("aec_matrix")
            if isinstance(cells, list):
                for cell in cells:
                    if not isinstance(cell, dict):
                        continue
                    _collect_strings(cell.get("mode"), modes)
                    _collect_numbers(cell.get("volume_pct"), volumes)
                    _collect_numbers(cell.get("distance_m"), distances)
                    _collect_strings(cell.get("orientation"), orientations)
                    _collect_strings(cell.get("environment"), environments)
                    _collect_strings(cell.get("voice"), voices)
                    _collect_strings(cell.get("content"), contents)
        elif result.item_id == "T13":
            saw_t13 = True
            t13_merge = _collect_max(scenario.get("rounds_merge"), t13_merge)
            t13_night = _collect_max(scenario.get("rounds_night"), t13_night)
        elif result.item_id == "T14":
            saw_t14 = True
            _collect_strings(scenario.get("platform"), platforms)
    if saw_t9 or saw_t10:
        for label, required, covered in (
            ("mode", set(AEC_MODES), modes),
            ("volume_pct", set(AEC_VOLUMES), volumes),
            ("distance_m", set(AEC_DISTANCES), distances),
            ("orientation", set(AEC_ORIENTATIONS), orientations),
            ("environment", set(AEC_ENVIRONMENTS), environments),
            ("voice", set(AEC_VOICES), voices),
            ("content", set(AEC_CONTENT), contents),
        ):
            missing = sorted(required - covered, key=str)
            if missing:
                reasons.append(f"AEC matrix axis {label} missing: {', '.join(map(str, missing))}")
    else:
        reasons.append("AEC matrix coverage requires at least one passing T9 or T10 receipt")
    if saw_t13:
        if t13_merge < T13_MERGE_ROUNDS:
            reasons.append(f"T13 merge rounds total must be >= {T13_MERGE_ROUNDS}")
        if t13_night < T13_NIGHT_ROUNDS:
            reasons.append(f"T13 night rounds total must be >= {T13_NIGHT_ROUNDS}")
    else:
        reasons.append("T13 stability coverage requires at least one passing T13 receipt")
    if saw_t14:
        missing_platforms = sorted(set(T14_PLATFORMS) - platforms)
        if missing_platforms:
            reasons.append(f"T14 platforms missing: {', '.join(missing_platforms)}")
    else:
        reasons.append("T14 independence coverage requires at least one passing T14 receipt")
    return reasons

@dataclass(frozen=True)
class _ProbeOutcome:
    name: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    ok: bool
    timed_out: bool
    output_tail: str


def _execute_probe(probe: _Probe, timeout: int) -> _ProbeOutcome:
    try:
        completed = subprocess.run(
            list(probe.argv),
            cwd=str(probe.cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _ProbeOutcome(
            probe.name, probe.argv, str(probe.cwd), -1, False, True, f"timed out after {timeout}s"
        )
    except OSError as exc:
        return _ProbeOutcome(
            probe.name, probe.argv, str(probe.cwd), -1, False, False, f"cannot execute: {exc}"
        )
    output = f"{completed.stdout or ''}{completed.stderr or ''}"
    lines = output.rstrip().splitlines()
    tail = "\n".join(lines[-15:])[-4000:]
    return _ProbeOutcome(
        probe.name,
        probe.argv,
        str(probe.cwd),
        completed.returncode,
        completed.returncode == 0,
        False,
        tail,
    )


def _emit(payload: dict[str, object], *, json_only: bool) -> None:
    if json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    command = cast(str, payload["command"])
    if command == "run":
        for entry in cast(list[dict[str, object]], payload["items"]):
            verdict = cast(str, entry["verdict"])
            print(f"{entry['item_id']} {entry['name']} [{entry['category']}] -> {verdict.upper()}")
            for reason in cast(list[str], entry["reasons"]):
                print(f"  - {reason}")
            for probe in cast(list[dict[str, object]], entry["probes"]):
                status = "ok" if cast(bool, probe["ok"]) else "FAILED"
                print(f"  probe: {probe['name']} ({status}, exit={probe['exit_code']})")
        summary = cast(dict[str, int], payload["summary"])
        print(f"summary: pass={summary['pass']} blocked={summary['blocked']} failed={summary['failed']}")
    elif command == "verify":
        for receipt in cast(list[dict[str, object]], payload["receipts"]):
            verdict = cast(str, receipt["verdict"])
            print(f"{receipt['path']}: {verdict.upper()} (declared result={receipt['result']})")
            for reason in cast(list[str], receipt["reasons"]):
                print(f"  - {reason}")
        coverage = cast(dict[str, object], payload["coverage"])
        if cast(bool, coverage["ok"]):
            print("coverage: OK")
        else:
            print("coverage: INCOMPLETE")
            for reason in cast(list[str], coverage["reasons"]):
                print(f"  - {reason}")
        summary = cast(dict[str, int], payload["summary"])
        print(f"summary: pass={summary['pass']} blocked={summary['blocked']} failed={summary['failed']}")
    elif command == "collect":
        print(f"trace: {payload['trace']}")
        print(f"output: {payload['output']}")
        for entry in cast(list[dict[str, object]], payload["receipts"]):
            result = cast(str, entry["result"])
            print(f"{entry['item_id']} -> {result.upper()} ({entry['path']})")
            for reason in cast(list[str], entry["reasons"]):
                print(f"  - {reason}")
        summary = cast(dict[str, int], payload["summary"])
        print(f"summary: pass={summary['pass']} blocked={summary['blocked']} failed={summary['failed']}")


def _parse_iso_time(text: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _now_type(text: str) -> datetime:
    return _parse_iso_time(text, "now")


def _parser_run() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardware-realtime-acceptance run",
        description="Run safe repository probes; external items stay blocked pending receipts.",
    )
    parser.add_argument("items", nargs="*", metavar="T", help="item ids T1..T14 (default: all)")
    parser.add_argument("--category", choices=CATEGORIES, help="filter by category")
    parser.add_argument("--force", action="store_true", help="rejected: real evidence cannot be forced; external items require a receipt")
    parser.add_argument("--probe-timeout", type=int, default=600, help="per-probe timeout in seconds (default 600)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    return parser


def _cmd_run(argv: Sequence[str]) -> int:
    parser = _parser_run()
    ns = parser.parse_args(argv)
    if cast(bool, ns.force):
        print(
            "error: --force is rejected: real evidence cannot be forced; "
            "external items require a verified receipt",
            file=sys.stderr,
        )
        return 2
    probe_timeout = cast(int, ns.probe_timeout)
    if probe_timeout < 1:
        parser.error("--probe-timeout must be >= 1")
    requested = cast(list[str], ns.items) or [item.item_id for item in ITEMS]
    unknown = sorted(set(requested) - set(ITEM_BY_ID))
    if unknown:
        print(f"error: unknown items: {', '.join(unknown)}", file=sys.stderr)
        return 2
    selected = [ITEM_BY_ID[item_id] for item_id in requested]
    category = cast(str | None, ns.category)
    if category is not None:
        selected = [item for item in selected if item.category == category]
    if not selected:
        print("error: selection matches no items", file=sys.stderr)
        return 2
    entries: list[dict[str, object]] = []
    counts = {"pass": 0, "blocked": 0, "failed": 0}
    for item in selected:
        probe_outcomes = [_execute_probe(probe, probe_timeout) for probe in item.probes]
        failures = [outcome for outcome in probe_outcomes if not outcome.ok]
        if item.category == CATEGORY_REPOSITORY:
            verdict = "pass" if not failures else "failed"
            reasons = [f"probe failed: {outcome.name}" for outcome in failures]
        elif failures:
            verdict = "failed"
            reasons = [
                f"repository probe failed: {failures[0].name} — the real run must not start",
                item.procedure,
            ]
        else:
            verdict = "blocked"
            reasons = [
                item.procedure,
                "external evidence required: perform the real run, then verify a structured receipt",
            ]
        counts[verdict] += 1
        entries.append(
            {
                "item_id": item.item_id,
                "name": item.name,
                "category": item.category,
                "verdict": verdict,
                "receipt_required": item.category != CATEGORY_REPOSITORY,
                "reasons": reasons,
                "probes": [
                    {
                        "name": outcome.name,
                        "cmd": list(outcome.argv),
                        "cwd": outcome.cwd,
                        "exit_code": outcome.exit_code,
                        "ok": outcome.ok,
                        "timed_out": outcome.timed_out,
                        "output_tail": outcome.output_tail,
                    }
                    for outcome in probe_outcomes
                ],
            }
        )
    _emit({"command": "run", "summary": counts, "items": entries}, json_only=cast(bool, ns.json))
    if counts["failed"]:
        return 1
    if counts["blocked"]:
        return 2
    return 0

def _parser_verify() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardware-realtime-acceptance verify",
        description="Validate structured acceptance receipts (fail-closed).",
    )
    parser.add_argument("receipts", nargs="*", metavar="RECEIPT", help="receipt JSON files")
    parser.add_argument("--dir", dest="directory", help="directory to scan for *.json receipts")
    parser.add_argument("--now", type=_now_type, help="reference time (ISO-8601; default: current UTC)")
    parser.add_argument("--max-age-hours", type=int, help="override the TTL for all receipts")
    parser.add_argument("--max-future-minutes", type=int, default=DEFAULT_MAX_FUTURE_MINUTES, help="allowed clock skew for collected_at (default 60)")
    parser.add_argument(
        "--evidence-root",
        help="root directory for evidence paths (default outputs/acceptance)",
    )
    parser.add_argument("--max-evidence-bytes", type=int, default=DEFAULT_EVIDENCE_MAX_BYTES, help="max evidence file size in bytes (default 5MB)")
    parser.add_argument("--coverage", action="store_true", help="require AEC matrix / T13 rounds / T14 platform coverage across passing receipts")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    return parser


def _cmd_verify(argv: Sequence[str]) -> int:
    parser = _parser_verify()
    ns = parser.parse_args(argv)
    max_age_hours = cast(int | None, ns.max_age_hours)
    if max_age_hours is not None and max_age_hours < 1:
        parser.error("--max-age-hours must be >= 1")
    max_future_minutes = cast(int, ns.max_future_minutes)
    if max_future_minutes < 0:
        parser.error("--max-future-minutes must be >= 0")
    now = cast(datetime, ns.now) if ns.now is not None else datetime.now(UTC)
    evidence_root = (
        Path(ns.evidence_root).resolve()
        if ns.evidence_root is not None
        else DEFAULT_EVIDENCE_ROOT.resolve()
    )
    max_evidence_bytes = cast(int, ns.max_evidence_bytes)
    if max_evidence_bytes < 1:
        parser.error("--max-evidence-bytes must be >= 1")

    receipt_paths: list[Path] = []
    directory = cast(str | None, ns.directory)
    if directory is not None:
        directory_path = Path(directory)
        if not directory_path.is_dir():
            print(f"error: directory not found: {directory_path}", file=sys.stderr)
            return 2
        found = sorted(
            path
            for path in directory_path.iterdir()
            if path.is_file() and path.suffix == ".json" and not path.is_symlink()
        )
        if not found:
            print(f"error: no .json receipt files under {directory_path}", file=sys.stderr)
            return 2
        receipt_paths.extend(found)
    receipt_paths.extend(Path(path) for path in cast(list[str], ns.receipts))
    seen: set[str] = set()
    unique_paths: list[Path] = []
    for path in receipt_paths:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique_paths.append(path)
    if not unique_paths:
        print("error: no receipts given (pass paths or --dir)", file=sys.stderr)
        return 2

    results = [
        _check_receipt(
            path,
            now=now,
            max_age_hours=max_age_hours,
            evidence_root=evidence_root,
            max_evidence_bytes=max_evidence_bytes,
        )
        for path in unique_paths
    ]
    pass_results = [result for result in results if result.verdict == "pass"]
    coverage_reasons = _coverage_reasons(pass_results) if cast(bool, ns.coverage) else []
    counts = {"pass": 0, "blocked": 0, "failed": 0}
    for result in results:
        counts[result.verdict] += 1
    payload: dict[str, object] = {
        "command": "verify",
        "now": now.isoformat(),
        "evidence_root": str(evidence_root),
        "summary": counts,
        "receipts": [
            {
                "path": result.path,
                "item_id": result.item_id,
                "result": result.result,
                "verdict": result.verdict,
                "reasons": list(result.reasons),
            }
            for result in results
        ],
        "coverage": {"ok": not coverage_reasons, "reasons": coverage_reasons},
    }
    _emit(payload, json_only=cast(bool, ns.json))
    if counts["failed"] or counts["blocked"]:
        return 1
    if coverage_reasons:
        return 1
    return 0


def _parser_collect() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardware-realtime-acceptance collect",
        description=(
            "Turn one real-device golden trace into T4-T7 candidate receipts. "
            "Fail-closed: repository/mock traces are refused, and receipts "
            "without device DAC/actual-heard evidence are emitted as blocked."
        ),
    )
    parser.add_argument(
        "--trace",
        metavar="TRACE",
        help="golden trace JSON captured from a real board session",
    )
    parser.add_argument(
        "--items",
        nargs="*",
        default=list(COLLECT_ITEMS),
        metavar="T",
        help="items to emit (default: T4 T5 T6 T7)",
    )
    parser.add_argument(
        "--tag",
        help="receipt tag and filename prefix (default collect-<UTC time>)",
    )
    parser.add_argument(
        "--output",
        help="output directory for receipts and evidence (created if missing; "
        "default ./acceptance-collected)",
    )
    parser.add_argument(
        "--now",
        type=_now_type,
        help="reference time for the TTL self-check (ISO-8601; default: current UTC)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    return parser


def _cmd_collect(argv: Sequence[str]) -> int:
    parser = _parser_collect()
    ns = parser.parse_args(argv)
    trace_arg = cast(str | None, ns.trace)
    if trace_arg is None:
        print("error: --trace is required", file=sys.stderr)
        return 2
    requested = list(dict.fromkeys(cast(list[str], ns.items)))
    unknown = sorted(set(requested) - set(COLLECT_ITEMS))
    if unknown:
        print(
            f"error: collect only supports {', '.join(COLLECT_ITEMS)}; "
            f"unknown items: {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 2
    now = cast(datetime, ns.now) if ns.now is not None else datetime.now(UTC)
    tag = cast(str | None, ns.tag) or f"collect-{now:%Y%m%d-%H%M%S}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag) is None:
        print(
            "error: --tag must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            file=sys.stderr,
        )
        return 2
    trace_path = Path(trace_arg)
    if trace_path.is_symlink() or not trace_path.is_file():
        print(f"error: trace is not a regular file: {trace_path}", file=sys.stderr)
        return 1
    try:
        trace = _read_receipt(trace_path)
    except ValueError as exc:
        print(f"error: cannot read trace {trace_path}: {exc}", file=sys.stderr)
        return 1
    problems = _validate_trace(trace)
    if problems:
        print("error: trace is not a valid golden trace:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    origin = cast(str, trace["origin"])
    if origin != TRACE_ORIGIN_REAL_DEVICE:
        print(
            f"error: origin {origin!r} cannot be upgraded to real-device evidence; "
            "run repository probes instead of collecting receipts",
            file=sys.stderr,
        )
        return 1
    out_dir = Path(ns.output or "acceptance-collected").resolve()
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_name = f"golden-trace-{tag}.json"
    evidence_path = evidence_dir / evidence_name
    evidence_path.write_text(
        json.dumps(_privacy_redacted_trace(trace), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = _sha256_file(evidence_path)
    outcomes = _collect_outcomes(trace)
    counts = {"pass": 0, "blocked": 0, "failed": 0}
    written: list[dict[str, object]] = []
    for item_id in requested:
        outcome = outcomes[item_id]
        receipt: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": RECEIPT_TYPE,
            "item_id": item_id,
            "category": ITEM_BY_ID[item_id].category,
            "result": outcome.result,
            "device": dict(cast(dict[str, object], trace["device"])),
            "tag": tag,
            "collected_at": cast(str, trace["collected_at"]),
            "scenario": dict(outcome.scenario),
            "evidence": (
                [{"path": evidence_name, "sha256": digest, "kind": "log"}]
                if outcome.result == "pass"
                else []
            ),
        }
        if outcome.reasons:
            receipt["notes"] = "; ".join(outcome.reasons)[:MAX_NOTES_CHARS]
        receipt_path = out_dir / f"{item_id}-{tag}.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        check = _check_receipt(
            receipt_path,
            now=now,
            max_age_hours=None,
            evidence_root=evidence_dir,
            max_evidence_bytes=DEFAULT_EVIDENCE_MAX_BYTES,
        )
        if check.verdict != outcome.result:
            print(
                f"error: collected receipt failed the verify gates: {receipt_path}",
                file=sys.stderr,
            )
            for reason in check.reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 1
        counts[outcome.result] += 1
        written.append(
            {
                "item_id": item_id,
                "path": str(receipt_path),
                "result": outcome.result,
                "verdict": check.verdict,
                "reasons": list(outcome.reasons),
            }
        )
    _emit(
        {
            "command": "collect",
            "trace": str(trace_path),
            "output": str(out_dir),
            "tag": tag,
            "summary": counts,
            "receipts": written,
        },
        json_only=cast(bool, ns.json),
    )
    if counts["failed"]:
        return 1
    if counts["blocked"]:
        return 2
    return 0


def _parser_list() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardware-realtime-acceptance list",
        description="Show the T1-T14 registry, AEC matrix and thresholds.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    return parser


def _cmd_list(argv: Sequence[str]) -> int:
    ns = _parser_list().parse_args(argv)
    items: list[dict[str, object]] = [
        {
            "item_id": item.item_id,
            "name": item.name,
            "category": item.category,
            "chain": item.chain,
            "procedure": item.procedure,
            "receipt_required": item.category != CATEGORY_REPOSITORY,
            "ttl_hours": DEFAULT_TTL_HOURS.get(item.category),
            "probes": [
                {"name": probe.name, "cmd": list(probe.argv), "cwd": str(probe.cwd)}
                for probe in item.probes
            ],
        }
        for item in ITEMS
    ]
    payload: dict[str, object] = {
        "command": "list",
        "schema_version": SCHEMA_VERSION,
        "receipt_type": RECEIPT_TYPE,
        "items": items,
        "aec_matrix": {
            "modes": list(AEC_MODES),
            "volumes_pct": list(AEC_VOLUMES),
            "distances_m": list(AEC_DISTANCES),
            "orientations": list(AEC_ORIENTATIONS),
            "environments": list(AEC_ENVIRONMENTS),
            "voices": list(AEC_VOICES),
            "contents": list(AEC_CONTENT),
        },
        "thresholds": {
            "stop_p95_ms": STOP_P95_MS,
            "backchannel_false_cancel_cap": BACKCHANNEL_FALSE_CANCEL_CAP,
            "t13_merge_rounds": T13_MERGE_ROUNDS,
            "t13_night_rounds": T13_NIGHT_ROUNDS,
            "default_ttl_hours": DEFAULT_TTL_HOURS,
        },
    }
    if cast(bool, ns.json):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("Memoria hardware realtime acceptance T1-T14 (fail-closed)")
    print("categories: repository | local_dependency | real_hardware | real_wechat | production_security")
    print(f"{'item':<5}{'category':<22}{'receipt':<9}name / chain")
    for item in ITEMS:
        receipt = "required" if item.category != CATEGORY_REPOSITORY else "none"
        print(f"{item.item_id:<5}{item.category:<22}{receipt:<9}{item.name}")
        print(f"       {item.chain}")
        print(f"       {item.procedure}")
        for probe in item.probes:
            print(f"       probe: {probe.name} -> {' '.join(probe.argv)} (cwd={probe.cwd})")
    print()
    print("AEC matrix axes (T9/T10 coverage):")
    print(f"  modes:        {', '.join(AEC_MODES)}")
    print(f"  volumes_pct:  {', '.join(str(v) for v in AEC_VOLUMES)}")
    print(f"  distances_m:  {', '.join(str(v) for v in AEC_DISTANCES)}")
    print(f"  orientations: {', '.join(AEC_ORIENTATIONS)}")
    print(f"  environments: {', '.join(AEC_ENVIRONMENTS)}")
    print(f"  voices:       {', '.join(AEC_VOICES)}")
    print(f"  contents:     {', '.join(AEC_CONTENT)}")
    print()
    print("Thresholds (initial gates, §17):")
    for item_id, ms in STOP_P95_MS.items():
        print(f"  {item_id} stop P95 <= {ms:g} ms")
    print(f"  T11 backchannel false-cancel <= {BACKCHANNEL_FALSE_CANCEL_CAP}")
    print(f"  T13 rounds: merge >= {T13_MERGE_ROUNDS}, night >= {T13_NIGHT_ROUNDS}")
    print(f"  TTL (hours): {DEFAULT_TTL_HOURS}")
    print()
    print("REJECT conditions (any -> release REJECT, §17):")
    for rule in (
        "旧 Generation 误播",
        "旧 Epoch 复活",
        "跨主体私人上下文泄漏",
        "设备发完未播却标记已听",
        "小程序关闭导致机器人对话停止",
        "回声被写成用户永久记忆",
        "无 AEC 证据却启用 full_duplex_verified",
    ):
        print(f"  - {rule}")
    return 0


def _print_usage() -> None:
    print(
        """usage: hardware-realtime-acceptance {list,run,collect,verify} [options]
  list     show T1-T14 registry, AEC matrix and thresholds
  run      run safe repository probes only; external items stay blocked
  collect  turn one real-device golden trace into T4-T7 candidate receipts
  verify   validate receipt JSON files (fail-closed)""",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _print_usage()
        return 2
    command, rest = args[0], args[1:]
    if command == "list":
        return _cmd_list(rest)
    if command == "run":
        return _cmd_run(rest)
    if command == "collect":
        return _cmd_collect(rest)
    if command == "verify":
        return _cmd_verify(rest)
    print(
        f"error: unknown command {command!r} (expected list|run|collect|verify)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
