#!/usr/bin/env python3
"""统一判读新旧 voice_soak 报告目录，并检查历史路径漂移。

本脚本故意不修改 ``voice_soak_verdict.py``：旧报告和新报告只是落盘目录不同，
判分语义仍应由唯一的 ``voice_soak_verdict.judge()`` 负责，避免历史兼容逻辑
改变既有判据或破坏 ``voice_soak_verdict.main()`` 的调用者。

默认扫描 ``outputs/acceptance`` 和 ``devicetest``，也可以指定一个或多个根目录：

    python scripts/voice_soak_history.py
    python scripts/voice_soak_history.py --roots outputs/acceptance devicetest
    python scripts/voice_soak_history.py --json --allow-drift
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from . import voice_soak_verdict
else:
    import voice_soak_verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (REPO_ROOT / "outputs/acceptance", REPO_ROOT / "devicetest")


def _json_path(path: Path) -> str:
    return str(path)


def _iter_runs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    pending = [root]
    runs: list[Path] = []
    while pending:
        directory = pending.pop()
        if (directory / "timeline.jsonl").is_file():
            runs.append(directory)
            continue
        try:
            child_directories = sorted(
                (path for path in directory.iterdir() if path.is_dir()),
                reverse=True,
            )
        except OSError:
            continue
        pending.extend(child_directories)
    return sorted(runs)


def _timeline_sha256(run: Path) -> str:
    digest = hashlib.sha256()
    with (run / "timeline.jsonl").open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_report_path(value: object, base_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _path_drift(run: Path, *, base_dir: Path) -> dict[str, object] | None:
    summary_path = run / "summary.json"
    if not summary_path.is_file():
        return {
            "run": _json_path(run),
            "reasons": ["缺 summary.json"],
            "out": None,
            "report_dir": None,
        }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "run": _json_path(run),
            "reasons": [f"summary.json 无法读取: {exc}"],
            "out": None,
            "report_dir": None,
        }
    if not isinstance(summary, dict):
        return {
            "run": _json_path(run),
            "reasons": ["summary.json 不是 JSON 对象"],
            "out": None,
            "report_dir": None,
        }

    out_value = summary.get("out")
    report_dir_value = summary.get("report_dir")
    out_path = _resolve_report_path(out_value, base_dir)
    report_dir_path = _resolve_report_path(report_dir_value, base_dir)
    reasons: list[str] = []
    if "out" not in summary:
        reasons.append("缺 out")
    elif out_path is None:
        reasons.append("summary.json 的 out 不是有效路径")
    if "report_dir" not in summary:
        reasons.append("缺 report_dir")
    elif report_dir_path is None:
        reasons.append("summary.json 的 report_dir 不是有效路径")
    if out_path is not None and not out_path.is_dir():
        reasons.append(f"out 目录不存在: {out_path}")
    if report_dir_path is not None and not report_dir_path.is_dir():
        reasons.append(f"report_dir 目录不存在: {report_dir_path}")
    declared_paths = {path for path in (out_path, report_dir_path) if path is not None}
    if declared_paths and run.resolve(strict=False) not in declared_paths:
        reasons.append(
            "扫描到的 run 不在 out 或 report_dir 指向的目录中: "
            f"{run.resolve(strict=False)}"
        )
    if not reasons:
        return None
    return {
        "run": _json_path(run),
        "reasons": reasons,
        "out": out_value if isinstance(out_value, str) else None,
        "report_dir": report_dir_value if isinstance(report_dir_value, str) else None,
    }


def scan_history(
    roots: Sequence[Path],
    *,
    base_dir: Path | None = None,
) -> dict[str, object]:
    """扫描 roots，调用统一 judge，并返回可直接序列化的报告。"""
    base_dir = (base_dir or REPO_ROOT).resolve()
    normalized_roots = [
        (root if root.is_absolute() else base_dir / root).resolve(strict=False) for root in roots
    ]
    missing_roots = [root for root in normalized_roots if not root.is_dir()]
    seen: set[Path] = set()
    runs_by_digest: dict[str, list[Path]] = {}
    for root in normalized_roots:
        for run in _iter_runs(root):
            resolved = run.resolve(strict=False)
            if resolved not in seen:
                seen.add(resolved)
                digest = _timeline_sha256(run)
                runs_by_digest.setdefault(digest, []).append(run)

    run_paths: list[Path] = []
    duplicates: list[dict[str, object]] = []
    for digest, candidates in runs_by_digest.items():
        kept = next(
            (candidate for candidate in candidates if (candidate / "summary.json").is_file()),
            candidates[0],
        )
        run_paths.append(kept)
        ignored = [candidate for candidate in candidates if candidate != kept]
        if ignored:
            duplicates.append(
                {
                    "sha256": digest,
                    "kept": _json_path(kept),
                    "ignored": [_json_path(candidate) for candidate in ignored],
                }
            )

    runs: list[dict[str, object]] = []
    drifts: list[dict[str, object]] = []
    for run in sorted(run_paths, key=lambda path: str(path)):
        result = voice_soak_verdict.judge(run)
        runs.append(
            {
                "run": _json_path(run),
                "verdict": result.status,
                "no_reply": result.no_reply,
                "p90": result.reply_latency_p90_s,
                "max": result.reply_latency_max_s,
            }
        )
        if drift := _path_drift(run, base_dir=base_dir):
            drifts.append(drift)

    fail_count = sum(item["verdict"] == "fail" for item in runs)
    incomplete_count = sum(item["verdict"] == "incomplete" for item in runs)
    return {
        "roots": [_json_path(root) for root in normalized_roots],
        "missing_roots": [_json_path(root) for root in missing_roots],
        "runs": runs,
        "duplicates": duplicates,
        "duplicate_count": sum(len(item["ignored"]) for item in duplicates),
        "path_inconsistencies": drifts,
        "run_count": len(runs),
        "fail_count": fail_count,
        "incomplete_count": incomplete_count,
        "non_pass_count": fail_count + incomplete_count,
        "path_inconsistency_count": len(drifts),
    }


def _display_metric(value: object) -> str:
    return "-" if value is None else str(value)


def _print_table(report: dict[str, object]) -> None:
    missing_roots = report["missing_roots"]
    if isinstance(missing_roots, list):
        for root in missing_roots:
            print(f"目录不存在: {root}")

    print("run\tverdict\tno_reply\tp90\tmax")
    runs = report["runs"]
    if isinstance(runs, list):
        for item in runs:
            print(
                f"{item['run']}\t{item['verdict']}\t{item['no_reply']}\t"
                f"{_display_metric(item['p90'])}\t{_display_metric(item['max'])}"
            )

    drifts = report["path_inconsistencies"]
    print(f"记录不一致 ({len(drifts) if isinstance(drifts, list) else 0}):")
    if isinstance(drifts, list) and drifts:
        for item in drifts:
            reasons = "；".join(str(reason) for reason in item["reasons"])
            print(f"- {item['run']}: {reasons}")
    else:
        print("- 无")
    all_reasons = (
        [str(reason) for item in drifts for reason in item["reasons"]]
        if isinstance(drifts, list)
        else []
    )
    missing_summary = sum(1 for reason in all_reasons if reason == "缺 summary.json")
    print(
        f"共扫描 {report['run_count']} 个去重后 run，其中 "
        f"{report['fail_count']} 个 fail、{report['incomplete_count']} 个 incomplete、"
        f"{report['path_inconsistency_count']} 个记录不一致"
        f"（其中 {missing_summary} 个缺 summary.json）"
    )
    duplicates = report["duplicates"]
    if isinstance(duplicates, list):
        print(f"重复 timeline ({report['duplicate_count']} 个副本):")
        if duplicates:
            for item in duplicates:
                print(f"- 保留 {item['kept']}，忽略 {', '.join(item['ignored'])}")
        else:
            print("- 无")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整报告")
    parser.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=list(DEFAULT_ROOTS),
        metavar="ROOT",
        help="要递归扫描的根目录，默认是 outputs/acceptance 和 devicetest",
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="允许存在路径不一致，但判分失败仍以非零退出",
    )
    args = parser.parse_args(argv)
    report = scan_history(args.roots)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)
    has_drift = bool(report["path_inconsistencies"])
    has_bad_verdict = report["non_pass_count"] > 0
    return 0 if (args.allow_drift or not has_drift) and not has_bad_verdict else 1


if __name__ == "__main__":
    sys.exit(main())
