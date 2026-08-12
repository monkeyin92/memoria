"""Generation-bound lifecycle for fixed ``AgentSession.say`` output."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


async def _stop_handle(handle: Any) -> None:
    interrupt = getattr(handle, "interrupt", None)
    if not callable(interrupt):
        return
    try:
        result = interrupt(force=True)
    except TypeError:
        result = interrupt()
    if inspect.isawaitable(result):
        await result


@dataclass(slots=True)
class FixedSpeechPlayer:
    """Publish the fenced floor state before any fixed TTS can emit PCM."""

    session: Any
    runtime: Any
    tts: Any
    settle_delay_s: float = 0.2

    async def say(
        self,
        text: str,
        *,
        interruptible: bool = False,
        emotion: str = "neutral",
        rate: float = 1.0,
        instruction: str = "",
        restore_state: str = "listening",
    ) -> None:
        fence = self.runtime.fence
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            apply_plan(
                emotion=emotion,
                rate=rate,
                instruction=instruction,
                fence=fence,
            )
        bind_fence = getattr(self.tts, "bind_fence", None)
        if callable(bind_fence):
            bind_fence(fence)

        handle: Any | None = None
        completed = False
        floor_attempted = False
        claimed_floor = False
        failure: BaseException | None = None
        try:
            if getattr(self.runtime, "_event_publisher", None) is None:
                raise RuntimeError("fixed speech requires the Agent UI publisher")
            floor_attempted = True
            if not await self.runtime.on_assistant_speaking(
                text,
                expected_fence=fence,
                publish_state=False,
            ):
                return
            claimed_floor = True
            speaking_publish = self.runtime.publish_assistant_state("speaking")
            if speaking_publish is None:
                raise RuntimeError("fixed speech requires the Agent UI publisher")
            await speaking_publish
            if not fence.matches(self.runtime.fence):
                return
            handle = self.session.say(
                text,
                allow_interruptions=interruptible,
                add_to_chat_ctx=False,
            )
            wait = getattr(handle, "wait_for_playout", None)
            if callable(wait):
                await wait()
            else:
                for _ in range(150):
                    if not self.runtime._was_speaking:
                        break
                    await asyncio.sleep(0.1)
            completed = True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if not completed and handle is not None:
                try:
                    await _stop_handle(handle)
                except Exception:
                    logger.warning("fixed speech handle cleanup failed", exc_info=True)
            # A stale output never overwrites the newer generation's floor state.
            owns_floor = claimed_floor or (
                floor_attempted and self.runtime._pending_assistant_text == text
            )
            if owns_floor and fence.matches(self.runtime.fence):
                try:
                    self.runtime._was_speaking = False
                    self.runtime._pending_assistant_text = ""
                    self.runtime._playback_fence = None
                    self.runtime._assistant_expression_fence = None
                    restore_publish = self.runtime.publish_assistant_state(restore_state)
                    if restore_publish is None:
                        raise RuntimeError("fixed speech requires the Agent UI publisher")
                    await restore_publish
                except Exception:
                    if failure is None:
                        raise
                    logger.warning("fixed speech state restoration failed", exc_info=True)
                if self.settle_delay_s > 0:
                    await asyncio.sleep(self.settle_delay_s)


__all__ = ["FixedSpeechPlayer"]
