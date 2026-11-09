"""Fixed-safety reply streaming mixin for the existing voice provider adapter.

``ExistingVoiceProviderFixedSafetyMixin`` streams the shipped fixed-safety
clip, the stepfun live path and the whole-script synthesis fallback.  Adapter
config, TTS handler and shared streaming helpers are declared here for type
checking only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.providers.fixed_safety_audio import (
    FixedSafetyClipUnavailableError,
    heard_complete_by_sentence,
    shipped_approved_catalog,
)
from services.common.fixed_safety_scripts import SpokenPhrase

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session import MediaReplyChunk
    from services.agent.src.voice_core.provider_adapter import (
        ExistingVoiceProviderConfig,
    )

logger = logging.getLogger(__name__)

# whole-sentence synthesis exists only while the stored clip is missing or fails
# its manifest hash, so a provider fault can never truncate the line mid-script.
# The fallback is retried once inside the opening budget (C1, 2026-10-21
# 03:51:53Z: the first Doubao attempt failed and the retry only produced audio
# 1.9 s later).
_FIXED_SAFETY_SYNTHESIS_ATTEMPTS = 2


class ExistingVoiceProviderFixedSafetyMixin:
    """Provider adapter seam."""

    if TYPE_CHECKING:
        config: ExistingVoiceProviderConfig
        language_model: Any
        speech_synthesis: Any
        metrics: Any
        _prerecorded_fixed_safety: dict[GenerationFence, tuple[str, int]]
        _generation_output_ends: dict[GenerationFence, int]

        @property
        def output_frame_samples(self) -> int: ...

        def _provider_pcm_chunks(
            self,
            spoken: str,
            fence: GenerationFence,
            cancellation: CancellationContext,
            cancel_event: asyncio.Event,
        ) -> AsyncGenerator[bytes, None]: ...


    async def _stream_fixed_safety_script(
        self,
        phrases: list[SpokenPhrase],
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[MediaReplyChunk]:
        """Speak one approved safety script from its stored clip first.

        The crisis line is the one reply a child must hear in full. The chain is
        fixed: the configured approved clip, then StepFun realtime TTS, then the
        fixed-safety clips shipped with the image (re-verified by sha256). Doubao never
        speaks a crisis turn. A live take is buffered whole so a dropped provider
        cannot truncate the line, and the fallback is retried once inside the
        opening budget (C1, 2026-10-21 03:51:53Z).
        """

        spoken = "".join(phrase.spoken for phrase in phrases)
        template = self._approved_prerecorded_template(spoken)
        if template is not None:
            pcm = template.pcm or b""
            source = "prerecorded"
            self._prerecorded_fixed_safety[fence] = (template.spoken, len(pcm) // 2)
        else:
            pcm, source = await self._fixed_safety_live_then_shipped_clip(
                spoken,
                fence,
                cancellation,
                cancel_event,
            )
        if cancel_event.is_set() or not pcm:
            return
        logger.info(
            "fixed safety speech ready source=%s text_len=%d bytes=%d session=%s turn=%s",
            source,
            len(spoken),
            len(pcm),
            fence.session_id,
            fence.turn_id,
        )
        async for chunk in self._emit_fixed_safety_pcm(
            pcm,
            display="".join(phrase.display for phrase in phrases),
            fence=fence,
        ):
            yield chunk

    async def _fixed_safety_live_then_shipped_clip(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> tuple[bytes, str]:
        """StepFun realtime, then the shipped approved clip. Never Doubao."""

        if self._fixed_safety_stepfun_available():
            try:
                return await self._fixed_safety_script_pcm(
                    spoken,
                    fence,
                    cancellation,
                    cancel_event,
                )
            except RuntimeError:
                logger.warning(
                    "fixed safety StepFun fallback produced no audio; using the shipped clip "
                    "session=%s",
                    fence.session_id,
                )
        else:
            logger.warning(
                "fixed safety live provider is not StepFun; using the shipped clip session=%s",
                fence.session_id,
            )
        template = self._approved_prerecorded_template(
            spoken,
            catalog=shipped_approved_catalog(),
        )
        if template is None:
            # Every rung is gone. The child will not hear the reviewed line,
            # and the one thing that must not happen next is a live engine
            # inventing it, so name the failure the adapter can hand back.
            raise FixedSafetyClipUnavailableError(
                "fixed safety script produced no audio"
            )
        pcm = template.pcm or b""
        self._prerecorded_fixed_safety[fence] = (template.spoken, len(pcm) // 2)
        return pcm, "prerecorded_fallback"

    def _fixed_safety_stepfun_available(self) -> bool:
        provider = str(getattr(self.speech_synthesis, "provider", "stepfun"))
        return provider not in {"doubao", "volcengine_doubao"}

    def _approved_prerecorded_template(
        self,
        spoken: str,
        *,
        catalog: Any | None = None,
    ) -> Any | None:
        """The stored clip for ``spoken``, only when its PCM matches the manifest."""

        if catalog is None:
            catalog = getattr(self.speech_synthesis, "fixed_safety_catalog", None)
        match = getattr(catalog, "match", None)
        if not callable(match):
            logger.warning("fixed safety prerecorded unavailable reason=no_catalog")
            return None
        template = match(spoken)
        if template is None:
            logger.warning(
                "fixed safety prerecorded unavailable reason=not_in_catalog spoken_sha256_16=%s",
                hashlib.sha256(spoken.encode("utf-8")).hexdigest()[:16],
            )
            return None
        template_id = str(getattr(template, "template_id", "") or "unknown")
        pcm = getattr(template, "pcm", None)
        if not getattr(template, "playable", False) or not isinstance(pcm, (bytes, bytearray)):
            logger.warning(
                "fixed safety prerecorded unavailable template_id=%s reason=not_playable",
                template_id,
            )
            return None
        expected = str(getattr(template, "pcm_sha256", "") or "")
        digest = hashlib.sha256(bytes(pcm)).hexdigest()
        if not expected or digest != expected:
            logger.warning(
                "fixed safety prerecorded rejected template_id=%s reason=pcm_sha256_mismatch",
                template_id,
            )
            return None
        return template

    def fixed_safety_heard_complete(
        self,
        fence: GenerationFence,
        played_samples: int,
    ) -> bool | None:
        """Whether every approved sentence of a prerecorded script played.

        ``None`` means this fence was not served from a stored clip, so the
        caller keeps the existing whole-reply playback evidence.
        """

        entry = self._prerecorded_fixed_safety.get(fence)
        if entry is None:
            return None
        spoken, total_samples = entry
        return heard_complete_by_sentence(
            spoken,
            played_samples=played_samples,
            total_samples=total_samples,
        )

    async def _fixed_safety_script_pcm(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> tuple[bytes, str]:
        """Synthesize the whole approved script on StepFun as the live fallback.

        Used only when the stored clip is missing or fails its manifest hash, and
        only while the bound engine is StepFun. The script is pushed as one unit,
        so a provider that loses the connection mid-unit still delivers the full
        line: the failover wrapper buffers the take, drops a partial one, and
        retries the whole text. Doubao is never a candidate for this unit; a
        StepFun failure moves to the shipped approved clip instead. The deadline
        bounds every attempt inside the five-second crisis budget. Nothing
        synthesized here is stored as an approved take.
        """

        deadline = monotonic() + self.config.fixed_safety_opening_budget_s
        failure: Exception | None = None
        for attempt in range(1, _FIXED_SAFETY_SYNTHESIS_ATTEMPTS + 1):
            if cancel_event.is_set():
                return b"", "cancelled"
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                pcm = await asyncio.wait_for(
                    self._synthesize_whole_script_once(
                        spoken,
                        fence,
                        cancellation,
                        cancel_event,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                pcm = b""
                logger.warning(
                    "fixed safety speech attempt exceeded the opening budget "
                    "attempt=%d/%d text_len=%d session=%s",
                    attempt,
                    _FIXED_SAFETY_SYNTHESIS_ATTEMPTS,
                    len(spoken),
                    fence.session_id,
                )
            if pcm:
                return pcm, "synthesis"
            failure = RuntimeError("speech provider returned no audio")
            logger.warning(
                "fixed safety speech attempt produced no audio attempt=%d/%d text_len=%d "
                "session=%s",
                attempt,
                _FIXED_SAFETY_SYNTHESIS_ATTEMPTS,
                len(spoken),
                fence.session_id,
            )
        raise RuntimeError("fixed safety script produced no audio") from failure

    async def _synthesize_whole_script_once(
        self,
        spoken: str,
        fence: GenerationFence,
        cancellation: CancellationContext,
        cancel_event: asyncio.Event,
    ) -> bytes:
        """One full attempt, with the engine that spoke it logged for provenance."""

        pcm = bytearray()
        try:
            async for chunk in self._provider_pcm_chunks(
                spoken,
                fence,
                cancellation,
                cancel_event,
            ):
                pcm.extend(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "fixed safety speech attempt failed text_len=%d error=%s session=%s",
                len(spoken),
                type(exc).__name__,
                fence.session_id,
            )
            return b""
        report = getattr(self.speech_synthesis, "report_sentence_from", None)
        if callable(report) and pcm:
            chosen = cast(Any, self.speech_synthesis).last_engine()
            report(chosen, index=0, switched=getattr(chosen, "provider", "") != "stepfun")
        return bytes(pcm)

    async def _emit_fixed_safety_pcm(
        self,
        pcm: bytes,
        *,
        display: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaTextSpan

        frame_samples = self.output_frame_samples
        frame_bytes = frame_samples * 2
        output_sample = 0
        offset = 0
        total = len(pcm)
        while offset < total:
            frame = pcm[offset : offset + frame_bytes]
            offset += frame_bytes
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            audio_end = output_sample + frame_samples
            is_last = offset >= total
            yield MediaReplyChunk(
                pcm_s16le=frame,
                source_start_sample=output_sample,
                text=display if is_last else "",
                assistant_text_delta=display if output_sample == 0 else "",
                first=output_sample == 0,
                final=is_last,
                text_audio_start_sample=0 if is_last else None,
                text_audio_end_sample=audio_end if is_last else None,
                text_spans=(MediaTextSpan(display, 0, audio_end),) if is_last else (),
            )
            output_sample = audio_end
            self._output_sample = output_sample
            self._generation_output_ends[fence] = output_sample
