#!/usr/bin/env python3
"""Write a bench or keep-awake manifest with one playback path compiled out.

The source manifest already carries CONFIG_HEAP_POISONING_COMPREHENSIVE. The
product manifest is never the source: spoken stop and standby-after-playback
stay at their Kconfig default, which is on.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def compose(manifest: dict, *, disable_playback_stop: bool, disable_standby: bool) -> dict:
    out = copy.deepcopy(manifest)
    options = out["builds"][0]["sdkconfig_append"]
    if "CONFIG_HEAP_POISONING_COMPREHENSIVE=y" not in options:
        raise SystemExit("bisect manifest is missing CONFIG_HEAP_POISONING_COMPREHENSIVE=y")
    if disable_playback_stop:
        options.append("CONFIG_MEMORIA_PLAYBACK_STOP=n")
    if disable_standby:
        options.append("CONFIG_MEMORIA_STANDBY_AFTER_PLAYBACK=n")
    if not disable_playback_stop and not disable_standby:
        raise SystemExit("compose_bisect_config needs a path to disable")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source")
    parser.add_argument("dest")
    parser.add_argument("--disable-playback-stop", action="store_true")
    parser.add_argument("--disable-standby-after-playback", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(args.source).read_text(encoding="utf-8"))
    composed = compose(
        manifest,
        disable_playback_stop=args.disable_playback_stop,
        disable_standby=args.disable_standby_after_playback,
    )
    Path(args.dest).write_text(json.dumps(composed, indent=4) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
