#!/usr/bin/env python3
"""Guard the memoria_identity partition.

A probe reports whether the NVS namespace "device" is present. It does not
decode or print entry payloads. The flash verdict fails only when that
namespace was present before the write and absent afterwards. A board with
no namespace prints a warning and exits 0.

Flash refuses a full-chip image unless --allow-danger-full-erase is set.
Erase may name the default nvs partition or one namespace. Whole-chip erase,
nvs_flash_erase() with no partition name, and any erase of this partition
are rejected. esp_partition_erase_range and esp_flash_erase_region are
rejected unless the file and function are listed in ERASE_RANGE_ALLOWLIST.
esp_ota_* is a different API and is not on that list. A future factory-reset
button uses the same erase rules.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path

PAGE_SIZE = 4096
ENTRY_DATA_OFFSET = 64
ENTRY_SIZE = 32
ENTRIES_PER_PAGE = 126
BITMAP_OFFSET = 32
BITMAP_SIZE = 32
PARTITION_OFFSET = 0x10000
PARTITION_SIZE = 0x10000
NAMESPACE = "device"
NAMESPACE_KEY = b"device" + b"\x00" * 10
ENTRY_WRITTEN = 0b10
TYPE_U8 = 0x01
NS_INDEX = 0
DANGER_MARKER = "DANGER_FULL_ERASE"
STATUS_PRESENT = "device namespace: present"
STATUS_ABSENT = "device namespace: absent"
ALLOWED_PARTITION_ARGUMENTS = ('"nvs"', "'nvs'", "NVS_DEFAULT_PART_NAME")
SCAN_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hh", ".hpp", ".py", ".sh", ".cmake", ".patch"}
EXCLUDED_SCAN_NAMES = {"identity_guard.py"}

WHOLE_FLASH = r"\berase[-_](?:flash|all)\b"
UNNAMED_NVS = r"\bnvs_flash_erase\s*\("
PARTITION_ERASE = r"\bnvs_flash_erase_partition\s*\(\s*([^)]*)\)"
ERASE_REGION = r"\berase[-_]region\b(?:\s+(0x[0-9a-fA-F]+|\d+)\s+(0x[0-9a-fA-F]+|\d+))?"
IDENTITY_TOKEN = r"\b(?:memoria_identity|kPartitionLabel)\b"
OPEN_IDENTITY = (
    r"nvs_open(?:_from_partition)?\s*\([^;\n]*(?:memoria_identity|kPartitionLabel)"
)
NAMESPACE_ERASE = r"\bnvs_erase_(?:all|key)\s*\("
PARTITION_RANGE = r"\besp_partition_erase_range\s*\("
FLASH_REGION = r"\besp_flash_erase_region\s*\("
LOST_IDENTITY = (
    "error: memoria_identity namespace device was present before flash "
    "and is absent after flash"
)
UNPROVISIONED_WARNING = (
    "WARNING: memoria_identity 尚未写入设备身份，需运行 provision_identity.py"
)
_FUNCTION_HEAD = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$"
)
_NOT_A_FUNCTION = {"if", "for", "while", "switch", "catch"}


@dataclass(frozen=True)
class EraseRangeAllowance:
    """One function that may call a ranged erase API.

    path is a suffix of the scanned file. function is the C function that
    contains the call. reason names the partition that call erases.
    """

    path: str
    function: str
    reason: str


# No overlay or script calls esp_partition_erase_range or esp_flash_erase_region.
# esp_ota_* is omitted on purpose. Those calls are not the two APIs above.
ERASE_RANGE_ALLOWLIST: tuple[EraseRangeAllowance, ...] = ()


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def device_namespace_present(image: bytes) -> bool:
    """Return whether namespace "device" has a written index entry.

    Bytes after the key are entry payloads and are not inspected.
    """
    if len(image) == 0 or len(image) % PAGE_SIZE != 0:
        raise ValueError("identity image is not a whole number of 4096-byte pages")
    for page_start in range(0, len(image), PAGE_SIZE):
        page = image[page_start : page_start + PAGE_SIZE]
        bitmap = page[BITMAP_OFFSET : BITMAP_OFFSET + BITMAP_SIZE]
        for index in range(ENTRIES_PER_PAGE):
            if _entry_state(bitmap, index) != ENTRY_WRITTEN:
                continue
            entry = page[
                ENTRY_DATA_OFFSET + index * ENTRY_SIZE : ENTRY_DATA_OFFSET
                + (index + 1) * ENTRY_SIZE
            ]
            if _is_device_namespace_entry(entry):
                return True
    return False


def _entry_state(bitmap: bytes, index: int) -> int:
    bit = index * 2
    return (bitmap[bit // 8] >> (bit & 7)) & 0x3


def _is_device_namespace_entry(entry: bytes) -> bool:
    if len(entry) < 24:
        return False
    if entry[0] != NS_INDEX or entry[1] != TYPE_U8 or entry[2] != 1:
        return False
    return entry[8:24] == NAMESPACE_KEY


def status_line(present: bool) -> str:
    if present:
        return STATUS_PRESENT
    return STATUS_ABSENT


def flash_identity_result(before_present: bool, after_present: bool) -> int:
    if after_present:
        return 0
    if before_present:
        print(LOST_IDENTITY, file=sys.stderr)
        return 1
    print(UNPROVISIONED_WARNING, file=sys.stderr)
    return 0


def is_danger_image_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        DANGER_MARKER in name
        or lowered.endswith("merged.bin")
        or lowered.endswith("merged-binary.bin")
    )


def overlaps_identity(offset: int, size: int) -> bool:
    identity_end = PARTITION_OFFSET + PARTITION_SIZE
    if PARTITION_OFFSET <= offset < identity_end:
        return True
    if size <= 0:
        return False
    return offset < identity_end and PARTITION_OFFSET < offset + size


def image_is_refused(name: str, offset: int, size: int, *, allow_danger: bool) -> bool:
    if allow_danger:
        return False
    return is_danger_image_name(name) or overlaps_identity(offset, size)


def scan_roots(roots: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for root in roots:
        if root.is_file():
            violations.extend(_scan_file(root))
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name in EXCLUDED_SCAN_NAMES:
                continue
            if path.suffix not in SCAN_SUFFIXES:
                continue
            violations.extend(_scan_file(path))
    return violations


def scan_text(
    path: str,
    text: str,
    *,
    kind: str | None = None,
    allowlist: tuple[EraseRangeAllowance, ...] | None = None,
) -> list[Violation]:
    suffix = Path(path).suffix
    resolved = kind or _kind_for_suffix(suffix)
    stripped = _strip_comments(text, resolved)
    chosen = ERASE_RANGE_ALLOWLIST if allowlist is None else allowlist
    return _violations_in_text(path, stripped, chosen)


def _scan_file(path: Path) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        return []
    return scan_text(str(path), text)


def _kind_for_suffix(suffix: str) -> str:
    if suffix == ".py":
        return "python"
    if suffix == ".sh":
        return "shell"
    if suffix == ".patch":
        return "patch"
    return "c"


def _strip_comments(text: str, kind: str) -> str:
    if kind == "python":
        return _strip_python_comments(text)
    if kind == "shell":
        return _strip_shell_comments(text)
    if kind == "patch":
        return _strip_c_comments(_strip_shell_comments(_patch_added(text)))
    return _strip_c_comments(text)


def _patch_added(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---") or not line.startswith("+"):
            lines.append("")
            continue
        lines.append(line[1:])
    return "\n".join(lines)


def _strip_python_comments(text: str) -> str:
    lines = text.splitlines()
    kept = [""] * len(lines)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type in {tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER, tokenize.NL, tokenize.NEWLINE}:
                continue
            index = token.start[0] - 1
            if 0 <= index < len(kept):
                kept[index] = f"{kept[index]} {token.string}".strip()
    except tokenize.TokenError:
        return text
    return "\n".join(kept)


def _strip_shell_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        lines.append(_strip_shell_comment_line(line))
    return "\n".join(lines)


def _strip_shell_comment_line(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "#":
            return line[:index]
    return line


def _strip_c_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            newline = text.find("\n", index)
            if newline < 0:
                break
            index = newline
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                break
            output.append("\n" * text.count("\n", index, end + 2))
            index = end + 2
            continue
        output.append(text[index])
        index += 1
    return "".join(output)


def _violations_in_text(
    path: str,
    text: str,
    allowlist: tuple[EraseRangeAllowance, ...],
) -> list[Violation]:
    lines = text.splitlines()
    violations: list[Violation] = []
    whole = re.compile(WHOLE_FLASH)
    unnamed = re.compile(UNNAMED_NVS)
    partition = re.compile(PARTITION_ERASE)
    region = re.compile(ERASE_REGION)
    identity = re.compile(IDENTITY_TOKEN)
    opener = re.compile(OPEN_IDENTITY)
    namespace_erase = re.compile(NAMESPACE_ERASE)
    partition_range = re.compile(PARTITION_RANGE)
    flash_region = re.compile(FLASH_REGION)
    for number, line in enumerate(lines, start=1):
        if whole.search(line):
            violations.append(Violation(path, number, "whole_flash_erase"))
        if unnamed.search(line):
            violations.append(Violation(path, number, "unnamed_nvs_erase"))
        for match in partition.finditer(line):
            argument = " ".join(match.group(1).split())
            if argument not in ALLOWED_PARTITION_ARGUMENTS:
                violations.append(Violation(path, number, "non_default_partition_erase"))
        region_match = region.search(line)
        if region_match is not None:
            offset_text = region_match.group(1)
            size_text = region_match.group(2)
            if offset_text is None or size_text is None:
                violations.append(Violation(path, number, "unqualified_region_erase"))
            elif overlaps_identity(int(offset_text, 0), int(size_text, 0)):
                violations.append(Violation(path, number, "identity_erase"))
        if identity.search(line) and _line_erases(line):
            violations.append(Violation(path, number, "identity_erase"))
        ranged = None
        if partition_range.search(line):
            ranged = "partition_erase_range"
        elif flash_region.search(line):
            ranged = "flash_erase_region"
        if ranged is not None and not _range_call_allowed(
            path, _enclosing_function(lines, number), allowlist
        ):
            violations.append(Violation(path, number, ranged))
    for number, line in enumerate(lines, start=1):
        if opener.search(line) is None:
            continue
        window = "\n".join(lines[number - 1 : number + 14])
        if namespace_erase.search(window) or unnamed.search(window):
            violations.append(Violation(path, number, "identity_erase"))
    return violations


def _enclosing_function(lines: list[str], number: int) -> str | None:
    for cursor in range(number - 2, -1, -1):
        match = _FUNCTION_HEAD.search(lines[cursor].strip())
        if match is None:
            continue
        name = match.group("name")
        if name in _NOT_A_FUNCTION:
            continue
        return name
    return None


def _range_call_allowed(
    path: str,
    function: str | None,
    allowlist: tuple[EraseRangeAllowance, ...],
) -> bool:
    if function is None:
        return False
    posix = Path(path).as_posix()
    return any(
        entry.reason.strip()
        and posix.endswith(entry.path)
        and function == entry.function
        for entry in allowlist
    )


def _line_erases(line: str) -> bool:
    return re.search(
        r"\b(?:nvs_flash_erase(?:_partition)?|nvs_erase_(?:all|key)|"
        r"erase[-_](?:flash|all|region)|esp_partition_erase_range)\s*\(",
        line,
    ) is not None or re.search(r"\berase[-_](?:flash|all|region)\b", line) is not None


def _refuse(name: str) -> int:
    print(
        f"refusing to flash {name} without --allow-danger-full-erase",
        file=sys.stderr,
    )
    return 2


def _probe_file(path: Path, phase: str) -> int:
    if phase not in {"before", "after"}:
        raise ValueError(f"unknown phase: {phase}")
    try:
        image = path.read_bytes()
        present = device_namespace_present(image)
    except (OSError, ValueError) as exc:
        print(f"identity probe failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    print(status_line(present))
    return 0


def _run_esptool(python: Path, args: list[str]) -> int:
    completed = subprocess.run(
        [str(python), "-m", "esptool", *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode


def _read_partition(python: Path, port: str, chip: str, baud: int, phase: str) -> int:
    descriptor, raw_path = tempfile.mkstemp(prefix="memoria-identity-probe-")
    path = Path(raw_path)
    os.close(descriptor)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    after = "hard-reset" if phase == "after" else "no-reset"
    image = b""
    try:
        code = _run_esptool(
            python,
            [
                "--chip",
                chip,
                "--port",
                port,
                "--baud",
                str(baud),
                "--before",
                "default-reset",
                "--after",
                after,
                "--silent",
                "read-flash",
                "--no-progress",
                f"0x{PARTITION_OFFSET:x}",
                f"0x{PARTITION_SIZE:x}",
                str(path),
            ],
        )
        if code != 0:
            print("identity partition read failed", file=sys.stderr)
            return 1
        image = path.read_bytes()
    except OSError:
        print("identity partition read failed", file=sys.stderr)
        return 1
    finally:
        path.unlink(missing_ok=True)
    if len(image) != PARTITION_SIZE:
        print("identity partition read failed", file=sys.stderr)
        return 1
    try:
        present = device_namespace_present(image)
    except ValueError:
        print("identity partition read failed", file=sys.stderr)
        return 1
    print(status_line(present))
    return 0


def _check_image(path: Path, offset: int, *, allow_danger: bool) -> int:
    if not path.is_file():
        print(f"image not found: {path}", file=sys.stderr)
        return 1
    if image_is_refused(path.name, offset, path.stat().st_size, allow_danger=allow_danger):
        return _refuse(path.name)
    return 0


def _check_plan(flasher_args: Path, build_dir: Path | None, *, allow_danger: bool) -> int:
    try:
        payload = json.loads(flasher_args.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("flash plan is unreadable", file=sys.stderr)
        return 1
    files = payload.get("flash_files")
    if not isinstance(files, dict):
        print("flash plan is unreadable", file=sys.stderr)
        return 1
    base = build_dir or flasher_args.parent
    for offset_text, relative in files.items():
        if not isinstance(relative, str):
            print("flash plan is unreadable", file=sys.stderr)
            return 1
        try:
            offset = int(str(offset_text), 0)
        except ValueError:
            print("flash plan is unreadable", file=sys.stderr)
            return 1
        candidate = Path(relative)
        full = candidate if candidate.is_absolute() else base / candidate
        size = full.stat().st_size if full.is_file() else 0
        if image_is_refused(full.name, offset, size, allow_danger=allow_danger):
            return _refuse(full.name)
    return 0


def _write_image(python: Path, port: str, chip: str, baud: int, image: Path, *, allow_danger: bool) -> int:
    refused = _check_image(image, 0, allow_danger=allow_danger)
    if refused != 0:
        return refused
    code = _run_esptool(
        python,
        [
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baud),
            "--before",
            "default-reset",
            "--after",
            "no-reset",
            "--silent",
            "write-flash",
            "0x0",
            str(image),
        ],
    )
    if code != 0:
        print("full-chip write failed", file=sys.stderr)
        return 1
    return 0


def _presence(status: str) -> bool | None:
    text = status.strip()
    if text == STATUS_PRESENT:
        return True
    if text == STATUS_ABSENT:
        return False
    print("identity status is unreadable", file=sys.stderr)
    return None


def _verdict(
    *,
    before_image: Path | None,
    after_image: Path | None,
    before_status: str | None,
    after_status: str | None,
) -> int:
    images = before_image is not None or after_image is not None
    statuses = before_status is not None or after_status is not None
    if images == statuses or (before_image is None) != (after_image is None):
        print("verdict needs both images or both status lines", file=sys.stderr)
        return 1
    if before_status is not None or after_status is not None:
        if before_status is None or after_status is None:
            print("verdict needs both images or both status lines", file=sys.stderr)
            return 1
        before_present = _presence(before_status)
        after_present = _presence(after_status)
        if before_present is None or after_present is None:
            return 1
        return flash_identity_result(before_present, after_present)
    assert before_image is not None and after_image is not None
    try:
        before_present = device_namespace_present(before_image.read_bytes())
        after_present = device_namespace_present(after_image.read_bytes())
    except (OSError, ValueError) as exc:
        print(f"identity probe failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    return flash_identity_result(before_present, after_present)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser("probe")
    probe.add_argument("--image", type=Path, required=True)
    probe.add_argument("--phase", choices=("before", "after"), required=True)

    image = commands.add_parser("image")
    image.add_argument("--image", type=Path, required=True)
    image.add_argument("--offset", type=lambda value: int(value, 0), default=0)
    image.add_argument("--allow-danger-full-erase", action="store_true")

    plan = commands.add_parser("plan")
    plan.add_argument("flasher_args", type=Path)
    plan.add_argument("--build-dir", type=Path)
    plan.add_argument("--allow-danger-full-erase", action="store_true")

    read = commands.add_parser("read")
    read.add_argument("--port", required=True)
    read.add_argument("--phase", choices=("before", "after"), required=True)
    read.add_argument("--chip", default="esp32s3")
    read.add_argument("--baud", type=int, default=460800)
    read.add_argument("--python", type=Path, default=Path(sys.executable))

    write = commands.add_parser("write-image")
    write.add_argument("--port", required=True)
    write.add_argument("--image", type=Path, required=True)
    write.add_argument("--chip", default="esp32s3")
    write.add_argument("--baud", type=int, default=460800)
    write.add_argument("--python", type=Path, default=Path(sys.executable))
    write.add_argument("--allow-danger-full-erase", action="store_true")

    scan = commands.add_parser("scan")
    scan.add_argument("roots", type=Path, nargs="+")

    verdict = commands.add_parser("verdict")
    verdict.add_argument("--before-image", type=Path)
    verdict.add_argument("--after-image", type=Path)
    verdict.add_argument("--before-status")
    verdict.add_argument("--after-status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "probe":
        return _probe_file(args.image, args.phase)
    if args.command == "image":
        return _check_image(args.image, args.offset, allow_danger=args.allow_danger_full_erase)
    if args.command == "plan":
        return _check_plan(
            args.flasher_args,
            args.build_dir,
            allow_danger=args.allow_danger_full_erase,
        )
    if args.command == "read":
        return _read_partition(args.python, args.port, args.chip, args.baud, args.phase)
    if args.command == "write-image":
        return _write_image(
            args.python,
            args.port,
            args.chip,
            args.baud,
            args.image,
            allow_danger=args.allow_danger_full_erase,
        )
    if args.command == "scan":
        violations = scan_roots(list(args.roots))
        for violation in violations:
            print(violation.format(), file=sys.stderr)
        return 1 if violations else 0
    if args.command == "verdict":
        return _verdict(
            before_image=args.before_image,
            after_image=args.after_image,
            before_status=args.before_status,
            after_status=args.after_status,
        )
    print("unknown command", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
