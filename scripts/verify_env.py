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

from services.common.security_constants import DEV_AUTH_SECRET


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _validate_environment() -> tuple[list[str], bool, str]:
    errors: list[str] = []

    def req(name: str) -> str:
        return os.getenv(name, "")

    offline = _truthy(req("OFFLINE_MOCK") or "false")

    if req("FUNASR_SAMPLE_RATE") not in ("", "16000"):
        errors.append("FUNASR_SAMPLE_RATE must be 16000")
    if req("COSYVOICE_SAMPLE_RATE") not in ("", "24000"):
        errors.append("COSYVOICE_SAMPLE_RATE must be 24000")

    try:
        vad = float(req("VAD_MIN_SILENCE_DURATION_S") or "0.30")
    except ValueError:
        errors.append("VAD_MIN_SILENCE_DURATION_S must be a number")
    else:
        if vad < 0.25:
            errors.append("VAD_MIN_SILENCE_DURATION_S must be >= 0.25")

    if not _truthy(req("COSYVOICE_WORD_TIMESTAMPS") or "true"):
        errors.append("COSYVOICE_WORD_TIMESTAMPS must be true")

    profile = req("DEPLOYMENT_PROFILE") or "livekit_cloud"
    if profile == "livekit_cloud" and not _truthy(
        req("LIVEKIT_ADAPTIVE_INTERRUPTION") or "true"
    ):
        errors.append("livekit_cloud requires LIVEKIT_ADAPTIVE_INTERRUPTION=true")

    for model_key in ("DEEPSEEK_FAST_MODEL", "DEEPSEEK_DEEP_MODEL"):
        model = req(model_key)
        if model in ("deepseek-chat", "deepseek-reasoner"):
            errors.append(f"{model_key} uses deprecated model {model}")

    environment = req("ENVIRONMENT") or "development"
    if environment == "production":
        if "*" in req("ALLOWED_ORIGINS"):
            errors.append("production must not allow * CORS")
        for key in ("PUBLIC_BASE_URL", "LIVEKIT_URL"):
            value = req(key)
            if value.startswith(("http://", "ws://")):
                errors.append(f"production forbids plaintext {key}")
        auth_secret = req("MEMORIA_AUTH_SECRET")
        if auth_secret == DEV_AUTH_SECRET or len(auth_secret) < 32:
            errors.append("production requires independent MEMORIA_AUTH_SECRET (>=32 chars)")
        if auth_secret and auth_secret == req("LIVEKIT_API_SECRET"):
            errors.append("MEMORIA_AUTH_SECRET must differ from LIVEKIT_API_SECRET")
        release_tag = req("MEMORIA_RELEASE_TAG").strip().lower()
        if release_tag in ("", "latest", "development"):
            errors.append("production requires an immutable MEMORIA_RELEASE_TAG")

    llm_provider = req("LLM_PROVIDER") or "qwen"
    if llm_provider not in ("qwen", "deepseek"):
        errors.append("LLM_PROVIDER must be qwen or deepseek")

    if not offline:
        for key in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "DASHSCOPE_API_KEY"):
            if not req(key):
                errors.append(f"missing required env: {key} (set OFFLINE_MOCK=true to skip)")
        if llm_provider == "deepseek" and not req("DEEPSEEK_API_KEY"):
            errors.append("missing required env: DEEPSEEK_API_KEY for LLM_PROVIDER=deepseek")

    return errors, offline, profile


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
            "cosyvoice": True,
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
