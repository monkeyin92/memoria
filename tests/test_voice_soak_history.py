"""验证历史 voice_soak 扫描器只复用统一 judge，不改变原判分语义。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import voice_soak_history, voice_soak_verdict


def _write_run(
    run: Path,
    rows: list[dict[str, object]],
    *,
    summary: dict[str, object] | None = None,
) -> None:
    run.mkdir(parents=True)
    (run / "timeline.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (run / "manifest.json").write_text(
        json.dumps(
            voice_soak_verdict.manifest_dict(
                scenario_hash="history-test",
                expected_step_tags=["t001"],
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if summary is not None:
        (run / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False),
            encoding="utf-8",
        )


def _rows(*, latency_s: float = 1.25) -> list[dict[str, object]]:
    return [
        {"kind": "reply", "tag": "t001", "latency_s": latency_s},
        {"kind": "post_scenario_observation", "verdict": "verified_idle"},
    ]


def test_history_reports_verdict_metrics_and_path_inconsistency(
    tmp_path: Path, capsys: object
) -> None:
    root = tmp_path / "runs"
    _write_run(root / "run-pass", _rows())
    _write_run(root / "run-fail", _rows(latency_s=4.0))
    missing_out = tmp_path / "missing" / "run-drift"
    _write_run(
        root / "run-drift",
        _rows(latency_s=1.5),
        summary={
            "out": str(missing_out),
            "report_dir": str(root / "run-drift"),
        },
    )

    exit_code = voice_soak_history.main(["--roots", str(root), "--json"])

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["run_count"] == 3
    assert output["path_inconsistency_count"] == 3
    by_name = {Path(item["run"]).name: item for item in output["runs"]}
    assert by_name["run-pass"] == {
        "run": str(root / "run-pass"),
        "verdict": "pass",
        "no_reply": 0,
        "p90": 1.25,
        "max": 1.25,
    }
    assert by_name["run-fail"]["verdict"] == "fail"
    assert by_name["run-fail"]["p90"] == 4.0
    drift = output["path_inconsistencies"][0]
    assert drift["run"] == str(root / "run-drift")
    assert drift["out"] == str(missing_out)
    assert drift["report_dir"] == str(root / "run-drift")
    assert any("out 目录不存在" in reason for reason in drift["reasons"])
    assert not any("out 与 report_dir 指向不同目录" in reason for reason in drift["reasons"])

    assert voice_soak_history.main(["--roots", str(root), "--allow-drift"]) == 1
    capsys.readouterr()


def test_history_stops_at_run_and_deduplicates_timeline_content(tmp_path: Path) -> None:
    primary_root = tmp_path / "primary"
    copy_root = tmp_path / "copy"
    primary = primary_root / "run-primary"
    copy = copy_root / "run-copy"
    _write_run(primary, _rows())
    _write_run(copy, _rows())
    nested = primary / "pytest-engineer" / "test_example"
    nested.mkdir(parents=True)
    (nested / "timeline.jsonl").write_text(
        json.dumps({"kind": "reply", "tag": "nested", "latency_s": 99.0}) + "\n",
        encoding="utf-8",
    )

    report = voice_soak_history.scan_history([primary_root, copy_root])

    assert report["run_count"] == 1
    assert report["duplicate_count"] == 1
    assert report["duplicates"][0]["kept"] == str(primary)
    assert report["duplicates"][0]["ignored"] == [str(copy)]
    assert report["runs"][0]["run"] == str(primary)


def test_history_reports_missing_summary_and_each_missing_path_field(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _write_run(root / "no-summary", _rows(latency_s=1.0))
    _write_run(
        root / "no-out",
        _rows(latency_s=1.1),
        summary={"report_dir": str(root / "no-out")},
    )
    _write_run(
        root / "no-report-dir",
        _rows(latency_s=1.2),
        summary={"out": str(root / "no-report-dir")},
    )
    driver = root / "normal-driver"
    driver.mkdir()
    normal = root / "normal"
    _write_run(
        normal,
        _rows(latency_s=1.3),
        summary={"out": str(driver), "report_dir": str(normal)},
    )

    report = voice_soak_history.scan_history([root])

    reasons_by_name = {
        Path(item["run"]).name: item["reasons"] for item in report["path_inconsistencies"]
    }
    assert reasons_by_name["no-summary"] == ["缺 summary.json"]
    assert reasons_by_name["no-out"] == ["缺 out"]
    assert reasons_by_name["no-report-dir"] == ["缺 report_dir"]
    assert "normal" not in reasons_by_name


def test_relative_roots_and_report_paths_use_the_scan_base(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = tmp_path / "repo"
    root = repo / "runs"
    run = root / "run-relative"
    _write_run(
        run,
        _rows(latency_s=1.4),
        summary={"out": "runs/run-relative", "report_dir": "runs/run-relative"},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    report = voice_soak_history.scan_history([Path("runs")], base_dir=repo)

    assert report["roots"] == [str(root)]
    assert report["run_count"] == 1
    assert report["path_inconsistency_count"] == 0


def test_missing_root_is_reported_without_crashing(tmp_path: Path, capsys: object) -> None:
    missing = tmp_path / "does-not-exist"

    assert voice_soak_history.main(["--roots", str(missing)]) == 0

    output = capsys.readouterr().out
    assert f"目录不存在: {missing}" in output
    assert "共扫描 0 个去重后 run" in output
