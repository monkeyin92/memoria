"""Behavior checks for the media-runtime replay release gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import media_runtime_replay


def _write_recorded_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fixture_count: int,
) -> None:
    script_path = tmp_path / "scripts/media_runtime_replay.py"
    script_path.parent.mkdir(parents=True)
    monkeypatch.setattr(media_runtime_replay, "__file__", str(script_path))

    fixtures = []
    for index in range(fixture_count):
        audio_path = Path("tests/fixtures/child_speech") / f"fixture-{index:03d}.wav"
        full_audio_path = tmp_path / audio_path
        full_audio_path.parent.mkdir(parents=True, exist_ok=True)
        full_audio_path.touch()
        fixtures.append(
            {
                "id": f"child-clean-{index:03d}",
                "kind": "child_clean",
                "expected_text": "test fixture",
                "expected_turns": 1,
                "expected_interrupt": False,
                "speaker_profile": "child",
                "noise": "none",
                "distance_cm": 50,
                "audio_path": audio_path.as_posix(),
            }
        )

    manifest_path = tmp_path / "packages/contracts/child-speech-corpus.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "consented_recorded",
                "consent_required_for_recorded_audio": True,
                "consent_artifact": "consent-record-reference",
                "fixtures": fixtures,
            }
        ),
        encoding="utf-8",
    )


def test_consented_recorded_manifest_rejects_199_complete_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_recorded_manifest(monkeypatch, tmp_path, fixture_count=199)

    with pytest.raises(SystemExit, match="requires at least 200 fixtures"):
        media_runtime_replay.main()


def test_consented_recorded_manifest_accepts_200_complete_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_recorded_manifest(monkeypatch, tmp_path, fixture_count=200)

    media_runtime_replay.main()


def test_consent_schema_matches_runtime_fixture_gate() -> None:
    root = Path(__file__).parents[1]
    schema = json.loads(
        (root / "packages/contracts/child-speech-corpus.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["allOf"][0]["then"]["properties"]["fixtures"]["minItems"] == 200
