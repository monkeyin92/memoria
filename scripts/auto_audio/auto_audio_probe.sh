#!/bin/bash
# Read-only capability probe for the automated audio acceptance (2026-09-15 design).
#
# Changes nothing: no playback, no recording, no serial port access, no setting edits.
# The avfoundation enumeration below only lists devices; it captures no audio.
#
#   bash auto_audio_probe.sh
set -u

PORT="${PORT:-/dev/cu.usbmodem101}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device}"

echo "== tools =="
for tool in say afplay ffmpeg sox rec play switchaudiosource SwitchAudioSource; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf '  present  %-18s %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '  absent   %-18s\n' "$tool"
  fi
done

echo "== ffmpeg avfoundation support =="
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -version 2>/dev/null | head -2
  ffmpeg -hide_banner -devices 2>/dev/null | grep -i avfoundation || true
fi

echo "== default audio devices =="
system_profiler SPAudioDataType 2>/dev/null | sed -n '1,60p'

echo "== chinese voices =="
say -v '?' 2>/dev/null | grep -E 'zh_CN|zh_TW' || echo "  (none)"

echo "== volume settings (read-only) =="
osascript -e 'get volume settings' 2>&1
osascript -e 'output muted of (get volume settings)' 2>&1 | sed 's/^/  output muted: /'

echo "== serial port =="
ls -l "$PORT" 2>&1
if command -v lsof >/dev/null 2>&1; then
  echo "  holders (must be empty before a capture):"
  lsof "$PORT" 2>/dev/null | sed 's/^/    /' || true
fi
echo "  other usb serial nodes:"
ls -1 /dev/cu.usb* /dev/tty.usb* 2>/dev/null | sed 's/^/    /'

echo "== avfoundation device list (enumeration only, captures nothing) =="
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | sed -n '1,40p'
fi

echo "== firmware receipt =="
ls -l "$EVIDENCE_ROOT/postflash.json" 2>&1
shasum -a 256 "$EVIDENCE_ROOT/postflash.json" 2>/dev/null

echo "== repo state =="
git status --short --branch 2>&1 | head -10

echo "== probe done (nothing changed) =="
