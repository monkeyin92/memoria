"""Firmware release blockers: a ``TODO(release-blocker):`` marker in the overlay
and the same line, unticked, in HANDOFF.md's ``## 发布前检查清单``.

The two must agree: a marker without its checklist line could ship unnoticed,
and a checklist line ticked off (or dropped) while the code still carries the
marker would claim a blocker is gone when it is not.
"""

from __future__ import annotations

import pathlib
import re

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
OVERLAY = FIRMWARE_ROOT / "overlay"
HANDOFF = FIRMWARE_ROOT.parents[1] / "HANDOFF.md"
MARKER = b"TODO(release-blocker):"
USB_WAKE = (
    "TODO(release-blocker): remove USB serial wake before final firmware; "
    "it opens the mic without a screen tap"
)


def _overlay_markers() -> list[str]:
    markers: list[str] = []
    for path in sorted(OVERLAY.rglob("*")):
        if not path.is_file() or MARKER not in (data := path.read_bytes()):
            continue
        for line in data.decode("utf-8").splitlines():
            if match := re.search(r"TODO\(release-blocker\):.*", line):
                markers.append(match.group(0).rstrip())
    return markers


def _checklist() -> dict[str, bool]:
    """Each checklist line mapped to whether it is ticked."""

    handoff = HANDOFF.read_text(encoding="utf-8")
    section = handoff.split("\n## 发布前检查清单\n", 1)[1].split("\n## ", 1)[0]
    items = re.findall(r"^- \[([ x])\] (.+)$", section, re.M)
    return {text.rstrip(): mark == "x" for mark, text in items}


def test_every_overlay_release_blocker_is_unticked_on_the_handoff_checklist() -> None:
    checklist = _checklist()
    for marker in _overlay_markers():
        assert marker in checklist, f"add to HANDOFF.md 发布前检查清单: - [ ] {marker}"
        assert not checklist[marker], f"ticked off but still in the firmware: {marker}"


def test_every_open_checklist_blocker_is_still_marked_in_the_firmware() -> None:
    markers = set(_overlay_markers())
    open_items = [item for item, ticked in _checklist().items() if not ticked]
    for item in open_items:
        assert item in markers, f"no longer in the firmware; tick or drop it: {item}"


def test_usb_wake_is_a_release_blocker_at_its_handler() -> None:
    board = OVERLAY / "files" / "main" / "boards" / "memoria" / "esp-vocat"
    source = (board / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    assert f"    // {USB_WAKE}\n    void HandleUsbWake() {{" in source
