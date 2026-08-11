"""Background tool TaskManager with tool_epoch isolation (ch.13/17)."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.agent.src.action_policy_client import (
    ActionPolicyPort,
    ActionPolicyUnavailable,
    VerifiedActionReceipt,
)
from services.agent.src.contracts.ids import GenerationFence, new_task_id
from services.agent.src.obligation_executor import execute_action_obligations
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.transactional_effect_commit import (
    CommitReceipt,
    CommitReconcileState,
    EffectAuthorization,
    PreparedToolEffect,
    TransactionalToolEffectCommitPort,
    canonical_json_bytes,
    fence_fingerprint,
    freeze_json_value,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Immutable static tool authority snapshot.

    Frozen at registration time (external mutation of the original spec or
    schema can never change an already-registered tool's permission
    boundary); per-invocation dynamic authority (resource id, fresh evidence
    refs, full fence) lives in :class:`EffectAuthorization`, never here.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    cancellable: bool
    idempotent: bool
    timeout_s: float
    contains_sensitive_data: bool = False
    side_effect_policy: str = "unspecified"
    required_capability: str | None = None
    required_purpose: str | None = None
    data_classification: str = "private"
    safety_state: str = "normal"

    def __post_init__(self) -> None:
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("input_schema must be a mapping")
        object.__setattr__(self, "input_schema", freeze_json_value(dict(self.input_schema)))


@dataclass
class ToolTask:
    tool_task_id: str
    tool_name: str
    fence: GenerationFence
    cancellable: bool
    task: asyncio.Task[Any]
    cancel_event: asyncio.Event
    task_epoch: int
    context_version: int
    expires_at_ms: int
    side_effect_policy: str
    committed: bool
    relevance: Callable[[], bool] | None = None
    started_mono_ns: int = field(default_factory=time.monotonic_ns)
    cancelled: bool = False
    finished: bool = False
    result: Any = None
    error: str | None = None
    commit_receipt: CommitReceipt | None = None


ToolHandler = Callable[[dict[str, Any], asyncio.Event], Awaitable[Any]]
ToolEffectPreparer = Callable[
    [dict[str, Any], asyncio.Event],
    Awaitable[PreparedToolEffect],
]


@dataclass
class TaskManager:
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    tasks: dict[str, ToolTask] = field(default_factory=dict)
    handlers: dict[str, ToolHandler] = field(default_factory=dict)
    preparers: dict[str, ToolEffectPreparer] = field(default_factory=dict)
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    _effect_commit_port: TransactionalToolEffectCommitPort | None = field(
        default=None, init=False, repr=False
    )
    _action_policy: ActionPolicyPort | None = field(default=None, init=False, repr=False)
    _profile_for_fence: Callable[[GenerationFence], VerifiedRuntimeProfile | None] | None = field(
        default=None, init=False, repr=False
    )
    _current_fence: Callable[[], GenerationFence] | None = field(
        default=None, init=False, repr=False
    )
    # Port calls that swallowed CancelledError past the bounded reap deadline.
    # Kept referenced so the event loop can reap them; their results are
    # never adopted (the port's idempotency key makes any durably recorded
    # intent discoverable by a later reconciliation).
    _detached_commit_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False
    )
    # Idempotency keys whose durable commit this manager has observed; a
    # retry under the same key adopts the original receipt, never re-commits.
    _idempotency_records: dict[str, CommitReceipt] = field(
        default_factory=dict, init=False, repr=False
    )
    stale_broadcast_count: int = 0
    accepted_broadcast_count: int = 0

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
    ) -> None:
        if spec.side_effect_policy not in {"none", "read_only"}:
            raise PermissionError("side-effect tools must be registered via register_side_effect")
        if not spec.cancellable and (
            spec.contains_sensitive_data or spec.side_effect_policy not in {"none", "read_only"}
        ):
            raise PermissionError("non-cancellable tools must be NONE/READ_ONLY and non-sensitive")
        self.specs[spec.name] = spec
        self.handlers[spec.name] = handler

    def register_side_effect(
        self,
        spec: ToolSpec,
        preparer: ToolEffectPreparer,
    ) -> None:
        """Register a side-effect tool with a typed preparer only.

        The preparer returns an immutable :class:`PreparedToolEffect` intent;
        it has no external-write capability.  Every adapter/writer lives in
        the transactional effect commit port, which must already be wired.
        """

        if spec.side_effect_policy in {"none", "read_only"}:
            raise PermissionError("read-only tools must be registered via register()")
        if spec.required_purpose is None:
            raise PermissionError("side-effect tools require a purpose")
        if self._effect_commit_port is None:
            raise PermissionError("side-effect tools require a transactional effect commit port")
        if spec.required_capability is None:
            raise PermissionError("side-effect tools require a canonical required_capability")
        if not spec.idempotent:
            raise PermissionError("side-effect tools must be idempotent")
        if not spec.cancellable and (
            spec.contains_sensitive_data or spec.side_effect_policy not in {"none", "read_only"}
        ):
            raise PermissionError("non-cancellable tools must be NONE/READ_ONLY and non-sensitive")
        self.specs[spec.name] = spec
        self.preparers[spec.name] = preparer

    def set_effect_commit_port(
        self,
        port: TransactionalToolEffectCommitPort | None,
    ) -> None:
        """Install the transactional effect commit port (typed deep seam).

        The TaskManager never writes an external side effect directly: it
        calls the port's single atomic ``commit_prepared`` operation, which
        re-verifies the complete current fence + signed RuntimeProfile +
        action receipt and durably records the intent.  Without a wired port
        (production default), side-effect tools cannot register (fail closed).
        """

        if port is not None and (
            not callable(getattr(port, "commit_prepared", None))
            or not callable(getattr(port, "reconcile", None))
        ):
            raise TypeError("effect commit port must implement commit_prepared and reconcile")
        self._effect_commit_port = port

    def set_action_policy_client(
        self,
        client: ActionPolicyPort | None,
        *,
        profile_for_fence: Callable[[GenerationFence], VerifiedRuntimeProfile | None] | None = None,
        current_fence: Callable[[], GenerationFence] | None = None,
    ) -> None:
        """Install the action-time policy gate for side-effect tools.

        A configured gate requires both a profile resolver and a current-fence
        resolver.  Without them the manager cannot prove actor/subject/resource
        ownership or reject a fence that became stale while the policy request
        was in flight, so installation fails closed.
        """

        if client is not None and (
            not callable(getattr(client, "authorize", None))
            or profile_for_fence is None
            or current_fence is None
        ):
            raise TypeError(
                "action policy requires authorize, profile_for_fence and current_fence"
            )
        self._action_policy = client
        self._profile_for_fence = profile_for_fence
        self._current_fence = current_fence

    def _fence_is_current(self, fence: GenerationFence) -> bool:
        resolver = self._current_fence
        if resolver is None:
            return True
        try:
            return fence.matches(resolver())
        except Exception:
            return False

    async def _authorize_side_effect(
        self,
        spec: ToolSpec,
        fence: GenerationFence,
        effect: EffectAuthorization | None,
    ) -> EffectAuthorization:
        """Obtain one exact action receipt before any side-effect work."""

        policy = self._action_policy
        profile_for_fence = self._profile_for_fence
        if policy is None:
            if effect is None:
                raise PermissionError(
                    "side-effect tools require a per-invocation EffectAuthorization"
                )
            return effect
        if not self._fence_is_current(fence):
            raise PermissionError("side-effect action fence is stale")
        if profile_for_fence is None or spec.required_capability is None:
            raise PermissionError("side-effect action policy context is unavailable")
        profile = profile_for_fence(fence)
        if profile is None:
            raise PermissionError("side-effect runtime profile is unavailable")
        result = await policy.authorize(
            profile=profile.profile,
            fence=fence,
            capability=spec.required_capability,
            data_classification=spec.data_classification,
            safety_state=spec.safety_state,
            accept_obligations=True,
        )
        if isinstance(result, ActionPolicyUnavailable):
            raise PermissionError(f"action policy unavailable: {result.reason}")
        if not isinstance(result, VerifiedActionReceipt):
            raise PermissionError("action policy returned an invalid result")
        if not self._fence_is_current(fence):
            raise PermissionError("side-effect action fence became stale")
        if result.obligations and result.effect != "allow_with_obligations":
            raise PermissionError("action policy obligations have no allow-with-obligations effect")
        if result.effect == "allow_with_obligations" and not execute_action_obligations(
            profile, result.receipt
        ):
            raise PermissionError("action policy obligations cannot be executed")
        if result.effect not in {"allow", "allow_with_obligations"}:
            # ``verify_action_receipt`` already rejects deny; keep the explicit
            # branch so a future result type cannot accidentally become allow.
            raise PermissionError("action policy did not allow the side effect")
        action_resource_id = result.receipt.action_resource_fence.action_resource_id
        return EffectAuthorization(
            fence=fence,
            capability=result.capability,
            purpose=result.purpose,
            resource_id=action_resource_id,
            evidence_refs=(result.receipt_id,),
            action_id=effect.action_id if effect is not None else None,
            obligations=result.obligations,
            runtime_profile=profile,
            policy_receipt=result.receipt,
        )

    async def start(
        self,
        tool_name: str,
        args: dict[str, Any],
        fence: GenerationFence,
        *,
        task_epoch: int,
        context_version: int,
        expires_at_ms: int,
        side_effect_policy: str,
        committed: bool,
        relevance: Callable[[], bool] | None = None,
        idempotency_key: str | None = None,
        effect: EffectAuthorization | None = None,
    ) -> ToolTask:
        if tool_name not in self.handlers and tool_name not in self.preparers:
            raise KeyError(f"unknown tool: {tool_name}")
        if task_epoch < 0 or context_version < 0:
            raise ValueError("task metadata must be non-negative")
        spec = self.specs[tool_name]
        if expires_at_ms <= int(time.time() * 1_000):
            raise ValueError("task expiry must be in the future")
        if side_effect_policy != spec.side_effect_policy:
            raise PermissionError("task side-effect policy does not match tool authority")
        if side_effect_policy not in {"none", "read_only"} and not committed:
            raise PermissionError("side-effect task requires an authoritative committed turn")
        if side_effect_policy not in {"none", "read_only"}:
            effect = await self._authorize_side_effect(spec, fence, effect)
            if effect.capability != spec.required_capability:
                raise PermissionError("authorization capability does not match the tool spec")
            if effect.purpose != spec.required_purpose:
                raise PermissionError("authorization purpose does not match the tool spec")
            if not effect.fence.matches(fence):
                raise PermissionError("authorization fence does not match the task fence")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 512
        ):
            raise ValueError("explicit idempotency_key must be a bounded non-blank string")
        # The idempotency key is stable across retries: it identifies the
        # complete action identity (tool + canonical args + full fence +
        # capability/purpose/resource/evidence + optional logical action id),
        # never a random task instance and never just tool+args.
        key = idempotency_key
        if key is None:
            if spec.side_effect_policy in {"none", "read_only"}:
                # Read-only tools have no durable intent; never force
                # runtime objects (e.g. a GenerationFence in args) into a
                # canonical JSON key.
                key = new_task_id()
            else:
                assert effect is not None  # validated above for side effects
                try:
                    key = hashlib.sha256(
                        canonical_json_bytes(
                            {
                                "tool": tool_name,
                                "args": args,
                                "fence": {
                                    "session_id": fence.session_id,
                                    "turn_id": fence.turn_id,
                                    "generation_id": fence.generation_id,
                                    "tool_epoch": fence.tool_epoch,
                                    "session_epoch": fence.session_epoch,
                                },
                                "capability": effect.capability,
                                "purpose": effect.purpose,
                                "resource_id": effect.resource_id,
                                "evidence_refs": list(effect.evidence_refs),
                                "action_id": effect.action_id,
                            }
                        )
                    ).hexdigest()
                except ValueError as exc:
                    raise ValueError(f"tool args are not canonical JSON: {exc}") from None
        if key in self._idempotency_records:
            # A retry under the same stable key adopts the original receipt;
            # the durable intent is never duplicated.  The receipt is
            # re-verified against the exact action before adoption.
            receipt = self._idempotency_records[key]
            if not self._validate_receipt(receipt, key, fence, effect):
                raise PermissionError(
                    "stored idempotency record does not match the action; fail closed"
                )
            return await self._completed_record(
                tool_name,
                args,
                fence,
                spec,
                side_effect_policy,
                committed,
                task_epoch,
                context_version,
                expires_at_ms,
                relevance,
                receipt,
                key,
            )
        port = self._effect_commit_port
        if port is not None and spec.side_effect_policy not in {"none", "read_only"}:
            # Bounded reconcile: run as its own task so a port that swallows
            # CancelledError cannot hang start(); a caller cancellation keeps
            # propagating after the bounded cancel/reap.
            reconcile_task = asyncio.create_task(
                port.reconcile(idempotency_key=key), name=f"reconcile-{key[:8]}"
            )
            try:
                outcome = await asyncio.wait_for(
                    asyncio.shield(reconcile_task), timeout=spec.timeout_s
                )
            except TimeoutError:
                await self._reap_late_commit(reconcile_task)
                raise PermissionError(
                    "effect commit port reconcile did not return; fail closed"
                ) from None
            except asyncio.CancelledError:
                await self._reap_late_commit(reconcile_task)
                raise
            if outcome.state is CommitReconcileState.COMMITTED:
                assert outcome.receipt is not None
                if not self._validate_receipt(outcome.receipt, key, fence, effect):
                    raise PermissionError(
                        "reconciled receipt does not match the exact action; fail closed"
                    )
                self._idempotency_records[key] = outcome.receipt
                return await self._completed_record(
                    tool_name,
                    args,
                    fence,
                    spec,
                    side_effect_policy,
                    committed,
                    task_epoch,
                    context_version,
                    expires_at_ms,
                    relevance,
                    outcome.receipt,
                    key,
                )
            if outcome.state is CommitReconcileState.UNKNOWN:
                raise PermissionError(
                    "effect commit outcome is UNKNOWN; never redo under a new key"
                )
        cancel_event = asyncio.Event()
        tool_task_id = new_task_id()

        async def _run() -> Any:
            try:
                if spec.side_effect_policy not in {"none", "read_only"}:
                    # The only external-write path is the transactional
                    # effect commit port: one atomic verify-and-record call.
                    # The preparer only assembles an immutable intent.
                    assert effect is not None  # start() validated the authorization
                    if not self._fence_is_current(fence):
                        rec.cancelled = True
                        return None
                    if tool_name not in self.preparers:
                        rec.cancelled = True
                        return None
                    prepared = await asyncio.wait_for(
                        self.preparers[tool_name](args, cancel_event),
                        timeout=spec.timeout_s,
                    )
                    if not isinstance(prepared, PreparedToolEffect):
                        rec.cancelled = True
                        return None
                    if not self._fence_is_current(fence):
                        rec.cancelled = True
                        return None
                    port = self._effect_commit_port
                    if port is None:
                        rec.cancelled = True
                        return None
                    # Bounded verify-and-record: run the port call as its own
                    # task so a port that swallows CancelledError can never
                    # turn the manager's timeout into a hang, and a late
                    # return is never adopted as a successful commit.
                    port_task = asyncio.create_task(
                        port.commit_prepared(
                            spec=spec,
                            authorization=effect,
                            prepared=prepared,
                            idempotency_key=key,
                            now=datetime.now(UTC),
                        ),
                        name=f"commit-{tool_task_id}",
                    )
                    try:
                        receipt = await asyncio.wait_for(
                            asyncio.shield(port_task), timeout=spec.timeout_s
                        )
                    except TimeoutError:
                        await self._reap_late_commit(port_task)
                        rec.cancelled = True
                        return None
                    except asyncio.CancelledError:
                        # User cancel / epoch drain: cancel the port call too,
                        # bounded-reap or detach, and never adopt its result.
                        await self._reap_late_commit(port_task)
                        raise
                    if receipt is None:
                        rec.cancelled = True
                        return None
                    # A late return after timeout/cancel is discarded: the
                    # caller never re-commits and the port's idempotency key
                    # makes any already-recorded intent discoverable.
                    # The receipt is re-verified against the exact action and
                    # intent before it is adopted.
                    if (
                        not self._validate_receipt(receipt, key, fence, effect)
                        or receipt.intent_sha256 != prepared.payload_sha256
                    ):
                        rec.cancelled = True
                        return None
                    rec.commit_receipt = receipt
                    self._idempotency_records[key] = receipt
                    return {"commit_receipt": receipt}
                handler = self.handlers[tool_name]
                return await asyncio.wait_for(
                    handler(args, cancel_event),
                    timeout=spec.timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return {"error": "tool_timeout", "tool": tool_name}

        aio_task = asyncio.create_task(_run(), name=f"tool-{tool_name}-{tool_task_id}")
        rec = ToolTask(
            tool_task_id=tool_task_id,
            tool_name=tool_name,
            fence=fence,
            cancellable=spec.cancellable,
            task=aio_task,
            cancel_event=cancel_event,
            task_epoch=task_epoch,
            context_version=context_version,
            expires_at_ms=expires_at_ms,
            side_effect_policy=side_effect_policy,
            committed=committed,
            relevance=relevance,
        )
        self.tasks[tool_task_id] = rec

        def _done(t: asyncio.Task[Any]) -> None:
            rec.finished = True
            if t.cancelled():
                rec.cancelled = True
                return
            exc = t.exception()
            if exc is not None:
                rec.error = str(exc)
            else:
                rec.result = t.result()

        aio_task.add_done_callback(_done)
        return rec

    async def _reap_late_commit(self, port_task: asyncio.Task[Any]) -> None:
        """Cancel a timed-out/cancelled port call and bound its late return.

        A port that swallows CancelledError is detached after the bounded reap
        deadline and its result is never adopted; the durable record (if any)
        stays discoverable via reconcile() under the same idempotency key.
        """

        port_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(port_task), timeout=0.15)
        except (TimeoutError, asyncio.CancelledError):
            self._detached_commit_tasks.add(port_task)
            port_task.add_done_callback(self._detached_commit_tasks.discard)

    def _validate_receipt(
        self,
        receipt: CommitReceipt,
        key: str,
        fence: GenerationFence,
        effect: EffectAuthorization | None,
    ) -> bool:
        """Verify a receipt against the exact action identity before adopting.

        A receipt for a different key, fence, capability, purpose, resource or
        evidence set is never adopted — it cannot authorise this action.
        """

        return (
            receipt.idempotency_key == key
            and receipt.fence_fingerprint == fence_fingerprint(fence)
            and effect is not None
            and receipt.capability == effect.capability
            and receipt.purpose == effect.purpose
            and receipt.resource_id == effect.resource_id
            and receipt.evidence_refs == effect.evidence_refs
        )

    async def _completed_record(
        self,
        tool_name: str,
        args: dict[str, Any],
        fence: GenerationFence,
        spec: ToolSpec,
        side_effect_policy: str,
        committed: bool,
        task_epoch: int,
        context_version: int,
        expires_at_ms: int,
        relevance: Callable[[], bool] | None,
        receipt: CommitReceipt,
        key: str,
    ) -> ToolTask:
        """Build a ToolTask that already carries a reconciled durable receipt."""

        cancel_event = asyncio.Event()

        async def _done() -> dict[str, object]:
            return {"commit_receipt": receipt}

        aio_task = asyncio.create_task(_done(), name=f"tool-{tool_name}-reconciled")
        rec = ToolTask(
            tool_task_id=key,
            tool_name=tool_name,
            fence=fence,
            cancellable=spec.cancellable,
            task=aio_task,
            cancel_event=cancel_event,
            task_epoch=task_epoch,
            context_version=context_version,
            expires_at_ms=expires_at_ms,
            side_effect_policy=side_effect_policy,
            committed=committed,
            relevance=relevance,
            commit_receipt=receipt,
        )
        self.tasks[key] = rec

        def _on_done(t: asyncio.Task[Any]) -> None:
            rec.finished = True
            if t.cancelled():
                rec.cancelled = True
                return
            exc = t.exception()
            if exc is not None:
                rec.error = str(exc)
            else:
                rec.result = t.result()

        aio_task.add_done_callback(_on_done)
        return rec

    async def cancel_cancellable(self, fence: GenerationFence) -> None:
        for rec in list(self.tasks.values()):
            if rec.finished:
                continue
            if rec.fence.matches(fence):
                if rec.cancellable and not rec.task.done():
                    # Cooperative cancel first (set event so handlers can exit cleanly).
                    rec.cancel_event.set()
                    rec.cancelled = True
                    try:
                        await asyncio.wait_for(asyncio.shield(rec.task), timeout=0.15)
                    except (TimeoutError, asyncio.CancelledError):
                        if not rec.task.done():
                            rec.task.cancel()
                            # Bounded reap: a handler that swallows
                            # CancelledError must not block the drain loop.
                            try:
                                await asyncio.wait_for(asyncio.shield(rec.task), timeout=0.15)
                            except (TimeoutError, asyncio.CancelledError):
                                pass

    def accept_result(
        self,
        tool_task_id: str,
        current_fence: GenerationFence,
        *,
        current_task_epoch: int,
        current_context_version: int,
        now_ms: int,
        relevant: bool,
        current_side_effect_policy: str,
    ) -> Any | None:
        """Return payload only when every supplied authority gate matches."""
        rec = self.tasks.get(tool_task_id)
        if rec is None or not rec.finished or rec.cancelled or rec.error is not None:
            return None
        try:
            relevance_current = rec.relevance is None or rec.relevance()
        except Exception:
            relevance_current = False
        stale = (
            not rec.fence.matches(current_fence)
            or rec.task_epoch != current_task_epoch
            or rec.context_version != current_context_version
            or now_ms >= rec.expires_at_ms
            or rec.side_effect_policy != current_side_effect_policy
            or not rec.committed
            or not relevant
            or not relevance_current
        )
        if stale:
            self.stale_broadcast_count += 1
            self.metrics.inc_stale_result_dropped("tool")
            return None
        self.accepted_broadcast_count += 1
        return rec.result

    async def cancel(self, tool_task_id: str) -> bool:
        rec = self.tasks.get(tool_task_id)
        if rec is None or rec.task.done() or not rec.cancellable:
            return False
        rec.cancel_event.set()
        rec.cancelled = True
        try:
            await asyncio.wait_for(asyncio.shield(rec.task), timeout=0.15)
        except (TimeoutError, asyncio.CancelledError):
            if not rec.task.done():
                rec.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(rec.task), timeout=0.15)
                except (TimeoutError, asyncio.CancelledError):
                    pass
        return True

    async def wait_result(
        self,
        tool_task_id: str,
        current_fence: GenerationFence,
        *,
        current_task_epoch: int,
        current_context_version: int,
        now_ms: int,
        relevant: bool,
        current_side_effect_policy: str,
    ) -> Any | None:
        rec = self.tasks[tool_task_id]
        try:
            await rec.task
        except asyncio.CancelledError:
            return None
        return self.accept_result(
            tool_task_id,
            current_fence,
            current_task_epoch=current_task_epoch,
            current_context_version=current_context_version,
            now_ms=now_ms,
            relevant=relevant,
            current_side_effect_policy=current_side_effect_policy,
        )

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if not t.finished and not t.cancelled)


def spoken_result_summarizer(payload: dict[str, Any], *, max_sentences: int = 3) -> str:
    """Compress tool payload to 1–3 spoken sentences. Never dump raw JSON."""
    if "error" in payload:
        return "刚才这项查询没有成功，我们稍后再试一次。"
    if "summary" in payload and isinstance(payload["summary"], str):
        text = payload["summary"].strip()
    elif "spoken" in payload and isinstance(payload["spoken"], str):
        text = payload["spoken"].strip()
    else:
        # Pick a few short string values
        parts = [str(v) for v in payload.values() if isinstance(v, str | int | float)][:3]
        text = "，".join(parts) if parts else ""
    # Keep at most max_sentences by strong punctuation.
    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？":
            chunks.append(buf)
            buf = ""
            if len(chunks) >= max_sentences:
                break
    if buf and len(chunks) < max_sentences:
        chunks.append(buf if buf.endswith(("。", "！", "？")) else buf + "。")
    return "".join(chunks)[:200]


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
