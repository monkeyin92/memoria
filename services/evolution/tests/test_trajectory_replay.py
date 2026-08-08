from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.control_api.app.account_gate import AccountOperationGate
from services.evolution.curation import EvolutionControlPlane
from services.evolution.replay import (
    OfflineTrajectoryReplayRequest,
    OfflineTrajectoryReplayWorker,
)
from services.evolution.store import EvolutionConflictError, EvolutionStore
from services.evolution.trajectory import (
    CanonicalTrajectoryError,
    TrajectoryAssessment,
    TrajectoryReplayInput,
)


class _StubEvaluator:
    evaluator_version = "offline-rubric-v1"

    def __init__(self, assessment: TrajectoryAssessment | None) -> None:
        self.assessment = assessment
        self.inputs: list[TrajectoryReplayInput] = []

    async def evaluate(self, trajectory: TrajectoryReplayInput) -> TrajectoryAssessment | None:
        self.inputs.append(trajectory)
        return self.assessment


def _assessment(*, completed: bool = False) -> TrajectoryAssessment:
    return TrajectoryAssessment(
        evaluator_version="offline-rubric-v1",
        task_family="weather",
        environment_version="weather-replay-v1",
        task_completed=completed,
        expected_state={"target_date": "requested"},
        actual_state={"target_date": "requested" if completed else "today"},
        allowed_tools=("weather.lookup",),
        commitment_action_consistent=True,
        quality_dimensions=(("factuality", "pass" if completed else "fail"),),
        failure_code=None if completed else "target_date_mismatch",
        diagnosis_code=None if completed else "forecast_date_not_bound",
        input_tokens=12,
        output_tokens=9,
        latency_ms=125.0,
    )


async def _record_pair(
    archive: LifeArchive,
    *,
    account_id: str,
    speaker_class: str,
    event_suffix: str,
    actual_heard: bool = True,
) -> tuple[str, str, datetime]:
    occurred_at = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    user_event_id = f"user-{event_suffix}"
    assistant_event_id = f"assistant-{event_suffix}"
    await archive.record(
        EvidenceEvent(
            event_id=user_event_id,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=occurred_at,
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="funasr.authoritative_final",
            session_id=f"session-{event_suffix}",
            turn_id=3,
            generation_id=5,
            payload={
                "text": "明天南京天气如何？",
                "tool_epoch": 2,
                "history_eligible": speaker_class == "owner",
                "owner_projection_eligible": speaker_class == "owner",
                "speaker_reason_code": "formal_owner" if speaker_class == "owner" else "guest",
                "speaker_profile_id": "owner-profile",
            },
        )
    )
    await archive.record(
        EvidenceEvent(
            event_id=assistant_event_id,
            account_id=account_id,
            event_type="assistant.playout_stopped",
            occurred_at=occurred_at,
            speaker_class="assistant",
            source="generation_fence.actual_heard",
            session_id=f"session-{event_suffix}",
            turn_id=3,
            generation_id=5,
            payload={
                "text": "明天有雨。",
                "tool_epoch": 2,
                "actual_heard": actual_heard,
            },
        )
    )
    return user_event_id, assistant_event_id, occurred_at


