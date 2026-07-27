from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from services.agent.src.providers.vosk_kws import (
    VoskKeywordSpotter,
    VoskKeywordSpotterConfig,
)


class _FakeRecognizer:
    def __init__(self) -> None:
        self.accepted: list[bytes] = []
        self.accept_result = False
        self.result = {"text": ""}
        self.final_result = {"text": ""}
        self.reset_count = 0
        self.words_enabled = False

    def SetWords(self, enabled: bool) -> None:
        self.words_enabled = enabled

    def AcceptWaveform(self, pcm: bytes) -> bool:
        self.accepted.append(pcm)
        return self.accept_result

    def Result(self) -> str:
        return json.dumps(self.result, ensure_ascii=False)

    def FinalResult(self) -> str:
        return json.dumps(self.final_result, ensure_ascii=False)

    def Reset(self) -> None:
        self.reset_count += 1


def _module(recognizer: _FakeRecognizer) -> SimpleNamespace:
    return SimpleNamespace(
        SetLogLevel=lambda _level: None,
        Model=lambda path: SimpleNamespace(path=path),
        KaldiRecognizer=lambda model, sample_rate, grammar: (
            setattr(recognizer, "model", model)
            or setattr(recognizer, "sample_rate", sample_rate)
            or setattr(recognizer, "grammar", grammar)
            or recognizer
        ),
    )


def _config(
    tmp_path: Path,
    *,
    min_confidence: float = 0.65,
) -> VoskKeywordSpotterConfig:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    keywords_file = tmp_path / "keywords.txt"
    keywords_file.write_text("停一下\n等一下\n别说了\n", encoding="utf-8")
    return VoskKeywordSpotterConfig(
        enabled=True,
        model_dir=model_dir,
        keywords_file=keywords_file,
        min_confidence=min_confidence,
    )


def test_vosk_keyword_spotter_accepts_only_complete_exact_control_phrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _FakeRecognizer()
    recognizer.final_result = {
        "text": "停 一 下",
        "result": [
            {"word": "停", "conf": 0.82},
            {"word": "一", "conf": 0.96},
            {"word": "下", "conf": 0.98},
        ],
    }
    monkeypatch.setattr(
        "services.agent.src.providers.vosk_kws.importlib.import_module",
        lambda name: _module(recognizer) if name == "vosk" else None,
    )
    spotter = VoskKeywordSpotter(_config(tmp_path))
    pcm = b"\x00\x40" * 320

    spotter.feed_pcm(pcm)
    hit = spotter.finish_utterance()

    assert hit == "停一下"
    assert recognizer.accepted == [pcm]
    assert recognizer.sample_rate == 16_000
    assert recognizer.words_enabled is True
    assert "停 一 下" in recognizer.grammar
    assert "[unk]" in recognizer.grammar
    assert recognizer.reset_count == 1


def test_vosk_keyword_spotter_rejects_content_after_control_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _FakeRecognizer()
    recognizer.accept_result = True
    recognizer.result = {
        "text": "等 一 下",
        "result": [
            {"word": "等", "conf": 1.0},
            {"word": "一", "conf": 1.0},
            {"word": "下", "conf": 1.0},
        ],
    }
    recognizer.final_result = {
        "text": "[unk]",
        "result": [{"word": "[unk]", "conf": 0.9}],
    }
    monkeypatch.setattr(
        "services.agent.src.providers.vosk_kws.importlib.import_module",
        lambda name: _module(recognizer) if name == "vosk" else None,
    )
    spotter = VoskKeywordSpotter(_config(tmp_path))

    spotter.feed_pcm(b"\x00\x20" * 320)

    assert spotter.finish_utterance() is None


def test_vosk_keyword_spotter_rejects_low_confidence_exact_phrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _FakeRecognizer()
    recognizer.final_result = {
        "text": "别 说 了",
        "result": [
            {"word": "别", "conf": 0.4},
            {"word": "说", "conf": 0.5},
            {"word": "了", "conf": 0.6},
        ],
    }
    monkeypatch.setattr(
        "services.agent.src.providers.vosk_kws.importlib.import_module",
        lambda name: _module(recognizer) if name == "vosk" else None,
    )
    spotter = VoskKeywordSpotter(_config(tmp_path, min_confidence=0.65))

    spotter.feed_pcm(b"\x00\x20" * 320)

    assert spotter.finish_utterance() is None


def test_vosk_keyword_spotter_fails_closed_when_model_files_are_missing(
    tmp_path: Path,
) -> None:
    config = VoskKeywordSpotterConfig(
        enabled=True,
        model_dir=tmp_path / "missing-model",
        keywords_file=tmp_path / "keywords.txt",
    )

    assert VoskKeywordSpotter.try_create(config) is None
