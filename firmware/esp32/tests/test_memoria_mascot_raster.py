"""Host-side checks for the mascot's sub-pixel sprite sampler (TODOLIST M-6).

`memoria_mascot_raster.cc` blends a sprite into a screen row at a fractional position and scale. It has no
ESP-IDF dependency, so the exact firmware code is compiled on the host (also under AddressSanitizer and
UndefinedBehaviorSanitizer, since it is fixed-point and pointer arithmetic) and compared with a plain
floating-point model of what it is meant to do.

Why it exists: the idle breathing is about 3 px over 3.6 s. Rounded to whole pixels it moved in a dozen
one-pixel jumps per cycle and was point-sampled; sampled at the exact position, a 0.2 px move per frame
changes the edge pixels' coverage instead of nothing at all.
"""

from __future__ import annotations

import math
import pathlib
import shutil
import subprocess
import tempfile

import pytest

np = pytest.importorskip("numpy")

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"

HARNESS = r"""
#include "memoria_mascot_raster.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr int kRowMax = 512;

memoria::SpriteTransform ReadTransform(std::istringstream& in, int* cx, int* fy) {
    memoria::SpriteTransform t;
    in >> *cx >> *fy >> t.ax_q >> t.ay_q >> t.sx_q >> t.sy_q;
    return t;
}

}  // namespace

int main() {
    std::vector<uint16_t> rgb;
    std::vector<uint8_t> alpha;
    std::vector<uint16_t> span;
    memoria::MascotSprite s;
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "sprite") {
            int x = 0, y = 0, w = 0, h = 0;
            in >> x >> y >> w >> h;
            rgb.assign(static_cast<size_t>(w) * h, 0);
            alpha.assign(static_cast<size_t>(w) * h, 0);
            span.assign(static_cast<size_t>(h) * 2, 0);
            std::string colours, alphas;
            std::getline(std::cin, colours);
            std::getline(std::cin, alphas);
            std::istringstream c(colours), a(alphas);
            for (size_t i = 0; i < rgb.size(); ++i) {
                unsigned hex = 0, op = 0;
                c >> std::hex >> hex;
                a >> std::dec >> op;
                rgb[i] = static_cast<uint16_t>(hex);
                alpha[i] = static_cast<uint8_t>(op);
            }
            for (int row = 0; row < h; ++row) {
                int first = 0, last = 0;
                bool seen = false;
                for (int col = 0; col < w; ++col) {
                    if (alpha[static_cast<size_t>(row) * w + col] > 0) {
                        if (!seen) first = col;
                        last = col + 1;
                        seen = true;
                    }
                }
                span[static_cast<size_t>(row) * 2] = static_cast<uint16_t>(first);
                span[static_cast<size_t>(row) * 2 + 1] = static_cast<uint16_t>(last);
            }
            s = memoria::MascotSprite();
            s.x = x;
            s.y = y;
            s.w = w;
            s.h = h;
            s.rgb = rgb.data();
            s.alpha = alpha.data();
            s.span = span.data();
        } else if (cmd == "render") {
            // render <cx> <fy> <ax_q> <ay_q> <sx_q> <sy_q> <y0> <y1> <x0> <x1> <bg hex> <print lo> <print hi>
            int cx = 0, fy = 0;
            const memoria::SpriteTransform t = ReadTransform(in, &cx, &fy);
            int y0 = 0, y1 = 0, x0 = 0, x1 = 0, lo = 0, hi = 0;
            unsigned bg = 0;
            in >> y0 >> y1 >> x0 >> x1 >> std::hex >> bg >> std::dec >> lo >> hi;
            for (int y = y0; y < y1; ++y) {
                uint16_t row[kRowMax];
                for (int i = 0; i < kRowMax; ++i) row[i] = static_cast<uint16_t>(bg);
                memoria::BlendSpriteRow(s, cx, fy, t, y, x0, x1, row);
                for (int x = lo; x < hi; ++x) printf("%s%04x", x == lo ? "" : " ", row[x]);
                printf("\n");
            }
        } else if (cmd == "bounds") {
            int cx = 0, fy = 0;
            const memoria::SpriteTransform t = ReadTransform(in, &cx, &fy);
            const memoria::SceneRect box = memoria::SpriteBounds(s, cx, fy, t);
            printf("%d %d %d %d\n", box.x0, box.y0, box.x1, box.y1);
        } else if (cmd == "extent") {
            // The box of the 360 x 360 screen that rendering the sprite changed, and how many pixels.
            int cx = 0, fy = 0;
            const memoria::SpriteTransform t = ReadTransform(in, &cx, &fy);
            int x0 = 1 << 30, y0 = 1 << 30, x1 = -1, y1 = -1;
            long changed = 0;
            for (int y = 0; y < 360; ++y) {
                uint16_t row[kRowMax];
                for (int i = 0; i < kRowMax; ++i) row[i] = 0x1234;
                memoria::BlendSpriteRow(s, cx, fy, t, y, 0, 360, row);
                for (int x = 0; x < kRowMax; ++x) {
                    if (row[x] == 0x1234) continue;
                    if (x >= 360) {
                        printf("wrote outside the clip at %d\n", x);
                        return 3;
                    }
                    ++changed;
                    if (x < x0) x0 = x;
                    if (x > x1) x1 = x;
                    if (y < y0) y0 = y;
                    if (y > y1) y1 = y;
                }
            }
            printf("%d %d %d %d %ld\n", x0, y0, x1 + 1, y1 + 1, changed);
        } else if (cmd == "clipped") {
            // clipped <cx> <fy> <ax_q> <ay_q> <sx_q> <sy_q> <y> <x0> <x1>: did anything outside [x0, x1) change?
            int cx = 0, fy = 0;
            const memoria::SpriteTransform t = ReadTransform(in, &cx, &fy);
            int y = 0, x0 = 0, x1 = 0;
            in >> y >> x0 >> x1;
            uint16_t row[kRowMax];
            for (int i = 0; i < kRowMax; ++i) row[i] = 0x1234;
            memoria::BlendSpriteRow(s, cx, fy, t, y, x0, x1, row);
            int outside = 0, inside = 0;
            for (int i = 0; i < kRowMax; ++i) {
                if (row[i] == 0x1234) continue;
                (i >= x0 && i < x1 ? inside : outside)++;
            }
            printf("%d %d\n", inside, outside);
        } else if (cmd == "guard") {
            // guard <kind>: calls the sampler cannot honour must leave the row alone and not crash.
            std::string kind;
            in >> kind;
            memoria::MascotSprite bad = s;
            memoria::SpriteTransform t;
            t.ax_q = 100 * 16;
            t.ay_q = 100 * 16;
            int x0 = 0, x1 = 200;
            uint16_t* target = nullptr;
            uint16_t row[kRowMax];
            for (int i = 0; i < kRowMax; ++i) row[i] = 0x4321;
            target = row;
            if (kind == "nullrgb") bad.rgb = nullptr;
            else if (kind == "nullalpha") bad.alpha = nullptr;
            else if (kind == "nullspan") bad.span = nullptr;
            else if (kind == "wide") bad.w = 513;
            else if (kind == "empty") bad.h = 0;
            else if (kind == "zeroscale") t.sx_q = 0;
            else if (kind == "negscale") t.sy_q = -4096;
            else if (kind == "hugescale") t.sx_q = 1 << 28;
            else if (kind == "tinyscale") t.sy_q = 1;
            else if (kind == "emptyrange") { x0 = 150; x1 = 150; }
            else if (kind == "reversedrange") { x0 = 180; x1 = 20; }
            else if (kind == "nullrow") target = nullptr;
            else return 2;
            memoria::BlendSpriteRow(bad, 14, 21, t, 100, x0, x1, target);
            int changed = 0;
            for (int i = 0; i < kRowMax; ++i) changed += row[i] != 0x4321;
            printf("%d\n", changed);
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


def _compile(sanitize: bool) -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="memoria-raster-"))
    source = tmp / "harness.cc"
    source.write_text(HARNESS, encoding="utf-8")
    binary = tmp / ("raster_tool_san" if sanitize else "raster_tool")
    flags = (
        ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined", "-fno-omit-frame-pointer", "-g"]
        if sanitize
        else []
    )
    probe = subprocess.run(
        [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", *flags, f"-I{BOARD_DIR}", str(source),
         str(BOARD_DIR / "memoria_mascot_raster.cc"), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        if sanitize:
            pytest.skip(f"host compiler cannot build with sanitizers: {probe.stderr[-200:]}")
        raise AssertionError(probe.stderr)
    return binary


@pytest.fixture(scope="module", params=["plain", "sanitized"])
def tool(request):
    binary = _compile(request.param == "sanitized")
    try:
        yield binary
    finally:
        shutil.rmtree(binary.parent, ignore_errors=True)


# --- a test sprite and a floating-point model of the sampler --------------------------------------------


POISON = 0xF81F  # what sits under a transparent texel: none of it may ever reach the screen


def _sprite(w: int = 30, h: int = 24, seed: int = 1, hard: bool = False):
    """A plush body: gradients inside, a transparent hole and a half-transparent column (the awkward cases
    for an interpolating sampler), and magenta under every transparent texel, so a sampler that lets a
    transparent neighbour's colour tint the rim shows it. Soft: an alpha ramp about 4 px wide at the rim.
    Hard: fully opaque out to the edge of the sprite's box, the way a tightly cropped pack sprite is."""
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = (w - 1) / 2, (h - 1) / 2
    distance = np.sqrt(((xx - cx) / (w / 2 - 1)) ** 2 + ((yy - cy) / (h / 2 - 1)) ** 2)
    alpha = np.clip((1.0 - distance) * (w / 2) * 64, 0, 255).astype(np.int64)
    if hard:
        alpha = np.full((h, w), 255, dtype=np.int64)
    alpha[8:10, 10:12] = 0
    alpha[:, 20] = np.where(alpha[:, 20] > 0, 128, 0)
    red = np.clip(40 + 5 * xx + rng.randint(-2, 3, (h, w)), 0, 255)
    green = np.clip(200 - 6 * yy + rng.randint(-2, 3, (h, w)), 0, 255)
    blue = np.clip(90 + 3 * (xx + yy) + rng.randint(-2, 3, (h, w)), 0, 255)
    rgb565 = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
    return np.where(alpha > 0, rgb565, POISON).astype(np.int64), alpha


