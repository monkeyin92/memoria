"""Labelled online lines load only real Doubao speech; scaffold never plays."""

from __future__ import annotations

import json
from pathlib import Path

from services.common.aigc_opus import require_aigc_tags, title_is_placeholder
from services.common.labelled_lines import (
    DEFAULT_ASSETS_ROOT,
    PLAYABLE_RENDER_TOOLS,
    SYNTHETIC_RENDER_TOOLS,
    LabelledLine,
    LabelledLineCatalogue,
    is_synthetic_render_tool,
    load_catalogue,
    override_labelled_catalogue,
)
from services.common.llm_failover import LLM_UNAVAILABLE_LINE_ID, LLM_UNAVAILABLE_TEXT
from services.common.llm_thinking_filler import LLM_THINKING_LINE_ID, LLM_THINKING_TEXT


def test_shipped_provenance_scaffold_is_not_loaded_for_play() -> None:
    provenance = json.loads((DEFAULT_ASSETS_ROOT / "provenance.json").read_text(encoding="utf-8"))
    catalogue = load_catalogue()
    for entry in provenance["lines"]:
        assert is_synthetic_render_tool(entry["tool"])
        assert entry.get("playable") is False
        assert catalogue.get(str(entry["id"])) is None
    assert catalogue.get(LLM_THINKING_LINE_ID) is None
    assert catalogue.get(LLM_UNAVAILABLE_LINE_ID) is None
    assert catalogue.by_text(LLM_THINKING_TEXT) is None
    assert catalogue.by_text(LLM_UNAVAILABLE_TEXT) is None


def test_catalogue_excludes_synthetic_tone_even_when_ogg_and_pcm_exist(
    tmp_path: Path,
) -> None:
    """A scaffold provenance row must never become a playable catalogue entry."""

    import subprocess

    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "generated": "2026-10-15",
                "tool": "ffmpeg-libopus-scaffold",
                "lines": [
                    {
                        "id": LLM_THINKING_LINE_ID,
                        "prompt": LLM_THINKING_TEXT,
                        "tool": "ffmpeg-libopus-scaffold",
                        "playable": False,
                    },
                    {
                        "id": LLM_UNAVAILABLE_LINE_ID,
                        "prompt": LLM_UNAVAILABLE_TEXT,
                        "tool": "ffmpeg-libopus-scaffold",
                        "playable": False,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for line_id in (LLM_THINKING_LINE_ID, LLM_UNAVAILABLE_LINE_ID):
        ogg = tmp_path / f"{line_id}.ogg"
        pcm = tmp_path / f"{line_id}.s16le"
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
                "-c:a",
                "libopus",
                "-metadata",
                f"CONTENT_ID={line_id}",
                "-metadata",
                "SERVICE_PROVIDER=Memoria",
                "-metadata",
                "AI_GENERATED=true",
                "-metadata",
                "ARTIST=zh_female_tianmeitaozi_uranus_bigtts",
                str(ogg),
            ],
            check=True,
        )
        pcm.write_bytes(b"\x01\x00" * 480)
        tags = require_aigc_tags(ogg.read_bytes()[:64_000], content_id=line_id)
        assert tags["ai_generated"] is True

    catalogue = load_catalogue(tmp_path)
    assert set(SYNTHETIC_RENDER_TOOLS) == {"ffmpeg-libopus-scaffold"}
    assert "doubao-seed-tts-2.0" in PLAYABLE_RENDER_TOOLS
    assert catalogue.get(LLM_THINKING_LINE_ID) is None
    assert catalogue.get(LLM_UNAVAILABLE_LINE_ID) is None


def test_doubao_provenance_loads_labelled_speech(tmp_path: Path) -> None:
    import subprocess

    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "generated": "2026-10-15",
                "tool": "doubao-seed-tts-2.0",
                "lines": [
                    {
                        "id": LLM_THINKING_LINE_ID,
                        "prompt": LLM_THINKING_TEXT,
                        "tool": "doubao-seed-tts-2.0",
                        "playable": True,
                    },
                    {
                        "id": LLM_UNAVAILABLE_LINE_ID,
                        "prompt": LLM_UNAVAILABLE_TEXT,
                        "tool": "doubao-seed-tts-2.0",
                        "playable": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for line_id in (LLM_THINKING_LINE_ID, LLM_UNAVAILABLE_LINE_ID):
        ogg = tmp_path / f"{line_id}.ogg"
        pcm = tmp_path / f"{line_id}.s16le"
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
                "sine=frequency=220:duration=0.15",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-c:a",
                "libopus",
                "-metadata",
                f"TITLE={line_id}",
                "-metadata",
                f"CONTENT_ID={line_id}",
                "-metadata",
                "SERVICE_PROVIDER=Memoria",
                "-metadata",
                "AI_GENERATED=true",
                str(ogg),
            ],
            check=True,
        )
        pcm.write_bytes(b"\x02\x00" * 960)
        assert not title_is_placeholder(ogg.read_bytes()[:64_000])

    catalogue = load_catalogue(tmp_path)
    assert catalogue.require(LLM_THINKING_LINE_ID).pcm_s16le == b"\x02\x00" * 960
    assert catalogue.by_text(LLM_UNAVAILABLE_TEXT) is not None


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
