#!/bin/bash
# R02 interrupt and backchannel acceptance on the same card5 candidate.
#
# Gates: devicetest/r02_gates.py. Fixed word lists: devicetest/build_r02_scenarios.py
# (source: Notion R02 card and the 内测准入门禁 v1 page; do not substitute words).
# Receipts: devicetest/r02-<stamp>/ and outputs/acceptance/run-<stamp>-r02-<suite>/.
set -uo pipefail
cd "$HOME/projects/memoria"

STAMP=$(date +%Y%m%d-%H%M%S)
RUN="devicetest/r02-${STAMP}"
SUITE=all
VOLUME=30
PORT=""
CANDIDATE="memoria-agent:20261106-card5-committed-boundary-v1"
REM=memoria-prod
while (($#)); do
  case "$1" in
    --volume) VOLUME="${2:-}"; shift 2 ;;
    --suite) SUITE="${2:-}"; shift 2 ;;
    --candidate) CANDIDATE="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$SUITE" in
  all) SUITES="a b c d" ;;
  a|b|c|d) SUITES="$SUITE" ;;
  *) echo "suite must be a, b, c, d or all" >&2; exit 2 ;;
esac
[[ "$VOLUME" =~ ^[0-9]+$ ]] || { echo "volume must be an integer" >&2; exit 2; }
[[ -n "$PORT" ]] || PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
[[ -n "$PORT" ]] || { echo "FAIL: no serial port" >&2; exit 2; }
mkdir -p "$RUN/scenarios" outputs/acceptance

suite_name() { case "$1" in a) echo stop ;; b) echo backchannel ;; c) echo content ;; d) echo crisis ;; esac; }
suite_json() { case "$1" in a) echo r02-a-stop.json ;; b) echo r02-b-backchannel.json ;; c) echo r02-c-content.json ;; d) echo r02-d-crisis.json ;; esac; }
suite_duration() { case "$1" in a) echo 60 ;; b) echo 40 ;; c) echo 20 ;; d) echo 20 ;; esac; }

image_meta=$(timeout 40 ssh -o BatchMode=yes "$REM" \
  'docker ps --format "{{.Names}} {{.Image}} {{.Status}}" | grep voice-core-media-bridge; docker inspect memoria-voice-core-media-bridge-1 --format "{{index .Config.Labels \"org.opencontainers.image.revision\"}} {{index .Config.Labels \"org.opencontainers.image.version\"}}"' 2>&1)
grep -q "$CANDIDATE" <<<"$image_meta" || { echo "FAIL: live bridge is not $CANDIDATE" >&2; echo "$image_meta" >&2; exit 3; }
grep -q "(healthy)" <<<"$image_meta" || { echo "FAIL: bridge is not healthy" >&2; echo "$image_meta" >&2; exit 3; }

{
  echo "stamp=$RUN"
  echo "candidate=$CANDIDATE"
  echo "port=$PORT"
  echo "volume=$VOLUME"
  echo "suites=$SUITES"
  echo "bridge=$image_meta"
  echo "word_lists=notion-r02-card"
} | tee "$RUN/meta.txt"

PREV_VOLUME=$(osascript -e 'output volume of (get volume settings)')
restore_volume() { osascript -e "set volume output volume $PREV_VOLUME" >/dev/null 2>&1 || true; }
trap restore_volume EXIT
osascript -e "set volume output volume $VOLUME"
V=$(osascript -e 'get volume settings')
echo "volume prev=$PREV_VOLUME now=$V" | tee -a "$RUN/meta.txt"
grep -q "output volume:$VOLUME" <<<"$V" || { echo "FAIL: volume did not reach $VOLUME" >&2; exit 4; }
grep -q "output muted:false" <<<"$V" || { echo "FAIL: output is muted" >&2; exit 4; }

uv run --no-project python -m devicetest.build_r02_scenarios --out "$RUN/scenarios" --suite "$SUITE" \
  | tee -a "$RUN/meta.txt"

FAILED=""
for suite in $SUITES; do
  name=$(suite_name "$suite")
  scen="$RUN/scenarios/$(suite_json "$suite")"
  duration=$(suite_duration "$suite")
  out="outputs/acceptance/run-${STAMP}-r02-${name}"
  serial_raw="$RUN/serial-${suite}-${name}.raw.log"
  manifest="$RUN/manifest-${suite}.json"

  [[ -f "$scen" ]] || { echo "FAIL: missing scenario $scen" >&2; FAILED+=" $suite(scenario)"; continue; }

  uv run --no-project python - "$scen" "$manifest" <<'PYMANIFEST'
