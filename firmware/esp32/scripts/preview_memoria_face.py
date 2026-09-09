#!/usr/bin/env python3
"""Render the Memoria device face with the exact firmware renderer.

The script compiles ``memoria_face.cc`` with the host compiler (the renderer has
no ESP-IDF or LVGL dependency on purpose), runs it, and writes PNG previews plus
the measured face geometry.  This is the design/verification path for the
conversation face that replaced the small colour emoji on the 360x360 round LCD.

Usage:
    uv run python firmware/esp32/scripts/preview_memoria_face.py --out .tmp-face/out
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import struct
import subprocess
import tempfile
import zlib

HERE = pathlib.Path(__file__).resolve()
BOARD_DIR = HERE.parents[1] / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
RENDERER = BOARD_DIR / "memoria_face.cc"
HEADER = BOARD_DIR / "memoria_face.h"

SCREEN = 360
# Blink is sampled over this many frames for the animated preview.
BLINK_FRAMES = 5

HARNESS = r"""
#include "memoria_face.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>

namespace {

constexpr int kWidth = 360;
constexpr int kHeight = 360;
uint16_t g_buffer[kWidth * kHeight];

bool WritePpm(const char* path) {
    FILE* file = fopen(path, "wb");
    if (file == nullptr) {
        return false;
    }
    fprintf(file, "P6\n%d %d\n255\n", kWidth, kHeight);
    for (int i = 0; i < kWidth * kHeight; ++i) {
        const uint16_t v = g_buffer[i];
        const uint8_t r = static_cast<uint8_t>((v >> 11) & 0x1F);
        const uint8_t g = static_cast<uint8_t>((v >> 5) & 0x3F);
        const uint8_t b = static_cast<uint8_t>(v & 0x1F);
        const uint8_t rgb[3] = {static_cast<uint8_t>((r << 3) | (r >> 2)),
                                static_cast<uint8_t>((g << 2) | (g >> 4)),
                                static_cast<uint8_t>((b << 3) | (b >> 2))};
        fwrite(rgb, 1, 3, file);
    }
    fclose(file);
    return true;
}

void RenderTo(const char* out_dir, const char* name, const memoria::Face& face) {
    memoria::RenderFaceRgb565(g_buffer, kWidth, kHeight, face);
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s.ppm", out_dir, name);
    if (!WritePpm(path)) {
        fprintf(stderr, "failed to write %s\n", path);
        exit(1);
    }
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out-dir>\n", argv[0]);
        return 2;
    }
    const char* out_dir = argv[1];
    for (size_t i = 0; i < memoria::kFaceEmotionCount; ++i) {
        const char* name = memoria::kFaceEmotions[i];
        RenderTo(out_dir, name, memoria::FaceForEmotion(name, 0.0f));
    }
    for (int frame = 0; frame < 5; ++frame) {
        const float blink = static_cast<float>(frame) / 4.0f;
        char name[64];
        snprintf(name, sizeof(name), "blink-%d", frame);
        RenderTo(out_dir, name, memoria::FaceForEmotion("surprised", blink));
    }
    // Geometry dump for the host test / design review.
    for (size_t i = 0; i < memoria::kFaceEmotionCount; ++i) {
        const char* name = memoria::kFaceEmotions[i];
        const memoria::Face face = memoria::FaceForEmotion(name, 0.0f);
        printf("emotion %s left=(%.4f,%.4f) right=(%.4f,%.4f) half_width=%.4f "
               "openness=%.3f almond=%d gaze=(%.3f,%.3f) rotation=%.2f scale=%.3f\n",
               name, face.left.center_x, face.left.center_y, face.right.center_x,
               face.right.center_y, face.left.half_width, face.left.openness,
               face.left.almond ? 1 : 0, face.left.gaze_x, face.left.gaze_y,
               face.left.rotation_deg, face.left.scale);
    }
    return 0;
}
"""


def _write_png(path: pathlib.Path, pixels: bytes, width: int, height: int) -> None:
    """Write a 24-bit RGB PNG with the standard library only."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        raw += pixels[row * stride : (row + 1) * stride]
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def _read_ppm(path: pathlib.Path) -> bytes:
    data = path.read_bytes()
    if not data.startswith(b"P6\n"):
        raise ValueError(f"{path} is not a binary PPM")
    parts = data.split(b"\n", 3)
    width, height = (int(value) for value in parts[1].split())
    pixels = parts[3]
    if len(pixels) != width * height * 3:
        raise ValueError(f"{path} has {len(pixels)} bytes, expected {width * height * 3}")
    return pixels


