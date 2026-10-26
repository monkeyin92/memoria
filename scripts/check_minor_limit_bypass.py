#!/usr/bin/env python3
"""Say whether one subject is the minor-limit bypass, without printing the id.

Read the subject id from stdin. The whitelist is ``MINOR_LIMIT_BYPASS_TEST_SUBJECTS``,
the same variable ``MinorLimitBypass.from_env`` reads. Stdout is three lines.
The raw id is not among them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.agent.src.minor_limit_bypass import (  # noqa: E402
    MinorLimitBypass,
    subject_id_hash,
)


def report(subject_id: str, environ: dict[str, str] | None = None) -> str:
    bypass = MinorLimitBypass.from_env(environ)
    hit = bypass.enabled and subject_id in bypass.subject_ids
    return "\n".join(
        (
            f"bypass={'yes' if hit else 'no'}",
            f"enabled={'yes' if bypass.enabled else 'no'}",
            f"subject_sha256_8={subject_id_hash(subject_id)}",
        )
    )


def main() -> int:
    subject_id = sys.stdin.read().strip()
    if not subject_id or "\n" in subject_id or "\r" in subject_id:
        print("pass one subject id on stdin", file=sys.stderr)
        return 2
    print(report(subject_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
