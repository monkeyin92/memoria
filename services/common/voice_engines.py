"""Which engines a child hears and is heard by.

One module owns both literals. Agent settings and control settings import
them, so the two services cannot disagree about the legal values and a test
can assert the annotations are the same object. Adding an engine means
editing this file and every ``match`` that consumes it, which ``assert_never``
makes a type error.

``ASR_PROVIDER`` is the selector. ``volc`` is the existing Volcengine failover
chain and ``funasr`` is the plain DashScope session. ``ASR_FAILOVER_ENABLED``
and ``ASR_PRIMARY`` used to spell the same fact and are rejected, because two
keys for one choice is how the shell copy of the resolver drifted from the
Python one. TTS has exactly one engine. StepFun is no longer selectable, and
the keys that used to select it are rejected with the replacement named.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, Self, get_args

ASREngine = Literal["funasr", "volc"]
"""``funasr`` is the plain DashScope session. ``volc`` is the Volcengine failover chain."""

TTSEngine = Literal["doubao"]
"""Only Doubao. ``TTS_PROVIDER=stepfun_primary`` fails settings validation."""

ASR_ENGINES: Final[tuple[ASREngine, ...]] = get_args(ASREngine)
TTS_ENGINES: Final[tuple[TTSEngine, ...]] = get_args(TTSEngine)

SMOKE_LINE_PREFIX: Final = "voice_engines"

# Keys whose meaning moved. A present key is a hard error that names the
# replacement, so a release cannot silently drop one and boot the wrong
# engine. Two kinds of key stay out: ones a service still reads, and ones that
# select nothing. STEPFUN_TTS_ENABLED is both. It is inert under
# TTS_PROVIDER=doubao, and the previous image still reads it, so an env-level
# rollback needs the key to survive a split until the StepFun files are deleted.
REPLACED_ENV_KEYS: Final[Mapping[str, str]] = {
    "STEPFUN_TTS_FIXED_SAFETY_PRIMARY_FIRST": "crisis lines never use live TTS; delete it",
    "ASR_FAILOVER_ENABLED": "use ASR_PROVIDER=volc or ASR_PROVIDER=funasr",
    "ASR_PRIMARY": "implied by ASR_PROVIDER=volc; delete it",
    "ASR_FAILOVER_FIRST_RESULT_TIMEOUT_MS": (
        "use ASR_FAILOVER_FINAL_RESULT_TIMEOUT_MS, measured from finalize"
    ),
    "VOLC_ASR_API_KEY": "Volc ASR uses VOLC_ASR_APP_ID + VOLC_ASR_ACCESS_TOKEN",
    "ASR_PROVIDER=none": "ASR_PROVIDER=none no longer means anything; use funasr or volc",
}

# Values a selector used to accept. A key can stay legal while its old value
# stops selecting anything, and settings validation would only fail once the
# service starts, after the release has already been cut.
REPLACED_ENV_VALUES: Final[Mapping[tuple[str, str], str]] = {
    ("TTS_PROVIDER", "stepfun_primary"): (
        "TTS_PROVIDER=stepfun_primary is no longer selectable; use TTS_PROVIDER=doubao"
    ),
}

REPLACED_ENV_PREFIXES: Final[Mapping[str, str]] = {
    "DOUBAO_ASR_": "Volc ASR uses VOLC_ASR_APP_ID + VOLC_ASR_ACCESS_TOKEN",
}

class _HasEngines(Protocol):
    asr_provider: ASREngine
    tts_provider: TTSEngine


@dataclass(frozen=True, slots=True)
class VoiceEngines:
    """The engines one process was configured with. Equality is the check."""

    asr: ASREngine
    tts: TTSEngine

    @classmethod
    def from_settings(cls, settings: _HasEngines) -> Self:
        """Both agent and control settings satisfy this. No env read here."""
        return cls(asr=settings.asr_provider, tts=settings.tts_provider)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """For scripts that post evidence without building full settings.

        The libraries are the same defaults the settings classes carry, so a
        short-lived container that omits a selector reports what a service
        built from the same env would run. An unknown value raises here rather
        than posting evidence for an engine no process can build.
        """
        asr = (env.get("ASR_PROVIDER", "").strip().lower() or "funasr")
        tts = (env.get("TTS_PROVIDER", "").strip().lower() or "doubao")
        if asr not in ASR_ENGINES:
            raise ValueError(f"unknown ASR_PROVIDER {asr!r}")
        if tts not in TTS_ENGINES:
            raise ValueError(f"unknown TTS_PROVIDER {tts!r}")
        return cls(asr=asr, tts=tts)

    def smoke_line(self) -> str:
        """The only producer of the wire form the smoke prints and readiness parses."""
        return f"{SMOKE_LINE_PREFIX} asr={self.asr} tts={self.tts}"

    @classmethod
    def parse_smoke_line(cls, line: str) -> Self:
        """Inverse of ``smoke_line``. Rejects an unknown engine or an extra field."""
        parts = line.split()
        if len(parts) != 3 or parts[0] != SMOKE_LINE_PREFIX:
            raise ValueError(f"not a voice_engines line: {line!r}")
        fields = dict(_pair(part) for part in parts[1:])
        if set(fields) != {"asr", "tts"}:
            raise ValueError(f"voice_engines line has unexpected fields: {sorted(fields)}")
        asr = fields["asr"]
        tts = fields["tts"]
        if asr not in ASR_ENGINES:
            raise ValueError(f"unknown ASR engine {asr!r}")
        if tts not in TTS_ENGINES:
            raise ValueError(f"unknown TTS engine {tts!r}")
        return cls(asr=asr, tts=tts)


def _pair(part: str) -> tuple[str, str]:
    key, separator, value = part.partition("=")
    if not separator or not key or not value:
        raise ValueError(f"voice_engines field {part!r} is not key=value")
    return key, value


def reject_replaced_keys(env: Mapping[str, str]) -> None:
    """Raise ``ValueError`` naming every stale key and the key that replaces it.

    Called by the env splitter, by release preparation before its silent
    filter, and by the bridge at boot. Pure, and it never includes a value.
    ``ASR_PROVIDER=none`` is matched on the value because the key itself stays
    legal for ``funasr`` and ``volc``. A live ``STEPFUN_TTS_ENABLED`` is not a
    replaced key: it is inert under ``TTS_PROVIDER=doubao`` and the previous
    image still reads it, so an env-level rollback needs it to survive a split.
    """
    problems: list[str] = []
    for key, reason in REPLACED_ENV_KEYS.items():
        if key == "ASR_PROVIDER=none":
            if env.get("ASR_PROVIDER", "").strip().lower() == "none":
                problems.append(f"ASR_PROVIDER=none: {reason}")
            continue
        if key in env:
            problems.append(f"{key}: {reason}")
    for key in env:
        for prefix, reason in REPLACED_ENV_PREFIXES.items():
            if key.startswith(prefix):
                problems.append(f"{key}: {reason}")
    for (key, retired), reason in REPLACED_ENV_VALUES.items():
        if env.get(key, "").strip().lower() == retired:
            problems.append(reason)
    if problems:
        raise ValueError("replaced voice engine keys: " + "; ".join(sorted(problems)))
