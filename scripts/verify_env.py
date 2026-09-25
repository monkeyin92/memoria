#!/usr/bin/env python3
"""Validate deployment config and update/check the short-lived readiness gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError
from services.agent.src.config import load_settings, load_turn_timing
from services.agent.src.contracts.errors import ConfigValidationError


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _validation_messages(error: ValidationError) -> list[str]:
    messages: list[str] = []
    for issue in error.errors():
        cause = issue.get("ctx", {}).get("error")
        message = str(cause) if cause is not None else str(issue["msg"])
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        if message not in messages:
            messages.append(message)
    return messages


def _validate_environment() -> tuple[list[str], bool, str]:
    offline = _truthy(os.getenv("OFFLINE_MOCK", "false"))
    profile = os.getenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    try:
        settings = load_settings(require_keys=not offline)
    except ValidationError as exc:
        return _validation_messages(exc), offline, profile
    except ConfigValidationError as exc:
        return [str(exc)], offline, profile
    try:
        load_turn_timing(settings.deployment_profile)
    except ValueError as exc:
        return [str(exc)], offline, profile
    return [], settings.offline_mock, settings.deployment_profile


def _request_json(
    url: str,
    *,
    method: str = "GET",
    authorization: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    data = None
    if authorization is not None:
        headers["Authorization"] = f"Bearer {authorization}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - operator-supplied URL
            status = response.status
            raw = response.read()
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except URLError:
        return 0, {}
    try:
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    return status, body if isinstance(body, dict) else {}


def _mark_smokes_passed(base_url: str) -> bool:
    secret = os.getenv("MEMORIA_AUTH_SECRET", "")
    if not secret:
        print("readiness mark FAILED: MEMORIA_AUTH_SECRET is not configured")
        return False
    status, body = _request_json(
        f"{base_url.rstrip('/')}/internal/readiness/smokes",
        method="POST",
        authorization=secret,
        payload={
            "livekit": True,
            "funasr": True,
            "llm": True,
            "llm_provider": os.getenv("LLM_PROVIDER", "qwen"),
            "release_tag": os.getenv("MEMORIA_RELEASE_TAG", "development"),
            "tts": {
                "provider": "doubao",
                "audio": True,
                "word_timestamps": True,
            },
        },
    )
    if status != 200 or body.get("status") != "marked":
        print(f"readiness mark FAILED: HTTP {status or 'unreachable'}")
        return False
    print("readiness mark OK")
    return True


def _check_ready(base_url: str) -> bool:
    status, body = _request_json(f"{base_url.rstrip('/')}/health/ready")
    if status != 200 or body.get("status") != "ready":
        print(f"readiness check FAILED: HTTP {status or 'unreachable'}")
        return False
    print("readiness check OK")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark-smokes-passed", action="store_true")
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument(
        "--control-api-url",
        default=os.getenv("CONTROL_API_URL", "http://127.0.0.1:8000"),
    )
    args = parser.parse_args(argv)

    errors, offline, profile = _validate_environment()
    if args.mark_smokes_passed and offline:
        errors.append("--mark-smokes-passed is not used with OFFLINE_MOCK=true")
    if errors:
        print("verify_env FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("verify_env OK")
    if offline:
        print("  mode: OFFLINE_MOCK (provider keys not required)")
    if profile == "cn_self_hosted":
        print("  note: cn_self_hosted should use LIVEKIT_TURN_DETECTOR_VERSION=v1-mini")
    if args.mark_smokes_passed and not _mark_smokes_passed(args.control_api_url):
        return 1
    if args.check_ready and not _check_ready(args.control_api_url):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
