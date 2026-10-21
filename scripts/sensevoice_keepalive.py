"""Keep the SenseVoice ASR sidecar warm with synthetic silence.

Committed as configuration only; nothing in this repository schedules it on a
server. The probe exercises the model rather than just the HTTP socket, because
the failure this guards against is anonymous model pages being swapped out
after hours of idleness (12.7 s first request, see
docs/runbooks/sensevoice-asr.md).

The probe audio is generated silence, never a recording. The response text is
discarded: this script never prints a transcript.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


def silence_pcm(seconds: float = 1.0, sample_rate: int = 16_000) -> bytes:
    """Return signed 16-bit little-endian mono silence."""

    if seconds <= 0:
        raise ValueError("probe duration must be positive")
    if sample_rate != 16_000:
        raise ValueError("SenseVoice sidecar only accepts 16000 Hz")
    return b"\x00\x00" * int(seconds * sample_rate)


def probe(url: str, *, timeout_s: float = 15.0, seconds: float = 1.0) -> bool:
    """POST one silence frame; return whether the sidecar answered 200."""

    request = urllib.request.Request(
        f"{url}?format=pcm&sample_rate=16000&language=zh",
        data=silence_pcm(seconds),
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response.read()
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8001/transcribe",
        help="SenseVoice /transcribe endpoint",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=15.0,
        help="cold-start budget; keep above the measured 12.7 s swap-in",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=1.0,
        help="synthetic silence duration",
    )
    args = parser.parse_args(argv)
    if probe(args.url, timeout_s=args.timeout_s, seconds=args.seconds):
        print("sensevoice keepalive ok")
        return 0
    print("sensevoice keepalive failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
