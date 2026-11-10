from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = PROJECT_ROOT / "services/common/assets/offline_audio_manifest.json"
STORIES_INDEX_PATH = (
    PROJECT_ROOT
    / "firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/stories/index.json"
)
OFFLINE_LINES_INDEX_PATH = (
    PROJECT_ROOT
    / "firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/assets/offline_lines/index.json"
)
OFFLINE_LINE_IDS = {"llm_unavailable", "llm_thinking"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.hermetic
def test_offline_audio_manifest_matches_shipped_assets() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    stories_index = json.loads(STORIES_INDEX_PATH.read_text(encoding="utf-8"))
    offline_lines_index = json.loads(OFFLINE_LINES_INDEX_PATH.read_text(encoding="utf-8"))

    expected_ids = {
        story["id"]
        for story in stories_index["stories"]
        if story["placeholder"] is False
    }
    shipped_offline_line_ids = {
        line["id"]
        for line in offline_lines_index["lines"]
        if line["id"] in OFFLINE_LINE_IDS and line["placeholder"] is False
    }
    assert shipped_offline_line_ids == OFFLINE_LINE_IDS
    expected_ids.update(shipped_offline_line_ids)

    assets = manifest["assets"]
    assert len(assets) == 6
    assert {asset["id"] for asset in assets} == expected_ids

    for asset in assets:
        path = PROJECT_ROOT / asset["path"]
        assert path.is_file(), asset["path"]
        assert _sha256(path) == asset["sha256"], asset["id"]
        assert asset["sample_rate_hz"] == 16000, asset["id"]
        assert asset["channels"] == 1, asset["id"]


@pytest.mark.hermetic
def test_target_band_accepts_in_range_asset() -> None:
    from scripts.verify_offline_audio import _target_errors

    assert _target_errors(
        {"id": "synthetic", "lufs": -16.0, "true_peak_dbtp": -1.0}
    ) == []


@pytest.mark.hermetic
def test_target_band_reports_out_of_range_values_and_deltas() -> None:
    from scripts.verify_offline_audio import _target_errors

    errors = _target_errors(
        {"id": "synthetic", "lufs": -20.4, "true_peak_dbtp": 0.2}
    )

    assert len(errors) == 2
    assert "低于下限 -16.70" in errors[0]
    assert "差值=3.70 LU" in errors[0]
    assert "高于上限 -0.10 dBTP" in errors[1]
    assert "差值=0.30 dB" in errors[1]


@pytest.mark.hermetic
def test_target_band_reports_missing_measurement_fields() -> None:
    from scripts.verify_offline_audio import _target_errors

    errors = _target_errors({"id": "synthetic", "lufs": -16.0})

    assert errors == ["synthetic: 目标带校验缺少字段：true_peak_dbtp"]
