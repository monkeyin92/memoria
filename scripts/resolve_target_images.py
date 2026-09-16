#!/usr/bin/env python3
"""Check that a Compose file set resolves to the candidate images we intend to run.

Usage:
  resolve_target_images.py <release-dir> [<override> ...]
  resolve_target_images.py --release-dir <dir> --stack-tag <tag> --expected-tag <tag> [--override <file> ...]

Verifies that the target services (agent, voice-core-media-bridge) exist in the resolved
Compose definition, match the expected release tag / candidate image, and resolve
consistently. Exits non-zero on mismatch, missing service, or compose error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

SERVICES = ("agent", "voice-core-media-bridge")


def verify_resolved_services(
    resolved_services: dict[str, Any],
    *,
    expected_tag: str | None = None,
    expected_images: dict[str, str] | None = None,
    target_services: tuple[str, ...] = SERVICES,
) -> tuple[bool, list[str]]:
    """Validate that target services exist, match expectations, and are consistent."""
    errors: list[str] = []
    resolved_images: dict[str, str] = {}

    for name in target_services:
        service = resolved_services.get(name)
        if service is None:
            errors.append(f"{name}: NOT RESOLVED")
            continue
        image = service.get("image")
        if not image or not isinstance(image, str):
            errors.append(f"{name}: image is missing or empty")
            continue
        resolved_images[name] = image.strip()

        # Check explicit expected image
        if expected_images and name in expected_images:
            exp_img = expected_images[name]
            if resolved_images[name] != exp_img:
                errors.append(
                    f"{name}: expected image '{exp_img}', got '{resolved_images[name]}'"
                )

        # Check expected tag
        if expected_tag:
            image_tag = resolved_images[name].split(":")[-1] if ":" in resolved_images[name] else ""
            if image_tag != expected_tag:
                errors.append(
                    f"{name}: image '{resolved_images[name]}' tag '{image_tag}' does not match expected tag '{expected_tag}'"
                )

    # Cross-service consistency check: all target services should resolve to the same image/tag
    if len(resolved_images) == len(target_services) and len(set(resolved_images.values())) > 1:
        # If explicit expected_images was provided and intentionally different, allow it;
        # otherwise, target services must resolve to the identical candidate image.
        if not expected_images or len(set(expected_images.values())) <= 1:
            errors.append(
                f"target services resolved to inconsistent images: {resolved_images}"
            )

    return (len(errors) == 0, errors)


def run_compose_config(
    release_dir: str,
    overrides: list[str],
    *,
    stack_tag: str,
    docker_cmd: list[str] | None = None,
) -> tuple[int, str, str]:
    """Execute docker compose config and return (exit_code, stdout, stderr)."""
    base_cmd = docker_cmd or ["sudo", "docker"]
    command = [
        *base_cmd,
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

    env = os.environ.copy()
    env["MEMORIA_RELEASE_TAG"] = stack_tag

    completed = subprocess.run(
        command,
        cwd=release_dir,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify resolved images in docker compose config.",
    )
    parser.add_argument("positional_args", nargs="*", help="[release-dir] [override ...]")
    parser.add_argument("--release-dir", "-d", help="Directory containing compose files")
    parser.add_argument(
        "--stack-tag",
        "-s",
        default=os.getenv("MEMORIA_STACK_TAG") or os.getenv("MEMORIA_RELEASE_TAG") or "20260901-0945-wake-word-whitelist",
        help="Base stack release tag for compose evaluation",
    )
    parser.add_argument(
        "--expected-tag",
        "-t",
        default=os.getenv("EXPECTED_CANDIDATE_TAG"),
        help="Expected release tag that target services must resolve to",
    )
    parser.add_argument(
        "--expected-image",
        action="append",
        default=[],
        help="Expected image in format service=image or image",
    )
    parser.add_argument(
        "--override",
        "-o",
        action="append",
        default=[],
        help="Compose override file",
    )
    args, extra = parser.parse_known_args(argv[1:])
    if extra:
        args.positional_args.extend(extra)
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    release_dir = args.release_dir
    overrides = list(args.override)

    if args.positional_args:
        if not release_dir:
            release_dir = args.positional_args[0]
            overrides.extend(args.positional_args[1:])
        else:
            overrides.extend(args.positional_args)

    if not release_dir:
        print("Error: release_dir must be provided", file=sys.stderr)
        return 2

    expected_images: dict[str, str] = {}
    for item in args.expected_image:
        if "=" in item:
            svc, img = item.split("=", 1)
            expected_images[svc.strip()] = img.strip()
        else:
            for svc in SERVICES:
                expected_images[svc] = item.strip()

    returncode, stdout, stderr = run_compose_config(
        release_dir,
        overrides,
        stack_tag=args.stack_tag,
    )
    if returncode != 0:
        print(stderr.strip()[:2000], file=sys.stderr)
        return 1

    try:
        data = json.loads(stdout)
        resolved = data.get("services", {})
    except json.JSONDecodeError as exc:
        print(f"Failed to parse compose json: {exc}", file=sys.stderr)
        return 1

    ok, errors = verify_resolved_services(
        resolved,
        expected_tag=args.expected_tag,
        expected_images=expected_images if expected_images else None,
    )

    for name in SERVICES:
        svc = resolved.get(name)
        img = svc.get("image") if svc else "NOT RESOLVED"
        print(f"{name} -> {img}")

    if not ok:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
