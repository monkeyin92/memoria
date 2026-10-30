#!/bin/bash
set -euo pipefail
cd "$HOME/projects/memoria"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="outputs/acceptance/run-${STAMP}-soak-tapwake"
REPORT_DIR="$HOME/projects/memoria/devicetest/run-${STAMP}-soak-tapwake"
mkdir -p "$REPORT_DIR"
export PATH="$HOME/projects/memoria/devicetest/bin:$PATH"
export AFPLAY_RETRY_LOG="$REPORT_DIR/afplay_retry.log"
PORT="/dev/cu.usbmodem101"
{
  echo "start $(date -Iseconds)"
  echo "port=$PORT"
  echo "gates MAX_NO_REPLY=0 P95<=2.0s; wait gate idle-only 90s (listening_closed log-only)"
} | tee "$REPORT_DIR/meta.txt"
ls -la "$PORT" || { echo "FAIL: serial missing"; exit 2; }
python3 devicetest/clock_offset.py pre --remote memoria-prod | tee -a "$REPORT_DIR/clock.jsonl" | sed 's/^/clock=/' | tee -a "$REPORT_DIR/meta.txt" || echo "clock=pre_failed" | tee -a "$REPORT_DIR/meta.txt"
SERIAL_RAW="$REPORT_DIR/serial_raw.log"
uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py "$SERIAL_RAW" --port "$PORT" \
  >"$REPORT_DIR/serial_logger.out" 2>&1 &
LOGGER_PID=$!
stop_logger() { kill "$LOGGER_PID" 2>/dev/null || true; wait "$LOGGER_PID" 2>/dev/null || true; }
trap stop_logger EXIT
sleep 3
kill -0 "$LOGGER_PID" 2>/dev/null || { echo "FAIL: serial logger died"; cat "$REPORT_DIR/serial_logger.out"; exit 2; }
ELF=$(grep -ao 'ELF file SHA256: *[0-9a-f]*' "$SERIAL_RAW" | head -1 | awk '{print $NF}' || true)
ELF=${ELF:-missing_no_boot_banner}
echo "firmware_marker=elf_sha256=$ELF (from boot serial)" | tee -a "$REPORT_DIR/meta.txt"
uv run --no-project --with pyserial python devicetest/voice_soak_tapwake.py \
  --scenario devicetest/scenarios/child_tapwake.json \
  --bank outputs/voice-bank \
  --port "$PORT" \
  --out "$OUT" \
  --duration-min 15 \
  --volume 30 \
  --remote memoria-prod \
  --serial-log "$SERIAL_RAW" \
  2>&1 | tee "$REPORT_DIR/stdout.log" || true
python3 devicetest/clock_offset.py post --remote memoria-prod | tee -a "$REPORT_DIR/clock.jsonl" | sed 's/^/clock=/' | tee -a "$REPORT_DIR/meta.txt" || echo "clock=post_failed" | tee -a "$REPORT_DIR/meta.txt"
ABORT=$(grep -ho '"verdict": "ABORT_[A-Z_0-9]*"' "$OUT/timeline.jsonl" 2>/dev/null | head -1 | cut -d'"' -f4 || true)
if [[ -n "$ABORT" ]]; then
  echo "verdict=$ABORT" | tee -a "$REPORT_DIR/meta.txt" "$REPORT_DIR/verdict.txt"
  stop_logger
  cp -f "$OUT"/bridge.log "$OUT"/serial.log "$OUT"/timeline.jsonl "$REPORT_DIR"/ 2>/dev/null || true
  exit 5
fi
set +e
uv run --no-project python scripts/voice_soak_verdict.py "$OUT" \
  --max-no-reply 0 --max-reply-latency-p95 2.0 \
  2>&1 | tee "$REPORT_DIR/verdict.txt"
VC=$?
set -e
python3 - <<PY
import json, pathlib
rd = pathlib.Path("$REPORT_DIR")
vt = (rd/"verdict.txt").read_text(encoding="utf-8", errors="replace") if (rd/"verdict.txt").exists() else ""
summary = {
  "out": "$OUT",
  "report_dir": str(rd),
  "verdict_exit": $VC,
  "gates": {"max_no_reply": 0, "max_reply_latency_p95_s": 2.0},
  "firmware": {"elf_sha256": "$ELF"},
}
import subprocess
summary["turns_report"] = json.loads(subprocess.run(
  ["python3", "devicetest/tapwake_report.py", "$OUT", "--scenario", "devicetest/scenarios/child_tapwake.json", "--clock", str(rd/"clock.jsonl")],
  capture_output=True, text=True, check=True).stdout)
for line in reversed(vt.splitlines()):
    line=line.strip()
    if line.startswith("{") and line.endswith("}"):
        try:
            summary["verdict"]=json.loads(line); break
        except Exception:
            pass
summary["verdict_text_tail"]=vt[-2000:]
(rd/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k:summary[k] for k in summary if k!='verdict_text_tail'}, ensure_ascii=False, indent=2))
PY
stop_logger
set +e
python3 devicetest/keepawake_verdict.py "$OUT" --serial "$SERIAL_RAW" --summary "$REPORT_DIR/summary.json" \
  2>&1 | tee "$REPORT_DIR/keepawake_verdict.txt"
VC=${PIPESTATUS[0]}
set -e
cp -f "$OUT"/bridge.log "$OUT"/serial.log "$OUT"/timeline.jsonl "$REPORT_DIR"/ 2>/dev/null || true
echo "done $(date -Iseconds) REPORT_DIR=$REPORT_DIR" | tee -a "$REPORT_DIR/meta.txt"
exit $VC
