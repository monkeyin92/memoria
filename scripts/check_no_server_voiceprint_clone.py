#!/usr/bin/env python3
"""Fail if live code can still enroll voiceprints or clone voices on the server.

Schema SQL, historical docs, and tests are out of scope for this gate.
Exit 0 means no remaining product path stores voice samples or templates.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS: list[tuple[Path, re.Pattern[str], str]] = [
    (
        ROOT / "services/voice_profile/cosyvoice_enrollment.py",
        re.compile(r"."),
        "CosyVoice enrollment client must be deleted",
    ),
    (
        ROOT / "services/voice_profile/doubao_voice_clone.py",
        re.compile(r"."),
        "Doubao voice clone client must be deleted",
    ),
    (
        ROOT / "apps/miniprogram/utils/voice-clone-gate.js",
        re.compile(r"."),
        "miniprogram voice clone gate must be deleted",
    ),
    (
        ROOT / "services/control_api/app/routes/speaker.py",
        re.compile(r"."),
        "speaker routes module must be deleted",
    ),
    (
        ROOT / "services/agent/src/orchestration/formal_speaker_enrollment.py",
        re.compile(r"."),
        "formal speaker enrollment module must be deleted",
    ),
    (
        ROOT / "services/agent/src/speaker_authority_client.py",
        re.compile(r"."),
        "speaker authority client module must be deleted",
    ),
    (
        ROOT / "apps/miniprogram/utils/voice-enrollment-state.js",
        re.compile(r"."),
        "miniprogram voice enrollment util must be deleted",
    ),
    (
        ROOT / "services/control_api/app",
        re.compile(
            r"tts/customization|DoubaoVoiceCloneClient|CosyVoiceEnrollmentClient|"
            r"VOICE_CLONE_ENABLED|qwen_omni_voice_clone_id"
        ),
        "control-api still wires a voice clone client or switch",
    ),
    (
        ROOT / "services/control_api/app/routes/voice.py",
        re.compile(
            r'@(?:router|app)\.(?:post|put|patch|delete)\(\s*["\']/(?:consent|enrollments|'
            r"profiles|blind-trials|provider-samples|provider-deletion)"
        ),
        "voice.py still exposes clone/enroll/sample write routes",
    ),
    (
        ROOT / "services/agent/src",
        re.compile(r"run_formal_speaker_enrollment|SpeakerAuthorityClient\b|"
                   r"speaker_authority_client_from_settings\b"),
        "agent still references speaker enrollment/authority client",
    ),
    (
        ROOT / "apps/miniprogram/pages",
        re.compile(r"voiceCloneAllowed|uploadVoiceSample|revokeVoiceCloneConsent|"
                   r"speakerEnrollment|主人声纹|声音复刻"),
        "miniprogram pages still expose voiceprint/clone UI",
    ),
    (
        ROOT / "apps/miniprogram/utils",
        re.compile(
            r"voiceCloneAllowed|uploadVoiceSample|revokeVoiceCloneConsent|"
            r"enrollVoiceClone|grantVoiceCloneConsent|listVoiceProfiles|"
            r"speakerEnrollment|主人声纹|声音复刻"
        ),
        "miniprogram utils still expose voiceprint/clone APIs",
    ),
]


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        return []
    return sorted(p for p in path.rglob("*") if p.is_file())


def main() -> int:
    hits: list[str] = []
    for path, pattern, label in CHECKS:
        if path.name.endswith(".py") and path.name in {
            "speaker.py",
            "formal_speaker_enrollment.py",
            "speaker_authority_client.py",
            "cosyvoice_enrollment.py",
            "doubao_voice_clone.py",
        }:
            if path.exists():
                hits.append(f"{path.relative_to(ROOT)}: {label}")
            continue
        if path.name in {"voice-enrollment-state.js", "voice-clone-gate.js"}:
            if path.exists():
                hits.append(f"{path.relative_to(ROOT)}: {label}")
            continue
        for file_path in _iter_files(path):
            if "/tests/" in str(file_path) or "/design-preview/" in str(file_path):
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if pattern.search(content):
                hits.append(f"{file_path.relative_to(ROOT)}: {label}")
    if hits:
        print("FORBIDDEN voiceprint/voice-clone surfaces still present:")
        for hit in hits:
            print(f"  - {hit}")
        return 1
    print("OK: no server voiceprint/clone write paths remain in product code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
