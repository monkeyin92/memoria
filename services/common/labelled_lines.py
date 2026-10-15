"""Pre-recorded AIGC-labelled lines played online instead of streaming TTS."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from services.common.aigc_opus import OPUS_TAGS_SCAN_BYTES, require_aigc_tags
from services.common.llm_failover import (
    LLM_UNAVAILABLE_AIGC,
    LLM_UNAVAILABLE_LINE_ID,
    LLM_UNAVAILABLE_TEXT,
)
from services.common.llm_thinking_filler import (
    LLM_THINKING_AIGC,
    LLM_THINKING_LINE_ID,
    LLM_THINKING_TEXT,
)

ONLINE_SAMPLE_RATE_HZ: Final = 24_000
DEFAULT_ASSETS_ROOT: Final = (
    Path(__file__).resolve().parent / "assets" / "labelled_lines"
)

LINE_SPECS: Final[tuple[tuple[str, str, Mapping[str, object]], ...]] = (
    (LLM_THINKING_LINE_ID, LLM_THINKING_TEXT, LLM_THINKING_AIGC),
    (LLM_UNAVAILABLE_LINE_ID, LLM_UNAVAILABLE_TEXT, LLM_UNAVAILABLE_AIGC),
)


@dataclass(frozen=True, slots=True)
class LabelledLine:
    """One fixed line: exact text, validated AIGC label, and playable PCM."""

    line_id: str
    text: str
    aigc: Mapping[str, object]
    pcm_s16le: bytes
    sample_rate_hz: int
    ogg_path: Path

    def frame_pcm(
        self,
        *,
        source_start_sample: int,
        frame_samples: int,
        announce_text: bool,
    ) -> Iterator[tuple[bytes, int, bool, bool, str]]:
        """Yield ``(pcm, start_sample, first, final, text_delta)`` frames."""

        if frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        frame_bytes = frame_samples * 2
        pcm = self.pcm_s16le
        if not pcm or len(pcm) % 2:
            raise ValueError(f"{self.line_id} PCM must be non-empty 16-bit audio")
        sample = source_start_sample
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset : offset + frame_bytes]
            final = offset + frame_bytes >= len(pcm)
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            text_delta = self.text if announce_text and offset == 0 else ""
            yield frame, sample, offset == 0, final, text_delta
            sample += frame_samples


class LabelledLineCatalogue:
    """Load-once registry of online labelled lines."""

    def __init__(self, lines: Mapping[str, LabelledLine]) -> None:
        self._by_id = dict(lines)
        self._by_text = {line.text: line for line in lines.values()}

    def get(self, line_id: str) -> LabelledLine | None:
        return self._by_id.get(line_id)

    def require(self, line_id: str) -> LabelledLine:
        line = self.get(line_id)
        if line is None:
            raise KeyError(line_id)
        return line

    def by_text(self, text: str) -> LabelledLine | None:
        return self._by_text.get(text.strip())

    def prefix_candidate(self, text: str) -> bool:
        """True when ``text`` is a non-empty proper or exact prefix of a labelled line."""

        piece = text.strip()
        if not piece:
            return False
        return any(line.text.startswith(piece) for line in self._by_id.values())

    def __contains__(self, line_id: object) -> bool:
        return isinstance(line_id, str) and line_id in self._by_id


_catalogue_lock = threading.Lock()
_catalogue_override: LabelledLineCatalogue | None = None


def _decode_ogg_pcm(path: Path, *, sample_rate_hz: int) -> bytes:
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                str(sample_rate_hz),
                "pipe:1",
            ],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"ffmpeg is required to decode {path} when no .s16le sidecar is present"
        ) from exc
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed decoding {path}: {detail or completed.returncode}")
    if len(completed.stdout) % 2:
        raise RuntimeError(f"ffmpeg returned odd PCM length for {path}")
    return completed.stdout


def _load_pcm(ogg_path: Path, *, sample_rate_hz: int) -> bytes:
    """Prefer the shipped s16le sidecar so runtime and CI need no ffmpeg."""

    sidecar = ogg_path.with_suffix(".s16le")
    if sidecar.is_file():
        pcm = sidecar.read_bytes()
        if not pcm or len(pcm) % 2:
            raise RuntimeError(f"{sidecar} must be non-empty 16-bit PCM")
        return pcm
    return _decode_ogg_pcm(ogg_path, sample_rate_hz=sample_rate_hz)


def load_catalogue(
    assets_root: Path | None = None,
    *,
    sample_rate_hz: int = ONLINE_SAMPLE_RATE_HZ,
) -> LabelledLineCatalogue:
    root = assets_root if assets_root is not None else DEFAULT_ASSETS_ROOT
    loaded: dict[str, LabelledLine] = {}
    for line_id, text, expected_aigc in LINE_SPECS:
        path = root / f"{line_id}.ogg"
        if not path.is_file():
            raise FileNotFoundError(f"labelled line asset missing: {path}")
        head = path.read_bytes()[:OPUS_TAGS_SCAN_BYTES]
        aigc = require_aigc_tags(head, content_id=line_id)
        if aigc["service_provider"] != expected_aigc["service_provider"]:
            raise ValueError(
                f"{line_id} SERVICE_PROVIDER must be {expected_aigc['service_provider']}"
            )
        pcm = _load_pcm(path, sample_rate_hz=sample_rate_hz)
        loaded[line_id] = LabelledLine(
            line_id=line_id,
            text=text,
            aigc=aigc,
            pcm_s16le=pcm,
            sample_rate_hz=sample_rate_hz,
            ogg_path=path,
        )
    return LabelledLineCatalogue(loaded)


def labelled_catalogue() -> LabelledLineCatalogue:
    with _catalogue_lock:
        if _catalogue_override is not None:
            return _catalogue_override
    return _default_catalogue()


@lru_cache(maxsize=1)
def _default_catalogue() -> LabelledLineCatalogue:
    return load_catalogue()


def override_labelled_catalogue(catalogue: LabelledLineCatalogue | None) -> None:
    """Tests install a fixture catalogue; pass ``None`` to restore the default."""

    global _catalogue_override
    with _catalogue_lock:
        _catalogue_override = catalogue
    if catalogue is None:
        _default_catalogue.cache_clear()


__all__ = [
    "DEFAULT_ASSETS_ROOT",
    "LINE_SPECS",
    "ONLINE_SAMPLE_RATE_HZ",
    "LabelledLine",
    "LabelledLineCatalogue",
    "labelled_catalogue",
    "load_catalogue",
    "override_labelled_catalogue",
]
