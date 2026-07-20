#!/usr/bin/env python3
"""Run the pinned official CAM++ examples through the production ONNX engine."""

from __future__ import annotations

import argparse
import json
import math
import wave
from pathlib import Path

from services.speaker_model.engine import CampPlusOnnxEngine, SpeakerModelEmbedding


def _read_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"official smoke WAV must be mono PCM16: {path}")
        sample_rate = source.getframerate()
        return source.readframes(source.getnframes()), sample_rate


def _similarity(left: SpeakerModelEmbedding, right: SpeakerModelEmbedding) -> float:
    return sum(a * b for a, b in zip(left.vector, right.vector, strict=True)) / math.sqrt(
        sum(value * value for value in left.vector)
        * sum(value * value for value in right.vector)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--speaker1-a", type=Path, required=True)
    parser.add_argument("--speaker1-b", type=Path, required=True)
    parser.add_argument("--speaker2-a", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.6)
    args = parser.parse_args()

    engine = CampPlusOnnxEngine(
        args.model,
        model_version=(
            "campplus-cn-common@v1.0.0+ckpt.3388cf5f+"
            "onnx.7a39d2e5e566+fbank.v1"
        ),
    )

    def embed(path: Path) -> SpeakerModelEmbedding:
        pcm, sample_rate = _read_pcm(path)
        return engine.embed(pcm, sample_rate=sample_rate)

    speaker1_a = embed(args.speaker1_a)
    speaker1_b = embed(args.speaker1_b)
    speaker2_a = embed(args.speaker2_a)
    same_speaker = _similarity(speaker1_a, speaker1_b)
    different_speaker = _similarity(speaker1_a, speaker2_a)
    passed = same_speaker >= args.threshold and different_speaker < args.threshold
    print(
        json.dumps(
            {
                "passed": passed,
                "model_version": engine.model_version,
                "threshold": args.threshold,
                "same_speaker_similarity": round(same_speaker, 6),
                "different_speaker_similarity": round(different_speaker, 6),
                "embedding_dimensions": len(speaker1_a.vector),
                "samples": [
                    {
                        "speech_ms": item.speech_ms,
                        "snr_db": round(item.snr_db, 3),
                        "quality_score": round(item.quality_score, 3),
                    }
                    for item in (speaker1_a, speaker1_b, speaker2_a)
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
