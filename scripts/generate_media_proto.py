"""Generate the checked-in Python media contract from the self-authored proto."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "services/agent/src/voice_core/generated"
    output.mkdir(parents=True, exist_ok=True)
    proto_root = root / "packages/proto"
    files = (
        "memoria/media/v1/media.proto",
        "memoria/media/v1/device.proto",
        "memoria/media/v1/events.proto",
    )
    try:
        import grpc_tools  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "grpcio-tools is required; install the dev extra before generating media proto"
        ) from exc
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={proto_root}",
            f"--python_out={output}",
            *files,
        ],
        check=True,
        cwd=root,
    )


if __name__ == "__main__":
    main()
