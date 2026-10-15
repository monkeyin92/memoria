#!/usr/bin/env python3
"""Render product-review mockups of the 「AI」 corner badge over expression frames.

Uses the host mascot preview harness for real expression frames, then draws the
same indigo corner badge the firmware LVGL chrome uses (app-v4 #4A52C9).
Outputs PNGs under docs/acceptance/run-20261015-aigc-labels/mockups/.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DEFAULT = REPO / "docs" / "acceptance" / "run-20261015-aigc-labels" / "mockups"
PREVIEW = REPO / "firmware" / "esp32" / "scripts" / "preview_memoria_mascot.py"
SIZE = 360
INDIGO = (0x4A, 0x52, 0xC9, 0xCC)
WHITE = (255, 255, 255, 255)

# (filename stem, ms into LIVELINESS_DAY)
MOMENTS = (
    ("listening", 12000),
    ("thinking", 18000),
    ("speaking-happy", 24000),
    ("speaking-mid", 27000),
)


def _load_preview():
    spec = importlib.util.spec_from_file_location("preview_memoria_mascot", PREVIEW)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _draw_badge(img) -> None:
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img, "RGBA")
    text = "AI"
    pad_x, pad_y = 10, 4
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    w, h = tw + 2 * pad_x, th + 2 * pad_y
    x0 = SIZE - 40 - w
    y0 = 36
    draw.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=12, fill=INDIGO)
    draw.text((x0 + pad_x, y0 + pad_y - 1), text, font=font, fill=WHITE)


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

    preview = _load_preview()
    fps = 25
    step = 1000 // fps
    # Prefer g++ when clang++ lacks a working libstdc++ (common on slim images).
    import os
    import shutil as _shutil
    import subprocess as _sp

    gxx = _shutil.which("g++")
    clangxx = _shutil.which("clang++")
    if gxx and clangxx:
        probe = _sp.run(
            [clangxx, "-std=c++17", "-x", "c++", "-", "-o", "/tmp/clang-probe"],
            input="int main(){return 0;}\n",
            text=True,
            capture_output=True,
        )
        if probe.returncode != 0:
            shim = Path("/tmp/bin-prefer-gxx")
            shim.mkdir(exist_ok=True)
            link = shim / "clang++"
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(gxx)
            os.environ["PATH"] = f"{shim}:{os.environ.get('PATH', '')}"
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
        img.convert("RGB").save(dest)
        written.append(dest)
        print(f"wrote {dest.relative_to(REPO)}")

    index = args.out / "README.md"
    lines = [
        "# AIGC 「AI」 badge mockups",
        "",
        "Product review stills for the persistent corner badge during live dialogue.",
        "Frames come from the host mascot compositor (`LIVELINESS_DAY`).",
        "Badge: indigo `#4A52C9` (app-v4), top-right, clear of the face.",
        "",
    ]
    for path in written:
        lines.append(f"- `{path.name}` at {dict(MOMENTS)[path.stem.replace('badge-', '')]} ms")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
