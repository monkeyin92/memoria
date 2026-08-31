#!/usr/bin/env python3
"""Half-duplex investor demo receipt helper (offline, no hardware required).

Creates and validates acceptance receipts documented in HANDOFF.md. Receipt
files live under ``outputs/acceptance/`` and stay out of Git.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs" / "acceptance"
HARDWARE_SCRIPT = REPO_ROOT / "scripts" / "hardware_realtime_acceptance.py"

MARKDOWN_TEMPLATE = """\
work_order: half_duplex_investor_demo
phase: {phase}
datetime_cst: {datetime_cst}
operator: {operator}
distance_cm: {distance_cm}
device_id: {device_id}
firmware_app_version: {firmware_app_version}
firmware_app_sha256: {firmware_app_sha256}
agent_image_id: {agent_image_id}
agent_source_commit: {agent_source_commit}
edge_image_id: {edge_image_id}
session_id / stream_epoch / turn_ids / generation_ids: {session_line}
serial_vad_start_end: {serial_vad_start_end}
tap_wav_path: {tap_wav_path}
dtln_post_rms_turn1: {dtln_post_rms_turn1}
dtln_post_rms_turn2: {dtln_post_rms_turn2}
asr_turn1: {asr_turn1}
asr_turn2: {asr_turn2}
sensevoice_fallback: {sensevoice_fallback}
playback_ended_turn1: {playback_ended_turn1}
playback_ended_turn2: {playback_ended_turn2}
actual_heard_turn1: {actual_heard_turn1}
actual_heard_turn2: {actual_heard_turn2}
fail_branch: {fail_branch}
notes: {notes}
"""

KNOWN_20260831_0949 = {
    "phase": "2",
    "datetime_cst": "2026-08-31 09:49",
    "operator": "operator",
    "distance_cm": "30-60",
    "device_id": "dev_atk_a4cb8fd6095c",
    "firmware_app_version": "2.4.2",
    "firmware_app_sha256": "f15a3b356f3eed604673da1f08afc784832be4e36fa44642ac9dc8d359d03115",
    "agent_image_id": "sha256:cc8c29f48793a07c78e16efad5212c8d848e613345e1357c08ec488b9bcd98d9",
    "agent_source_commit": "6ede9909262661f834f850703848f7dc919334d7",
    "edge_image_id": "sha256:230f94b8e1d0839827b9c5cd3c0c8bbbdf526d7b34e8cf59f3c688f6a71c9513",
    "session_line": "79b6e405-347d-4aad-89be-6e82d4fa1c65 / stream_epoch=1310 / turn1+turn2",
    "serial_vad_start_end": "missing",
    "tap_wav_path": "outputs/acceptance/half_duplex_investor_demo-20260831-0949-tap.wav",
    "dtln_post_rms_turn1": "pending",
    "dtln_post_rms_turn2": "pending",
    "asr_turn1": "final",
    "asr_turn2": "final",
    "sensevoice_fallback": "no",
    "playback_ended_turn1": "yes",
    "playback_ended_turn2": "yes",
    "actual_heard_turn1": "yes",
    "actual_heard_turn2": "yes",
    "fail_branch": "none",
    "notes": (
        "Operator two-turn Actual Heard (星期几 + 南京天气). "
        "UART vad.start/vad.end still missing; do not set direct_real_device_verified."
    ),
}


def _render_markdown(values: dict[str, str]) -> str:
    return MARKDOWN_TEMPLATE.format(**values)


def cmd_template(_: argparse.Namespace) -> int:
    print(MARKDOWN_TEMPLATE.format(
        phase="2",
        datetime_cst="YYYY-MM-DD HH:MM",
        operator="",
        distance_cm="30-60",
        device_id="",
        firmware_app_version="",
        firmware_app_sha256="",
        agent_image_id="",
        agent_source_commit="",
        edge_image_id="",
        session_line="",
        serial_vad_start_end="missing",
        tap_wav_path="",
        dtln_post_rms_turn1="",
        dtln_post_rms_turn2="",
        asr_turn1="final|empty+vendor|empty+gating",
        asr_turn2="final|empty+vendor|empty+gating",
        sensevoice_fallback="yes|no",
        playback_ended_turn1="yes|no",
        playback_ended_turn2="yes|no",
        actual_heard_turn1="yes|no",
        actual_heard_turn2="yes|no",
        fail_branch="none|no_vad_start|low_rms|funasr_empty|fence_playback",
        notes="",
    ))
    return 0


def cmd_write_known(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = args.stamp or "20260831-0949"
    path = output_dir / f"half_duplex_investor_demo-{stamp}.md"
    path.write_text(_render_markdown(KNOWN_20260831_0949), encoding="utf-8")
    print(path)
    return 0


def cmd_verify_json(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    if not HARDWARE_SCRIPT.is_file():
        print(f"missing verifier script: {HARDWARE_SCRIPT}", file=sys.stderr)
        return 2
    completed = subprocess.run(
        [sys.executable, str(HARDWARE_SCRIPT), "verify", str(receipt_path)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed.returncode


def cmd_stage0(args: argparse.Namespace) -> int:
    commands = [
        ["uv", "run", "ruff", "check", "."],
        [
            "uv",
            "run",
            "pytest",
            "services/agent/tests/unit/test_device_vad.py",
            "services/agent/tests/unit/test_agent_production_wiring.py",
            "-q",
        ],
        ["uv", "run", "pytest", "firmware/esp32/tests/test_memoria_protocol_source.py", "-q"],
        ["go", "test", "./..."],
    ]
    cwd = REPO_ROOT if args.scope != "media_edge" else REPO_ROOT / "services" / "media_edge"
    for index, command in enumerate(commands if args.scope == "all" else commands[-1:]):
        run_cwd = REPO_ROOT if index < 3 else cwd
        if args.scope == "media_edge":
            run_cwd = cwd
        print(f"+ {' '.join(command)}", flush=True)
        completed = subprocess.run(command, cwd=run_cwd, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def cmd_write_stage7_script(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "half_duplex_investor_demo-stage7-script.md"
    path.write_text(
        STAGE7_SCRIPT.format(
            as_of=datetime.now(UTC).strftime("%Y-%m-%d"),
        ),
        encoding="utf-8",
    )
    print(path)
    return 0


STAGE7_SCRIPT = """\
# 半双工投资人 Demo 锁定剧本（草案）

