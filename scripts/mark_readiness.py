#!/usr/bin/env python3
"""Mark provider smoke evidence through the Control API readiness endpoint.

This helper is deliberately separate from ``verify_env.py``.  It must run in
the Control API container so the internal authentication secret never needs to
be present in the least-privileged Agent environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
    parser.add_argument(
        "--control-api-url",
        default=os.getenv("CONTROL_API_URL", "http://control-api:8000"),
    )
    args = parser.parse_args(argv)

    if not _mark_smokes_passed(args.control_api_url):
        return 1
    if not _check_ready(args.control_api_url):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