@pytest.mark.asyncio
async def test_offline_replay_emits_idempotent_three_layer_failure_without_raw_signal(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    user_event_id, assistant_event_id, occurred_at = await _record_pair(
        archive,
        account_id="owner-a",
        speaker_class="owner",
        event_suffix="owner",
    )
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    evaluator = _StubEvaluator(_assessment())
    worker = OfflineTrajectoryReplayWorker(
        archive,
        EvolutionControlPlane(store, trusted_root_sha256="a" * 64),
        evaluator,
    )
    request = OfflineTrajectoryReplayRequest(
        evaluation_id="weather-owner-replay-v1",
        account_id="owner-a",
        user_event_id=user_event_id,
        assistant_event_id=assistant_event_id,
    )

    first = await worker.replay(request)
    second = await worker.replay(request)

    assert first == second
    assert first.signal_id is not None
    assert first.skipped is False
    assert len(evaluator.inputs) == 2
    assert evaluator.inputs[0].scope == "owner_private"
    assert evaluator.inputs[0].account_id == "owner-a"
    assert evaluator.inputs[0].user["text"] == "明天南京天气如何？"
    signal = store.get_signal(first.signal_id)
    assert signal.scope == "owner_private"
    assert signal.account_id == "owner-a"
    assert signal.created_at == occurred_at
    assert signal.result.verdict == "fail"
    assert signal.process.verdict == "pass"
    assert signal.quality.verdict == "fail"
    assert signal.failure_code == "target_date_mismatch"
    assert signal.artifact_versions == (
        ("trajectory_evaluator", "offline-rubric-v1"),
    )
    serialized = repr(signal.to_dict())
    assert "明天南京天气如何" not in serialized
    assert "明天有雨" not in serialized
    assert len(store.list_signals()) == 1


@pytest.mark.asyncio
async def test_replay_rejects_inconsistent_retry_instead_of_replacing_evidence(tmp_path: Path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    user_event_id, assistant_event_id, _ = await _record_pair(
        archive,
        account_id="owner-a",
        speaker_class="owner",
        event_suffix="immutable",
    )
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    evaluator = _StubEvaluator(_assessment(completed=False))
    worker = OfflineTrajectoryReplayWorker(
        archive,
        EvolutionControlPlane(store, trusted_root_sha256="b" * 64),
        evaluator,
    )
    request = OfflineTrajectoryReplayRequest(
        evaluation_id="weather-immutable-replay-v1",
        account_id="owner-a",
        user_event_id=user_event_id,
        assistant_event_id=assistant_event_id,
    )
    first = await worker.replay(request)
    evaluator.assessment = _assessment(completed=True)

    with pytest.raises(EvolutionConflictError, match="learning signal id is immutable"):
        await worker.replay(request)

    assert first.signal_id is not None
    assert store.get_signal(first.signal_id).failed is True
    assert len(store.list_signals()) == 1

    with pytest.raises(EvolutionConflictError, match="already evaluated by this evaluator version"):
        await worker.replay(
            OfflineTrajectoryReplayRequest(
                evaluation_id="weather-immutable-replay-rekeyed",
                account_id="owner-a",
                user_event_id=user_event_id,
                assistant_event_id=assistant_event_id,
            )
        )
    assert len(store.list_signals()) == 1


@pytest.mark.asyncio
async def test_global_replay_is_redacted_and_invalid_delivery_never_reaches_evaluator(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    user_event_id, assistant_event_id, _ = await _record_pair(
        archive,
        account_id="guest-account",
        speaker_class="guest",
        event_suffix="guest",
    )
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    evaluator = _StubEvaluator(_assessment())
    worker = OfflineTrajectoryReplayWorker(
        archive,
        EvolutionControlPlane(store, trusted_root_sha256="c" * 64),
        evaluator,
    )
    result = await worker.replay(
        OfflineTrajectoryReplayRequest(
            evaluation_id="weather-guest-replay-v1",
            account_id="guest-account",
            user_event_id=user_event_id,
            assistant_event_id=assistant_event_id,
        )
    )

    assert result.signal_id is not None
    assert evaluator.inputs[0].scope == "global_redacted"
    assert evaluator.inputs[0].account_id is None
    assert evaluator.inputs[0].speaker.profile_id is None
    assert "text" not in evaluator.inputs[0].user
    assert "text" not in evaluator.inputs[0].assistant
    signal = store.get_signal(result.signal_id)
    assert signal.scope == "global_redacted"
    assert signal.account_id is None
    assert signal.speaker.profile_id is None

    bad_user_id, bad_assistant_id, _ = await _record_pair(
        archive,
        account_id="guest-account",
        speaker_class="guest",
        event_suffix="not-heard",
        actual_heard=False,
    )
    input_count = len(evaluator.inputs)
    with pytest.raises(CanonicalTrajectoryError, match="actual-heard"):
        await worker.replay(
            OfflineTrajectoryReplayRequest(
                evaluation_id="weather-not-heard-replay-v1",
                account_id="guest-account",
                user_event_id=bad_user_id,
                assistant_event_id=bad_assistant_id,
            )
        )
    assert len(evaluator.inputs) == input_count


@pytest.mark.asyncio
async def test_replay_skips_owner_snapshot_after_deletion_fence(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    user_event_id, assistant_event_id, _ = await _record_pair(
        archive,
        account_id="owner-deleting",
        speaker_class="owner",
        event_suffix="deleting",
    )
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    store.mark_account_deleting("owner-deleting")
    evaluator = _StubEvaluator(_assessment())
    worker = OfflineTrajectoryReplayWorker(
        archive,
        EvolutionControlPlane(store, trusted_root_sha256="d" * 64),
        evaluator,
        account_read_guard=AccountOperationGate().sync_read,
    )

    result = await worker.replay(
        OfflineTrajectoryReplayRequest(
            evaluation_id="owner-deleting-replay-v1",
            account_id="owner-deleting",
            user_event_id=user_event_id,
            assistant_event_id=assistant_event_id,
        )
    )

    assert result.skipped is True
    assert result.signal_id is None
    assert evaluator.inputs == []
