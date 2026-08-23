#!/usr/bin/env python3
"""Verify voice interaction fixes are correctly applied."""

import ast
import re
from pathlib import Path

def check_file(filepath: Path, checks: list[tuple[str, str]]) -> tuple[bool, list[str]]:
    """Check if file contains expected patterns."""
    content = filepath.read_text()
    results = []
    all_passed = True

    for check_name, pattern in checks:
        if re.search(pattern, content, re.MULTILINE | re.DOTALL):
            results.append(f"✓ {check_name}")
        else:
            results.append(f"✗ {check_name}")
            all_passed = False

    return all_passed, results

print("🔍 Verifying voice interaction fixes...\n")

# Check 1: MediaAudioIngress has start_speaking method
ingress_checks = [
    ("start_speaking method exists", r"async def start_speaking\(self, session_id: str\)"),
    ("skip_stage2_on_silence is False", r"skip_stage2_on_silence=False"),
    ("VAD threshold is 0.5", r"vad_threshold=0\.5"),
    ("Auto-listening is disabled", r"# Don't auto-enter LISTENING state"),
]

passed, results = check_file(
    Path("services/agent/src/voice_core/media_audio_ingress.py"),
    ingress_checks
)
print("📄 media_audio_ingress.py:")
for r in results:
    print(f"  {r}")
print()

# Check 2: Output stream calls start_speaking
output_checks = [
    ("Calls start_speaking on first frame", r"await self\._audio_ingress\.start_speaking\(session_id\)"),
    ("Calls stop_speaking on completion", r"await self\._audio_ingress\.stop_speaking\(session\.identity\.session_id\)"),
]

passed2, results2 = check_file(
    Path("services/agent/src/voice_core/media_session_output_stream.py"),
    output_checks
)
print("📄 media_session_output_stream.py:")
for r in results2:
    print(f"  {r}")
print()

if passed and passed2:
    print("✅ All fixes verified successfully!")
    exit(0)
else:
    print("❌ Some checks failed. Please review the fixes.")
    exit(1)
