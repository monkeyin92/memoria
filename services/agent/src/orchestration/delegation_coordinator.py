"""Async deep-work coordinator; accepted results become fenced OutputIntent only."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.task_manager import (
    TaskManager,
    ToolTask,
    spoken_result_summarizer,
)
from services.agent.src.prompts import is_allowlisted_device_phrase
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2

media_pb2: Any = _media_pb2

_DEEP_RESULT_DELIVERY_TTL_MS = 120_000
logger = logging.getLogger(__name__)
_MAX_SHADOW_OUTPUT_CANDIDATES_PER_DOMAIN = 4


class SideEffectPolicy(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    HIGH_RISK = "high_risk"


class DelegationEventKind(StrEnum):
    STARTED = "started"
    RESULT_CANDIDATE = "result_candidate"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    tool_name: str
    arguments: dict[str, Any]
    fence: GenerationFence
    task_epoch: int
    context_version: int
    expires_at_ms: int
    side_effect_policy: SideEffectPolicy
    committed: bool
    user_confirmed: bool = False
    relevance: Callable[[], bool] | None = None
    output_kind: int = media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
    priority: int = 50


@dataclass(frozen=True, slots=True)
class DelegationEvent:
    kind: DelegationEventKind
    task_id: str
    fence: GenerationFence
    task_epoch: int
    context_version: int
    expires_at_ms: int
    side_effect_policy: SideEffectPolicy
    candidate: Any = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OutputIntentAdmission:
    intent: Any
    current_fence: GenerationFence
    current_context_version: int
    floor_allows_output: bool
    observed_at_ms: int
    accepted: bool
    reason: str
    consumed: bool = False
    selected: bool = False
    authoritative_candidate: Any | None = None
    authoritative_candidates: tuple[Any, ...] = ()


@dataclass(slots=True)
class TaskHandle:
    task_id: str
    request: DelegationRequest
    record: ToolTask
    consumed: bool = False
    events_consumed: bool = False


@dataclass(slots=True)
class DelegationCoordinator:
    task_manager: TaskManager
    _task_epoch_by_session: dict[str, int] = field(default_factory=dict)
    _context_version_by_session: dict[str, int] = field(default_factory=dict)
    _evaluated_intents: set[str] = field(default_factory=set)
    _evaluated_intent_order: deque[str] = field(default_factory=lambda: deque(maxlen=1_024))
    _output_intent_observer: Callable[[OutputIntentAdmission], None] | None = None
    _shadow_output_by_session: dict[str, dict[str, Any]] = field(default_factory=dict)

    def set_output_intent_observer(
        self,
        observer: Callable[[OutputIntentAdmission], None] | None,
    ) -> None:
        self._output_intent_observer = observer

    def _observe_output_intent(self, admission: OutputIntentAdmission) -> None:
        observer = self._output_intent_observer
        if observer is None:
            return
        try:
            observer(admission)
        except Exception:
            logger.exception("output intent observer failed")

    @staticmethod
    def _shadow_output_domain_rank(intent: Any) -> int | None:
        return {
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT: 4,
            media_pb2.OUTPUT_INTENT_KIND_BACKCHANNEL: 3,
            media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY: 3,
            media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT: 2,
            media_pb2.OUTPUT_INTENT_KIND_TOOL_RESULT: 2,
            media_pb2.OUTPUT_INTENT_KIND_REMINDER: 1,
            media_pb2.OUTPUT_INTENT_KIND_NOTIFICATION: 1,
        }.get(int(getattr(intent, "kind", 0)))

    @classmethod
    def _shadow_output_order_key(cls, intent: Any) -> tuple[int, int, int, str]:
        return (
            cls._shadow_output_domain_rank(intent) or 0,
            int(intent.priority),
            int(intent.created_at_ms),
            str(intent.intent_id),
        )

    def _shadow_output_state(
        self,
        intent: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        accepted: bool,
        consumed: bool,
        now_ms: int,
    ) -> tuple[Any | None, tuple[Any, ...]]:
        session_id = str(getattr(intent, "session_id", ""))
        if not session_id:
            return None, ()
        candidates = self._shadow_output_by_session.setdefault(session_id, {})
        for intent_id, candidate in list(candidates.items()):
            candidate_fence = GenerationFence(
                session_id=str(candidate.session_id),
                turn_id=int(candidate.turn_id),
                generation_id=int(candidate.generation_id),
                tool_epoch=int(candidate.tool_epoch),
                # The wire intent carries no epoch; the current identity epoch
                # is authoritative for the shadow admission check (P0-3).
                session_epoch=current_fence.session_epoch,
            )
            if (
                int(candidate.expires_at_ms) <= now_ms
                or int(candidate.context_version) != current_context_version
                or not candidate_fence.matches(current_fence)
            ):
                candidates.pop(intent_id, None)
        if not floor_allows_output:
            candidates.clear()
        if consumed:
            candidates.pop(str(getattr(intent, "intent_id", "")), None)
        if accepted:
            candidate_rank = self._shadow_output_domain_rank(intent)
            if candidate_rank is not None:
                candidate = media_pb2.OutputIntent()
                candidate.CopyFrom(intent)
                candidates[str(candidate.intent_id)] = candidate
                same_domain = sorted(
                    (
                        item
                        for item in candidates.values()
                        if self._shadow_output_domain_rank(item) == candidate_rank
                    ),
                    key=self._shadow_output_order_key,
                    reverse=True,
                )
                for superseded in same_domain[
                    _MAX_SHADOW_OUTPUT_CANDIDATES_PER_DOMAIN:
                ]:
                    candidates.pop(str(superseded.intent_id), None)
        if not candidates:
            self._shadow_output_by_session.pop(session_id, None)
            return None, ()
        ordered = tuple(
            sorted(
                candidates.values(),
                key=self._shadow_output_order_key,
                reverse=True,
            )
        )
        return ordered[0], ordered

    def next_task_epoch(self, session_id: str) -> int:
        task_epoch = self._task_epoch_by_session.get(session_id, 0) + 1
        self._task_epoch_by_session[session_id] = task_epoch
        return task_epoch

    def current_task_epoch(self, session_id: str) -> int:
        return self._task_epoch_by_session.get(session_id, 0)

    def activate_context_version(self, session_id: str, context_version: int) -> None:
        current = self._context_version_by_session.get(session_id, 0)
        if context_version < current:
            raise ValueError("delegation context version moved backwards")
        if context_version > current:
            self._shadow_output_by_session.pop(session_id, None)
        self._context_version_by_session[session_id] = context_version

    def current_context_version(self, session_id: str) -> int:
        return self._context_version_by_session.get(session_id, 0)

    def reset_output_intent_state(self, session_id: str) -> None:
        """Drop candidate-only output state when a transport epoch changes."""

        if session_id:
            self._shadow_output_by_session.pop(session_id, None)

    @staticmethod
    def _event(
        handle: TaskHandle,
        kind: DelegationEventKind,
        *,
        candidate: Any = None,
        reason: str | None = None,
    ) -> DelegationEvent:
        request = handle.request
        return DelegationEvent(
            kind=kind,
            task_id=handle.task_id,
            fence=request.fence,
            task_epoch=request.task_epoch,
            context_version=request.context_version,
            expires_at_ms=request.expires_at_ms,
            side_effect_policy=request.side_effect_policy,
            candidate=candidate,
            reason=reason,
        )

    async def delegate(self, request: DelegationRequest) -> TaskHandle:
        if request.task_epoch < 0 or request.context_version < 0:
            raise ValueError("delegation versions must be non-negative")
        if request.priority < 0 or request.priority > 0xFFFFFFFF:
            raise ValueError("delegation priority must fit uint32")
        if request.expires_at_ms <= int(time.time() * 1_000):
            raise ValueError("delegation request is already expired")
        if request.side_effect_policy is SideEffectPolicy.HIGH_RISK and not request.committed:
            raise PermissionError("high-risk delegation requires a committed turn")
        spec = self.task_manager.specs.get(request.tool_name)
        if spec is None:
            raise KeyError(f"unknown tool: {request.tool_name}")
        if spec.side_effect_policy != request.side_effect_policy.value:
            raise PermissionError("delegation side-effect policy does not match tool authority")
        if request.side_effect_policy is SideEffectPolicy.HIGH_RISK and not request.user_confirmed:
            raise PermissionError("high-risk delegation requires explicit user confirmation")
        if (
            request.side_effect_policy in {SideEffectPolicy.NONE, SideEffectPolicy.READ_ONLY}
            and not spec.idempotent
        ):
            raise PermissionError("read-only delegation requires an idempotent tool")
        session_id = request.fence.session_id
        current_task_epoch = self._task_epoch_by_session.get(session_id)
        if current_task_epoch is not None and request.task_epoch < current_task_epoch:
            raise ValueError("delegation task epoch moved backwards")
        self._task_epoch_by_session[session_id] = request.task_epoch
        self.activate_context_version(session_id, request.context_version)
        record = await self.task_manager.start(
            request.tool_name,
            request.arguments,
            request.fence,
            task_epoch=request.task_epoch,
            context_version=request.context_version,
            expires_at_ms=request.expires_at_ms,
            side_effect_policy=request.side_effect_policy.value,
            committed=request.committed,
            relevance=request.relevance,
        )
        return TaskHandle(record.tool_task_id, request, record)

    async def cancel(self, handle: TaskHandle, reason: str) -> DelegationEvent:
        cancelled = await self.task_manager.cancel(handle.task_id)
        return self._event(
            handle,
            DelegationEventKind.CANCELLED,
            reason=reason if cancelled else "already_finished",
        )

    async def events(self, handle: TaskHandle) -> AsyncIterator[DelegationEvent]:
        if handle.events_consumed:
            raise RuntimeError("delegation events are one-shot")
        handle.events_consumed = True
        yield self._event(handle, DelegationEventKind.STARTED)
        try:
            await handle.record.task
        except asyncio.CancelledError:
            yield self._event(handle, DelegationEventKind.CANCELLED, reason="cancelled")
            return
        except Exception as exc:
            yield self._event(handle, DelegationEventKind.FAILED, reason=str(exc))
            return
        if handle.record.cancelled:
            yield self._event(handle, DelegationEventKind.CANCELLED, reason="cancelled")
        elif handle.record.error is not None:
            yield self._event(handle, DelegationEventKind.FAILED, reason=handle.record.error)
        else:
            yield self._event(
                handle,
                DelegationEventKind.RESULT_CANDIDATE,
                candidate=handle.record.result,
            )

    def output_intent(
        self,
        handle: TaskHandle,
        *,
        current_fence: GenerationFence,
        current_task_epoch: int,
        current_context_version: int,
        relevant: bool,
        now_ms: int | None = None,
    ) -> Any | None:
        payload = self._accept_result(
            handle,
            current_fence=current_fence,
            current_task_epoch=current_task_epoch,
            current_context_version=current_context_version,
            relevant=relevant,
            allow_sensitive=False,
            now_ms=now_ms,
        )
        if payload is None:
            return None
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        request = handle.request
        if isinstance(payload, dict):
            spoken = spoken_result_summarizer(payload)
        elif isinstance(payload, str):
            spoken = payload.strip()
        else:
            return None
        if not spoken:
            return None
        return media_pb2.OutputIntent(
            intent_id=str(uuid.uuid4()),
            session_id=request.fence.session_id,
            turn_id=request.fence.turn_id,
            generation_id=request.fence.generation_id,
            tool_epoch=request.fence.tool_epoch,
            kind=request.output_kind,
            priority=request.priority,
            created_at_ms=now,
            # The request TTL bounds task resolution; a spoken deep result
            # needs its own delivery TTL so a long answer cannot expire while
            # it is still being streamed to the device.
            expires_at_ms=now + _DEEP_RESULT_DELIVERY_TTL_MS,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=request.context_version,
            tts_source=spoken,
        )

    def accept_control_result(
        self,
        handle: TaskHandle,
        *,
        current_fence: GenerationFence,
        current_task_epoch: int,
        current_context_version: int,
        relevant: bool,
        now_ms: int | None = None,
    ) -> Any | None:
        """Accept fenced internal control data that can never become audio directly."""

        if handle.request.output_kind != media_pb2.OUTPUT_INTENT_KIND_UNSPECIFIED:
            return None
        return self._accept_result(
            handle,
            current_fence=current_fence,
            current_task_epoch=current_task_epoch,
            current_context_version=current_context_version,
            relevant=relevant,
            allow_sensitive=True,
            now_ms=now_ms,
        )

    def _accept_result(
        self,
        handle: TaskHandle,
        *,
        current_fence: GenerationFence,
        current_task_epoch: int,
        current_context_version: int,
        relevant: bool,
        allow_sensitive: bool,
        now_ms: int | None,
    ) -> Any | None:
        if handle.consumed:
            return None
        handle.consumed = True
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        request = handle.request

        def discard(source: str) -> None:
            self.task_manager.tasks.pop(handle.task_id, None)
            self.task_manager.metrics.inc_stale_result_dropped(source)

        if not request.committed:
            discard("delegation_uncommitted")
            return None
        session_id = request.fence.session_id
        authoritative_task_epoch = self._task_epoch_by_session.get(session_id)
        authoritative_context_version = self._context_version_by_session.get(session_id)
        if (
            authoritative_task_epoch is None
            or authoritative_context_version is None
            or current_task_epoch != authoritative_task_epoch
            or current_context_version != authoritative_context_version
        ):
            discard("delegation")
            return None
        spec = self.task_manager.specs.get(request.tool_name)
        if spec is None or (spec.contains_sensitive_data and not allow_sensitive):
            discard("delegation_sensitive")
            return None
        payload = self.task_manager.accept_result(
            handle.task_id,
            current_fence,
            current_task_epoch=authoritative_task_epoch,
            current_context_version=authoritative_context_version,
            now_ms=now,
            relevant=relevant,
            current_side_effect_policy=handle.request.side_effect_policy.value,
        )
        self.task_manager.tasks.pop(handle.task_id, None)
        if payload is None:
            return None
        return payload

    def admit_output_intent(
        self,
        intent: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> str | None:
        now = int(time.time() * 1_000) if now_ms is None else now_ms

        def finish(text: str | None, reason: str) -> str | None:
            authoritative_candidate, authoritative_candidates = self._shadow_output_state(
                intent,
                current_fence=current_fence,
                current_context_version=current_context_version,
                floor_allows_output=floor_allows_output,
                accepted=text is not None,
                consumed=False,
                now_ms=now,
            )
            selected = bool(
                text is not None
                and authoritative_candidate is not None
                and str(authoritative_candidate.intent_id) == str(intent.intent_id)
            )
            observed_reason = reason
            if text is not None and not selected:
                retained = any(
                    str(candidate.intent_id) == str(intent.intent_id)
                    for candidate in authoritative_candidates
                )
                observed_reason = "queued" if retained else "superseded"
            self._observe_output_intent(
                OutputIntentAdmission(
                    intent=intent,
                    current_fence=current_fence,
                    current_context_version=current_context_version,
                    floor_allows_output=floor_allows_output,
                    observed_at_ms=now,
                    accepted=text is not None,
                    reason=observed_reason,
                    consumed=False,
                    selected=selected,
                    authoritative_candidate=authoritative_candidate,
                    authoritative_candidates=authoritative_candidates,
                )
            )
            return text if selected else None

        intent_id = str(intent.intent_id)
        if not intent_id:
            return finish(None, "missing_intent_id")
        if intent_id in self._evaluated_intents:
            return finish(None, "duplicate_intent")
        if len(self._evaluated_intent_order) == self._evaluated_intent_order.maxlen:
            oldest = self._evaluated_intent_order.popleft()
            self._evaluated_intents.discard(oldest)
        self._evaluated_intent_order.append(intent_id)
        self._evaluated_intents.add(intent_id)
        if self._shadow_output_domain_rank(intent) is None:
            return finish(None, "invalid_kind")
        fence = GenerationFence(
            session_id=str(intent.session_id),
            turn_id=int(intent.turn_id),
            generation_id=int(intent.generation_id),
            tool_epoch=int(intent.tool_epoch),
            session_epoch=current_fence.session_epoch,
        )
        reason = ""
        if not fence.matches(current_fence):
            reason = "stale_fence"
        elif int(intent.context_version) != current_context_version:
            reason = "stale_context"
        elif int(intent.created_at_ms) <= 0 or int(intent.created_at_ms) > now:
            reason = "invalid_created_at"
        elif now >= int(intent.expires_at_ms):
            reason = "expired"
        elif not floor_allows_output:
            reason = "floor_blocked"
        elif int(intent.floor_requirement) != media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK:
            reason = "floor_requirement"
        if reason:
            self.task_manager.metrics.inc_stale_result_dropped("output_intent")
            return finish(None, reason)
        source = getattr(intent, "WhichOneof", lambda _name: None)("source")
        if int(intent.kind) == media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY:
            if source is not None:
                return finish(None, "conversation_reply_has_source")
            return finish("", "accepted")
        if source == "tts_source":
            text = str(intent.tts_source).strip()
            return finish(text or None, "empty_tts_source" if not text else "accepted")
        if source == "pcm_s16le":
            pcm = bytes(intent.pcm_s16le)
            if not pcm or len(pcm) % 2:
                return finish(None, "invalid_pcm_source")
            return finish("", "accepted")
        return finish(None, "missing_output_source")

    def output_intent_is_selected(
        self,
        intent: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> bool:
        authoritative_candidate = self.current_output_intent(
            str(getattr(intent, "session_id", "")),
            current_fence=current_fence,
            current_context_version=current_context_version,
            floor_allows_output=floor_allows_output,
            now_ms=now_ms,
        )
        return bool(
            authoritative_candidate is not None
            and str(authoritative_candidate.intent_id) == str(getattr(intent, "intent_id", ""))
        )

    def output_intent_is_active(
        self,
        intent: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> bool:
        """Whether an admitted intent remains queued or selected."""

        now = int(time.time() * 1_000) if now_ms is None else now_ms
        _winner, candidates = self._shadow_output_state(
            intent,
            current_fence=current_fence,
            current_context_version=current_context_version,
            floor_allows_output=floor_allows_output,
            accepted=False,
            consumed=False,
            now_ms=now,
        )
        intent_id = str(getattr(intent, "intent_id", ""))
        return bool(intent_id and any(str(item.intent_id) == intent_id for item in candidates))

    def current_output_intent(
        self,
        session_id: str,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> Any | None:
        """Return the active winner after applying stale and floor gates."""

        candidates = self._shadow_output_by_session.get(session_id)
        if not candidates:
            return None
        seed = next(iter(candidates.values()), None)
        if seed is None:
            return None
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        winner, _ = self._shadow_output_state(
            seed,
            current_fence=current_fence,
            current_context_version=current_context_version,
            floor_allows_output=floor_allows_output,
            accepted=False,
            consumed=False,
            now_ms=now,
        )
        return winner

    def complete_output_intent(
        self,
        intent: Any,
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
        reason: str = "completed",
    ) -> bool:
        session_id = str(getattr(intent, "session_id", ""))
        intent_id = str(getattr(intent, "intent_id", ""))
        candidates = self._shadow_output_by_session.get(session_id)
        if not intent_id or candidates is None or intent_id not in candidates:
            return False
        completion_reason = reason.strip()[:64]
        if not completion_reason:
            raise ValueError("output intent completion reason must not be empty")
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        authoritative_candidate, authoritative_candidates = self._shadow_output_state(
            intent,
            current_fence=current_fence,
            current_context_version=current_context_version,
            floor_allows_output=floor_allows_output,
            accepted=False,
            consumed=True,
            now_ms=now,
        )
        self._observe_output_intent(
            OutputIntentAdmission(
                intent=intent,
                current_fence=current_fence,
                current_context_version=current_context_version,
                floor_allows_output=floor_allows_output,
                observed_at_ms=now,
                accepted=False,
                reason=completion_reason,
                consumed=True,
                selected=False,
                authoritative_candidate=authoritative_candidate,
                authoritative_candidates=authoritative_candidates,
            )
        )
        return True

    @staticmethod
    def bridge_acknowledgement(
        phrase: str,
        *,
        fence: GenerationFence,
        context_version: int,
        expires_at_ms: int,
        now_ms: int | None = None,
    ) -> Any:
        if not is_allowlisted_device_phrase(phrase):
            raise ValueError("bridge acknowledgement is not allowlisted")
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        if context_version < 0 or expires_at_ms <= now:
            raise ValueError("bridge acknowledgement metadata is stale")
        return media_pb2.OutputIntent(
            intent_id=str(uuid.uuid4()),
            session_id=fence.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            kind=media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
            priority=100,
            created_at_ms=now,
            expires_at_ms=expires_at_ms,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=context_version,
            tts_source=phrase,
        )

    @staticmethod
    def conversation_reply(
        *,
        fence: GenerationFence,
        context_version: int,
        expires_at_ms: int,
        now_ms: int | None = None,
    ) -> Any:
        now = int(time.time() * 1_000) if now_ms is None else now_ms
        if context_version < 0 or expires_at_ms <= now:
            raise ValueError("conversation reply source metadata is invalid")
        return media_pb2.OutputIntent(
            intent_id=str(uuid.uuid4()),
            session_id=fence.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            kind=media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY,
            priority=50,
            created_at_ms=now,
            expires_at_ms=expires_at_ms,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=context_version,
        )
