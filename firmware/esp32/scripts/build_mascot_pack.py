#!/usr/bin/env python3
"""Pack the v2 companion mascots for the device screen.

Reads the source art in ``apps/miniprogram/assets/companions/<id>/`` (512 px
RGBA, including the device-only ``*-blink``, ``*-talk`` and ``greeting``
frames made by ``design-preview/memoria-v2/tools/device_frames.py``) and writes
one ``mascot_<id>.mmp`` per companion plus ``brand_mark.mma`` into the board's
assets directory, which the firmware build copies into the ``assets``
partition.

The pack format is read by ``memoria_mascot_pack.cc`` (see the layout comment
in ``memoria_mascot_pack.h``).  Every frame is palette-indexed (at most 255
colours plus alpha) and zlib-compressed.  Blink and talk frames are stored as
patches over their mood frame: only pixels that differ are kept, everything
else is index 255 ("keep the base pixel"), so swapping them on the device can
never shift or recolour the rest of the character.

The output is deterministic; tests/test_memoria_mascot.py rebuilds it and
compares bytes, so rerun this script after changing any source frame:

    uv run python firmware/esp32/scripts/build_mascot_pack.py
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import zlib

import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[3]
SOURCE_DIR = REPO / "apps" / "miniprogram" / "assets" / "companions"
BOARD_DIR = HERE.parents[1] / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
OUT_DIR = BOARD_DIR / "assets"
BRAND_FONT_CANDIDATES = (
    # Montserrat is OFL-licensed and ships with the LVGL component.
    HERE.parents[1] / ".cache" / "upstream" / "managed_components" / "lvgl__lvgl" / "scripts"
    / "built_in_font" / "Montserrat-Medium.ttf",
)

MAGIC = b"MMP1"
VERSION = 1
CANVAS = 256
KEEP_INDEX = 255
HEADER = struct.Struct("<4sHHHHHHIIIII16s12s")  # 64 bytes
ENTRY = struct.Struct("<BBHHHHHIII")  # 24 bytes
assert HEADER.size == 64 and ENTRY.size == 24

# Frame ids are shared with memoria_mascot_pack.h (enum class MascotFrame).
FRAMES: tuple[tuple[str, str | None], ...] = (
    ("default", None),
    ("happy", None),
    ("sad", None),
    ("surprised", None),
    ("thinking", None),
    ("listening", None),
    ("sleepy", None),
    ("dizzy", None),
    ("greeting", None),
    ("default-blink", "default"),
    ("sad-blink", "sad"),
    ("surprised-blink", "surprised"),
    ("thinking-blink", "thinking"),
    ("listening-blink", "listening"),
    ("default-talk", "default"),
    ("happy-talk", "happy"),
    ("sad-talk", "sad"),
    ("surprised-talk", "surprised"),
    ("thinking-talk", "thinking"),
)
FRAME_ID = {name: index for index, (name, _) in enumerate(FRAMES)}

# Scene colours per companion, from the Mini Program role tokens (app.wxss):
# the background runs from a lightened role-soft centre to role-soft at the
# rim; ink is for text, primary for the state ring, accent for highlights.
THEMES = {
    "starlight": {"soft": 0xDDE6F5, "primary": 0x3D5A80, "accent": 0xF2C14E, "ink": 0x22344D},
    "taoxi": {"soft": 0xFBE3E8, "primary": 0xE07A95, "accent": 0xFFB84D, "ink": 0x7E3149},
    "mianmian": {"soft": 0xE4F0E6, "primary": 0x7FA88A, "accent": 0xC9A6E0, "ink": 0x3F5A45},
    "axu": {"soft": 0xF1E5D6, "primary": 0x9A6B3C, "accent": 0x5F7F95, "ink": 0x5A3E1E},
    "xuanmo": {"soft": 0xE2E6DA, "primary": 0x5E6B4E, "accent": 0x5B8DEF, "ink": 0x2B3326},
}
COMPANION_IDS = tuple(THEMES)


def _mix(color: int, other: int, amount: float) -> int:
    out = 0
    for shift in (16, 8, 0):
        a = (color >> shift) & 0xFF
        b = (other >> shift) & 0xFF
        out |= round(a + (b - a) * amount) << shift
    return out


def _rgb565(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0].astype(np.uint16)
    g = rgb[..., 1].astype(np.uint16)
    b = rgb[..., 2].astype(np.uint16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _load(companion: str, frame: str) -> np.ndarray:
    image = Image.open(SOURCE_DIR / companion / f"{frame}.png").convert("RGBA")
    image = image.resize((CANVAS, CANVAS), Image.LANCZOS)
    pixels = np.asarray(image, dtype=np.uint8).copy()
    pixels[pixels[..., 3] < 4] = 0  # fully clear pixels share one palette slot
    return pixels


def _quantize(pixels: np.ndarray, keep: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return (indices, palette RGBA) with at most 255 colours.

    ``keep`` marks pixels that are not stored (patch frames); they become
    KEEP_INDEX and do not take part in the palette.
    """
    height, width = pixels.shape[:2]
    flat = pixels.reshape(-1, 4)
    active = np.ones(flat.shape[0], dtype=bool) if keep is None else ~keep.reshape(-1)
    indices = np.full(flat.shape[0], KEEP_INDEX, dtype=np.uint8)
    chosen = flat[active]
    if chosen.shape[0] == 0:
        return indices.reshape(height, width), np.zeros((0, 4), dtype=np.uint8)
    strip = Image.fromarray(chosen.reshape(1, -1, 4), "RGBA")
    quantized = strip.quantize(colors=255, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    palette = np.asarray(quantized.getpalette(rawmode="RGBA"), dtype=np.uint8).reshape(-1, 4)
    used = np.asarray(quantized, dtype=np.uint8).reshape(-1)
    count = int(used.max()) + 1
    indices[active] = used
    return indices.reshape(height, width), palette[:count]


def _encode(indices: np.ndarray, palette: np.ndarray) -> tuple[bytes, bytes]:
    colours = _rgb565(palette[:, :3])
    table = b"".join(struct.pack("<HBB", int(c), int(a), 0) for c, a in zip(colours, palette[:, 3], strict=True))
    return table, zlib.compress(indices.tobytes(), 9)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def build_companion(companion: str) -> bytes:
    theme = THEMES[companion]
    loaded = {name: _load(companion, name) for name, _ in FRAMES}
    default_alpha = loaded["default"][..., 3] > 128
    ys, xs = np.nonzero(default_alpha)
    foot_y = int(ys.max())
    feet_row = np.nonzero(default_alpha[max(0, foot_y - 12)])[0]
    foot_half_w = int((feet_row.max() - feet_row.min()) // 2) if feet_row.size else 40

    entries = []
    blobs = []
    for name, base in FRAMES:
        pixels = loaded[name]
        if base is None:
            box = _bbox(pixels[..., 3] > 0)
            assert box is not None, f"{companion}/{name} is empty"
            x0, y0, x1, y1 = box
            crop = pixels[y0:y1, x0:x1]
            indices, palette = _quantize(crop)
            base_id = 0xFF
        else:
            source = loaded[base].astype(np.int16)
            delta = np.abs(pixels.astype(np.int16) - source).max(axis=2)
            changed = delta > 3
            box = _bbox(changed)
            assert box is not None, f"{companion}/{name} does not differ from {base}"
            x0, y0, x1, y1 = box
            crop = pixels[y0:y1, x0:x1]
            indices, palette = _quantize(crop, keep=~changed[y0:y1, x0:x1])
            base_id = FRAME_ID[base]
        table, stream = _encode(indices, palette)
        entries.append((FRAME_ID[name], base_id, len(palette), x0, y0, x1 - x0, y1 - y0, len(stream)))
        blobs.append(table + stream)

    offset = HEADER.size + ENTRY.size * len(entries)
    table_bytes = b""
    for (frame_id, base_id, colours, x, y, w, h, stream_size), blob in zip(entries, blobs, strict=True):
        palette_bytes = colours * 4
        table_bytes += ENTRY.pack(frame_id, base_id, colours, x, y, w, h, offset, stream_size, 0)
        offset += palette_bytes + stream_size
        assert len(blob) == palette_bytes + stream_size
    header = HEADER.pack(
        MAGIC,
        VERSION,
        len(entries),
        CANVAS,
        CANVAS,
        foot_y,
        foot_half_w,
        _mix(theme["soft"], 0xFFFFFF, 0.72),
        theme["soft"],
        theme["ink"],
        theme["primary"],
        theme["accent"],
        companion.encode().ljust(16, b"\0"),
        b"\0" * 12,
    )
    return header + table_bytes + b"".join(blobs)


BRAND_MAGIC = b"MMA1"
BRAND_HEADER = struct.Struct("<4sHHI")  # magic, width, height, compressed size


def build_brand_mark(font_path: pathlib.Path) -> bytes:
    """The boot wordmark as an 8-bit alpha mask ("memoria", tracked out)."""
    from PIL import ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), 34)
    text = "memoria"
    tracking = 5
    widths = [font.getbbox(ch)[2] - font.getbbox(ch)[0] for ch in text]
    ascent, descent = font.getmetrics()
    width = sum(font.getlength(ch) for ch in text) + tracking * (len(text) - 1) + 8
    image = Image.new("L", (int(width) + 1, ascent + descent + 4), 0)
    draw = ImageDraw.Draw(image)
    x = 4.0
    for ch in text:
        draw.text((x, 2), ch, font=font, fill=255)
        x += font.getlength(ch) + tracking
    del widths
    box = image.getbbox()
    image = image.crop((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2))
    alpha = np.asarray(image, dtype=np.uint8)
    stream = zlib.compress(alpha.tobytes(), 9)
    return BRAND_HEADER.pack(BRAND_MAGIC, image.width, image.height, len(stream)) + stream


def build_all(out_dir: pathlib.Path, font_path: pathlib.Path | None) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for companion in COMPANION_IDS:
        data = build_companion(companion)
        (out_dir / f"mascot_{companion}.mmp").write_bytes(data)
        sizes[f"mascot_{companion}.mmp"] = len(data)
    if font_path is not None:
        data = build_brand_mark(font_path)
        (out_dir / "brand_mark.mma").write_bytes(data)
        sizes["brand_mark.mma"] = len(data)
    return sizes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=pathlib.Path, default=OUT_DIR)
    parser.add_argument("--font", type=pathlib.Path, default=None,
                        help="TTF for the boot wordmark (default: LVGL's Montserrat-Medium)")
    args = parser.parse_args()
    font = args.font or next((path for path in BRAND_FONT_CANDIDATES if path.exists()), None)
    if font is None:
        print("warning: no wordmark font found; brand_mark.mma not rebuilt")
    sizes = build_all(args.out, font)
    for name, size in sizes.items():
        print(f"{name:24s} {size / 1024:8.1f} KiB")
    print(f"{'total':24s} {sum(sizes.values()) / 1024:8.1f} KiB")


if __name__ == "__main__":
    main()
