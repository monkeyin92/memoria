"""Effect commit and reconcile contract tests.

Covers prepared tool effects, idempotency keys, the commit and reconcile
ports, authorization, and fail-closed handling of cancellation and rotation."""

from __future__ import annotations

import asyncio
from datetime import (
    UTC,
    datetime,
)

import pytest
from services.agent.src.runtime_profile import parse_runtime_profile
from services.agent.src.transactional_effect_commit import (
    CommitReceipt,
    CommitReconcileResult,
    CommitReconcileState,
    EffectAuthorization,
    PreparedToolEffect,
    fence_fingerprint,
)
from services.agent.tests.unit.production_wiring_support import (
    _policy,
    _prepared_intent,
    _profile,
    _runtime,
)
from services.agent.tests.unit.runtime_profile_test_helpers import TEST_VERIFY_KEY


class _ReferenceRecordingTTS:
    """Minimal TTS that accepts a speech plan with its reference context."""

    def __init__(self) -> None:
        self.reference_contexts: list[tuple[str, ...]] = []

    def bind_fence(self, _fence: object) -> None:
        return None

    def apply_speech_plan(self, **kwargs: object) -> None:
        self.reference_contexts.append(kwargs["reference_contexts"])  # type: ignore[arg-type]


class _FakePort:
    """Test-only commit port implementing the async protocol shape."""

    async def commit_prepared(
        self,
        *,
        spec: object,
        authorization: EffectAuthorization,
        prepared: PreparedToolEffect,
        idempotency_key: str,
        now: object,
    ) -> CommitReceipt | None:
        del spec, now
        return CommitReceipt(
            intent_id=f"intent-{idempotency_key}",
            idempotency_key=idempotency_key,
            committed_at=datetime.now(UTC),
            fence_fingerprint=fence_fingerprint(authorization.fence),
            authority_revision=1,
            capability=authorization.capability,
            purpose=authorization.purpose,
            resource_id=authorization.resource_id,
            intent_sha256=prepared.payload_sha256,
            evidence_refs=authorization.evidence_refs,
        )

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        del idempotency_key
        return CommitReconcileResult(CommitReconcileState.NOT_FOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize("rotation", ["subject_switch", "degrade"])
async def test_identity_rotation_drops_the_previous_subject_tts_references(
    monkeypatch: pytest.MonkeyPatch,
    rotation: str,
) -> None:
    """Batch 5 intended change: the TTS reference context holds the previous
    subject's text. It was unreachable after a rotation (lookup is by exact
    fence incl. session epoch) but stayed in memory; it is now dropped."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    tts = _ReferenceRecordingTTS()
    runtime.tts = tts
    await runtime.on_turn_committed("A 的私人安排。", input_modality="text")
    old_fence = runtime.fence
    records = runtime._generation_records
    references = records.tts_references_for(old_fence)
    assert any("A 的私人安排" in line for line in references)
    assert tts.reference_contexts[-1] == references

    if rotation == "subject_switch":
        switched = _profile(epoch=2, subject="person_parent", mode="adult_companion")
        assert runtime.apply_runtime_profile(switched) is not None
    else:
        runtime.degrade_runtime_profile()
    assert runtime.fence.session_epoch == 2

    assert records.retained_fences()["tts_references"] == ()
    assert records.tts_references_for(old_fence) == ()


@pytest.mark.asyncio
async def test_refresh_invalid_payloads_degrade_and_recovery_needs_new_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: every non-applicable authority refresh (None/tampered/wrong
    session/expired) rotates to unknown-safe; only a valid higher-epoch
    profile recovers the session."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    gate = runtime.orchestrator.runtime_profiles

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("第一轮。")
    old_fence = runtime.fence
    assert runtime.gate_llm_token(old_fence, "ok") == "ok"

    invalid_payloads = [
        None,
        _profile(epoch=1, subject="person_parent"),
        _profile(epoch=2, subject="person_parent", session_id="ses_other"),
    ]
    tampered = _profile(epoch=2, subject="person_parent")
    tampered["capabilities"] = ["chat", "tutor", "voice_clone_use"]
    invalid_payloads.append(tampered)
    for payload in invalid_payloads:

        async def _serve(payload: object = payload) -> object:
            return payload

        runtime.set_runtime_profile_refresher(_serve)  # type: ignore[arg-type]
        await _commit("又一轮。")
        assert gate.current is None
        assert runtime.fence.session_epoch >= 2
        assert runtime.gate_llm_token(old_fence, "late") is None
        assert runtime.gate_tts_audio(old_fence, b"late") is None
        assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
        assert gate.output_allowed(runtime.fence, current_fence=runtime.fence) is True
        assert gate.permits(runtime.fence, current_fence=runtime.fence) is False

    assert runtime.orchestrator.metrics.get(
        "runtime_profile_refresh_failures_total", {"reason": "invalid"}
    ) == len(invalid_payloads)

    # Valid higher-epoch recovery works through the same seam.
    async def _recover() -> object:
        return parse_runtime_profile(
            _profile(epoch=3, subject="person_parent", mode="adult_companion"),
            verify_key=TEST_VERIFY_KEY,
        )

    runtime.set_runtime_profile_refresher(_recover)  # type: ignore[arg-type]
    await _commit("恢复。")
    assert gate.current is not None
    assert runtime.fence.session_epoch == 3
    assert gate.permits(runtime.fence, current_fence=runtime.fence) is True


@pytest.mark.asyncio
async def test_epoch_rotation_cancels_old_fence_async_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: a subject switch synchronously signals/cancels every old-fence
    async owner (TTS event, LLM/TTS tasks, cancellable tool/delegation
    records) so old-subject work cannot keep running or commit, while the new
    subject's public work starts normally."""

    from services.agent.src.orchestration.task_manager import ToolTask

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    old_fence = runtime.fence
    before_discarded = list(orch.tts_pool.discarded)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking() -> None:
        started.set()
        await release.wait()

    llm_task = asyncio.create_task(_blocking())
    tts_task = asyncio.create_task(_blocking())
    tool_task = asyncio.create_task(_blocking())
    orch.set_active_llm_task(llm_task)
    orch.set_active_tts_task(tts_task)
    record = ToolTask(
        tool_task_id="old-tool-1",
        tool_name="external_write",
        fence=old_fence,
        cancellable=True,
        task=tool_task,
        cancel_event=asyncio.Event(),
        task_epoch=0,
        context_version=1,
        expires_at_ms=0,
        side_effect_policy="write",
        committed=False,
    )
    orch.task_manager.tasks["old-tool-1"] = record
    await asyncio.sleep(0)
    # Prove the tasks were really running before the switch.
    assert started.is_set()
    assert not llm_task.done() and not tts_task.done() and not tool_task.done()

    async def _switch_and_commit(text: str) -> object:
        applied = runtime.apply_runtime_profile(
            _profile(epoch=2, subject="person_parent", mode="adult_companion")
        )
        assert applied is not None
        await asyncio.sleep(0)
        # Synchronous bump already signalled/cancelled the old-fence owners.
        assert orch.tts_cancel_event().is_set()
        assert llm_task.cancelled()
        assert tts_task.cancelled()
        assert tool_task.cancelled()
        assert record.cancelled is True
        assert record.cancel_event.is_set()
        # Fail-closed while the drain barrier is pending.
        assert runtime.gate_llm_token(runtime.fence, "blocked") is None
        assert runtime.gate_tts_audio(runtime.fence, b"blocked") is None
        assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
        await _commit(text)
        return applied

    applied = await _switch_and_commit("B 第一轮。")
    assert applied is not None
    assert runtime.fence.session_epoch == 2
    release.set()
    await asyncio.sleep(0)

    # Cancellation signals fired; old work cannot commit.
    # Drain barrier observable: old TTS provider connection was discarded.
    assert old_fence in orch.tts_pool.discarded
    assert len(orch.tts_pool.discarded) == len(before_discarded) + 1
    assert runtime._pending_epoch_drain is None
    assert runtime.gate_llm_token(old_fence, "late") is None
    assert runtime.gate_tts_audio(old_fence, b"late") is None
    assert runtime.gate_tool_result(old_fence, {"ok": True}) is None
    # New-subject public work starts normally.
    assert runtime.gate_llm_token(runtime.fence, "B 输出") == "B 输出"
    assert runtime.gate_tts_audio(runtime.fence, b"B pcm") == b"B pcm"
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) == {"ok": True}
    await runtime.close()