import hashlib, json, sys
from pathlib import Path
scenario, manifest_path = Path(sys.argv[1]), Path(sys.argv[2])
raw = json.loads(scenario.read_text(encoding="utf-8"))
steps = raw if isinstance(raw, list) else raw["steps"]
tags = [f"t{i:03d}-{step['tag']}" for i, step in enumerate(steps, 1)]
manifest = {
    "scenario_hash": hashlib.sha256(scenario.read_bytes()).hexdigest(),
    "expected_step_tags": tags,
    "required_logs": ["bridge.log", "serial.log"],
    "min_valid_interrupt_samples": 0,
    "require_started": True,
    "require_completed": True,
}
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"manifest={manifest_path} steps={len(tags)}")
PYMANIFEST

  # The driver's idle-only gate needs a real `activating -> idle` line from a board reset; the
  # logger alone cannot supply it for a board that is already idle and prints nothing.
  boot="$RUN/boot-${suite}.log"
  uv run --no-project --with pyserial python - "$PORT" >"$boot" 2>&1 <<'PYRESET'
import serial,sys,time
s=serial.Serial(sys.argv[1],115200,timeout=0.2); s.dtr=False; s.rts=True; time.sleep(0.2); s.rts=False; s.dtr=True
end=time.time()+25; b=b""
while time.time()<end: b+=s.read(4096)
s.close(); sys.stdout.write(b.decode(errors="replace"))
PYRESET
  grep -q 'activating -> idle' "$boot" || { echo "FAIL: no activating->idle after reset for $suite" >&2; FAILED+=" $suite(boot)"; continue; }
  cp -f "$boot" "$serial_raw"
  uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py "$serial_raw" --port "$PORT" \
    >"$RUN/logger-${suite}.out" 2>&1 &
  LOGGER_PID=$!
  sleep 3
  kill -0 "$LOGGER_PID" 2>/dev/null || { echo "FAIL: serial logger died for $suite" >&2; cat "$RUN/logger-${suite}.out" >&2; FAILED+=" $suite(logger)"; continue; }

  echo "--- suite $suite ($name) start $(date -Iseconds)" | tee -a "$RUN/meta.txt"
  uv run --no-project --with pyserial python -u devicetest/voice_soak_tapwake.py \
    --scenario "$scen" \
    --bank outputs/voice-bank \
    --port "$PORT" \
    --serial-log "$serial_raw" \
    --out "$out" \
    --volume "$VOLUME" \
    --duration-min "$duration" \
    --loops 1 \
    --gate-timeout 180 \
    --turn-gate-timeout 60 \
    --post-scenario-wait 45 \
    --remote "$REM" \
    2>&1 | tee "$RUN/stdout-${suite}.log" || true
  cp -f "$manifest" "$out/manifest.json"

  kill -INT "$LOGGER_PID" 2>/dev/null
  for _ in $(seq 1 15); do kill -0 "$LOGGER_PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$LOGGER_PID" 2>/dev/null
  wait "$LOGGER_PID" 2>/dev/null || true

  uv run --no-project python scripts/voice_soak_evidence.py "$out" >"$RUN/evidence-${suite}.json" 2>"$RUN/evidence-${suite}.err" || true
  uv run --no-project python scripts/voice_soak_verdict.py "$out" >"$RUN/verdict-${suite}.txt" 2>&1 || true
  uv run --no-project python scripts/voice_soak_pack.py "$out" >"$RUN/pack-${suite}.txt" 2>&1 || true
  if uv run --no-project python -m devicetest.r02_gates "$out" --suite "$suite" \
       --out "$RUN/gates-${suite}.md" --json-out "$RUN/gates-${suite}.json" >/dev/null 2>"$RUN/gates-${suite}.err"; then
    echo "suite $suite PASS" | tee -a "$RUN/meta.txt"
  else
    echo "suite $suite FAIL" | tee -a "$RUN/meta.txt"
    FAILED+=" $suite"
  fi
done

restore_volume
{
  echo "finished $(date -Iseconds)"
  echo "volume restored=$(osascript -e 'get volume settings')"
  echo "run=$RUN"
  echo "failed_suites=${FAILED:-none}"
} | tee -a "$RUN/meta.txt"

echo "RUN=$RUN"
[[ -z "$FAILED" ]] || { echo "R02_GATES_FAILED:$FAILED"; exit 5; }
echo "R02_GATES_PASS"
