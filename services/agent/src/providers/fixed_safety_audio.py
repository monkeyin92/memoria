"""Pre-synthesized audio for the approved fixed-safety and daily-limit turns.

The four clips live under ``infra/voices/fixed_safety`` and were rendered once
with the StepFun ``stepaudio-2.5-tts`` ``wenrounansheng`` voice on 2026-10-23.  A
crisis turn must not depend on a live TTS connection: this module validates the
manifest and the labelled WAVs, then serves the stored PCM directly.  Any
validation failure degrades to the existing realtime provider with an alert
that names only the template id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from services.agent.src.providers.provider_errors import TTSUnavailableError

logger = logging.getLogger(__name__)

FixedSafetyAudioTemplateId = Literal[
    "crisis-self-v1",
    "victim-support-v1",
    "support-other-v1",
    "daily-limit",
]

DEFAULT_FIXED_SAFETY_AUDIO_DIR: Final = "infra/voices/fixed_safety"
FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ: Final = 24_000
FIXED_SAFETY_AUDIO_CHANNELS: Final = 1
FIXED_SAFETY_AUDIO_BITS_PER_SAMPLE: Final = 16
FIXED_SAFETY_AUDIO_VOICE: Final = "wenrounansheng"
FIXED_SAFETY_AUDIO_SYNTHESIZED_ON: Final = "2026-10-23"
FIXED_SAFETY_AUDIO_ENGINE: Final = "stepfun stepaudio-2.5-tts"
FIXED_SAFETY_AUDIO_ARTIST: Final = "StepFun stepaudio-2.5-tts / memoria"
FIXED_SAFETY_AUDIO_SOFTWARE: Final = "StepFun stepaudio-2.5-tts / memoria"
_MANIFEST_SCHEMA_VERSION: Final = 1
_FRAME_BYTES: Final = FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ * 2 * 20 // 1000
_SENTENCE_END = re.compile(r"(?<=[。！？!?])")


class FixedSafetyAudioError(ValueError):
    """The stored fixed-safety audio cannot be trusted."""


class FixedSafetyClipUnavailableError(TTSUnavailableError):
    """A reviewed crisis line has no approved clip to play.

    Crisis copy is reviewed verbatim and its approved artifact is the stored
    clip. A live engine speaking it would be runtime synthesis of child-facing
    safety audio, so this is the floor: the media adapter sees a TTS failure it
    can name and gives the device its floor back.
    """


def content_id_for(template_id: str) -> str:
    return f"memoria-fixed-safety-{template_id}-v1"


@dataclass(frozen=True, slots=True)
class FixedSafetySentence:
    text: str
    start_sample: int
    end_sample: int


@dataclass(frozen=True, slots=True)
class FixedSafetyAudioTemplate:
    template_id: str
    display: str
    spoken: str
    pcm: bytes | None = None
    audio_sha256: str = ""
    pcm_sha256: str = ""
    voice: str = FIXED_SAFETY_AUDIO_VOICE
    synthesized_on: str = FIXED_SAFETY_AUDIO_SYNTHESIZED_ON
    file: str = ""
    # Reviewed safety copy: the approved artifact is the stored clip, and a
    # live engine speaking it would be runtime synthesis of child-facing
    # safety audio. False only for copy that is safe to speak when the clip
    # is missing.
    clip_only: bool = True

    @property
    def playable(self) -> bool:
        return bool(self.pcm)

    @property
    def content_id(self) -> str:
        return content_id_for(self.template_id)

    def sentences(self) -> tuple[FixedSafetySentence, ...]:
        return sentence_spans(self.spoken, len(self.pcm or b"") // 2)


@dataclass(frozen=True, slots=True)
class FixedSafetyAudioIssue:
    template_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class FixedSafetyAudioCatalog:
    """Approved copy plus whichever stored clips passed validation."""

    templates: tuple[FixedSafetyAudioTemplate, ...]
    issues: tuple[FixedSafetyAudioIssue, ...] = ()
    directory: str = ""

    @property
    def by_id(self) -> Mapping[str, FixedSafetyAudioTemplate]:
        return {template.template_id: template for template in self.templates}

    def get(self, template_id: str) -> FixedSafetyAudioTemplate | None:
        return self.by_id.get(template_id)

    def match(self, text: str) -> FixedSafetyAudioTemplate | None:
        stripped = text.strip()
        if not stripped:
            return None
        for template in self.templates:
            if stripped in (template.spoken, template.display):
                return template
        return None

    def could_continue(self, prefix: str) -> bool:
        """True while ``prefix`` may still grow into an approved line."""

        if not prefix:
            return True
        return any(
            template.spoken.startswith(prefix) or template.display.startswith(prefix)
            for template in self.templates
        )

    def playable(self, template_id: str) -> bool:
        template = self.get(template_id)
        return bool(template is not None and template.playable)

    @classmethod
    def load(
        cls,
        directory: str | Path,
        *,
        enabled: bool = True,
    ) -> FixedSafetyAudioCatalog:
        path = Path(directory)
        approved = approved_templates()
        if not enabled:
            return cls(approved, directory=str(path))
        if not path.is_dir():
            issue = FixedSafetyAudioIssue("*", "directory_missing")
            _log_issue(issue)
            return cls(approved, (issue,), str(path))
        manifest_path = path / "manifest.json"
        try:
            manifest = _load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            issue = FixedSafetyAudioIssue("*", f"manifest_{type(exc).__name__}")
            _log_issue(issue)
            return cls(approved, (issue,), str(path))
        try:
            entries = _manifest_entries(manifest)
        except ValueError as exc:
            issue = FixedSafetyAudioIssue("*", f"manifest_{type(exc).__name__}:{exc}")
            _log_issue(issue)
            return cls(approved, (issue,), str(path))
        validated: list[FixedSafetyAudioTemplate] = []
        issues: list[FixedSafetyAudioIssue] = []
        for template in approved:
            entry = entries.get(template.template_id)
            if entry is None:
                issue = FixedSafetyAudioIssue(template.template_id, "manifest_entry_missing")
                issues.append(issue)
                _log_issue(issue)
                validated.append(template)
                continue
            try:
                validated.append(_validated_template(template, entry, manifest, path))
            except (OSError, ValueError) as exc:
                issue = FixedSafetyAudioIssue(
                    template.template_id,
                    f"audio_invalid_{type(exc).__name__}:{exc}",
                )
                issues.append(issue)
                _log_issue(issue)
                validated.append(template)
        return cls(tuple(validated), tuple(issues), str(path))


def _log_issue(issue: FixedSafetyAudioIssue) -> None:
    logger.error(
        "fixed_safety_audio_manifest_invalid template_id=%s reason=%s",
        issue.template_id,
        issue.reason,
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FixedSafetyAudioError("manifest root must be an object")
    schema = payload.get("schema_version")
    if schema != _MANIFEST_SCHEMA_VERSION:
        raise FixedSafetyAudioError("unsupported manifest schema_version")
    if payload.get("sample_rate_hz") != FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ:
        raise FixedSafetyAudioError("manifest sample rate must be 24000")
    if payload.get("channels") != FIXED_SAFETY_AUDIO_CHANNELS:
        raise FixedSafetyAudioError("manifest channels must be 1")
    if payload.get("bits_per_sample") != FIXED_SAFETY_AUDIO_BITS_PER_SAMPLE:
        raise FixedSafetyAudioError("manifest bits_per_sample must be 16")
    return payload


def _manifest_entries(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = manifest.get("templates")
    if not isinstance(raw, list):
        raise FixedSafetyAudioError("manifest templates must be a list")
    entries: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise FixedSafetyAudioError("manifest template entry must be an object")
        template_id = item.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise FixedSafetyAudioError("manifest template_id must be a string")
        if template_id in entries:
            raise FixedSafetyAudioError("duplicate manifest template_id")
        entries[template_id] = item
    return entries


def _validated_template(
    template: FixedSafetyAudioTemplate,
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any],
    directory: Path,
) -> FixedSafetyAudioTemplate:
    if entry.get("template_id") != template.template_id:
        raise FixedSafetyAudioError("template id mismatch")
    if not _is_sha256(entry.get("spoken_sha256")):
        raise FixedSafetyAudioError("spoken_sha256 missing")
    if entry["spoken_sha256"] != spoken_sha256(template.spoken):
        raise FixedSafetyAudioError("spoken_sha256 mismatch")
    if not _is_sha256(entry.get("audio_sha256")):
        raise FixedSafetyAudioError("audio_sha256 missing")
    if not _is_sha256(entry.get("pcm_sha256")):
        raise FixedSafetyAudioError("pcm_sha256 missing")
    voice = entry.get("voice")
    if voice != FIXED_SAFETY_AUDIO_VOICE or voice != manifest.get("voice"):
        raise FixedSafetyAudioError("voice mismatch")
    synthesized_on = entry.get("synthesized_on")
    if synthesized_on != FIXED_SAFETY_AUDIO_SYNTHESIZED_ON or synthesized_on != manifest.get(
        "synthesized_on"
    ):
        raise FixedSafetyAudioError("synthesized_on mismatch")
    if entry.get("content_id") != content_id_for(template.template_id):
        raise FixedSafetyAudioError("content_id mismatch")
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name or "/" in file_name:
        raise FixedSafetyAudioError("file must be a plain name")
    path = directory / file_name
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["audio_sha256"]:
        raise FixedSafetyAudioError("audio_sha256 mismatch")
    parsed = parse_wav_bytes(raw)
    if hashlib.sha256(parsed.pcm).hexdigest() != entry["pcm_sha256"]:
        raise FixedSafetyAudioError("pcm_sha256 mismatch")
    _validate_wav_format(parsed)
    if parsed.info.get("ICMT", "") != content_id_for(template.template_id):
        raise FixedSafetyAudioError("implicit AIGC label missing")
    if (
        parsed.info.get("IART") != FIXED_SAFETY_AUDIO_ARTIST
        or parsed.info.get("ISFT") != FIXED_SAFETY_AUDIO_SOFTWARE
        or parsed.info.get("ICRD") != FIXED_SAFETY_AUDIO_SYNTHESIZED_ON
    ):
        raise FixedSafetyAudioError("implicit AIGC label mismatch")
    return replace(
        template,
        pcm=parsed.pcm,
        audio_sha256=entry["audio_sha256"],
        pcm_sha256=entry["pcm_sha256"],
        voice=str(voice),
        synthesized_on=str(synthesized_on),
        file=file_name,
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def spoken_sha256(spoken: str) -> str:
    return hashlib.sha256(spoken.encode("utf-8")).hexdigest()


def approved_templates() -> tuple[FixedSafetyAudioTemplate, ...]:
    """The reviewed copy from code, independent of any stored file."""

    from services.agent.src.prompts import MINOR_DAILY_LIMIT_PHRASE
    from services.common.fixed_safety_scripts import (
        CRISIS_SELF_SCRIPT,
        SUPPORT_RELAY_SCRIPT,
        SUPPORT_VICTIM_SCRIPT,
    )

    return (
        FixedSafetyAudioTemplate(
            "crisis-self-v1", CRISIS_SELF_SCRIPT.display, CRISIS_SELF_SCRIPT.spoken
        ),
        FixedSafetyAudioTemplate(
            "victim-support-v1",
            SUPPORT_VICTIM_SCRIPT.display,
            SUPPORT_VICTIM_SCRIPT.spoken,
        ),
        FixedSafetyAudioTemplate(
            "support-other-v1",
            SUPPORT_RELAY_SCRIPT.display,
            SUPPORT_RELAY_SCRIPT.spoken,
        ),
        FixedSafetyAudioTemplate(
            "daily-limit",
            MINOR_DAILY_LIMIT_PHRASE,
            MINOR_DAILY_LIMIT_PHRASE,
            clip_only=False,
        ),
    )


@lru_cache(maxsize=8)
def load_fixed_safety_audio_catalog(
    directory: str,
    *,
    enabled: bool = True,
) -> FixedSafetyAudioCatalog:
    return FixedSafetyAudioCatalog.load(directory, enabled=enabled)


@lru_cache(maxsize=1)
def shipped_approved_catalog() -> FixedSafetyAudioCatalog:
    """The four clips that ship with the image, independent of deployment config.

    This is the last rung of the crisis chain. If a deployment's
    ``FIXED_SAFETY_AUDIO_DIR`` is stale or disabled, these files still carry
    the reviewed copy and their sha256 manifest, so the child hears the
    approved line without any live provider.
    """

    directory = Path(__file__).resolve().parents[4] / DEFAULT_FIXED_SAFETY_AUDIO_DIR
    return FixedSafetyAudioCatalog.load(directory)


def fixed_safety_audio_directory(settings: object) -> str:
    value = getattr(settings, "fixed_safety_audio_dir", None)
    raw = (
        value.strip()
        if isinstance(value, str) and value.strip()
        else (DEFAULT_FIXED_SAFETY_AUDIO_DIR)
    )
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or candidate.exists():
        return str(candidate)
    # A service process may not run from the repository root; the image does,
    # but a local bridge launched from elsewhere must still find the clips.
    repository_root = Path(__file__).resolve().parents[4]
    return str(repository_root / candidate)


def fixed_safety_audio_enabled(settings: object) -> bool:
    # AgentSettings defaults this on; a bare test double keeps the old factory
    # shape unless it opts in explicitly.
    return bool(getattr(settings, "fixed_safety_audio_enabled", False))


def catalog_for_settings(settings: object) -> FixedSafetyAudioCatalog:
    return load_fixed_safety_audio_catalog(
        str(Path(fixed_safety_audio_directory(settings))),
        enabled=fixed_safety_audio_enabled(settings),
    )


def verify_fixed_safety_audio_directory(
    directory: str | Path = DEFAULT_FIXED_SAFETY_AUDIO_DIR,
) -> None:
    """Raise when any approved clip is missing or invalid (image gate)."""

    catalog = FixedSafetyAudioCatalog.load(directory)
    for template in catalog.templates:
        if not template.playable:
            reason = next(
                (
                    issue.reason
                    for issue in catalog.issues
                    if issue.template_id == template.template_id
                ),
                "not_playable",
            )
            raise FixedSafetyAudioError(
                f"fixed safety audio {template.template_id} is not playable: {reason}"
            )


# WAV / RIFF ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedWav:
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    audio_format: int
    pcm: bytes
    info: Mapping[str, str]


def _u32(value: bytes) -> int:
    return int(struct.unpack("<I", value)[0])


def parse_wav_bytes(raw: bytes) -> ParsedWav:
    chunks = _riff_chunks(raw)
    fmt = next((payload for chunk_id, payload in chunks if chunk_id == b"fmt "), None)
    pcm = next((payload for chunk_id, payload in chunks if chunk_id == b"data"), None)
    if fmt is None or len(fmt) < 16 or pcm is None:
        raise FixedSafetyAudioError("WAV must carry fmt and data chunks")
    audio_format, channels, sample_rate, _byte_rate, _block_align, bits = struct.unpack(
        "<HHIIHH", fmt[:16]
    )
    info_payload = next(
        (payload for chunk_id, payload in chunks if chunk_id == b"LIST" and payload[:4] == b"INFO"),
        b"",
    )
    return ParsedWav(
        sample_rate_hz=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        audio_format=audio_format,
        pcm=pcm,
        info=_parse_info(info_payload),
    )


def _riff_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise FixedSafetyAudioError("not a RIFF/WAVE file")
    if _u32(raw[4:8]) + 8 != len(raw):
        raise FixedSafetyAudioError("RIFF size does not match the file")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        size = _u32(raw[offset + 4 : offset + 8])
        start = offset + 8
        end = start + size
        if end > len(raw):
            raise FixedSafetyAudioError("truncated RIFF chunk")
        chunks.append((chunk_id, raw[start:end]))
        offset = end + (size & 1)
    if offset != len(raw):
        raise FixedSafetyAudioError("trailing RIFF bytes")
    return chunks


def _parse_info(payload: bytes) -> dict[str, str]:
    if len(payload) < 4 or payload[:4] != b"INFO":
        return {}
    values: dict[str, str] = {}
    offset = 4
    while offset + 8 <= len(payload):
        key = payload[offset : offset + 4].decode("ascii", "replace")
        size = _u32(payload[offset + 4 : offset + 8])
        start = offset + 8
        end = start + size
        if end > len(payload):
            break
        values[key] = payload[start:end].rstrip(b"\x00").decode("utf-8", "replace")
        offset = end + (size & 1)
    return values


def _info_payload(fields: Mapping[str, str]) -> bytes:
    payload = bytearray(b"INFO")
    for key, value in fields.items():
        encoded = value.encode("utf-8") + b"\x00"
        payload.extend(key.encode("ascii"))
        payload.extend(struct.pack("<I", len(encoded)))
        payload.extend(encoded)
        if len(encoded) & 1:
            payload.append(0)
    return bytes(payload)


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    out = bytearray(chunk_id)
    out.extend(struct.pack("<I", len(payload)))
    out.extend(payload)
    if len(payload) & 1:
        out.append(0)
    return bytes(out)


def _validate_wav_format(parsed: ParsedWav) -> None:
    if parsed.audio_format != 1:
        raise FixedSafetyAudioError("WAV must be integer PCM")
    if parsed.channels != FIXED_SAFETY_AUDIO_CHANNELS:
        raise FixedSafetyAudioError("WAV must be mono")
    if parsed.bits_per_sample != FIXED_SAFETY_AUDIO_BITS_PER_SAMPLE:
        raise FixedSafetyAudioError("WAV must be 16-bit")
    if parsed.sample_rate_hz != FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ:
        raise FixedSafetyAudioError("WAV must be 24000 Hz")
    if not parsed.pcm or len(parsed.pcm) % 2:
        raise FixedSafetyAudioError("WAV must contain non-empty 16-bit PCM")


def label_wav_with_aigc_info(
    path: Path,
    *,
    template_id: str,
    voice: str = FIXED_SAFETY_AUDIO_VOICE,
    synthesized_on: str = FIXED_SAFETY_AUDIO_SYNTHESIZED_ON,
) -> tuple[str, str]:
    """Rewrite ``path`` with a compliant RIFF INFO label; return file/PCM hashes.

    The ``data`` chunk payload is copied byte for byte.  Only the RIFF size and
    the ``LIST/INFO`` chunk change, so the playable PCM is identical.
    """

    raw = path.read_bytes()
    chunks = _riff_chunks(raw)
    parsed = parse_wav_bytes(raw)
    _validate_wav_format(parsed)
    data_payload = next(payload for chunk_id, payload in chunks if chunk_id == b"data")
    info = _info_payload(
        {
            "IART": FIXED_SAFETY_AUDIO_ARTIST,
            "ISFT": FIXED_SAFETY_AUDIO_SOFTWARE,
            "ICMT": content_id_for(template_id),
            "ICRD": synthesized_on,
            "IENG": voice,
        }
    )
    rebuilt: list[bytes] = []
    inserted = False
    for chunk_id, payload in chunks:
        if chunk_id == b"LIST" and payload[:4] == b"INFO":
            continue
        rebuilt.append(_chunk(chunk_id, payload))
        if chunk_id == b"fmt " and not inserted:
            rebuilt.append(_chunk(b"LIST", info))
            inserted = True
    if not inserted:
        rebuilt.insert(0, _chunk(b"LIST", info))
    body = b"WAVE" + b"".join(rebuilt)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    new_raw = path.read_bytes()
    new_parsed = parse_wav_bytes(new_raw)
    if new_parsed.pcm != data_payload or new_parsed.pcm != parsed.pcm:
        raise FixedSafetyAudioError("labelling changed the PCM data chunk")
    return (
        hashlib.sha256(new_raw).hexdigest(),
        hashlib.sha256(new_parsed.pcm).hexdigest(),
    )


def write_manifest(
    directory: Path,
    *,
    voice: str = FIXED_SAFETY_AUDIO_VOICE,
    synthesized_on: str = FIXED_SAFETY_AUDIO_SYNTHESIZED_ON,
    engine: str = FIXED_SAFETY_AUDIO_ENGINE,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for template in approved_templates():
        file_name = f"{template.template_id}.wav"
        path = directory / file_name
        audio_sha256, pcm_sha256 = label_wav_with_aigc_info(
            path,
            template_id=template.template_id,
            voice=voice,
            synthesized_on=synthesized_on,
        )
        entries.append(
            {
                "template_id": template.template_id,
                "file": file_name,
                "content_id": content_id_for(template.template_id),
                "spoken_sha256": spoken_sha256(template.spoken),
                "audio_sha256": audio_sha256,
                "pcm_sha256": pcm_sha256,
                "voice": voice,
                "synthesized_on": synthesized_on,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "engine": engine,
        "voice": voice,
        "synthesized_on": synthesized_on,
        "sample_rate_hz": FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ,
        "channels": FIXED_SAFETY_AUDIO_CHANNELS,
        "bits_per_sample": FIXED_SAFETY_AUDIO_BITS_PER_SAMPLE,
        "templates": entries,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


# Sentence estimate ---------------------------------------------------------


def sentence_spans(spoken: str, total_samples: int) -> tuple[FixedSafetySentence, ...]:
    """Split at Chinese sentence punctuation and allocate time by text length.

    The WAV carries no per-sentence timestamps, so a proportional estimate is
    the honest bound: it can say which whole sentences finished, never claim a
    partial sentence was heard.
    """

    if total_samples < 0:
        raise ValueError("total_samples must be non-negative")
    sentences = [part for part in _SENTENCE_END.split(spoken) if part]
    if not sentences:
        return ()
    total_chars = sum(len(sentence) for sentence in sentences)
    start = 0
    consumed = 0
    spans: list[FixedSafetySentence] = []
    for index, sentence in enumerate(sentences):
        consumed += len(sentence)
        if index + 1 == len(sentences):
            end = total_samples
        else:
            end = round(total_samples * consumed / total_chars)
        spans.append(FixedSafetySentence(sentence, start, end))
        start = end
    return tuple(spans)


def heard_complete_by_sentence(
    spoken: str,
    *,
    played_samples: int,
    total_samples: int,
) -> bool:
    """True only when the final approved sentence finished playing."""

    if total_samples <= 0:
        return False
    spans = sentence_spans(spoken, total_samples)
    if not spans:
        return False
    return played_samples >= spans[-1].end_sample


# Wrapper -------------------------------------------------------------------


class FixedSafetyTTS:
    """Serve an approved clip from disk, otherwise use the bound realtime TTS."""

    __slots__ = ("_catalog", "_inner")

    def __init__(self, inner: Any, catalog: FixedSafetyAudioCatalog) -> None:
        self._inner = inner
        self._catalog = catalog

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def fixed_safety_catalog(self) -> FixedSafetyAudioCatalog:
        return self._catalog

    def stream(self, **kwargs: Any) -> FixedSafetySpeechStream:
        return FixedSafetySpeechStream(self._catalog, self._inner, kwargs)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if callable(close):
            await close()


class FixedSafetySpeechStream:
    """One TTS stream that can still hand the whole text to the live provider."""

    __slots__ = (
        "_buffer",
        "_catalog",
        "_clip_error",
        "_closed",
        "_decided",
        "_inner",
        "_inner_stream",
        "_kwargs",
        "_matched",
        "_offset",
        "_passthrough",
    )

    def __init__(
        self,
        catalog: FixedSafetyAudioCatalog,
        inner: Any,
        kwargs: Mapping[str, Any],
    ) -> None:
        self._catalog = catalog
        self._inner = inner
        self._kwargs = dict(kwargs)
        self._inner_stream: Any = None
        self._buffer = ""
        self._matched: FixedSafetyAudioTemplate | None = None
        self._clip_error: FixedSafetyClipUnavailableError | None = None
        self._offset = 0
        self._passthrough = False
        self._closed = False
        self._decided = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        if self._inner_stream is None:
            raise AttributeError(name)
        return getattr(self._inner_stream, name)

    def push_text(self, text: str) -> None:
        if self._closed or not text:
            return
        if self._matched is not None:
            self._to_inner(text)
            return
        if self._passthrough or self._inner_stream is not None:
            self._to_inner(text)
            return
        self._buffer += text
        template = self._catalog.match(self._buffer)
        if template is not None:
            self._matched_template(template)
            return
        if not self._catalog.could_continue(self._buffer):
            self._fallback(self._buffer)

    def _matched_template(self, template: FixedSafetyAudioTemplate) -> None:
        if template.playable:
            logger.info(
                "fixed_safety_audio_playout template_id=%s source=prerecorded",
                template.template_id,
            )
            self._matched = template
            self._decided.set()
            return
        if template.clip_only:
            # Reviewed safety copy whose approved artifact is the clip. The
            # shipped image carries a second copy, and that is the last rung.
            # A live engine is never a candidate: speaking it would synthesize
            # child-facing safety audio that no one approved.
            shipped = shipped_approved_catalog().get(template.template_id)
            if shipped is not None and shipped.playable:
                logger.warning(
                    "fixed_safety_audio_playout template_id=%s source=shipped_clip",
                    template.template_id,
                )
                self._matched = shipped
                self._decided.set()
                return
            logger.error(
                "fixed_safety_audio_playout template_id=%s source=none reason=no_approved_clip",
                template.template_id,
            )
            self._clip_error = FixedSafetyClipUnavailableError(
                f"no approved clip for fixed safety template {template.template_id}"
            )
            self._decided.set()
            return
        # Approved copy with no trusted clip that is still safe to speak at
        # runtime: the live engine must receive the reviewed 幺/幺/幺 readings,
        # never ASCII digits.
        self._fallback(template.spoken)

    def end_input(self) -> None:
        if self._closed:
            return
        if self._inner_stream is None and self._matched is None and self._clip_error is None:
            template = self._catalog.match(self._buffer)
            if template is not None and template.clip_only:
                self._matched_template(template)
            else:
                text = template.spoken if template is not None else self._buffer
                self._buffer = ""
                if text:
                    self._to_inner(text)
        self._buffer = ""
        if self._inner_stream is not None:
            self._inner_stream.end_input()
        self._decided.set()

    async def aclose(self) -> None:
        self._closed = True
        self._decided.set()
        close = getattr(self._inner_stream, "aclose", None)
        if callable(close):
            await close()

    def __aiter__(self) -> FixedSafetySpeechStream:
        return self

    async def __anext__(self) -> Any:
        await self._decided.wait()
        if self._clip_error is not None:
            raise self._clip_error
        if self._matched is not None:
            pcm = self._matched.pcm or b""
            if self._offset < len(pcm):
                chunk = pcm[self._offset : self._offset + _FRAME_BYTES]
                self._offset += len(chunk)
                return chunk
            self._matched = None
            if self._inner_stream is None:
                raise StopAsyncIteration
        if self._inner_stream is None:
            raise StopAsyncIteration
        return await self._inner_stream.__anext__()

    def _to_inner(self, text: str) -> None:
        if self._inner_stream is None:
            self._inner_stream = self._inner.stream(**self._kwargs)
        self._inner_stream.push_text(text)
        self._passthrough = True

    def _fallback(self, text: str) -> None:
        self._buffer = ""
        self._to_inner(text)
        self._decided.set()


def wrap_fixed_safety_tts(inner: Any, settings: object) -> Any:
    """Always wrap. ``FIXED_SAFETY_AUDIO_ENABLED=false`` only drops the
    deployment's own clip directory; the shipped clips still guard every
    reviewed safety line, and crisis copy must never reach a live engine."""

    return FixedSafetyTTS(inner, catalog_for_settings(settings))


__all__ = [
    "DEFAULT_FIXED_SAFETY_AUDIO_DIR",
    "FIXED_SAFETY_AUDIO_ARTIST",
    "FIXED_SAFETY_AUDIO_BITS_PER_SAMPLE",
    "FIXED_SAFETY_AUDIO_CHANNELS",
    "FIXED_SAFETY_AUDIO_ENGINE",
    "FIXED_SAFETY_AUDIO_SAMPLE_RATE_HZ",
    "FIXED_SAFETY_AUDIO_SOFTWARE",
    "FIXED_SAFETY_AUDIO_SYNTHESIZED_ON",
    "FIXED_SAFETY_AUDIO_VOICE",
    "FixedSafetyAudioCatalog",
    "FixedSafetyAudioError",
    "FixedSafetyAudioIssue",
    "FixedSafetyAudioTemplate",
    "FixedSafetyClipUnavailableError",
    "FixedSafetySentence",
    "FixedSafetySpeechStream",
    "FixedSafetyTTS",
    "ParsedWav",
    "approved_templates",
    "catalog_for_settings",
    "content_id_for",
    "fixed_safety_audio_directory",
    "fixed_safety_audio_enabled",
    "heard_complete_by_sentence",
    "label_wav_with_aigc_info",
    "load_fixed_safety_audio_catalog",
    "parse_wav_bytes",
    "sentence_spans",
    "shipped_approved_catalog",
    "spoken_sha256",
    "verify_fixed_safety_audio_directory",
    "wrap_fixed_safety_tts",
    "write_manifest",
]