def _block(w: int = 36, h: int = 28, seed: int = 5):
    """An opaque rectangle of random colours: every edge texel is fully visible, the worst case for how far
    the interpolating filter reaches past the sprite's box."""
    rng = np.random.RandomState(seed)
    return rng.randint(0, 65536, (h, w)).astype(np.int64), np.full((h, w), 255, dtype=np.int64)


def _kind(kind: str, w: int = 30, h: int = 24):
    """The sprites the model comparisons run on: soft-edged, hard-edged, and the random block whose
    neighbouring texels differ wildly (nearest-texel sampling instead of interpolation cannot hide there)."""
    if kind == "soft":
        return _sprite(w, h)
    if kind == "hard":
        return _sprite(w, h, hard=True)
    return _block(w, h)


def _stripes(w: int = 60, h: int = 48):
    """One-texel black and white columns: the finest detail a sprite can carry."""
    columns = np.tile(np.arange(w), (h, 1))
    return np.where(columns % 2 == 0, 0xFFFF, 0x0000).astype(np.int64), np.full((h, w), 255, dtype=np.int64)


def _send(binary: pathlib.Path, rgb565, alpha, x: int, y: int, *commands: str) -> list[str]:
    h, w = rgb565.shape
    lines = [
        f"sprite {x} {y} {w} {h}",
        " ".join(f"{int(c):04x}" for c in rgb565.ravel()),
        " ".join(str(int(a)) for a in alpha.ravel()),
        *commands,
    ]
    completed = subprocess.run(
        [str(binary)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True,
        env={"ASAN_OPTIONS": "detect_leaks=0", "UBSAN_OPTIONS": "print_stacktrace=1"},
    )
    return completed.stdout.splitlines()


def _rows(output: list[str]) -> np.ndarray:
    return np.array([[int(t, 16) for t in line.split()] for line in output], dtype=np.int64)


def _expand(c: np.ndarray) -> np.ndarray:
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1).astype(np.float64)


