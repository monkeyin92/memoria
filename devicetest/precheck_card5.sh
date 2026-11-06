#!/bin/bash
# Precheck for board card 5 (vol30 retest) against the live MEM-52 wave cutover image.
# Expectations match memoria-prod after 20261104-card5-wave-main-v1 (commit ddaf683b).
set -uo pipefail
cd "$HOME/projects/memoria"
TAG=card5-overlay
EXPECT_IMAGE=memoria-agent:20261104-card5-victim-handoff-v1
EXPECT_BUILD=28
EXPECT_ELF=d0e90d73a
EXPECT_BYPASS=ed2b4c0e
EXPECT_VOL=40
D="devicetest/precheck-$(date +%Y%m%d-%H%M%S)-${TAG}"; mkdir -p "$D"
PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1); REM=memoria-prod; FAIL=""
echo "stamp=$D expect_image=$EXPECT_IMAGE expect_build=$EXPECT_BUILD expect_elf=$EXPECT_ELF vol=$EXPECT_VOL" | tee "$D/meta.txt"
[ -n "$PORT" ] || { echo "FAIL: no serial port"; exit 2; }

# Reset the board first. The idle-only gate (#461) only accepts a real `activating -> idle` line, so a
# board that was already idle when the logger opened the port can never pass it: two of the three
# 11-02 attempts aborted exactly there.
uv run --no-project --with pyserial python - "$PORT" > "$D/boot.log" 2>&1 <<'PY'
import serial,sys,time
s=serial.Serial(sys.argv[1],115200,timeout=0.2); s.dtr=False; s.rts=True; time.sleep(0.2); s.rts=False; s.dtr=True
end=time.time()+25; b=b""
while time.time()<end: b+=s.read(4096)
s.close(); sys.stdout.write(b.decode(errors="replace"))
PY
E=$(grep -o 'ELF file SHA256: *[0-9a-f]*' "$D/boot.log" | head -1 | awk '{print $NF}')
B=$(grep -o 'MEMORIA_FIRMWARE_BUILD=[0-9]*' "$D/boot.log" | head -1 | cut -d= -f2)
{ echo "elf=${E:-missing}"
  echo "build=${B:-missing}"
  echo "idle=$(grep -q 'StateMachine: State: activating -> idle' "$D/boot.log" && echo OK || echo MISSING)"
  echo "download=$(grep -q DOWNLOAD "$D/boot.log" && echo FOUND || echo none)"
  echo "quiet_hours=$(grep -o 'quiet hours -> [0-9:-]*' "$D/boot.log" | head -1)"; } | tee -a "$D/meta.txt"
[[ "$E" == *"$EXPECT_ELF"* ]] || FAIL+=" elf"
[ "${B:-}" = "$EXPECT_BUILD" ] || FAIL+=" build"
grep -q 'activating -> idle' "$D/boot.log" || FAIL+=" idle"
grep -q DOWNLOAD "$D/boot.log" && FAIL+=" download"
grep -q 'wifi hold suppressed MEMORIA_KEEP_AWAKE_BUILD=1' "$D/boot.log" || FAIL+=" keepawake"

ssh $REM 'docker ps --format "{{.Names}} {{.Image}} {{.Status}}"' > "$D/containers.txt" 2>&1
grep media-bridge "$D/containers.txt" | tee -a "$D/meta.txt"
grep -q "$EXPECT_IMAGE" "$D/containers.txt" || FAIL+=" image"
grep -q '(healthy)' "$D/containers.txt" || FAIL+=" health"
ssh $REM 'df -h / | tail -1' | tee -a "$D/meta.txt"
ssh $REM 'for c in $(docker ps --format "{{.Names}}"); do v=$(docker exec $c printenv MINOR_LIMIT_BYPASS_TEST_SUBJECTS 2>/dev/null) || continue; for s in $(echo "$v"|tr , " "); do echo "$c subject_sha256_8=$(printf %s "$s"|sha256sum|cut -c1-8)"; done; done' | tee "$D/bypass.txt"
grep -q "$EXPECT_BYPASS" "$D/bypass.txt" || FAIL+=" bypass"
ssh $REM 'docker exec memoria-voice-core-media-bridge-1 printenv | grep -E "^(ASR_PROVIDER|VOLC_ASR_BOOSTING_TABLE_ID)="' | tee -a "$D/meta.txt"

# Setting the level also clears output mute, so record both.
PREV=$(osascript -e 'output volume of (get volume settings)')
osascript -e "set volume output volume $EXPECT_VOL"
V=$(osascript -e 'get volume settings')
echo "volume prev=$PREV now=$V" | tee -a "$D/meta.txt"
grep -q 'output volume:40' <<<"$V" || FAIL+=" volume"
grep -q 'output muted:false' <<<"$V" || FAIL+=" muted"

ssh $REM 'docker logs -f -t --since 0s memoria-voice-core-media-bridge-1 2>&1' > "$D/bridge.log" & BP=$!
uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py "$D/serial.log" --port "$PORT" > "$D/logger.out" 2>&1 & LP=$!
sleep 20
W(){ (cd scripts && uv run --no-project --with pyserial python -c "from voice_soak_serial_command import send_command; print(send_command('wake', port='$PORT'))") 2>&1; }
echo "wake1 $(date +%T) $(W)" | tee -a "$D/meta.txt"
for i in $(seq 1 120); do grep -q 'closed=true' "$D/serial.log" 2>/dev/null && break; sleep 1; done
echo "closed_after_s=$i" | tee -a "$D/meta.txt"
sleep 3
echo "wake2 $(date +%T) $(W)" | tee -a "$D/meta.txt"
sleep 8
kill $LP $BP 2>/dev/null; wait 2>/dev/null
osascript -e "set volume output volume $PREV"
grep -nE 'usb wake|resume_listening|closed=|-> ' "$D/serial.log" | tail -20 | tee "$D/serial_hits.txt"
H=$(grep -ciE 'vad|listening|reply|asr|transcript|tts' "$D/bridge.log"); echo bridge_hits=$H | tee -a "$D/meta.txt"
[ "$H" -gt 0 ] || FAIL+=" no_reaction"
echo "FAIL=[$FAIL]" | tee "$D/result.txt"
[ -z "$FAIL" ] && echo PRECHECK_PASS || echo PRECHECK_FAIL
echo "DIR=$D"
[ -z "$FAIL" ] || exit 3
