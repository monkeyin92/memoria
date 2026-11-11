"""Deterministic external ASR/LLM/TTS boundary for the real bridge preflight.

Transport, registry, admission, epochs and runtime remain production code.
"""

import logging

from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import FakeMediaProvider


class Provider(FakeMediaProvider):
    def set_asr_task_epoch_floor(self, epoch):
        self.task_epoch = epoch

    async def ingest_audio(self, identity, frame):
        logging.getLogger(__name__).info(
            "fixture ASR session=%s stream=%s sequence=%s samples=%s",
            identity.session_id,
            identity.stream_epoch,
            frame.sequence,
            frame.frame_samples,
        )
        if identity.stream_epoch in self.audio_calls:
            return ()
        self.audio_calls.append(identity.stream_epoch)
        return (
            ASRResult(
                task_epoch=getattr(self, "task_epoch", 1),
                sentence_id="r4",
                revision=1,
                capture_start_sample=frame.capture_start_sample,
                capture_end_sample=frame.capture_start_sample + frame.frame_samples,
                text="你好",
                is_final=True,
                confidence=0.99,
                stream_epoch=identity.stream_epoch,
            ),
        )


def build(_settings):
    return lambda _identity: Provider()