@pytest.mark.asyncio
async def test_stubborn_side_effect_handler_commit_is_denied_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the external write is only reachable through the transactional
    effect commit port.  A handler that ignores cancellation and returns a
    payload can only prepare; the port's atomic re-verification denies the
    commit after a real identity switch (0 commits)."""

    import time
    from types import SimpleNamespace

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.identity_state import exact_fence_commit_authority
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    proceed = asyncio.Event()

    async def side_effect_writer(
        _args: dict[str, object],
        _cancel: asyncio.Event,
    ) -> PreparedToolEffect:
        await proceed.wait()
        # Malicious/faulty preparer: ignores the cancel event and returns an
        # intent; it has no external-write capability of its own.
        return PreparedToolEffect(intent="memory_write", payload={"done": True})

    commits = 0
    evidence = SimpleNamespace(expires_at=datetime(2099, 1, 1, tzinfo=UTC))

    class Port:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if not exact_fence_commit_authority(
                runtime,
                authorization.fence,
                authorization.capability,
                evidence,
            ):
                return None
            commits += 1
            return CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(Port())
    tm.register_side_effect(
        ToolSpec(
            name="external_write",
            description="committed side-effect tool",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        side_effect_writer,
    )
    old_fence = GenerationFence(
        session_id="ses-stubborn",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    rec = await tm.start(
        "external_write",
        {},
        old_fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=EffectAuthorization(
            fence=old_fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id="memory:subject-scoped",
            evidence_refs=("receipt_memory_1",),
            action_id="memory-write-123",
        ),
    )
    await asyncio.sleep(0)
    assert not rec.task.done()
    # Real identity switch while the intent is pending.
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    proceed.set()
    await rec.task
    assert rec.finished and rec.error is None
    assert commits == 0
    assert rec.cancelled is True
    # The commit-time exact-fence CAS still denies the result.
    rec.cancel_event.set()
    assert (
        tm.accept_result(
            rec.tool_task_id,
            old_fence.with_session_epoch(2),
            current_task_epoch=0,
            current_context_version=1,
            now_ms=int(time.time() * 1_000),
            relevant=True,
            current_side_effect_policy="idempotent",
        )
        is None
    )
    assert tm.accepted_broadcast_count == 0
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cancel_swallowing_commit_port_is_bounded_and_idempotent() -> None:
    """P0: a commit port that swallows CancelledError cannot hang the
    TaskManager — the first attempt returns bounded with no adopted receipt,
    and a real retry through ``TaskManager.start`` with the same action
    identity reconciles to the SAME durable receipt (exactly one commit)."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0
    seen: dict[str, CommitReceipt] = {}

    class SwallowPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if idempotency_key in seen:
                return seen[idempotency_key]  # idempotent reconcile
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Durable commit happened, then the coroutine keeps running.
                pass
            commits += 1
            receipt = CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )
            seen[idempotency_key] = receipt
            return receipt

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            if idempotency_key in seen:
                return CommitReconcileResult(CommitReconcileState.COMMITTED, seen[idempotency_key])
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(SwallowPort())
    tm.register_side_effect(
        ToolSpec(
            name="stubborn_commit",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-timeout",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="memory-write-retry",
    )
    rec = await tm.start(
        "stubborn_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.wait_for(rec.task, timeout=2)
    assert rec.finished
    # Bounded return: the TaskManager never adopted the late commit.
    assert rec.commit_receipt is None
    assert rec.cancelled is True
    assert tm.accepted_broadcast_count == 0
    # A real service retry through the TaskManager with the SAME action
    # identity (same args/fence/effect) derives the same key, reconciles to
    # the port's durable record and adopts the SAME receipt — the durable
    # intent is never duplicated.
    retry = await tm.start(
        "stubborn_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    assert retry.commit_receipt is not None
    await asyncio.wait_for(retry.task, timeout=2)
    assert retry.finished and retry.error is None
    assert commits == 1  # exactly one durable intent across both attempts
    assert retry.commit_receipt.idempotency_key == retry.tool_task_id
    assert retry.commit_receipt.fence_fingerprint == fence_fingerprint(fence)
    assert retry.commit_receipt.capability == "memory_capture"
    assert retry.commit_receipt.purpose == "memory_capture"
    assert retry.commit_receipt.evidence_refs == ("receipt_memory_1",)
    assert (
        retry.commit_receipt.intent_sha256
        == (await _prepared_intent({}, asyncio.Event())).payload_sha256
    )


@pytest.mark.asyncio
async def test_prepared_tool_effect_is_deeply_immutable_and_canonical() -> None:
    """P0: a prepared intent's payload is deep-frozen canonical JSON; nested
    mutation attempts after construction cannot change what the port sees."""

    import hashlib

    from services.agent.src.transactional_effect_commit import (
        PreparedToolEffect,
        canonical_json_bytes,
    )

    original = {"outer": {"inner": [1, 2, {"deep": "v"}]}, "flag": True}
    prepared = PreparedToolEffect(intent="memory_write", payload=original)
    assert prepared.payload_sha256 == hashlib.sha256(canonical_json_bytes(original)).hexdigest()
    snapshot = prepared.payload_json
    with pytest.raises(TypeError):
        prepared.payload["outer"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.payload["outer"]["inner"][0] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.payload["outer"]["inner"][2]["deep"] = "mutated"  # type: ignore[index]
    assert prepared.payload_json == snapshot
    assert prepared.payload_sha256 == hashlib.sha256(snapshot).hexdigest()
    # The port sees the same fixed content.
    assert prepared.payload["outer"]["inner"][2]["deep"] == "v"


@pytest.mark.asyncio
async def test_prepared_tool_effect_rejects_non_json_nan_and_oversized() -> None:
    """P0: non-JSON values, NaN/Infinity, bytes and oversized payloads are
    rejected at construction; the intent is bounded and serializable."""

    from services.agent.src.transactional_effect_commit import PreparedToolEffect

    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"fn": lambda: None})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"nan": float("nan")})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"inf": float("inf")})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"blob": b"bytes"})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="bad", payload={"data": "x" * (300 * 1024)})
    with pytest.raises(ValueError):
        PreparedToolEffect(intent="   ", payload={})


