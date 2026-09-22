#!/usr/bin/env python3
"""Check that a Compose file set resolves to the candidate images we intend to run.

The effective stack tag and the candidate tag are two different identities.
The stack tag decides which live base configuration Compose interpolates; the
candidate tag is the artifact this release is about to run.  A check that only
compares the two target services with each other passes when both still point
at the *live* image, so a candidate identity is mandatory here.

Usage:
  resolve_target_images.py --release-dir <dir> --stack-tag <tag> \\
    --expected-tag <candidate-tag> [--expected-image <svc=image|image> ...] \\
    [--override <file> ...]

Exits 2 when the invocation cannot describe a candidate (missing candidate
identity, missing stack tag, unusable release dir/override), and 1 when the
resolved services miss, diverge, or still name the live stack image.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

SERVICES = ("agent", "voice-core-media-bridge")
PROJECT_NAME = "memoria"
BASE_COMPOSE_FILE = "docker-compose.production.yml"
PROFILE = "media-runtime"

#: Candidate tags are image tags produced by the release pipeline; anything
#: else (empty, whitespace, shell metacharacters) is a caller mistake.
_CANDIDATE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def image_tag(image: str) -> str:
    """Return the tag of a Compose image reference, or '' when it has none."""

    repository = image.rsplit("/", 1)[-1]
    return repository.split(":", 1)[1] if ":" in repository else ""


def stack_image(stack_tag: str, repository: str = "memoria-agent") -> str:
    """Return the image the live stack resolves the selected services to."""

    return f"{repository}:{stack_tag}"


def verify_resolved_services(
    resolved_services: Mapping[str, Any],
    *,
    candidate_tag: str = "",
    stack_tag: str = "",
    expected_images: Mapping[str, str] | None = None,
    target_services: tuple[str, ...] = SERVICES,
    live_stack_image: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate that the target services exist, name the candidate, and agree.

    ``candidate_tag`` / ``expected_images`` carry the release identity and at
    least one of them is required.  ``stack_tag`` is used only to report the
    specific "still on the live stack image" failure instead of a bare tag
    mismatch.
    """

    errors: list[str] = []
    resolved_images: dict[str, str] = {}
    candidate_tag = candidate_tag.strip()

    if not candidate_tag and not expected_images:
        errors.append(
            "expected candidate identity is required: pass --expected-tag "
            "(or EXPECTED_CANDIDATE_TAG) and/or --expected-image"
        )
    if expected_images and len(set(expected_images.values())) > 1:
        errors.append(
            f"expected images must describe one candidate artifact: {dict(expected_images)}"
        )
    if expected_images:
        missing_expectations = set(target_services) - set(expected_images)
        if missing_expectations:
            errors.append(
                "expected images do not cover every target service: "
                f"{sorted(missing_expectations)}"
            )

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
        resolved_tag = image_tag(resolved_images[name])

        if expected_images and name in expected_images:
            expected_image = expected_images[name]
            if resolved_images[name] != expected_image:
                errors.append(
                    f"{name}: expected image '{expected_image}', "
                    f"got '{resolved_images[name]}'"
                )

        if candidate_tag and resolved_tag != candidate_tag:
            effective_stack_image = live_stack_image or stack_image(stack_tag)
            if stack_tag and resolved_images[name] == effective_stack_image:
                errors.append(
                    f"{name}: still resolves to the effective stack image "
                    f"'{resolved_images[name]}', not candidate '{candidate_tag}' "
                    "(candidate override missing, shadowed by an old-image "
                    "override, or applied out of order)"
                )
            else:
                errors.append(
                    f"{name}: image '{resolved_images[name]}' tag '{resolved_tag}' "
                    f"does not match expected tag '{candidate_tag}'"
                )

    # Agent and the media bridge ship as one runnable artifact, so a divergence
    # between them is never an intentional override.
    if len(resolved_images) == len(target_services) and len(set(resolved_images.values())) > 1:
        errors.append(f"target services resolved to inconsistent images: {resolved_images}")

    return (len(errors) == 0, errors)