def _contact_sheet(images: list[tuple[str, bytes]], tile: int) -> bytes:
    columns = 3
    rows = (len(images) + columns - 1) // columns
    canvas = bytearray(b"\x10" * (columns * tile * rows * tile * 3))
    for index, (_name, pixels) in enumerate(images):
        ox = (index % columns) * tile
        oy = (index // columns) * tile
        for y in range(tile):
            src = y * tile * 3
            dst = ((oy + y) * columns * tile + ox) * 3
            canvas[dst : dst + tile * 3] = pixels[src : src + tile * 3]
    return bytes(canvas)


def _eye_boxes(pixels: bytes, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of the bright blobs, left to right."""
    seen = bytearray(width * height)
    boxes: list[tuple[int, int, int, int, int]] = []
    for y in range(height):
        for x in range(width):
            if seen[y * width + x] or pixels[(y * width + x) * 3] < 128:
                continue
            stack = [(y, x)]
            seen[y * width + x] = 1
            count = 0
            min_x = max_x = x
            min_y = max_y = y
            while stack:
                cy, cx = stack.pop()
                count += 1
                min_x, max_x = min(min_x, cx), max(max_x, cx)
                min_y, max_y = min(min_y, cy), max(max_y, cy)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < height and 0 <= nx < width and not seen[ny * width + nx]:
                        if pixels[(ny * width + nx) * 3] >= 128:
                            seen[ny * width + nx] = 1
                            stack.append((ny, nx))
            boxes.append((count, min_x, min_y, max_x, max_y))
    boxes.sort(key=lambda box: box[1])
    return [(b[1], b[2], b[3], b[4]) for b in boxes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path(".tmp-face/out"))
    parser.add_argument("--keep", action="store_true", help="keep the compiled harness")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        raise SystemExit("no host C++ compiler found (clang++ or g++)")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = pathlib.Path(tmp)
        harness = tmp_dir / "memoria_face_preview.cc"
        harness.write_text(HARNESS, encoding="utf-8")
        binary = tmp_dir / "memoria_face_preview"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{BOARD_DIR}",
                str(harness),
                str(RENDERER),
                "-o",
                str(binary),
            ],
            check=True,
        )
        result = subprocess.run(
            [str(binary), str(tmp_dir)], check=True, capture_output=True, text=True
        )
        print(result.stdout, end="")

        images: list[tuple[str, bytes]] = []
        for name in [
            "neutral",
            "happy",
            "sad",
            "surprised",
            "loving",
            "thinking",
            "embarrassed",
            "wink",
            "speaking",
            "blink-0",
            "blink-2",
            "blink-4",
        ]:
            pixels = _read_ppm(tmp_dir / f"{name}.ppm")
            _write_png(args.out / f"{name}.png", pixels, SCREEN, SCREEN)
            if not name.startswith("blink"):
                images.append((name, pixels))

        sheet = _contact_sheet(images, SCREEN)
        rows = (len(images) + 2) // 3
        _write_png(args.out / "sheet.png", sheet, 3 * SCREEN, rows * SCREEN)

        print("\nmeasured bright-blob boxes (px on the 360x360 panel):")
        for name, pixels in images:
            boxes = _eye_boxes(pixels, SCREEN, SCREEN)
            print(f"  {name}: {len(boxes)} blob(s)")
            for index, box in enumerate(boxes[:6]):
                print(
                    f"    [{index}] x[{box[0]},{box[2]}] y[{box[1]},{box[3]}] "
                    f"w={box[2] - box[0] + 1} h={box[3] - box[1] + 1}"
                )
    if args.keep:
        print(f"\npreviews written to {args.out}")
    else:
        print(f"\npreviews written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
