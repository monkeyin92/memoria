"""校验离线音频清单。

默认模式只校验文件存在性与 sha256；``--measure`` 复测并比较清单中的
时长、LUFS 和真峰值，记录不符即返回非零；``--target`` 复测实际文件并按
目标带判定是否达标，任一条不达标即返回非零。当前仓库预期 ``--target``
返回非零，因为两条 ``llm_*`` 仍未进入归一化后的离线包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "services/common/assets/offline_audio_manifest.json"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
TARGET_LUFS_MIN = -16.7
TARGET_LUFS_MAX = -15.4
TARGET_TRUE_PEAK_MAX = -0.1


class VerificationError(RuntimeError):
    """表示清单或音频校验失败。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio_file:
        for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise VerificationError(f"无法执行 {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise VerificationError(
            f"命令失败（退出码 {result.returncode}）：{' '.join(command)}\n{detail}"
        )
    return result


def _parse_loudnorm(stderr: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", stderr):
        try:
            payload, _ = decoder.raw_decode(stderr[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "input_i" in payload and "input_tp" in payload:
            return payload
    raise VerificationError("ffmpeg 输出中没有找到 loudnorm 测量结果")


def _tool(name: str) -> str:
    """返回 PATH 中的可执行文件，缺失时给出可读错误而不是 OSError。"""

    resolved = shutil.which(name)
    if resolved is None:
        raise VerificationError(f"找不到 {name}，请确认已安装并在 PATH 中")
    return resolved


def _measure(path: Path) -> dict[str, float]:
    probe = _run(
        [
            _tool(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    probe_payload = json.loads(probe.stdout)
    duration_s = float(probe_payload["format"]["duration"])

    measured = _run(
        [
            _tool(FFMPEG),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    loudnorm = _parse_loudnorm(measured.stderr)
    return {
        "duration_s": duration_s,
        "lufs": float(loudnorm["input_i"]),
        "true_peak_dbtp": float(loudnorm["input_tp"]),
    }


def _manifest_path(argument: str) -> Path:
    path = Path(argument)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _target_errors(asset: dict[str, Any]) -> list[str]:
    """返回一条合成或实测资产的目标带错误。"""

    asset_id = asset.get("id", "<unknown>")
    required_fields = ("lufs", "true_peak_dbtp")
    missing_fields = [
        field for field in required_fields if field not in asset or asset[field] is None
    ]
    if missing_fields:
        fields = ", ".join(missing_fields)
        return [f"{asset_id}: 目标带校验缺少字段：{fields}"]

    errors: list[str] = []
    try:
        lufs = float(asset["lufs"])
        true_peak_dbtp = float(asset["true_peak_dbtp"])
    except (TypeError, ValueError):
        return [f"{asset_id}: 目标带校验字段不是数字：lufs、true_peak_dbtp"]

    if not math.isfinite(lufs) or not math.isfinite(true_peak_dbtp):
        return [f"{asset_id}: 目标带校验字段必须是有限数字：lufs、true_peak_dbtp"]

    if lufs < TARGET_LUFS_MIN:
        delta = TARGET_LUFS_MIN - lufs
        errors.append(
            f"{asset_id}: 目标不达标：LUFS={lufs:.2f}，低于下限 "
            f"{TARGET_LUFS_MIN:.2f}，差值={delta:.2f} LU"
        )
    elif lufs > TARGET_LUFS_MAX:
        delta = lufs - TARGET_LUFS_MAX
        errors.append(
            f"{asset_id}: 目标不达标：LUFS={lufs:.2f}，高于上限 "
            f"{TARGET_LUFS_MAX:.2f}，差值={delta:.2f} LU"
        )

    if true_peak_dbtp > TARGET_TRUE_PEAK_MAX:
        delta = true_peak_dbtp - TARGET_TRUE_PEAK_MAX
        errors.append(
            f"{asset_id}: 目标不达标：真峰值={true_peak_dbtp:.2f} dBTP，高于上限 "
            f"{TARGET_TRUE_PEAK_MAX:.2f} dBTP，差值={delta:.2f} dB"
        )
    return errors


def _check_asset(
    asset: dict[str, Any],
    tolerance: float,
    measure: bool,
    target: bool,
) -> list[str]:
    asset_id = asset["id"]
    path = PROJECT_ROOT / asset["path"]
    errors: list[str] = []
    if not path.is_file():
        return [f"{asset_id}: 文件不存在：{asset['path']}"]

    actual_sha256 = _sha256(path)
    if actual_sha256 != asset["sha256"]:
        errors.append(
            f"{asset_id}: sha256 不匹配，manifest={asset['sha256']} "
            f"actual={actual_sha256}"
        )

    if not measure and not target:
        return errors

    actual = _measure(path)
    print(
        f"{asset_id}: lufs={actual['lufs']:.2f} LUFS, "
        f"true_peak={actual['true_peak_dbtp']:.2f} dBTP, "
        f"duration={actual['duration_s']:.6f} s"
    )
    if measure:
        for field in ("lufs", "true_peak_dbtp", "duration_s"):
            if field not in asset or asset[field] is None:
                errors.append(f"{asset_id}: manifest 缺少测量字段：{field}")
                continue
            difference = abs(actual[field] - float(asset[field]))
            if difference > tolerance:
                errors.append(
                    f"{asset_id}: {field} 超出容差，manifest={float(asset[field]):.6f} "
                    f"actual={actual[field]:.6f} delta={difference:.6f} "
                    f"tolerance={tolerance:.6f}"
                )

    if target:
        target_errors = _target_errors({**asset, **actual})
        errors.extend(target_errors)
        if target_errors and asset.get("note"):
            errors.append(f"{asset_id}: {asset['note']}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验离线音频 manifest")
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST.relative_to(PROJECT_ROOT)),
        help="manifest 路径，默认 services/common/assets/offline_audio_manifest.json",
    )
    parser.add_argument(
        "--measure",
        action="store_true",
        help="用 ffprobe 和 ffmpeg 重算时长、LUFS 与真峰值",
    )
    parser.add_argument(
        "--target",
        action="store_true",
        help="实测并校验 LUFS -16.7 到 -15.4、真峰值不高于 -0.1 dBTP",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.3,
        help="测量值允许的绝对误差，默认 0.3",
    )
    args = parser.parse_args()
    if args.tolerance < 0:
        parser.error("--tolerance 不能小于 0")

    manifest_path = _manifest_path(args.manifest)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assets = manifest["assets"]
        errors: list[str] = []
        target_failures: list[str] = []
        for asset in assets:
            asset_errors = _check_asset(
                asset,
                args.tolerance,
                args.measure,
                args.target,
            )
            errors.extend(asset_errors)
            if args.target and any("目标不达标" in error for error in asset_errors):
                target_failures.append(asset["id"])
    except (OSError, KeyError, TypeError, ValueError, VerificationError) as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return 1

    if errors:
        if args.target and target_failures:
            print(
                "目标带校验不达标："
                + ", ".join(target_failures),
                file=sys.stderr,
            )
        print("校验失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"校验通过：{len(assets)} 条音频")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
