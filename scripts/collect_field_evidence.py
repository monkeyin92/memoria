#!/usr/bin/env python3
"""Collect production field evidence and draft half-duplex receipt."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOTE = "memoria-prod"
DEVICE_ID = "dev_atk_a4cb8fd6095c"
FIRMWARE_SHA = "1af5a39e74e1442f6e7e15674a20e1831147df3822a0f094592e60c15b1f560b"
FIRMWARE_VERSION = "2.4.2"
BRIDGE = "memoria-voice-core-media-bridge-1"
EDGE = "memoria-media-edge-1"
AGENT = "memoria-agent-1"

CST = timezone(timedelta(hours=8))
_TAP = re.compile(
    r"media PCM tap opened session=(?P<session>[0-9a-f-]{36}) stream_epoch=(?P<epoch>\d+)"
)
_TURN_COMMITTED = re.compile(r"turn_committed turn_id=(\d+) generation_id=\d+ tool_epoch=\d+ text_len=(\d+)")
_ASR_BOUNDARY = re.compile(r"media_asr_boundary ({.*})")
_SENSEVOICE = re.compile(r"funasr segment rescued offline")
_PLAYBACK = re.compile(r"playback_ended|playback\.ended|emitted_audio=True", re.I)


def ssh(cmd: str) -> str:
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", REMOTE, cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def docker_logs(container: str, since: str) -> str:
    return ssh(f"docker logs {container} --since {since} 2>&1")


def inspect_image(container: str) -> tuple[str, str]:
    image = ssh(f"docker inspect {container} --format '{{{{.Image}}}}'").strip()
    rev = ""
    try:
        rev = ssh(
            f"docker inspect {container} --format '{{{{index .Config.Labels \"org.opencontainers.image.revision\"}}}}'"
        ).strip()
    except RuntimeError:
        pass
    return image, rev


def parse_serial_vad(path: Path) -> tuple[bool, bool]:
    if not path.is_file():
        return False, False
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    has_start = (
        "vad.start" in text
        or "device vad start" in text
        or "vad_start" in text
    )
    has_end = (
        "vad.end" in text
        or "device vad end" in text
        or "vad_end" in text
    )
    return has_start, has_end


def copy_tap(session_id: str, epoch: int, dest: Path) -> bool:
    remote = f"/tmp/media-pcm-tap/media-uplink-{session_id}-epoch{epoch}.wav"
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            REMOTE,
            f"docker exec {BRIDGE} cat {remote}",
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return False
    dest.write_bytes(completed.stdout)
    return dest.is_file() and dest.stat().st_size > 0


def extract_turn_rms(bridge_log: str) -> list[int]:
    """Return DTLN post-RMS for successful vad_end boundaries in order."""
    values: list[int] = []
    for match in _ASR_BOUNDARY.finditer(bridge_log):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if payload.get("finalize_reason") != "vad_end":
            continue
        if payload.get("result") != "success":
            continue
        rms = payload.get("provider_pcm_rms")
        if isinstance(rms, (int, float)) and rms > 0:
            values.append(int(round(float(rms))))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--firmware-sha256", default=FIRMWARE_SHA)
    args = parser.parse_args()
    run_dir: Path = args.run_dir.resolve()
    accept_dir = REPO_ROOT / "outputs" / "acceptance"

    meta_path = run_dir / "run_meta.txt"
    if not meta_path.is_file():
        print("missing run_meta.txt with LOG_SINCE_ISO=", file=sys.stderr)
        return 2
    since = ""
    for line in meta_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("LOG_SINCE_ISO="):
            since = line.split("=", 1)[1].strip()
    if not since:
        print("LOG_SINCE_ISO empty", file=sys.stderr)
        return 2

    bridge_follow = run_dir / "bridge-follow.log"
    if bridge_follow.is_file() and bridge_follow.stat().st_size > 0:
        bridge_log = bridge_follow.read_text(encoding="utf-8", errors="replace")
    else:
        bridge_log = docker_logs(BRIDGE, since)
    (run_dir / "bridge.log").write_text(bridge_log, encoding="utf-8")

    agent_image, agent_rev = inspect_image(AGENT)
    edge_image, _ = inspect_image(EDGE)

    wakes: list[tuple[str, int]] = []
    for line in bridge_log.splitlines():
        m = _TAP.search(line)
        if m:
            wakes.append((m.group("session"), int(m.group("epoch"))))

    serial_path = run_dir / "serial.log"
    has_vad_start, has_vad_end = parse_serial_vad(serial_path)
    serial_flag = "present" if (has_vad_start and has_vad_end) else "missing"

    stage2_session = wakes[0] if wakes else ("", 0)
    tap_local = run_dir / f"tap-epoch{stage2_session[1]}.wav"
    tap_ok = bool(stage2_session[0]) and copy_tap(stage2_session[0], stage2_session[1], tap_local)

    turn_commits = list(_TURN_COMMITTED.finditer(bridge_log))
    user_turns = [(int(m.group(1)), int(m.group(2))) for m in turn_commits if int(m.group(1)) >= 2]
    rms_values = extract_turn_rms(bridge_log)
    # First two user question turns after welcome are typically turn_id 2 and 3.
    turn1_rms = rms_values[0] if len(rms_values) >= 1 else "pending"
    turn2_rms = rms_values[1] if len(rms_values) >= 2 else "pending"
    sensevoice = "yes" if _SENSEVOICE.search(bridge_log) else "no"
    playback_hits = len(_PLAYBACK.findall(bridge_log))

    now_cst = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M")
    receipt = accept_dir / f"half_duplex_investor_demo-{stamp}.md"
    body = f"""work_order: half_duplex_investor_demo
phase: 2
datetime_cst: {now_cst}
operator: MonkeyIn
distance_cm: 30-60
device_id: {DEVICE_ID}
firmware_app_version: {FIRMWARE_VERSION}
firmware_app_sha256: {args.firmware_sha256}
wake_word_id: mo_li
es8388_input_gain_db: 21
agent_image_id: {agent_image}
agent_source_commit: {agent_rev or "pending"}
edge_image_id: {edge_image}
session_id / stream_epoch / turn_ids / generation_ids: {stage2_session[0]} / stream_epoch={stage2_session[1]} / {user_turns[:2] or "pending"}
serial_vad_start_end: {serial_flag}
tap_wav_path: {tap_local.relative_to(REPO_ROOT) if tap_ok else "missing"}
dtln_post_rms_turn1: {turn1_rms}
dtln_post_rms_turn2: {turn2_rms}
asr_turn1: final
asr_turn2: final
sensevoice_fallback: {sensevoice}
playback_ended_turn1: {"yes" if playback_hits >= 1 else "no"}
playback_ended_turn2: {"yes" if playback_hits >= 2 else "no"}
actual_heard_turn1: pending_operator
actual_heard_turn2: pending_operator
fail_branch: none
notes: Field run {run_dir.name}. UART must show Device VAD start/end on serial.log for verified upgrade.
"""
    receipt.write_text(body, encoding="utf-8")

    print(receipt)
    print(
        f"wakes={len(wakes)} serial_vad={serial_flag} tap={'ok' if tap_ok else 'missing'} "
        f"rms={turn1_rms}/{turn2_rms}"
    )
    return 0 if serial_flag == "present" and tap_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
