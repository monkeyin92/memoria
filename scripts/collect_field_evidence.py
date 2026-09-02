#!/usr/bin/env python3
"""Collect production field evidence and draft half-duplex receipt."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOTE = "memoria-prod"
DEVICE_ID = "dev_atk_a4cb8fd6095c"
FIRMWARE_SHA = "c11fb87ed4ffd0206aa5a9554d6226d539186f71040e4ea8525dea6b3dd31492"
FIRMWARE_VERSION = "2.4.2"
BRIDGE = "memoria-voice-core-media-bridge-1"
EDGE = "memoria-media-edge-1"
AGENT = "memoria-agent-1"

CST = timezone(timedelta(hours=8))
_TAP = re.compile(
    r"media PCM tap opened session=(?P<session>[0-9a-f-]{36}) stream_epoch=(?P<epoch>\d+)"
)
_PLAYBACK = re.compile(r"playback_ended|playback\.ended", re.I)
_ASR = re.compile(r"text_len=(\d+)|committed text_len=(\d+)")


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


def parse_serial(path: Path) -> tuple[bool, bool]:
    if not path.is_file():
        return False, False
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    return "vad.start" in text or "vad_start" in text, "vad.end" in text or "vad_end" in text


def copy_tap(session_id: str, epoch: int, dest: Path) -> bool:
    remote = f"/tmp/media-pcm-tap/media-uplink-{session_id}-epoch{epoch}.wav"
    scp = subprocess.run(
        ["scp", "-o", "BatchMode=yes", f"{REMOTE}:{remote}", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    return scp.returncode == 0 and dest.is_file()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
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
    has_vad_start, has_vad_end = parse_serial(serial_path)
    serial_flag = "present" if (has_vad_start and has_vad_end) else "missing"

    stage2_session = wakes[0] if wakes else ("", 0)
    tap_local = run_dir / f"stage2-tap-epoch{stage2_session[1]}.wav"
    tap_ok = bool(stage2_session[0]) and copy_tap(stage2_session[0], stage2_session[1], tap_local)

    text_lens = [
        int(m.group(1) or m.group(2))
        for m in _ASR.finditer(bridge_log)
        if (m.group(1) or m.group(2))
    ]
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
firmware_app_sha256: {FIRMWARE_SHA}
wake_word_id: mo_li
agent_image_id: {agent_image}
agent_source_commit: {agent_rev or "pending"}
edge_image_id: {edge_image}
session_id / stream_epoch / turn_ids / generation_ids: {stage2_session[0]} / stream_epoch={stage2_session[1]} / pending_manual
serial_vad_start_end: {serial_flag}
tap_wav_path: {tap_local if tap_ok else "missing"}
dtln_post_rms_turn1: pending_manual
dtln_post_rms_turn2: pending_manual
asr_turn1: {"final" if text_lens else "pending"}
asr_turn2: {"final" if len(text_lens) > 1 else "pending"}
sensevoice_fallback: pending_manual
playback_ended_turn1: {"yes" if playback_hits >= 1 else "no"}
playback_ended_turn2: {"yes" if playback_hits >= 2 else "no"}
actual_heard_turn1: pending_operator
actual_heard_turn2: pending_operator
fail_branch: none
notes: Field run {run_dir.name}. Operator must confirm actual_heard; Mac TTS invalid for verified.
"""
    receipt.write_text(body, encoding="utf-8")

    print(receipt)
    print(f"wakes={len(wakes)} serial_vad={serial_flag} tap={'ok' if tap_ok else 'missing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