@pytest.mark.asyncio
async def test_idempotency_key_binds_full_action_identity() -> None:
    """P0: the derived key covers the full fence + capability/purpose/resource/
    evidence + action id, so two different logical actions never merge into
    one durable intent; explicit keys must be bounded non-blank."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    seen_keys: list[str] = []

    class RecordingPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, now
            seen_keys.append(idempotency_key)
            return None  # deny; we only observe the derived key

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(RecordingPort())
    tm.register_side_effect(
        ToolSpec(
            name="keyed_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )

    def _effect(fence: GenerationFence, *, resource: str) -> EffectAuthorization:
        return EffectAuthorization(
            fence=fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id=resource,
            evidence_refs=("receipt_memory_1",),
            action_id="same-logical-action",
        )

    base = GenerationFence(
        session_id="ses-key",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    # Same tool + args, but a different identity epoch: different action.
    rec1 = await tm.start(
        "keyed_write",
        {},
        base,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base, resource="memory:a"),
    )
    await asyncio.wait_for(rec1.task, timeout=2)
    rec2 = await tm.start(
        "keyed_write",
        {},
        base.with_session_epoch(2),
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base.with_session_epoch(2), resource="memory:a"),
    )
    await asyncio.wait_for(rec2.task, timeout=2)
    # Same tool/args but different resource: different action.
    rec3 = await tm.start(
        "keyed_write",
        {},
        base,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=_effect(base, resource="memory:b"),
    )
    await asyncio.wait_for(rec3.task, timeout=2)
    assert len(seen_keys) == 3
    assert len(set(seen_keys)) == 3  # no accidental merge
    # An explicit key must be bounded/non-blank.
    with pytest.raises(ValueError):
        await tm.start(
            "keyed_write",
            {},
            base,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=_effect(base, resource="memory:a"),
            idempotency_key="   ",
        )


@pytest.mark.asyncio
async def test_reconciled_receipt_mismatch_is_rejected() -> None:
    """P0: a COMMITTED reconcile outcome whose receipt does not match the
    exact action identity (fence/capability/evidence) is never adopted."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    fence = GenerationFence(
        session_id="ses-mismatch",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="memory-write-mismatch",
    )

    class WrongReceiptPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            # A receipt for a DIFFERENT fence fingerprint / capability.
            return CommitReconcileResult(
                CommitReconcileState.COMMITTED,
                CommitReceipt(
                    intent_id="intent-other",
                    idempotency_key=idempotency_key,
                    committed_at=datetime.now(UTC),
                    fence_fingerprint="1" * 64,
                    authority_revision=1,
                    capability="payment",
                    purpose="payment",
                    resource_id="payment:other",
                    intent_sha256="2" * 64,
                    evidence_refs=("receipt_other",),
                ),
            )

    tm = TaskManager()
    tm.set_effect_commit_port(WrongReceiptPort())
    tm.register_side_effect(
        ToolSpec(
            name="mismatch_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "mismatch_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
    assert tm.accepted_broadcast_count == 0


@pytest.mark.asyncio
async def test_reconcile_swallowing_port_is_bounded_and_fails_closed() -> None:
    """P0: a reconcile that swallows CancelledError cannot hang start(); the
    attempt fails closed within the bounded timeout."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class HangingReconcilePort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # swallow; never return
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(HangingReconcilePort())
    tm.register_side_effect(
        ToolSpec(
            name="hang_reconcile",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-reconcile-hang",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="hang-reconcile-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "hang_reconcile",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )


@pytest.mark.asyncio
async def test_reconcile_caller_cancellation_propagates() -> None:
    """P0: cancelling the caller while reconcile hangs must keep propagating
    (the start task ends cancelled), with the port call bounded-reaped."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class HangingReconcilePort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # swallow forever
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(HangingReconcilePort())
    tm.register_side_effect(
        ToolSpec(
            name="hang_reconcile_cancel",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-reconcile-cancel",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="hang-reconcile-cancel-1",
    )
    start_task = asyncio.create_task(
        tm.start(
            "hang_reconcile_cancel",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
    )
    await asyncio.sleep(0.02)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start_task, timeout=2)
    # The cancelled reconcile port call was bounded-reaped (detached if it
    # swallowed forever); no task record remains pending.
    assert not any(not rec.finished for rec in tm.tasks.values())


@pytest.mark.asyncio
async def test_side_effect_start_requires_committed_turn_and_authorization() -> None:
    """P0: every non-read-only start needs an authoritative committed turn AND
    a per-invocation EffectAuthorization; otherwise the port is never called."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0

    class CountingPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            del spec, authorization, prepared, idempotency_key, now
            commits += 1
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(CountingPort())
    tm.register_side_effect(
        ToolSpec(
            name="guarded_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-committed",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="guarded-write-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "guarded_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=False,
            effect=effect,
        )
    with pytest.raises(PermissionError):
        await tm.start(
            "guarded_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
        )
    assert commits == 0
    assert tm.accepted_broadcast_count == 0


@pytest.mark.asyncio
async def test_user_cancel_of_commit_port_is_bounded_and_never_adopts() -> None:
    """P0: cancelling the running tool task (user interrupt / epoch drain)
    bounded-reaps the in-flight port call, never adopts a late receipt, and a
    later retry under the same action reconciles to one durable intent."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    commits = 0
    seen: dict[str, CommitReceipt] = {}

    class SwallowPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            nonlocal commits
            if idempotency_key in seen:
                return seen[idempotency_key]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            commits += 1
            receipt = CommitReceipt(
                intent_id=f"intent-{idempotency_key}",
                idempotency_key=idempotency_key,
                committed_at=datetime.now(UTC),
                fence_fingerprint=fence_fingerprint(authorization.fence),
                authority_revision=1,
                capability=authorization.capability,
                purpose=authorization.purpose,
                resource_id=authorization.resource_id,
                intent_sha256=prepared.payload_sha256,
                evidence_refs=authorization.evidence_refs,
            )
            seen[idempotency_key] = receipt
            return receipt

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            if idempotency_key in seen:
                return CommitReconcileResult(CommitReconcileState.COMMITTED, seen[idempotency_key])
            return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    tm = TaskManager()
    tm.set_effect_commit_port(SwallowPort())
    tm.register_side_effect(
        ToolSpec(
            name="cancel_commit",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-user-cancel",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="user-cancel-write-1",
    )
    rec = await tm.start(
        "cancel_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.sleep(0)
    assert not rec.task.done()
    rec.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(rec.task, timeout=2)
    assert rec.cancelled is True
    assert rec.commit_receipt is None
    assert tm.accepted_broadcast_count == 0
    # Retry under the same action: reconciles to the SAME durable receipt.
    retry = await tm.start(
        "cancel_commit",
        {},
        fence,
        task_epoch=0,
        context_version=1,
        expires_at_ms=int(time.time() * 1_000) + 60_000,
        side_effect_policy="idempotent",
        committed=True,
        effect=effect,
    )
    await asyncio.wait_for(retry.task, timeout=2)
    assert retry.commit_receipt is not None
    assert commits == 1
    assert retry.commit_receipt.fence_fingerprint == fence_fingerprint(fence)


@pytest.mark.asyncio
async def test_set_effect_commit_port_requires_reconcile() -> None:
    """P0: a port lacking reconcile() is rejected at install time."""

    from services.agent.src.orchestration.task_manager import TaskManager

    class CommitOnlyPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

    tm = TaskManager()
    with pytest.raises(TypeError):
        tm.set_effect_commit_port(CommitOnlyPort())


@pytest.mark.asyncio
async def test_reconcile_unknown_state_fails_closed() -> None:
    """P0: an UNKNOWN reconcile outcome must never be re-done under a new key."""

    import time

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    class UnknownPort:
        async def commit_prepared(
            self,
            *,
            spec: object,
            authorization: EffectAuthorization,
            prepared: PreparedToolEffect,
            idempotency_key: str,
            now: object,
        ) -> CommitReceipt | None:
            del spec, authorization, prepared, idempotency_key, now
            return None

        async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
            del idempotency_key
            return CommitReconcileResult(CommitReconcileState.UNKNOWN)

    tm = TaskManager()
    tm.set_effect_commit_port(UnknownPort())
    tm.register_side_effect(
        ToolSpec(
            name="unknown_write",
            description="t",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=0.05,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        _prepared_intent,
    )
    fence = GenerationFence(
        session_id="ses-unknown",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    effect = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="memory:subject-scoped",
        evidence_refs=("receipt_memory_1",),
        action_id="unknown-write-1",
    )
    with pytest.raises(PermissionError):
        await tm.start(
            "unknown_write",
            {},
            fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )


async def test_side_effect_tool_registration_fails_closed_without_port() -> None:
    """P0: without a transactional effect commit port (the production
    default), every side-effect registration fails closed; read-only tools
    stay available."""

    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    async def _handler(
        _args: dict[str, object],
        _cancel: asyncio.Event,
    ) -> dict[str, object]:
        return {}

    tm = TaskManager()
    with pytest.raises(PermissionError):
        tm.register(
            ToolSpec(
                name="w1",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose="memory_capture",
            ),
            _handler,
        )
    # A writer handler cannot masquerade as a preparer for side effects.
    with pytest.raises(PermissionError):
        tm.register_side_effect(
            ToolSpec(
                name="w0",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose="memory_capture",
            ),
            _handler,  # type: ignore[arg-type]  # wrong preparer shape
        )
    # Missing purpose is rejected even with a port installed.
    with pytest.raises(TypeError):
        tm.set_effect_commit_port(object())  # not a commit port -> rejected
    tm.set_effect_commit_port(_FakePort())
    with pytest.raises(PermissionError):
        tm.register_side_effect(
            ToolSpec(
                name="w2",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_purpose=None,
            ),
            _handler,  # type: ignore[arg-type]
        )
    assert "w1" not in tm.specs and "w2" not in tm.specs and "w0" not in tm.specs
    # Read-only tools register fine without any port.
    tm.register(
        ToolSpec(
            name="probe",
            description="t",
            input_schema={},
            cancellable=False,
            idempotent=False,
            timeout_s=5,
            contains_sensitive_data=False,
            side_effect_policy="read_only",
        ),
        _handler,
    )
    assert "probe" in tm.specs