as_of: {as_of}
advertised_duplex_level: none
hardware: ATK ES8388 单麦，无 AEC reference
barge_in: forbidden

## 事前检查（路演当天）

- 小程序首页显示「在线，可开始对话」；从首页 tab 起手，不从配网页。
- 生产 Agent/Bridge/Edge 容器 healthy；设备走 Direct WSS，不是 LiveKit compat。
- 固件 2.4.2；半双工 hello 仍为 `aec_mode=none`。
- 串口 + Agent 日志 + Edge 日志 + PCM tap 四件套就位（若缺 UART，不得宣称 verified）。

## 3 分钟口播顺序

1. **展示控制面（15s）**  
   「Memoria 是家庭桌面记忆终端：小程序只做配网、选角和档案，不采实时语音。」

2. **唤醒（10s）**  
   30–60 cm 正常音量：「茉莉」。  
   等欢迎语播完再开口（半双工契约）。

3. **第一问（30s）**  
   「今天星期几。」  
   听完完整回答；口头确认「听完再答，不能抢话」。

4. **第二问（30s）**  
   「南京今天的天气怎么样。」  
   听完完整回答。

5. **收尾待命（20s）**  
   保持安静 10 秒；期望 `owner_silence_timeout` → 设备回 Idle。  
   **不要说「再见」**（主人 capability 未就绪，已降级）。

6. **再唤醒（15s）**  
   再说「茉莉」+ 一句短话，证明不是一次性会话。

7. **口径收口（20s）**  
   「这一代是听完再答的半双工；自然抢话和全双工等带 AEC 的下一 SKU。BOOT 键随时硬停。」

## 禁止演示

- 语音打断 / 抢话 / barge-in
- 「再见」结束语（除非 owner 声纹已 verified 且 receipt 已补）
- 用 H5 麦克风冒充设备 demo
- 宣称全双工、持续聆听或 99% 唤醒率

## 失败时的单分支排障

无响应 → 设备状态/票据 → WSS epoch → VAD → ASR final → generation → 首帧 0/0 → playback terminal。
FunASR 空转写与门控分账：`empty+vendor` / `empty+gating` / `low_rms`。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("template", help="Print empty markdown receipt template").set_defaults(
        func=cmd_template
    )

    write_known = sub.add_parser("write-known", help="Write the 2026-08-31 phase-2 receipt")
    write_known.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    write_known.add_argument("--stamp", default="20260831-0949")
    write_known.set_defaults(func=cmd_write_known)

    verify = sub.add_parser("verify-json", help="Validate structured JSON via hardware script")
    verify.add_argument("receipt", help="Path to receipt JSON")
    verify.set_defaults(func=cmd_verify_json)

    stage0 = sub.add_parser("stage0", help="Run HANDOFF stage-0 gates")
    stage0.add_argument(
        "--scope",
        choices=("all", "media_edge"),
        default="all",
        help="Run full stage-0 suite or media_edge tests only",
    )
    stage0.set_defaults(func=cmd_stage0)

    stage7 = sub.add_parser("stage7-script", help="Write stage-7 investor script draft")
    stage7.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    stage7.set_defaults(func=cmd_write_stage7_script)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
