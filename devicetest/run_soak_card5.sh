#!/bin/bash
set -euo pipefail
cd "$HOME/projects/memoria"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="outputs/acceptance/run-${STAMP}-soak-card5"
REPORT_DIR="$HOME/projects/memoria/devicetest/run-${STAMP}-soak-card5"
mkdir -p "$REPORT_DIR"
export PATH="$HOME/projects/memoria/devicetest/bin:$PATH"
export AFPLAY_RETRY_LOG="$REPORT_DIR/afplay_retry.log"
PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
SCENARIO="devicetest/scenarios/child_tapwake.json"
{
  echo "start $(date -Iseconds)"
  echo "port=$PORT"
  echo "gates MAX_NO_REPLY=0 P90<=2.0s max<=3.0s; pre gate idle-only 180s (real State line, no assume_idle); turn gate idle|listening|listening_closed 30s (listening window is product state, #453)"
} | tee "$REPORT_DIR/meta.txt"
ls -la "$PORT" || { echo "FAIL: serial missing"; exit 2; }

# Freeze the exact scenario and completeness contract before the device run.
uv run --no-project python - "$SCENARIO" "$REPORT_DIR" <<'PYMANIFEST'
import hashlib
import json
import sys
from pathlib import Path

scenario = Path(sys.argv[1])
run = Path(sys.argv[2])
raw = json.loads(scenario.read_text(encoding="utf-8"))
steps = raw if isinstance(raw, list) else raw["steps"]
tagged_steps = [step for step in steps if isinstance(step, dict) and isinstance(step.get("tag"), str)]
silence_index = next((i for i, step in enumerate(tagged_steps) if step["tag"] == "silence"), len(tagged_steps) - 1)
tagged_steps = tagged_steps[: silence_index + 1]
tags = [f"t{i:03d}-{step['tag']}" for i, step in enumerate(tagged_steps, 1)]
if not tags:
    raise SystemExit("FAIL: scenario has no tagged steps")
manifest = {
    "scenario_hash": hashlib.sha256(scenario.read_bytes()).hexdigest(),
    "expected_step_tags": tags,
    "required_logs": ["bridge.log", "serial.log"],
    "min_valid_interrupt_samples": 0,
    "require_started": True,
    "require_completed": True,
}
run.mkdir(parents=True, exist_ok=True)
(run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"manifest={run / 'manifest.json'} steps={len(tags)} scenario_sha256={manifest['scenario_hash']}")
PYMANIFEST

# Reset the board and keep the transcript. open_port() never resets, so a board already parked can
# never print the `activating -> idle` line the idle-only gate requires (#461).
uv run --no-project --with pyserial python - "$PORT" > "/tmp/memoria-soak-boot-${STAMP}.log" 2>&1 <<'PYRESET'
import serial,sys,time
s=serial.Serial(sys.argv[1],115200,timeout=0.2); s.dtr=False; s.rts=True; time.sleep(0.2); s.rts=False; s.dtr=True
end=time.time()+25; b=b""
while time.time()<end: b+=s.read(4096)
s.close(); sys.stdout.write(b.decode(errors="replace"))
PYRESET
grep -q 'activating -> idle' "/tmp/memoria-soak-boot-${STAMP}.log" || { echo "FAIL: no activating->idle after reset"; exit 2; }
grep -o 'ELF file SHA256: *[0-9a-f]*' "/tmp/memoria-soak-boot-${STAMP}.log" | head -1 | tee -a "$REPORT_DIR/meta.txt"
python3 devicetest/clock_offset.py pre --remote memoria-prod | tee -a "$REPORT_DIR/clock.jsonl" | sed 's/^/clock=/' | tee -a "$REPORT_DIR/meta.txt" || echo "clock=pre_failed" | tee -a "$REPORT_DIR/meta.txt"
SERIAL_RAW="$REPORT_DIR/serial_raw.log"
cp -f "/tmp/memoria-soak-boot-${STAMP}.log" "$SERIAL_RAW"
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
  --scenario "$SCENARIO" \
  --bank outputs/voice-bank \
  --port "$PORT" \
  --out "$OUT" \
  --duration-min 15 \
  --volume 30 \
  --remote memoria-prod \
  --serial-log "$SERIAL_RAW" \
  --gate-timeout 180 \
  --turn-gate-timeout 30 \
  2>&1 | tee "$REPORT_DIR/stdout.log" || true
# The driver creates OUT itself. Attach the frozen manifest before scoring.
cp -f "$REPORT_DIR/manifest.json" "$OUT/manifest.json"
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
  --max-no-reply 0 --max-reply-latency-p90 2.0 --max-reply-latency-max 3.0 \
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
  "gates": {"max_no_reply": 0, "max_reply_latency_p90_s": 2.0, "max_reply_latency_max_s": 3.0},
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
