#!/usr/bin/env python3
"""Render product-review mockups of the 「AI」 corner badge over expression frames.

Uses the host mascot preview harness for real expression frames, then draws the
badge using the same geometry as memoria_aigc_label.h (inside the round panel).
Outputs PNGs under docs/acceptance/run-20261015-aigc-labels/mockups/.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DEFAULT = REPO / "docs" / "acceptance" / "run-20261015-aigc-labels" / "mockups"
PREVIEW = REPO / "firmware" / "esp32" / "scripts" / "preview_memoria_mascot.py"
LABEL_H = (
    REPO
    / "firmware"
    / "esp32"
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_aigc_label.h"
)
SIZE = 360
INDIGO = (0x4A, 0x52, 0xC9, 255)  # opacity 100%
WHITE = (255, 255, 255, 255)

# (filename stem, ms into LIVELINESS_DAY) — offline-menu uses idle frame + badge
MOMENTS = (
    ("listening", 12000),
    ("thinking", 18000),
    ("speaking-happy", 24000),
    ("speaking-mid", 27000),
    ("stuck-offline-menu", 2000),  # idle expression; badge forced for stuck → menu
)


def _load_preview():
    spec = importlib.util.spec_from_file_location("preview_memoria_mascot", PREVIEW)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _badge_geometry() -> tuple[int, int, int, int, int]:
    text = LABEL_H.read_text(encoding="utf-8")
    def const(name: str) -> int:
        import re

        match = re.search(rf"inline constexpr int {name} = (-?\d+);", text)
        if not match:
            raise SystemExit(f"missing {name} in {LABEL_H}")
        return int(match.group(1))

    return (
        const("kAigcBadgeX"),
        const("kAigcBadgeY"),
        const("kAigcBadgeW"),
        const("kAigcBadgeH"),
        const("kAigcBadgeFontPx"),
    )


def _draw_badge(img) -> None:
    from PIL import ImageDraw, ImageFont

    x0, y0, w, h, font_px = _badge_geometry()
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_px
        )
    except OSError:
        font = ImageFont.load_default()
    draw.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=12, fill=INDIGO)
    text = "AI"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (w - tw) // 2
    ty = y0 + (h - th) // 2 - 1
    draw.text((tx, ty), text, font=font, fill=WHITE)


def _prefer_gxx() -> None:
    gxx = shutil.which("g++")
    clangxx = shutil.which("clang++")
    if not gxx or not clangxx:
        return
    probe = subprocess.run(
        [clangxx, "-std=c++17", "-x", "c++", "-", "-o", "/tmp/clang-probe"],
        input="int main(){return 0;}\n",
        text=True,
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    shim = Path("/tmp/bin-prefer-gxx")
    shim.mkdir(exist_ok=True)
    link = shim / "clang++"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(gxx)
    os.environ["PATH"] = f"{shim}:{os.environ.get('PATH', '')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if shutil.which("clang++") is None and shutil.which("g++") is None:
        print("host C++ compiler unavailable", file=sys.stderr)
        return 1

    import numpy as np
    from PIL import Image

    x0, y0, w, h, _ = _badge_geometry()
    # Mirror the compile-time static_assert for mockup/CI.
    cx = cy = SIZE / 2
    limit = (180 - 8) ** 2
    for x, y in ((x0, y0), (x0 + w, y0), (x0, y0 + h), (x0 + w, y0 + h)):
        if (x - cx) ** 2 + (y - cy) ** 2 > limit:
            raise SystemExit(f"badge corner ({x},{y}) outside safe circle")

    _prefer_gxx()
    preview = _load_preview()
    fps = 25
    step = 1000 // fps
    with tempfile.TemporaryDirectory(prefix="aigc-badge-") as tmp:
        work = Path(tmp)
        binary = preview.compile_harness(work)
        raw = work / "frames.raw"
        preview.run(
            binary,
            work,
            fps,
            str(raw),
            timeline=preview.LIVELINESS_DAY,
            first="starlight",
        )
        data = np.fromfile(raw, dtype="<u2").reshape(-1, SIZE, SIZE)

    def to_rgb(frame: np.ndarray) -> np.ndarray:
        r = ((frame >> 11) & 0x1F).astype(np.uint16)
        g = ((frame >> 5) & 0x3F).astype(np.uint16)
        b = (frame & 0x1F).astype(np.uint16)
        rgb = np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1)
        yy, xx = np.mgrid[0:SIZE, 0:SIZE]
        outside = (xx + 0.5 - SIZE / 2) ** 2 + (yy + 0.5 - SIZE / 2) ** 2 > (SIZE / 2) ** 2
        rgb[outside] = 24
        return rgb.astype(np.uint8)

    written: list[Path] = []
    for name, ms in MOMENTS:
        index = min(ms // step, data.shape[0] - 1)
        img = Image.fromarray(to_rgb(data[index])).convert("RGBA")
        _draw_badge(img)
        dest = args.out / f"badge-{name}.png"
        img.convert("RGB").save(dest, format="PNG")
        written.append(dest)
        print(f"wrote {dest.relative_to(REPO)}")

    index = args.out / "README.md"
    lines = [
        "# AIGC 「AI」 badge mockups",
        "",
        "Product review stills for the persistent corner badge during live dialogue",
        "and stuck → offline menu. Geometry matches `memoria_aigc_label.h`",
        "(inside the 360 round panel, ≥8 px from the edge).",
        "",
    ]
    for path in written:
        stem = path.stem.replace("badge-", "")
        ms = dict(MOMENTS)[stem]
        lines.append(f"- `{path.name}` at {ms} ms")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
