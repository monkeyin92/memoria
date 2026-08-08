from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.evolution.receipt import sign_resolution_receipt, verify_resolution_receipt


def _artifacts() -> list[dict[str, object]]:
    return [
        {
            "candidate_id": "weather-v2",
            "version": 2,
            "kind": "prompt",
            "status": "canary",
            "artifact_hash": "a" * 64,
        }
    ]


def test_resolution_receipt_binds_query_account_speaker_fence_and_artifacts() -> None:
    now = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    receipt = sign_resolution_receipt(
        "receipt-secret",
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="  明天南京天气如何？ ",
        artifacts=_artifacts(),
        issued_at=now,
    )

    assert verify_resolution_receipt(
        "receipt-secret",
        receipt,
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="明天南京天气如何？",
        artifacts=_artifacts(),
        now=now + timedelta(seconds=30),
    )
    assert not verify_resolution_receipt(
        "receipt-secret",
        receipt,
        account_id="owner-b",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="明天南京天气如何？",
        artifacts=_artifacts(),
        now=now,
    )
    forged = _artifacts()
    forged[0]["artifact_hash"] = "b" * 64
    assert not verify_resolution_receipt(
        "receipt-secret",
        receipt,
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="明天南京天气如何？",
        artifacts=forged,
        now=now,
    )


def test_resolution_receipt_expires_and_rejects_unknown_fields() -> None:
    now = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    receipt = sign_resolution_receipt(
        "receipt-secret",
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="天气",
        artifacts=_artifacts(),
        issued_at=now,
    )
    assert not verify_resolution_receipt(
        "receipt-secret",
        receipt,
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="天气",
        artifacts=_artifacts(),
        now=now + timedelta(hours=2),
    )
    assert not verify_resolution_receipt(
        "receipt-secret",
        {**receipt, "extra": "forged"},
        account_id="owner-a",
        session_id="session-a",
        turn_id=2,
        generation_id=3,
        tool_epoch=1,
        speaker_class="owner",
        query="天气",
        artifacts=_artifacts(),
        now=now,
    )
