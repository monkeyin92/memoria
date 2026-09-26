"""Evaluate the SenseVoice rescue sidecar on real 16 kHz mono recordings (P1-02).

Segments each recording into utterances with a simple energy detector, then
sends every utterance to ``/transcribe`` as recorded, 20 dB quieter and
clipped (x8 gain), plus silence/noise probes and a concurrent pass. Reports
non-empty rates, foreign-script output (the language-ID failure mode),
similarity of the degraded variants to the original transcript, and latency
against the rescue client's budget. Transcripts stay out of the report unless
``--show-text`` is given: the recordings are private speech.

    uv run python scripts/evaluate_sensevoice_rescue.py \
        --url http://127.0.0.1:18001 outputs/acceptance/*.wav
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import json
import re
import statistics
import time
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
FRAME = SAMPLE_RATE * 30 // 1000
MIN_TEXT_CHARS = 2  # SENSEVOICE_MIN_TEXT_CHARS default
_FOREIGN = re.compile(r"[぀-ヿ가-힯]")  # kana, Hangul
_CJK = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class Result:
    text: str
    latency_s: float


def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise ValueError(f"{path}: need 16 kHz mono s16le")
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")


def segments(samples: np.ndarray, *, max_s: float) -> list[np.ndarray]:
    """Utterances: frames above an adaptive energy floor, 0.5 s gaps merged."""

    count = len(samples) // FRAME
    if count == 0:
        return []
    frames = samples[: count * FRAME].reshape(count, FRAME).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    voiced = rms > max(3 * float(np.percentile(rms, 20)), 150.0)
    spans: list[list[int]] = []
    for index, is_voiced in enumerate(voiced):
        if not is_voiced:
            continue
        if spans and index - spans[-1][1] <= 16:  # merge gaps up to ~0.5 s
            spans[-1][1] = index
        else:
            spans.append([index, index])
    pad, cap = 7, int(max_s * 1000 / 30)
    out = []
    for start, end in spans:
        if end - start + 1 < 10:  # shorter than 0.3 s
            continue
        start, end = max(0, start - pad), min(count - 1, end + pad)
        end = min(end, start + cap - 1)
        out.append(samples[start * FRAME : (end + 1) * FRAME])
    return out


def transcribe(url: str, samples: np.ndarray, timeout_s: float) -> Result:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/transcribe?format=pcm&sample_rate={SAMPLE_RATE}&language=zh",
        data=samples.astype("<i2").tobytes(),
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        text = str(json.loads(response.read())["text"])
    return Result(text=text, latency_s=time.monotonic() - started)


def variant(samples: np.ndarray, name: str) -> np.ndarray:
    if name == "quiet_20db":
        return (samples.astype(np.float64) * 0.1).astype(np.int16)
    if name == "clipped_x8":
        return np.clip(samples.astype(np.int32) * 8, -32768, 32767).astype(np.int16)
    return samples


def summarize(results: list[Result], budget_s: float, reference: list[str] | None) -> dict:
    latencies = sorted(result.latency_s for result in results)
    texts = [result.text for result in results]
    summary: dict[str, object] = {
        "count": len(results),
        "nonempty_rate": round(sum(len(t) >= MIN_TEXT_CHARS for t in texts) / len(texts), 3),
        "foreign_script": sum(bool(_FOREIGN.search(t)) for t in texts),
        "cjk_char_ratio": round(
            sum(len(_CJK.findall(t)) for t in texts) / max(1, sum(len(t) for t in texts)), 3
        ),
        "latency_p50_s": round(statistics.median(latencies), 3),
        "latency_max_s": round(latencies[-1], 3),
        "over_budget": sum(latency > budget_s for latency in latencies),
    }
    if reference is not None:
        ratios = [
            difflib.SequenceMatcher(None, ref, text).ratio()
            for ref, text in zip(reference, texts, strict=True)
            if ref
        ]
        summary["similarity_to_original_mean"] = round(statistics.mean(ratios), 3) if ratios else None
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--budget-s", type=float, default=2.5)
    parser.add_argument("--max-audio-s", type=float, default=30.0)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--show-text", action="store_true")
    parser.add_argument("recordings", nargs="+", type=Path)
    args = parser.parse_args()

    utterances = [u for path in args.recordings for u in segments(read_pcm(path), max_s=args.max_audio_s)]
    if not utterances:
        raise SystemExit("no utterances found")
    report: dict[str, object] = {
        "recordings": len(args.recordings),
        "utterances": len(utterances),
        "utterance_s": {
            "min": round(min(len(u) for u in utterances) / SAMPLE_RATE, 2),
            "median": round(statistics.median(len(u) for u in utterances) / SAMPLE_RATE, 2),
            "max": round(max(len(u) for u in utterances) / SAMPLE_RATE, 2),
        },
    }
    original: list[str] = []
    for name in ("original", "quiet_20db", "clipped_x8"):
        results = [transcribe(args.url, variant(u, name), args.timeout_s) for u in utterances]
        if name == "original":
            original = [result.text for result in results]
        report[name] = summarize(results, args.budget_s, None if name == "original" else original)
        if args.show_text:
            report[f"{name}_texts"] = [result.text for result in results]

    rng = np.random.default_rng(0)
    probes = {
        "silence_2s": np.zeros(2 * SAMPLE_RATE, dtype=np.int16),
        "noise_rms30_2s": (rng.normal(0, 30, 2 * SAMPLE_RATE)).astype(np.int16),
        "noise_rms150_2s": (rng.normal(0, 150, 2 * SAMPLE_RATE)).astype(np.int16),
    }
    report["probes_text_chars"] = {
        name: len(transcribe(args.url, samples, args.timeout_s).text) for name, samples in probes.items()
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        batch = utterances[: args.concurrency * 4]
        results = list(pool.map(lambda u: transcribe(args.url, u, args.timeout_s), batch))
    concurrent_summary = summarize(results, args.budget_s, None)
    concurrent_summary["same_text_as_sequential"] = sum(
        result.text == reference for result, reference in zip(results, original, strict=False)
    )
    report[f"concurrent_x{args.concurrency}"] = concurrent_summary
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