def run_compose_config(
    release_dir: str,
    overrides: list[str],
    *,
    stack_tag: str,
    release_commit: str | None = None,
    docker_cmd: list[str] | None = None,
    profile: str = PROFILE,
) -> tuple[int, str, str]:
    """Execute docker compose config and return (exit_code, stdout, stderr)."""

    base_cmd = docker_cmd or ["sudo", "docker"]
    command = [
        *base_cmd,
        "compose",
        "--project-name",
        PROJECT_NAME,
    ]
    if profile:
        command += ["--profile", profile]
    command += ["--file", BASE_COMPOSE_FILE]
    for override in overrides:
        command += ["--file", override]
    command += ["config", "--format", "json"]

    env = os.environ.copy()
    env["MEMORIA_RELEASE_TAG"] = stack_tag
    # The production base file declares MEMORIA_RELEASE_COMMIT with `:?`, so the
    # render aborts before the image check unless the live value is provided.
    if release_commit:
        env["MEMORIA_RELEASE_COMMIT"] = release_commit

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
        default=os.getenv("MEMORIA_STACK_TAG"),
        help=(
            "Effective live stack release tag Compose interpolates the base file "
            "with (MEMORIA_RELEASE_TAG); distinct from the candidate tag"
        ),
    )
    parser.add_argument(
        "--release-commit",
        default=os.getenv("MEMORIA_RELEASE_COMMIT"),
        help="Effective live stack source commit (MEMORIA_RELEASE_COMMIT)",
    )
    parser.add_argument(
        "--expected-tag",
        "-t",
        default=os.getenv("EXPECTED_CANDIDATE_TAG"),
        help="Candidate release tag that target services must resolve to",
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
    parser.add_argument(
        "--docker-cmd",
        default=os.getenv("MEMORIA_DOCKER_CMD", "sudo docker"),
        help="Command that runs docker, split like a shell word list",
    )
    parser.add_argument(
        "--services",
        default=",".join(SERVICES),
        help="Comma-separated target services (default: agent and voice-core-media-bridge)",
    )
    parser.add_argument(
        "--profile",
        default=PROFILE,
        help="Compose profile used to render the stack (default: media-runtime)",
    )
    parser.add_argument(
        "--stack-image",
        help=(
            "Effective live image for the selected services; defaults to "
            "memoria-agent:<stack-tag>"
        ),
    )
    args, extra = parser.parse_known_args(argv[1:])
    if extra:
        args.positional_args.extend(extra)
    return args


def _collect_expected_images(items: list[str], services: tuple[str, ...]) -> dict[str, str]:
    expected_images: dict[str, str] = {}
    for item in items:
        if "=" in item:
            service, image = item.split("=", 1)
            service = service.strip()
            image = image.strip()
            if not service or not image or service in expected_images:
                raise ValueError("--expected-image entries must be unique and non-empty")
            expected_images[service] = image
        else:
            image = item.strip()
            if not image:
                raise ValueError("--expected-image entries must be non-empty")
            for service in services:
                if service in expected_images:
                    raise ValueError("--expected-image entries must not overlap")
                expected_images[service] = image
    return expected_images


def _target_services(value: str) -> tuple[str, ...]:
    services = tuple(item.strip() for item in value.split(",") if item.strip())
    if not services or len(services) != len(set(services)):
        raise ValueError("--services must contain unique, non-empty service names")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item) for item in services):
        raise ValueError("--services contains an invalid service name")
    return services


def _preflight(
    *,
    release_dir: str,
    overrides: list[str],
) -> str | None:
    if not os.path.isdir(release_dir):
        return f"release_dir is not a directory: {release_dir}"
    base_file = os.path.join(release_dir, BASE_COMPOSE_FILE)
    if not os.path.isfile(base_file):
        return f"base compose file is missing: {base_file}"
    for override in overrides:
        if not override or os.path.islink(override) or not os.path.isfile(override):
            return f"override is missing, unsafe or not a file: {override!r}"
    return None


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

    try:
        target_services = _target_services(args.services)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    candidate_tag = (args.expected_tag or "").strip()
    stack_tag = (args.stack_tag or "").strip()
    try:
        expected_images = _collect_expected_images(args.expected_image, target_services)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if set(expected_images) - set(target_services):
        print("Error: --expected-image names a service outside --services", file=sys.stderr)
        return 2
    live_stack_image = (args.stack_image or "").strip() or stack_image(stack_tag)

    # Refuse to run a consistency-only check: two services on the live image
    # would otherwise pass, which is exactly what this gate must stop.
    if not candidate_tag and not expected_images:
        print(
            "Error: explicit candidate identity is required; refusing to check internal "
            "consistency alone (pass --expected-tag or --expected-image)",
            file=sys.stderr,
        )
        return 2
    if candidate_tag and not _CANDIDATE_TAG_RE.fullmatch(candidate_tag):
        print(f"Error: invalid candidate tag: {candidate_tag!r}", file=sys.stderr)
        return 2
    if not stack_tag:
        print(
            "Error: --stack-tag (or MEMORIA_STACK_TAG / MEMORIA_RELEASE_TAG) is "
            "required; the effective stack tag and the candidate tag are "
            "different identities",
            file=sys.stderr,
        )
        return 2

    preflight_error = _preflight(release_dir=release_dir, overrides=overrides)
    if preflight_error is not None:
        print(f"Error: {preflight_error}", file=sys.stderr)
        return 2

    returncode, stdout, stderr = run_compose_config(
        release_dir,
        overrides,
        stack_tag=stack_tag,
        release_commit=args.release_commit,
        docker_cmd=shlex.split(args.docker_cmd),
        profile=args.profile,
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
        candidate_tag=candidate_tag,
        stack_tag=stack_tag,
        expected_images=expected_images if expected_images else None,
        target_services=target_services,
        live_stack_image=live_stack_image,
    )

    print(f"stack_tag={stack_tag} candidate_tag={candidate_tag or '<image-only>'}")
    for name in target_services:
        service = resolved.get(name)
        image = service.get("image") if service else "NOT RESOLVED"
        print(f"{name} -> {image}")

    if not ok:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print("target images resolve to the candidate")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
