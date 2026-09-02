#!/usr/bin/env bash
# Automated preflight + evidence capture for half-duplex field runs.
# Does NOT play Mac TTS — operator must speak at the board (30–60 cm).
set -euo pipefail

REPO="/Users/monkeyin/projects/memoria"
RUN="${1:-$REPO/outputs/acceptance/run-$(date +%Y%m%d-%H%M)}"
REMOTE="${MEMORIA_SSH:-memoria-prod}"
DEVICE_ID="${MEMORIA_DEVICE_ID:-dev_atk_a4cb8fd6095c}"
PORT="${MEMORIA_SERIAL_PORT:-/dev/cu.usbmodem101}"
LOG_SINCE=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

mkdir -p "$RUN"
echo "LOG_SINCE_ISO=$LOG_SINCE" > "$RUN/run_meta.txt"
echo "RUN_DIR=$RUN"

cd "$REPO"

echo "=== stage 0 gates ==="
uv run ruff check .
uv run pytest services/agent/tests/unit/test_device_vad.py \
  services/agent/tests/unit/test_agent_production_wiring.py -q
uv run pytest firmware/esp32/tests/test_memoria_protocol_source.py -q
( cd services/media_edge && go test ./... -count=1 )

echo "=== production health ==="
ssh -o BatchMode=yes "$REMOTE" \
  'docker ps --format "{{.Names}} {{.Status}}" | grep -E "memoria-agent|media-bridge|media-edge"'

if [[ ! -e "$PORT" ]]; then
  echo "WARN: serial $PORT not found — UART evidence will be missing"
else
  cat > "$RUN/capture_serial.py" <<PY
#!/usr/bin/env python3
import sys, time
from datetime import datetime, timedelta, timezone
import serial
PORT = "$PORT"
BAUD = 460800
OUT = "$RUN/serial.log"
CST = timezone(timedelta(hours=8))
with serial.Serial(PORT, BAUD, timeout=0.5) as ser, open(OUT, "ab", buffering=0) as out:
    out.write(f"=== serial start {datetime.now(CST).isoformat()} port={PORT} ===\\n".encode())
    while True:
        chunk = ser.readline()
        if chunk:
            ts = datetime.now(CST).strftime("%H:%M:%S.%f")[:-3]
            out.write(f"[{ts}] ".encode() + chunk)
            out.flush()
        else:
            time.sleep(0.05)
PY
  if ! pgrep -f "$RUN/capture_serial.py" >/dev/null 2>&1; then
    nohup python3 "$RUN/capture_serial.py" >> "$RUN/capture_serial.stderr" 2>&1 &
    echo "serial capture pid=$!"
  fi
fi

if ! pgrep -f "$RUN/bridge-follow.log" >/dev/null 2>&1; then
  nohup ssh -o BatchMode=yes "$REMOTE" \
    "docker logs -f memoria-voice-core-media-bridge-1 --since ${LOG_SINCE} 2>&1" \
    >> "$RUN/bridge-follow.log" 2>&1 &
  echo "bridge log follow pid=$!"
fi

cat <<EOF

=== monitoring started ===
device: $DEVICE_ID
run:    $RUN
since:  $LOG_SINCE

NEXT (you speak at board, NOT Mac speakers):
  1. Confirm mini-program device tab online
  2. Follow locked script: outputs/acceptance/half_duplex_investor_demo-stage7-script.md
  3. After run: uv run python $REPO/scripts/collect_field_evidence.py --run-dir $RUN

Mac TTS/say is NOT valid for receipt or direct_real_device_verified.
EOF
