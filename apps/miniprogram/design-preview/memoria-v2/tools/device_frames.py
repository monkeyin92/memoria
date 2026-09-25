"""Device animation frames for the v2 mascots: blink, talk and greeting.

The device screen animates each companion from the eight mood frames in
``assets/companions/<id>/``.  Blinking and lip movement need extra frames that
match a mood frame exactly outside the eyes or mouth, otherwise the character
visibly jumps when the device swaps them.  The image model cannot guarantee
that, so this tool:

1. ``jobs``: writes a run.py job list that asks the model (via gen.py) to edit
   only the eyes (``<mood>-blink``) or only the mouth (``<mood>-talk``) of a
   mood frame, plus one freshly posed ``greeting`` frame per companion.
2. ``composite``: aligns each edit onto its source frame and copies back only
   the region the model actually changed inside the character, feathered, so
   every other pixel stays bit-identical to the source frame.
3. ``sheet``: renders a review contact sheet of the finished frames.

Usage (from this directory, IMG_KEY in the environment for run.py):

    python3 device_frames.py jobs WORK_DIR
    python3 run.py WORK_DIR/jobs.jsonl 10
    python3 device_frames.py composite WORK_DIR
    python3 device_frames.py sheet WORK_DIR/sheet.png
"""

from __future__ import annotations

import json
import pathlib
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = pathlib.Path(__file__).resolve().parent
COMPANIONS_DIR = HERE.parents[2] / "assets" / "companions"
COMPANION_IDS = ("axu", "mianmian", "starlight", "taoxi", "xuanmo")
# Moods whose eyes are open and therefore blink on the device.
BLINK_MOODS = ("default", "sad", "surprised", "thinking", "listening")
# Moods the device can show while speaking; each gets an alternate mouth.
TALK_MOODS = ("default", "happy", "sad", "surprised", "thinking")
# Frames whose mouth is already open: their talk frame closes it instead.
OPEN_MOUTH = {("taoxi", "default")} | {(c, m) for c in COMPANION_IDS for m in ("happy", "surprised")}
SIZE = 512
EDIT_SIZE = 1024

KEEP = (
    "Edit the reference image. Keep EVERYTHING else identical: the same character, the same pose, "
    "the same position and scale in the frame, paws, props, outfit, accessories, colors, lighting, "
    "felt/fur texture and transparent background. "
)


def _blink_prompt(companion: str) -> str:
    glasses = " (the glasses stay exactly as they are)" if companion == "axu" else ""
    mouth = "beak" if companion == "axu" else "mouth"
    return (
        KEEP
        + "Change ONLY the eyes: both eyes gently closed mid-blink, drawn as soft curved embroidered "
        + f"stitch lines at the same position and size as the current eyes{glasses}. "
        + f"The {mouth}, eyebrows and everything else do not change."
    )


def _talk_prompt(companion: str, mood: str) -> str:
    is_open = (companion, mood) in OPEN_MOUTH
    if companion == "axu":
        change = (
            "Change ONLY the beak: the beak slightly closed, a smaller calm beak."
            if is_open
            else "Change ONLY the beak: the beak opened slightly as if mid-word while speaking, "
            "showing a little dark inside."
        )
    elif is_open:
        change = (
            "Change ONLY the mouth: the mouth nearly closed mid-word, a small soft mouth about half "
            "the current opening, same expression mood."
        )
    else:
        change = (
            "Change ONLY the mouth: the mouth opened slightly as if mid-word while speaking, a small "
            "rounded opening with a little dark inside and a hint of tongue, same expression mood."
        )
    return KEEP + change + " The eyes and everything else do not change."


GREETING_PROMPT = (
    "Same exact character as the reference image: identical design, colors, materials, outfit, "
    "accessories and proportions. GREETING expression: waving hello with one paw raised high beside "
    "the head, bright open eyes, a warm friendly open smile, slight welcoming lean. Centered full body "
    "at the same scale and position as the reference, same soft studio lighting and plush render "
    "quality. Transparent background, isolated character, no floor, no text."
)


