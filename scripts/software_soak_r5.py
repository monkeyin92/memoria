"""Run a frozen local three-process window with real transport and PG authority.

Only deterministic external ASR/LLM/TTS and local test composition are injected.
Use --duration 45 for fault preflight, then a new output and --duration 900.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from scripts.local_process_probe import rss_bytes
from scripts.software_soak_r4 import stop_process


def main() -> int:
    root = Path.cwd()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, choices=(45, 900), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    ports = []
    for _ in range(4):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            ports.append(holder.getsockname()[1])
    port, metrics_port, control_port, pg_port = ports
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
    env.update(
        PYTHONPATH=str(root),
        ENVIRONMENT="development",
        OFFLINE_MOCK="true",
        MEDIA_BRIDGE_GRPC_ENABLED="true",
        MEDIA_BRIDGE_GRPC_ADDR=f"127.0.0.1:{port}",
        MEDIA_BRIDGE_MTLS="false",
        PROMETHEUS_PORT=str(metrics_port),
        MEDIA_BRIDGE_SESSION_FACTORY="scripts.software_soak_r5_provider:build",
        MEMORIA_RUNTIME_PROFILE_VERIFY_KEY="postgres-session-runtime-signing-key",
        MEMORIA_INTERACTION_POLICY_TOKEN="r5-local-policy-authority-token-32bytes",
        MEMORIA_INTERACTION_POLICY_TIMEOUT_S="2",
        MEMORIA_INTERACTION_POLICY_URL=f"http://127.0.0.1:{control_port}/fixture/session-policy",
        MEMORIA_R5_OUTPUT=str(output),
        MEMORIA_R5_CONTROL_PORT=str(control_port),
        MEMORIA_R5_CONTROL_URL=f"http://127.0.0.1:{control_port}",
        MEMORIA_R5_BRIDGE=f"127.0.0.1:{port}",
        MEMORIA_R5_DURATION=str(args.duration),
        MEMORIA_TEST_POSTGRES_DSN=f"postgresql://postgres:local-r5-disposable-only@127.0.0.1:{pg_port}/postgres",
    )
    binary = output / "edge.test"
    subprocess.run(
        ["go", "test", "-C", "services/media_edge", "-c", "-race", "-o", str(binary), "."],
        check=True,
    )
    config = {
        "http_receipt_schema": "scenario-v1",
        "candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dirty": subprocess.check_output(["git", "status", "--porcelain"], text=True),
        "duration_s": args.duration,
        "warmup": "Control/PG and bridge readiness before window-start",
        "sample_interval_s": 5,
        "traffic_interval_s": 5,
        "fault_schedule_s": [args.duration / 5, args.duration * 2 / 5, args.duration * 3 / 5],
        "faults": [
            "actual device TCP close and higher epoch reconnect",
            "SIGKILL actual Python bridge and restart",
            "8s unread WSS plus test socket Write delay 300ms; actual production queue and old PCM replay after stop",
        ],
        "positive_predicate": "actual final ASR text, Opus downlink and exact Control online speaking with matching session/stream/positive signed epoch",
        "reject_predicate": "Bearer absent 401; nonexistent session 403; retired signed profile rejected; old PCM after actual generation stop refused",
        "drain_predicate": "Edge connections/leases zero each cycle; Python buffers cleared at terminal close; children reaped and owned PG removed",
        "external_fixture": "deterministic ASR/LLM/TTS only; production registry/runtime/admission/transport/fences",
        "edge_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "python_sha256": hashlib.sha256(
            (root / ".venv/bin/python").resolve().read_bytes()
        ).hexdigest(),
        "host": {"loadavg": os.getloadavg(), "cpu_count": os.cpu_count()},
        "claim": "one local software stability observation on shared host; no leak absence, capacity or model-quality claim",
    }
    (output / "config.json").write_text(json.dumps(config, indent=2))
    container = "memoria-r5-" + str(os.getpid())
    children = []
    handles = []
    result = {"window_completed": False, "duration_s": args.duration, "exit_code": None}
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                "POSTGRES_PASSWORD=local-r5-disposable-only",
                "-p",
                f"127.0.0.1:{pg_port}:5432",
                "postgres:17",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with tempfile.TemporaryDirectory(prefix="cwd-", dir=output) as temporary:

            def launch(module, log_name, cwd):
                handle = (output / log_name).open("a")
                handles.append(handle)
                process = subprocess.Popen(
                    [str(root / ".venv/bin/python"), "-m", module],
                    cwd=cwd,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
                children.append(process)
                return process

            control = launch("scripts.software_soak_r5_control", "control.log", root)
            bridge = launch("scripts.run_media_bridge", "bridge.log", temporary)

            def bridge_ready():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if bridge.poll() is not None:
                        raise RuntimeError("bridge exited; see bridge.log")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            return
                    except OSError:
                        time.sleep(0.1)
                raise TimeoutError("bridge startup timed out")

            bridge_ready()
            edge_log = (output / "edge.log").open("w")
            handles.append(edge_log)
            edge = subprocess.Popen(
                [
                    str(binary),
                    "-test.run=^TestSoftwareR5Window$",
                    "-test.v",
                    f"-test.timeout={args.duration + 120}s",
                ],
                env=env,
                stdout=edge_log,
                stderr=subprocess.STDOUT,
            )
            children.append(edge)
            began = time.monotonic()
            restarts = []
            next_sample = began
            with (output / "process.jsonl").open("w") as series:
                while edge.poll() is None:
                    if (output / "kill-bridge").exists() and not restarts:
                        old_pid = bridge.pid
                        bridge.kill()
                        killed_exit = bridge.wait(timeout=5)
                        bridge = launch("scripts.run_media_bridge", "bridge.log", temporary)
                        bridge_ready()
                        restarts.append(
                            {
                                "old_pid": old_pid,
                                "old_exit": killed_exit,
                                "new_pid": bridge.pid,
                                "at": datetime.now(UTC).isoformat(),
                                "elapsed_s": time.monotonic() - began,
                            }
                        )
                        (output / "bridge-restarted").write_text(json.dumps(restarts[-1]))
                    if time.monotonic() >= next_sample:
                        processes = {}
                        for name, process in (
                            ("control", control),
                            ("bridge", bridge),
                            ("edge", edge),
                        ):
                            if process.poll() is not None:
                                raise RuntimeError(f"unexpected {name} exit {process.returncode}")
                            descriptors = subprocess.run(
                                ["/usr/sbin/lsof", "-nP", "-p", str(process.pid), "-FftT"],
                                capture_output=True,
                                text=True,
                            )
                            rows = descriptors.stdout.splitlines()
                            processes[name] = {
                                "pid": process.pid,
                                "rss_bytes": rss_bytes(process.pid),
                                "fd_count": sum(
                                    row.startswith("f") and row[1:].isdigit() for row in rows
                                )
                                if descriptors.returncode == 0
                                else None,
                                "established_tcp": rows.count("TST=ESTABLISHED")
                                if descriptors.returncode == 0
                                else None,
                            }
                        series.write(
                            json.dumps(
                                {
                                    "at": datetime.now(UTC).isoformat(),
                                    "elapsed_s": time.monotonic() - began,
                                    "loadavg": os.getloadavg(),
                                    "processes": processes,
                                }
                            )
                            + "\n"
                        )
                        series.flush()
                        next_sample += 5
                    time.sleep(0.1)
            result.update(
                exit_code=edge.returncode,
                restarts=restarts,
                wall_s=time.monotonic() - began,
                control_pid=control.pid,
                initial_bridge_pid=children[1].pid,
                final_bridge_pid=bridge.pid,
                edge_pid=edge.pid,
            )
            if (output / "window-end").exists():
                start = datetime.fromisoformat((output / "window-start").read_text())
                end = datetime.fromisoformat((output / "window-end").read_text())
                elapsed = (end - start).total_seconds()
                result.update(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    actual_window_s=elapsed,
                    window_completed=edge.returncode == 0 and elapsed >= args.duration,
                )
    finally:
        for child in reversed(children):
            stop_process(child)
        for handle in handles:
            handle.close()
        cleanup = subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)
        result.update(
            cleanup={
                "owned_pids": [child.pid for child in children],
                "all_reaped": all(child.poll() is not None for child in children),
                "exit_codes": [child.returncode for child in children],
                "container": container,
                "remove_exit": cleanup.returncode,
            }
        )
        (output / "result.json").write_text(json.dumps(result, indent=2))
    return 0 if result["window_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
