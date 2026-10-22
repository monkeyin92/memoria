#!/usr/bin/env python3
"""Decode every board-asset Ogg and require the full implicit AIGC OpusTags set.

Fails CI when any ``*.ogg`` under the Memoria ESP-VoCat assets tree cannot
decode through ffmpeg, is missing any of CONTENT_ID, SERVICE_PROVIDER,
AI_GENERATED, ARTIST, VOICE_ID, DATE, CONTENT_PRODUCER, has a
SERVICE_PROVIDER that does not own its VOICE_ID, or has ARTIST != VOICE_ID.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.common.aigc_opus import (  # noqa: E402
    IMPLICIT_AIGC_TAG_KEYS,
    pcm_digest,
    require_aigc_tags,
)

BOARD_ASSETS = (
    REPO
    / "firmware"
    / "esp32"
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "assets"
)


def check_assets(root: Path) -> int:
    errors: list[str] = []
    accepted_providers: set[str] = set()
    oggs = sorted(path for path in root.rglob("*.ogg") if path.is_file())
    if not oggs:
        print(f"error: no .ogg files under {root}", file=sys.stderr)
        return 1
    for path in oggs:
        rel = path.relative_to(root).as_posix()
        blob = path.read_bytes()
        try:
            pcm_digest(blob)
        except ValueError as exc:
            errors.append(f"{rel} decode failed: {exc}")
            continue
        try:
            tags = require_aigc_tags(blob[:65536], content_id=path.stem, implicit=True)
        except ValueError as exc:
            errors.append(f"{rel} {exc}")
            continue
        if tags["artist"] != tags["voice_id"]:
            errors.append(
                f"{rel} ARTIST must equal VOICE_ID, "
                f"got ARTIST={tags['artist']!r} VOICE_ID={tags['voice_id']!r}"
            )
            continue
        accepted_providers.add(str(tags["service_provider"]))
    print(f"checked_oggs={len(oggs)}")
    print(f"required_tags={','.join(IMPLICIT_AIGC_TAG_KEYS)}")
    print("service_providers=" + ",".join(sorted(accepted_providers)))
    for message in errors:
        print(f"error: {message}", file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-root",
        type=Path,
        default=BOARD_ASSETS,
        help="board assets root to scan for .ogg files",
    )
    args = parser.parse_args(argv)
    return check_assets(args.assets_root)


if __name__ == "__main__":
    raise SystemExit(main())
