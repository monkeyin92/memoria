"""Labelled online lines carry AIGC tags and never need streaming TTS to load."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.common.aigc_opus import require_aigc_tags
from services.common.labelled_lines import (
    DEFAULT_ASSETS_ROOT,
    ONLINE_SAMPLE_RATE_HZ,
    LabelledLine,
    LabelledLineCatalogue,
    load_catalogue,
    override_labelled_catalogue,
)
from services.common.llm_failover import LLM_UNAVAILABLE_LINE_ID, LLM_UNAVAILABLE_TEXT
from services.common.llm_thinking_filler import LLM_THINKING_LINE_ID, LLM_THINKING_TEXT


def test_shipped_assets_have_aigc_labels_and_pcm() -> None:
    catalogue = load_catalogue()
    thinking = catalogue.require(LLM_THINKING_LINE_ID)
    unavailable = catalogue.require(LLM_UNAVAILABLE_LINE_ID)
    assert thinking.text == LLM_THINKING_TEXT
    assert unavailable.text == LLM_UNAVAILABLE_TEXT
    assert thinking.aigc == {
        "content_id": LLM_THINKING_LINE_ID,
        "service_provider": "Memoria",
        "ai_generated": True,
    }
    assert unavailable.aigc == {
        "content_id": LLM_UNAVAILABLE_LINE_ID,
        "service_provider": "Memoria",
        "ai_generated": True,
    }
    assert thinking.sample_rate_hz == ONLINE_SAMPLE_RATE_HZ
    assert len(thinking.pcm_s16le) > 1000
    assert len(unavailable.pcm_s16le) > len(thinking.pcm_s16le)
    assert catalogue.by_text(LLM_THINKING_TEXT) is thinking
    assert catalogue.by_text(LLM_UNAVAILABLE_TEXT) is unavailable


def test_ogg_files_themselves_embed_aigc_tags() -> None:
    for line_id in (LLM_THINKING_LINE_ID, LLM_UNAVAILABLE_LINE_ID):
        path = DEFAULT_ASSETS_ROOT / f"{line_id}.ogg"
        tags = require_aigc_tags(path.read_bytes()[:64_000], content_id=line_id)
        assert tags["ai_generated"] is True
        assert tags["service_provider"] == "Memoria"


def test_catalogue_loads_from_s16le_sidecar_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbid_ffmpeg(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ffmpeg must not run when .s16le sidecars are present")

    monkeypatch.setattr("services.common.labelled_lines.subprocess.run", _forbid_ffmpeg)
    catalogue = load_catalogue()
    thinking = catalogue.require(LLM_THINKING_LINE_ID)
    assert thinking.pcm_s16le == (
        DEFAULT_ASSETS_ROOT / f"{LLM_THINKING_LINE_ID}.s16le"
    ).read_bytes()


def test_missing_aigc_tag_fails_at_catalogue_boundary(tmp_path: Path) -> None:
    import subprocess
    import tempfile

    bare = tmp_path / f"{LLM_THINKING_LINE_ID}.ogg"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav:
        wav_path = Path(wav.name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=196:duration=0.2",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(wav_path),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav_path),
                "-c:a",
                "libopus",
                "-b:a",
                "16000",
                "-metadata",
                f"CONTENT_ID={LLM_THINKING_LINE_ID}",
                "-metadata",
                "SERVICE_PROVIDER=Memoria",
                str(bare),
            ],
            check=True,
        )
    finally:
        wav_path.unlink(missing_ok=True)
    (tmp_path / f"{LLM_UNAVAILABLE_LINE_ID}.ogg").write_bytes(
        (DEFAULT_ASSETS_ROOT / f"{LLM_UNAVAILABLE_LINE_ID}.ogg").read_bytes()
    )
    for line_id in (LLM_THINKING_LINE_ID, LLM_UNAVAILABLE_LINE_ID):
        (tmp_path / f"{line_id}.s16le").write_bytes(
            (DEFAULT_ASSETS_ROOT / f"{line_id}.s16le").read_bytes()
        )
    with pytest.raises(ValueError, match="AI_GENERATED"):
        load_catalogue(tmp_path)


def test_override_catalogue_is_restored() -> None:
    fake = LabelledLine(
        line_id=LLM_THINKING_LINE_ID,
        text=LLM_THINKING_TEXT,
        aigc={
            "content_id": LLM_THINKING_LINE_ID,
            "service_provider": "Memoria",
            "ai_generated": True,
        },
        pcm_s16le=b"\x01\x00" * 480,
        sample_rate_hz=24_000,
        ogg_path=DEFAULT_ASSETS_ROOT / f"{LLM_THINKING_LINE_ID}.ogg",
    )
    override_labelled_catalogue(LabelledLineCatalogue({LLM_THINKING_LINE_ID: fake}))
    try:
        from services.common.labelled_lines import labelled_catalogue

        assert labelled_catalogue().require(LLM_THINKING_LINE_ID).pcm_s16le == fake.pcm_s16le
    finally:
        override_labelled_catalogue(None)
