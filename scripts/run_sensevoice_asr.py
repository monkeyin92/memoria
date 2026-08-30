"""SenseVoice offline transcription sidecar for the FunASR rescue path.

Serves the one-shot HTTP contract consumed by
``services.agent.src.providers.sensevoice.SenseVoiceRescue``:

    POST /transcribe?format=pcm&sample_rate=16000&language=zh
        body: raw s16le mono PCM
        200:  {"text": "<transcript>", "duration_ms": 1234}

The endpoint is synchronous per request and CPU-bound, so each request runs
in the web framework's threadpool; sherpa-onnx decodes SenseVoice-small well
under real time on one core, which keeps the rescue latency budget (2.5 s
client timeout) comfortable.  Unknown query parameters and non-PCM formats
fail closed with 4xx so a contract drift is loud, never silently wrong.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import numpy as np
import sherpa_onnx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("sensevoice-asr")

MODEL_DIR = Path(os.getenv("SENSEVOICE_MODEL_DIR", "/models/sensevoice"))
HOST = os.getenv("SENSEVOICE_HOST", "0.0.0.0")
PORT = int(os.getenv("SENSEVOICE_PORT", "8001"))
MAX_BODY_BYTES = int(os.getenv("SENSEVOICE_MAX_BODY_BYTES", str(32 * 1024 * 1024)))

# SenseVoice transcripts carry detection tags like <|zh|><|NEUTRAL|><|Speech|>;
# the rescue consumer wants plain text only.
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_SUPPORTED_SAMPLE_RATES = {16000}

app = FastAPI(title="memoria sensevoice-asr", docs_url=None, redoc_url=None)


class TranscribeResponse(BaseModel):
    text: str
    duration_ms: int


def _load_recognizer() -> sherpa_onnx.OfflineRecognizer:
    model = MODEL_DIR / "model.int8.onnx"
    model = model if model.exists() else MODEL_DIR / "model.onnx"
    tokens = MODEL_DIR / "tokens.txt"
    if not model.exists() or not tokens.exists():
        raise RuntimeError(
            f"sensevoice model files missing in {MODEL_DIR}: need "
            f"{model.name} and tokens.txt"
        )
    started = time.monotonic()
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        use_itn=True,
    )
    logger.info(
        "sensevoice loaded model=%s elapsed_s=%.1f", model.name, time.monotonic() - started
    )
    return recognizer


recognizer = _load_recognizer()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    request: Request,
    format: str = Query(...),
    sample_rate: int = Query(...),
    language: str = Query("auto"),
) -> TranscribeResponse:
    body = await request.body()
    return await run_in_threadpool(_decode, body, format, sample_rate, language)


def _decode(
    body: bytes,
    format: str,
    sample_rate: int,
    language: str,
) -> TranscribeResponse:
    if format != "pcm":
        raise HTTPException(status_code=415, detail=f"unsupported format: {format}")
    if sample_rate not in _SUPPORTED_SAMPLE_RATES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported sample_rate: {sample_rate} (want 16000)",
        )
    if not body or len(body) % 2:
        raise HTTPException(status_code=400, detail="body must be non-empty even-length PCM")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    samples = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    started = time.monotonic()
    recognizer.decode_stream(stream)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = _TAG_RE.sub("", stream.result.text or "").strip()
    logger.info(
        "transcribed audio_ms=%d decode_ms=%d language=%s text_len=%d",
        len(samples) * 1000 // sample_rate,
        elapsed_ms,
        language,
        len(text),
    )
    return TranscribeResponse(text=text, duration_ms=elapsed_ms)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