def _blend565(fg: np.ndarray, bg: int, alpha: np.ndarray) -> np.ndarray:
    """Blend565 from memoria_mascot_raster.h, vectorised (uint32 wrap-around included)."""
    mask = np.uint64(0x07E0F81F)
    wrap = np.uint64(0xFFFFFFFF)
    a = ((alpha + 4) >> 3).astype(np.uint64)
    f = ((fg.astype(np.uint64) | (fg.astype(np.uint64) << np.uint64(16))) & mask)
    b = np.uint64((bg | (bg << 16)) & 0x07E0F81F)
    r = ((((f - b) & wrap) * a >> np.uint64(5)) + b) & mask
    out = ((r | (r >> np.uint64(16))) & np.uint64(0xFFFF)).astype(np.int64)
    out = np.where(alpha >= 250, fg, out)
    return np.where(alpha < 4, bg, out)


def _scale_q(scale: float) -> int:
    return round(scale * 4096)


def _render(binary, rgb565, alpha, *, sprite_xy=(60, 40), cx=75, fy=64, ax=100.0, ay=100.0, sx=1.0, sy=1.0,
            y0=60, y1=140, x0=40, x1=170, bg=0x7BEF):
    """Rows y0..y1 over columns x0..x1 as RGB565, through the firmware sampler."""
    ax_q, ay_q = round(ax * 16), round(ay * 16)
    out = _send(
        binary, rgb565, alpha, *sprite_xy,
        f"render {cx} {fy} {ax_q} {ay_q} {_scale_q(sx)} {_scale_q(sy)} {y0} {y1} {x0} {x1} {bg:04x} {x0 - 6} {x1 + 6}",
    )
    return _rows(out)


