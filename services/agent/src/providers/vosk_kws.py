"""Optional Vosk grammar recognizer for short playback-control commands."""

from __future__ import annotations

import importlib
import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.agent.src.orchestration.interruption_guard import normalize_short

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoskKeywordSpotterConfig:
    enabled: bool = False
    model_dir: Path = Path("")
    keywords_file: Path = Path("")
    sample_rate: int = 16_000
    min_confidence: float = 0.65

    @classmethod
    def from_settings(cls, settings: Any) -> VoskKeywordSpotterConfig:
        return cls(
            enabled=bool(settings.miniprogram_kws_enabled),
            model_dir=Path(str(settings.miniprogram_kws_model_dir)),
            keywords_file=Path(str(settings.miniprogram_kws_keywords_file)),
            min_confidence=float(settings.miniprogram_kws_min_confidence),
        )


class VoskKeywordSpotter:
    """Small synchronous grammar decoder fed from the existing post-AEC PCM."""

    def __init__(self, config: VoskKeywordSpotterConfig) -> None:
        if not config.enabled:
            raise ValueError("keyword spotter is disabled")
        if config.sample_rate != 16_000:
            raise ValueError("keyword spotter requires 16 kHz PCM")
        if not 0.0 <= config.min_confidence <= 1.0:
            raise ValueError("keyword spotter confidence must be between 0 and 1")
        if not config.model_dir.is_dir() or not config.keywords_file.is_file():
            raise FileNotFoundError("keyword spotter model files are unavailable")
        keywords = self._load_keywords(config.keywords_file)
        if not keywords:
            raise ValueError("keyword spotter requires at least one keyword")
        vosk = importlib.import_module("vosk")
        set_log_level = getattr(vosk, "SetLogLevel", None)
        if callable(set_log_level):
            set_log_level(-1)
        self._config = config
        self._keywords = {normalize_short(keyword): keyword for keyword in keywords}
        self._grammar = json.dumps(
            [" ".join(normalize_short(keyword)) for keyword in keywords] + ["[unk]"],
            ensure_ascii=False,
        )
        self._model: Any = vosk.Model(str(config.model_dir))
        self._recognizer: Any = vosk.KaldiRecognizer(
            self._model,
            config.sample_rate,
            self._grammar,
        )
        self._recognizer.SetWords(True)
        self._results: list[dict[str, Any]] = []
        self._saw_pcm = False
        self._healthy = True

    @classmethod
    def try_create(
        cls,
        config: VoskKeywordSpotterConfig,
    ) -> VoskKeywordSpotter | None:
        if not config.enabled:
            return None
        try:
            return cls(config)
        except Exception:
            logger.warning("keyword spotter unavailable; ordinary ASR remains active", exc_info=True)
            return None

    @property
    def healthy(self) -> bool:
        return self._healthy

    def reset(self) -> None:
        if not self._healthy:
            return
        try:
            self._recognizer.Reset()
            self._results.clear()
            self._saw_pcm = False
        except Exception:
            self._disable()

    def feed_pcm(self, pcm: bytes) -> None:
        if not self._healthy or not pcm or len(pcm) % 2:
            return
        try:
            self._saw_pcm = True
            if self._recognizer.AcceptWaveform(pcm):
                self._results.append(self._parse_result(self._recognizer.Result()))
        except Exception:
            self._disable()

    def finish_utterance(self) -> str | None:
        """Return only an exact whole-utterance control phrase."""

        if not self._healthy or not self._saw_pcm:
            self.reset()
            return None
        try:
            self._results.append(self._parse_result(self._recognizer.FinalResult()))
            keyword = self._matched_keyword()
            self._recognizer.Reset()
            self._results.clear()
            self._saw_pcm = False
            return keyword
        except Exception:
            self._disable()
            return None

    @staticmethod
    def _load_keywords(path: Path) -> tuple[str, ...]:
        keywords: list[str] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            keyword = normalize_short(raw_line.partition("#")[0])
            if keyword and keyword not in keywords:
                keywords.append(keyword)
        return tuple(keywords)

    @staticmethod
    def _parse_result(raw_result: str) -> dict[str, Any]:
        parsed = json.loads(raw_result)
        if not isinstance(parsed, dict):
            raise ValueError("keyword spotter result must be an object")
        return parsed

    def _matched_keyword(self) -> str | None:
        text = normalize_short(
            "".join(str(result.get("text") or "") for result in self._results)
        )
        keyword = self._keywords.get(text)
        if keyword is None:
            return None
        words = [
            word
            for result in self._results
            for word in result.get("result", [])
            if isinstance(word, dict)
        ]
        if not words or any(str(word.get("word") or "") == "[unk]" for word in words):
            return None
        confidences = [
            float(word["conf"])
            for word in words
            if isinstance(word.get("conf"), int | float)
        ]
        if len(confidences) != len(words):
            return None
        if statistics.fmean(confidences) < self._config.min_confidence:
            return None
        return keyword

    def _disable(self) -> None:
        if self._healthy:
            logger.warning(
                "keyword spotter failed; ordinary ASR remains active",
                exc_info=True,
            )
        self._healthy = False
        self._results.clear()
        self._saw_pcm = False
