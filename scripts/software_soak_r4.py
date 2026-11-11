"""Real Python registry and Edge preflight. Refuses to count a failed gate as soak.

Run from the isolated repository with .venv/bin/python -m scripts.software_soak_r4.
Children run in an empty temporary cwd and inherit no provider credentials.
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
from pathlib import Path


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    root = Path.cwd()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="memoria-r4-") as temporary:
        holders = [socket.socket() for _ in range(4)]
        for holder in holders:
            holder.bind(("127.0.0.1", 0))
        port, metrics_port, control_port, pg_port = [holder.getsockname()[1] for holder in holders]
        for holder in holders:
            holder.close()
        env = {
            key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ
        }
        env.update(
            PYTHONPATH=str(root),
            ENVIRONMENT="development",
            OFFLINE_MOCK="true",
            MEDIA_BRIDGE_GRPC_ENABLED="true",
            MEDIA_BRIDGE_GRPC_ADDR=f"127.0.0.1:{port}",
            MEDIA_BRIDGE_MTLS="false",
            PROMETHEUS_PORT=str(metrics_port),
            MEDIA_BRIDGE_PROVIDER_FACTORY="scripts.software_soak_r4_provider:build",
        )
        for name in ("control-ready", "edge-url"):
            (output / name).unlink(missing_ok=True)
        container = "memoria-r4-" + str(os.getpid())
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                "POSTGRES_PASSWORD=r4-local-only",
                "-p",
                f"127.0.0.1:{pg_port}:5432",
                "postgres:17",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        env.update(
            MEMORIA_R4_OUTPUT=str(output),
            MEMORIA_R4_CONTROL_PORT=str(control_port),
            MEMORIA_R4_CONTROL_URL=f"http://127.0.0.1:{control_port}",
            MEMORIA_TEST_POSTGRES_DSN=f"postgresql://postgres:r4-local-only@127.0.0.1:{pg_port}/postgres",
        )
        control_log = (output / "control.log").open("w")
        control = subprocess.Popen(
            [str(root / ".venv/bin/python"), "-m", "scripts.software_soak_r4_control"],
            cwd=root,
            env=env,
            stdout=control_log,
            stderr=subprocess.STDOUT,
        )
        with (output / "bridge.log").open("w") as log:
            process = subprocess.Popen(
                [str(root / ".venv/bin/python"), "-m", "scripts.run_media_bridge"],
                cwd=temporary,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("bridge startup failed; see bridge.log")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("bridge did not listen") from None
                        time.sleep(0.1)
                binary = output / "edge.test"
                subprocess.run(
                    [
                        "go",
                        "test",
                        "-C",
                        "services/media_edge",
                        "-c",
                        "-race",
                        "-o",
                        str(binary),
                        ".",
                    ],
                    check=True,
                )
                command = [
                    str(binary),
                    "-test.run=^TestSoftwareR4RealBridgePreflight$",
                    "-test.v",
                    "-test.timeout=90s",
                ]
                with (output / "preflight.log").open("w") as receipt:
                    result = subprocess.run(
                        command,
                        env={**env, "MEMORIA_R4_BRIDGE": f"127.0.0.1:{port}"},
                        stdout=receipt,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                (output / "preflight.json").write_text(
                    json.dumps(
                        {
                            "candidate": subprocess.check_output(
                                ["git", "rev-parse", "HEAD"], text=True
                            ).strip(),
                            "bridge_pid": process.pid,
                            "control_pid": control.pid,
                            "edge_pid": int((output / "edge-pid").read_text()),
                            "edge_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                            "production_source_changed": False,
                            "control_positive_gate": "requires online snapshot; unknown fails",
                            "cleanup": "owned children stopped and own container removed by finally",
                            "exit_code": result.returncode,
                            "command": command,
                            "external_fixture": "ASR/LLM/TTS FakeMediaProvider only",
                            "long_window_started": False,
                        },
                        indent=2,
                    )
                )
                return result.returncode
            finally:
                try:
                    stop_process(process)
                finally:
                    try:
                        stop_process(control)
                    finally:
                        control_log.close()
                        subprocess.run(
                            ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
                        )


if __name__ == "__main__":
    raise SystemExit(main())