def _model(rgb565, alpha, *, sprite_xy=(60, 40), cx=75, fy=64, ax=100.0, ay=100.0, sx=1.0, sy=1.0,
           y0=60, y1=140, x0=40, x1=170, bg=0x7BEF):
    """What the sampler is meant to do, in floating point: bilinear at pixel centres, colour weighted by
    coverage, blended over the background. Returns 8-bit RGB for columns x0-6..x1+6."""
    h, w = rgb565.shape
    ax_q, ay_q = round(ax * 16), round(ay * 16)
    scale_x, scale_y = _scale_q(sx) / 4096, _scale_q(sy) / 4096
    colours = _expand(rgb565)
    background = _expand(np.array([bg]))[0]
    columns = np.arange(x0 - 6, x1 + 6)
    result = np.empty((y1 - y0, len(columns), 3))
    for r, y in enumerate(range(y0, y1)):
        v = ((y + 0.5) - ay_q / 16) / scale_y + (fy - sprite_xy[1]) - 0.5
        u = ((columns + 0.5) - ax_q / 16) / scale_x + (cx - sprite_xy[0]) - 0.5
        j0, i0 = math.floor(v), np.floor(u).astype(int)
        wy = math.floor((v - j0) * 32) / 32
        wx = np.floor((u - i0) * 32) / 32
        total = np.zeros(len(columns))
        mixed = np.zeros((len(columns), 3))
        for dj, wj in ((0, 1 - wy), (1, wy)):
            for di in (0, 1):
                weight = wj * ((1 - wx) if di == 0 else wx)
                jj, ii = j0 + dj, i0 + di
                ok = (jj >= 0) & (jj < h) & (ii >= 0) & (ii < w)
                a = np.where(ok, alpha[np.clip(jj, 0, h - 1), np.clip(ii, 0, w - 1)], 0) * weight
                c = colours[np.clip(jj, 0, h - 1), np.clip(ii, 0, w - 1)]
                total += a
                mixed += a[:, None] * c
        coverage = total / 255
        colour = np.where(total[:, None] > 0, mixed / np.maximum(total, 1e-9)[:, None], background)
        result[r] = background + (colour - background) * coverage[:, None]
    return result


