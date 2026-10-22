#!/usr/bin/env python3
"""The persistent AI badge was removed on 2026-10-22.

This script used to paint review stills. It no longer draws one.
Historical stills stay under docs/acceptance/run-20261015-aigc-labels/mockups/.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "persistent AI badge was removed; this script no longer draws one",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
