#!/usr/bin/env python3
"""Check that a Compose file set resolves to the images we intend to run.

Usage: resolve_target_images.py <release-dir> <override> [<override> ...]

Prints the resolved image for each Agent-role service and exits non-zero when a
service named there does not resolve to the given release tag.
"""

from __future__ import annotations

import json
import subprocess
import sys

SERVICES = ("agent", "voice-core-media-bridge")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    release_dir, *overrides = argv[1:]
    command = [
        "sudo",
        "env",
        "MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist",
        "docker",
        "compose",
        "--project-name",
        "memoria",
        "--profile",
        "media-runtime",
        "--file",
        "docker-compose.production.yml",
    ]
    for override in overrides:
        command += ["--file", override]
    command += ["config", "--format", "json"]
    completed = subprocess.run(
        command,
        cwd=release_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip()[:2000], file=sys.stderr)
        return 1
    resolved = json.loads(completed.stdout)["services"]
    for name in SERVICES:
        service = resolved.get(name)
        if service is None:
            print(f"{name}: NOT RESOLVED", file=sys.stderr)
            return 1
        print(f"{name} -> {service.get('image')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