def _area_average(rgb565, alpha, *, sprite_xy=(60, 40), cx=90, fy=87, ax=100.0, ay=100.0, sx=1.0, sy=1.0,
                  y0=60, y1=140, x0=40, x1=170, bg=0x7BEF, grid=8):
    """The ideal shrink: each screen pixel is the coverage-weighted mean of the source under its whole
    footprint (a grid x grid brute-force box filter). Same columns as `_model`."""
    h, w = rgb565.shape
    colours = _expand(rgb565)
    background = _expand(np.array([bg]))[0]
    scale_x, scale_y = _scale_q(sx) / 4096, _scale_q(sy) / 4096
    ys = np.arange(y0, y1)[:, None]
    xs = np.arange(x0 - 6, x1 + 6)[None, :]
    total = np.zeros((y1 - y0, x1 - x0 + 12))
    mixed = np.zeros(total.shape + (3,))
    for dy in (np.arange(grid) + 0.5) / grid:
        for dx in (np.arange(grid) + 0.5) / grid:
            u = ((xs + dx) - round(ax * 16) / 16) / scale_x + (cx - sprite_xy[0])
            v = ((ys + dy) - round(ay * 16) / 16) / scale_y + (fy - sprite_xy[1])
            i, j = np.floor(u).astype(int), np.floor(v).astype(int)
            inside = (i >= 0) & (i < w) & (j >= 0) & (j < h)
            ii, jj = np.clip(i, 0, w - 1), np.clip(j, 0, h - 1)
            a = np.where(inside, alpha[jj, ii], 0)
            total += a
            mixed += a[..., None] * colours[jj, ii]
    coverage = total / (255 * grid * grid)
    colour = np.where(total[..., None] > 0, mixed / np.maximum(total, 1e-9)[..., None], background)
    return background + (colour - background) * coverage[..., None]


# --- tests ----------------------------------------------------------------------------------------------


def test_a_whole_pixel_position_at_scale_one_is_the_plain_blit_exactly(tool) -> None:
    rgb565, alpha = _sprite()
    got = _render(tool, rgb565, alpha)  # the anchor (feet centre) lands on pixel (100, 100)
    expected = np.full_like(got, 0x7BEF)
    sprite_x, sprite_y, cx, fy = 60, 40, 75, 64
    for j in range(rgb565.shape[0]):
        y = 100 + (sprite_y + j - fy)
        if not 60 <= y < 140:
            continue
        for i in range(rgb565.shape[1]):
            x = 100 + (sprite_x + i - cx)
            if 34 <= x < 176:
                expected[y - 60, x - 34] = _blend565(
                    np.array([rgb565[j, i]]), 0x7BEF, np.array([alpha[j, i]])
                )[0]
    # Columns outside [40, 170) are the sampler's clip: they must still be background.
    expected[:, :6] = 0x7BEF
    expected[:, -6:] = 0x7BEF
    assert np.array_equal(got, expected)


@pytest.mark.parametrize("kind", ["soft", "hard", "block"])
def test_a_fractional_position_matches_the_floating_point_model(tool, kind: str) -> None:
    rgb565, alpha = _kind(kind)
    for ax, ay in ((100.5, 100.0), (100.0, 100.5), (100.3125, 99.6875), (99.75, 100.25)):
        got = _expand(_render(tool, rgb565, alpha, ax=ax, ay=ay))
        want = _model(rgb565, alpha, ax=ax, ay=ay)
        error = np.abs(got - want)
        assert error.mean() < 1.0, (ax, ay, error.mean())
        assert error.max() <= 24, (ax, ay, error.max())