def cmd_jobs(work: pathlib.Path, only: set[str] | None = None) -> None:
    (work / "in").mkdir(parents=True, exist_ok=True)
    (work / "raw").mkdir(parents=True, exist_ok=True)
    jobs = []

    def source(companion: str, mood: str) -> str:
        path = work / "in" / f"{companion}_{mood}.png"
        if not path.exists():
            Image.open(COMPANIONS_DIR / companion / f"{mood}.png").resize(
                (EDIT_SIZE, EDIT_SIZE), Image.LANCZOS
            ).save(path)
        return str(path)

    def add(name: str, prompt: str, ref: str) -> None:
        if only is None or name in only:
            jobs.append([str(work / "raw" / f"{name}.png"), f"{EDIT_SIZE}x{EDIT_SIZE}", prompt, ref])

    for companion in COMPANION_IDS:
        for mood in BLINK_MOODS:
            add(f"{companion}_{mood}_blink", _blink_prompt(companion), source(companion, mood))
        for mood in TALK_MOODS:
            add(f"{companion}_{mood}_talk", _talk_prompt(companion, mood), source(companion, mood))
        add(f"{companion}_greeting", GREETING_PROMPT, source(companion, "default"))
    with open(work / "jobs.jsonl", "w", encoding="utf-8") as handle:
        for job in jobs:
            handle.write(json.dumps(job, ensure_ascii=False) + "\n")
    print(f"{len(jobs)} jobs -> {work / 'jobs.jsonl'}")