@pytest.mark.parametrize("kind", ["soft", "hard", "block"])
def test_a_breathing_scale_matches_the_model_in_both_directions(tool, kind: str) -> None:
    rgb565, alpha = _kind(kind)
    for sx, sy in ((1.0, 1.04), (0.985, 1.035), (1.06, 1.0), (1.2, 1.2)):
        got = _expand(_render(tool, rgb565, alpha, ax=100.25, ay=100.125, sx=sx, sy=sy))
        want = _model(rgb565, alpha, ax=100.25, ay=100.125, sx=sx, sy=sy)
        error = np.abs(got - want)
        assert error.mean() < 1.2, (sx, sy, error.mean())
        assert error.max() <= 40, (sx, sy, error.max())


def test_a_clear_shrink_is_supersampled_close_to_the_area_average(tool) -> None:
    # The captioned layout draws the mascot at 0.64: one sample per pixel would crawl; four are averaged.
    rgb565, alpha = _sprite(60, 48)
    where = dict(cx=90, fy=87, ax=120.3, ay=100.2, sx=0.64, sy=0.64, y0=60, y1=120, x0=80, x1=170)
    error = np.abs(_expand(_render(tool, rgb565, alpha, **where)) - _area_average(rgb565, alpha, **where))
    assert error.mean() < 3.0, error.mean()
    assert np.percentile(error, 99) <= 40, np.percentile(error, 99)


def test_supersampling_beats_a_single_sample_on_fine_detail_when_shrinking(tool) -> None:
    # One-texel stripes shrunk well below 1 are what a single bilinear sample per pixel aliases worst (the
    # soft plush art hides the difference at the scene's own 0.64). The sampler only supersamples when it
    # shrinks in both directions, so shrinking in x alone (sy 0.86) shows what the plain path does with the
    # very same sprite and position.
    rgb565, alpha = _stripes(120, 96)
    for ax, ay in ((120.3, 100.2), (121.0, 100.7), (119.6, 99.4)):
        where = dict(cx=120, fy=135, ax=ax, ay=ay, y0=50, y1=120, x0=80, x1=170)
        supersampled = dict(sx=0.45, sy=0.45, **where)
        interpolated = dict(sx=0.45, sy=0.86, **where)
        error_supersampled = np.abs(
            _expand(_render(tool, rgb565, alpha, **supersampled)) - _area_average(rgb565, alpha, **supersampled)
        ).mean()
        error_interpolated = np.abs(
            _expand(_render(tool, rgb565, alpha, **interpolated)) - _area_average(rgb565, alpha, **interpolated)
        ).mean()
        assert error_supersampled < 14, (ax, ay, error_supersampled)
        assert error_supersampled < error_interpolated * 0.6, (ax, ay, error_supersampled, error_interpolated)


def test_sixteenth_pixel_steps_change_the_picture_gently_where_whole_pixels_jump(tool) -> None:
    rgb565, alpha = _sprite()
    steps = [_expand(_render(tool, rgb565, alpha, ax=100 + k / 16)) for k in range(17)]
    gentle = max(np.abs(a - b).max() for a, b in zip(steps, steps[1:], strict=False))
    jump = np.abs(steps[16] - steps[0]).max()
    assert jump >= 40, "the test sprite must have a sharp enough edge to tell"
    assert gentle <= 16, gentle
    assert gentle * 3 < jump
    # And the 16th sixteenth is the next whole pixel, exactly: sub-pixel placement has no seam at the integers.
    shifted = _render(tool, rgb565, alpha, ax=101.0)
    assert np.array_equal(shifted, _render(tool, rgb565, alpha, ax=101.0))
    base = _render(tool, rgb565, alpha, ax=100.0)
    inner = slice(8, -8)
    assert np.array_equal(shifted[:, inner][:, 1:], base[:, inner][:, :-1])