def _rgba(path: pathlib.Path, size: int = SIZE) -> np.ndarray:
    image = Image.open(path).convert("RGBA")
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    return np.asarray(image, dtype=np.float32)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _scaled(edit_full: Image.Image, scale: float) -> np.ndarray:
    """The edit scaled about the frame centre onto a SIZE x SIZE canvas."""
    side = round(SIZE * scale)
    scaled = edit_full.resize((side, side), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    offset = (SIZE - side) // 2
    canvas.paste(scaled, (offset, offset))
    return np.asarray(canvas, dtype=np.float32)


def _shift(image: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(image)
    height, width = image.shape[:2]
    ys, yd = (slice(0, height - dy), slice(dy, height)) if dy >= 0 else (slice(-dy, height), slice(0, height + dy))
    xs, xd = (slice(0, width - dx), slice(dx, width)) if dx >= 0 else (slice(-dx, width), slice(0, width + dx))
    out[yd, xd] = image[ys, xs]
    return out


def _erode(mask: np.ndarray, radius: int) -> np.ndarray:
    image = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(image.filter(ImageFilter.MinFilter(radius * 2 + 1))) > 127


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    image = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(image.filter(ImageFilter.MaxFilter(radius * 2 + 1))) > 127


def _blur(values: np.ndarray, radius: float) -> np.ndarray:
    image = Image.fromarray(np.clip(values, 0, 255).astype(np.uint8))
    return np.asarray(image.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """4-connected components, largest first (small images, plain BFS)."""
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    parts = []
    for y0, x0 in zip(*np.nonzero(mask), strict=False):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pixels = []
        while stack:
            y, x = stack.pop()
            pixels.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        part = np.zeros_like(mask, dtype=bool)
        ys, xs = zip(*pixels, strict=False)
        part[list(ys), list(xs)] = True
        parts.append(part)
    parts.sort(key=lambda item: -int(item.sum()))
    return parts


def composite_one(source_path: pathlib.Path, edit_path: pathlib.Path) -> tuple[Image.Image, dict]:
    source = _rgba(source_path)
    edit_full = Image.open(edit_path).convert("RGBA")
    # Only the character's opaque interior is comparable: several edits come
    # back with a baked checkerboard or black backdrop instead of alpha.
    interior = _erode(source[..., 3] > 250, 3)
    source_luma = _luma(source[..., :3])

    def score(candidate: np.ndarray) -> float:
        return float(np.abs(_luma(candidate[..., :3]) - source_luma)[interior].mean())

    best = (1e9, 1.0, 0, 0)
    scaled = {scale: _scaled(edit_full, scale) for scale in (0.98, 0.99, 1.0, 1.01, 1.02)}
    for scale, candidate in scaled.items():
        for dy in range(-8, 9, 2):
            for dx in range(-8, 9, 2):
                value = score(_shift(candidate, dx, dy))
                if value < best[0]:
                    best = (value, scale, dx, dy)
    _, scale, cx, cy = best
    for dy in (cy - 1, cy, cy + 1):
        for dx in (cx - 1, cx, cx + 1):
            value = score(_shift(scaled[scale], dx, dy))
            if value < best[0]:
                best = (value, scale, dx, dy)
    _, scale, dx, dy = best
    edit = _shift(scaled[scale], dx, dy)

    diff = np.abs(_luma(edit[..., :3]) - source_luma) + 0.5 * np.abs(
        edit[..., :3] - source[..., :3]
    ).mean(axis=2)
    diff = np.where(interior, diff, 0.0)
    changed = _blur(diff, 2.0) > 22.0
    # Opening drops the thin residue of fur texture and 1 px misalignment.
    changed = _dilate(_erode(changed, 2), 2)
    parts = [part for part in _components(changed) if part.sum() >= 60][:4]
    region = np.zeros_like(changed)
    for part in parts:
        region |= part
    mask = _blur(_dilate(region, 5).astype(np.float32) * 255.0, 3.0) / 255.0
    mask = np.where(interior | _dilate(region, 5), mask, 0.0)[..., None]

    out = source.copy()
    out[..., :3] = source[..., :3] * (1.0 - mask) + edit[..., :3] * mask
    residual = float(np.abs(_luma(edit[..., :3]) - source_luma)[interior & (mask[..., 0] < 0.05)].mean())
    stats = {
        "scale": scale,
        "shift": (dx, dy),
        "changed_px": int(region.sum()),
        "regions": len(parts),
        "residual": round(residual, 2),
    }
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA"), stats


def cmd_composite(work: pathlib.Path, names: list[str] | None = None) -> None:
    raw = work / "raw"
    report = {}
    for companion in COMPANION_IDS:
        target_dir = COMPANIONS_DIR / companion
        jobs = [(mood, "blink") for mood in BLINK_MOODS] + [(mood, "talk") for mood in TALK_MOODS]
        for mood, kind in jobs:
            name = f"{companion}_{mood}_{kind}"
            if names and name not in names:
                continue
            edit_path = raw / f"{name}.png"
            if not edit_path.exists():
                print("missing", name)
                continue
            image, stats = composite_one(target_dir / f"{mood}.png", edit_path)
            image.save(target_dir / f"{mood}-{kind}.png", optimize=True)
            report[name] = stats
            print(name, stats)
        name = f"{companion}_greeting"
        if (not names or name in names) and (raw / f"{name}.png").exists():
            greeting = Image.open(raw / f"{name}.png").convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
            greeting.save(target_dir / "greeting.png", optimize=True)
            print(name, "saved")
    (work / "composite-report.json").write_text(json.dumps(report, indent=2))


def cmd_sheet(out: pathlib.Path) -> None:
    columns = ["default", "default-blink", "default-talk", "happy", "happy-talk", "sad", "sad-blink",
               "sad-talk", "surprised", "surprised-blink", "surprised-talk", "thinking",
               "thinking-blink", "thinking-talk", "listening", "listening-blink", "greeting"]
    cell = 150
    sheet = Image.new("RGB", (cell * len(columns), cell * len(COMPANION_IDS)), (238, 230, 218))
    for row, companion in enumerate(COMPANION_IDS):
        for column, frame in enumerate(columns):
            path = COMPANIONS_DIR / companion / f"{frame}.png"
            if not path.exists():
                continue
            image = Image.open(path).convert("RGBA").resize((cell, cell), Image.LANCZOS)
            sheet.paste(image, (column * cell, row * cell), image)
    sheet.save(out)
    print(out)


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] not in {"jobs", "composite", "sheet"}:
        raise SystemExit(__doc__)
    target = pathlib.Path(argv[1])
    rest = argv[2:] or None
    if argv[0] == "jobs":
        cmd_jobs(target, set(rest) if rest else None)
    elif argv[0] == "composite":
        cmd_composite(target, rest)
    else:
        cmd_sheet(target)


if __name__ == "__main__":
    main(sys.argv[1:])