@pytest.mark.parametrize("kind", ["soft", "block"])
def test_every_pixel_the_sampler_touches_is_inside_the_reported_bounds(tool, kind: str) -> None:
    # The block's edge texels are fully opaque, so when it is enlarged the interpolating filter really does
    # reach past the sprite's box: that is what the margin in SpriteBounds is for.
    rgb565, alpha = _sprite(40, 32, seed=4) if kind == "soft" else _block(40, 32)
    rng = np.random.RandomState(7)
    commands, scales = [], []
    for _ in range(160):
        ax, ay = rng.uniform(40, 320), rng.uniform(40, 320)
        sx, sy = rng.uniform(0.55, 1.5), rng.uniform(0.55, 1.5)
        args = f"{70 + 20} {64 + 32} {round(ax * 16)} {round(ay * 16)} {_scale_q(sx)} {_scale_q(sy)}"
        commands += [f"bounds {args}", f"extent {args}"]
        scales.append((sx, sy))
    out = _send(tool, rgb565, alpha, 70, 64, *commands)
    for index, (sx, sy) in enumerate(scales):
        bx0, by0, bx1, by1 = (int(v) for v in out[index * 2].split())
        ex0, ey0, ex1, ey1, changed = (int(v) for v in out[index * 2 + 1].split())
        assert changed > 0
        assert bx0 <= ex0 and by0 <= ey0 and bx1 >= ex1 and by1 >= ey1, (index, out[index * 2], out[index * 2 + 1])
        # ...and not wastefully larger than the sprite, its scale and the filter's reach.
        assert (bx1 - bx0) <= 40 * sx + 8 and (by1 - by0) <= 32 * sy + 8


def test_nothing_is_written_outside_the_requested_columns(tool) -> None:
    rgb565, alpha = _sprite()
    out = _send(
        tool, rgb565, alpha, 60, 40,
        *(f"clipped 75 64 {round(ax * 16)} 1600 4096 4096 {y} 90 105" for ax in (100.0, 100.4, 97.2) for y in range(84, 112, 3)),
    )
    results = [tuple(int(v) for v in line.split()) for line in out]
    assert all(outside == 0 for _, outside in results)
    assert any(inside > 0 for inside, _ in results)


@pytest.mark.parametrize(
    "kind",
    ["nullrgb", "nullalpha", "nullspan", "wide", "empty", "zeroscale", "negscale", "hugescale", "tinyscale",
     "emptyrange", "reversedrange", "nullrow"],
)
def test_calls_the_sampler_cannot_honour_leave_the_row_alone(tool, kind: str) -> None:
    rgb565, alpha = _sprite()
    assert _send(tool, rgb565, alpha, 60, 40, f"guard {kind}") == ["0"]


def test_extreme_placements_stay_inside_the_sprites_arithmetic(tool) -> None:
    # Anchors and scales far outside what the scene produces: the sanitized build aborts on any overflow
    # or out-of-bounds read, the plain one must still agree that the sampler writes only inside its clip.
    rgb565, alpha = _sprite(64, 48, seed=9)
    rng = np.random.RandomState(3)
    commands = []
    for _ in range(120):
        ax = round(rng.uniform(-2000, 2400) * 16)
        ay = round(rng.uniform(-2000, 2400) * 16)
        sx = int(rng.choice([512, 700, 2621, 3487, 3488, 4096, 4300, 6000, 32768, 20000]))
        sy = int(rng.choice([512, 900, 2621, 3487, 3488, 4096, 4400, 7000, 32768, 31000]))
        commands.append(f"extent 90 87 {ax} {ay} {sx} {sy}")
        commands.append(f"bounds 90 87 {ax} {ay} {sx} {sy}")
    out = _send(tool, rgb565, alpha, 60, 40, *commands)
    assert len(out) == 240
    assert not any("outside" in line for line in out)
