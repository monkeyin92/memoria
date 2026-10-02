"""The guardian reviews what the robot may remember about the child they bound a device for.

Round 10 (2026-10-02): a child's 「帮我记住…」 only reached candidate, and the one review endpoint
answers the account it is signed in as, so nobody could confirm it.  Only confirmed memories reach the
child's conversations; these pin the guardian's gate on them: who may open it, that it shows only this
child's own claims, and that a confirmation makes the memory recallable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import (
    _bind,
    _configure,
    _owner,
    _RecordingConsent,
)

STUDY = "今天我练习了乘法口诀。"
FAVOURITE = "帮我记住我最喜欢蓝色。"


def _turn(
    event_id: str, owner_id: str, subject: str, text: str, at: datetime, number: int, **extra: Any
) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id=owner_id,
        subject_id=subject,
        session_id="child-memory-session",
        turn_id=number,
        generation_id=number,
        event_type="speech.utterance_finalized",
        occurred_at=at,
        speaker_class="owner",
        source="test",
        payload={
            "text": text,
            "interaction_mode": "companion",
            "prompt_kind": "open",
            "history_eligible": True,
            "owner_projection_eligible": True,
            "memory_retention": "retained",
            "tool_epoch": 0,
            **extra,
        },
    )


async def _bound_child(
    client: AsyncClient, app: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, dict[str, str]]:
    app.state.bound_subject_consent = _RecordingConsent()
    owner_id, headers = await _owner(client, app, "child-memory-owner")
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id="device-child-memory",
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    return owner_id, created.json()["primary_subject_ids"][0], headers


async def _grant_memory(client: AsyncClient, child_id: str, headers: dict[str, str]) -> None:
    granted = await client.post(
        f"/v1/guardian/minors/{child_id}/consents",
        headers={**headers, "Idempotency-Key": "child-memory-grant-0001"},
        json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
    )
    assert granted.status_code == 201, granted.text


async def _record_and_compile(app: Any, owner_id: str, child_id: str) -> None:
    now = datetime.now(UTC)
    archive = app.state.life_archive
    # The child's study sentence stays a candidate; the child's explicit request confirms itself
    # (the robot's previous line was a question: prompt_kind "open"); the parent's own sentence is
    # another subject's evidence in the same account.
    await archive.record(_turn("child-study", owner_id, child_id, STUDY, now, 1))
    await archive.record(
        _turn(
            "child-favourite",
            owner_id,
            child_id,
            FAVOURITE,
            now + timedelta(minutes=1),
            2,
            memory_write_intent={"kind": "explicit_remember", "policy_version": "explicit-memory-v2"},
        )
    )
    await archive.record(
        _turn("parent-study", owner_id, owner_id, "今天我练习了英语单词。", now, 3)
    )
    report = await app.state.memory_catalog.compile_pending()
    assert report.failed_events == 0


@pytest.mark.asyncio
async def test_the_binding_guardian_confirms_and_retracts_what_the_robot_may_remember(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(client, app, monkeypatch)
        url = f"/v1/guardian/minors/{child_id}/memories"

        # Closed until the guardian ticks long-term memory for the child, like every child view.
        closed = await client.get(url, headers=headers)
        await _grant_memory(client, child_id, headers)
        await _record_and_compile(app, owner_id, child_id)

        listed = await client.get(url, headers=headers)
        body = listed.json()
        candidate = next(item for item in body["candidates"] if "乘法口诀" in item["value"])
        reviewed = await client.post(
            f"{url}/{candidate['claim_id']}/review", headers=headers, json={"action": "confirm"}
        )
        after_confirm = (await client.get(url, headers=headers)).json()

        favourite = next(item for item in body["confirmed"] if "蓝色" in item["snippet"])
        forgotten = await client.post(
            f"{url}/{favourite['memory_id']}/review", headers=headers, json={"action": "retract"}
        )
        after_retract = (await client.get(url, headers=headers)).json()

    assert closed.status_code == 403
    assert closed.json()["detail"]["code"] == "guardian_consent_required"
    assert listed.status_code == 200, listed.text
    assert [item["value"] for item in body["candidates"]] == [candidate["value"]]
    assert candidate["status"] == "candidate"
    assert [item["status"] for item in body["confirmed"]] == ["confirmed"]
    assert "英语单词" not in listed.text, "the parent's own sentence is another subject's evidence"

    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "confirmed"
    assert after_confirm["candidates"] == []
    assert {item["snippet"] for item in after_confirm["confirmed"]} >= {candidate["value"]}

    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["status"] == "retracted"
    assert all("蓝色" not in item["snippet"] for item in after_retract["confirmed"])


@pytest.mark.asyncio
async def test_a_confirmed_memory_is_what_the_childs_conversation_may_recall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(client, app, monkeypatch)
        await _grant_memory(client, child_id, headers)
        await _record_and_compile(app, owner_id, child_id)
        catalog = app.state.memory_catalog

        async def recall() -> list[str]:
            from services.archive.memory_domain import MemorySearchQuery

            found = await catalog.search(
                MemorySearchQuery(
                    account_id=owner_id,
                    subject_id=child_id,
                    speaker_class="owner",
                    text="乘法口诀",
                    kinds=("claim",),
                    include_candidates=False,
                )
            )
            return [item.snippet for item in found.items]

        before = await recall()
        queue = (await client.get(f"/v1/guardian/minors/{child_id}/memories", headers=headers)).json()
        claim = queue["candidates"][0]["claim_id"]
        await client.post(
            f"/v1/guardian/minors/{child_id}/memories/{claim}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        after = await recall()

    assert before == [], "a candidate never reaches the child's conversations"
    assert len(after) == 1 and "乘法口诀" in after[0]


@pytest.mark.asyncio
async def test_nobody_else_can_open_or_review_this_childs_memories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(client, app, monkeypatch)
        await _grant_memory(client, child_id, headers)
        await _record_and_compile(app, owner_id, child_id)
        url = f"/v1/guardian/minors/{child_id}/memories"
        claim = (await client.get(url, headers=headers)).json()["candidates"][0]["claim_id"]
        _, stranger_headers = await _owner(client, app, "child-memory-stranger")

        seen = await client.get(url, headers=stranger_headers)
        touched = await client.post(
            f"{url}/{claim}/review", headers=stranger_headers, json={"action": "confirm"}
        )
        anonymous = await client.get(url)
        # The parent's own claim sits in the same account but is not this child's evidence.
        parent_claim = next(
            item.item_id
            for item in await app.state.memory_catalog.review_queue(account_id=owner_id)
            if item.source_event_id == "parent-study"
        )
        wrong_subject = await client.post(
            f"{url}/{parent_claim}/review", headers=headers, json={"action": "confirm"}
        )
        unknown = await client.post(
            f"{url}/00000000-0000-4000-8000-000000000000/review",
            headers=headers,
            json={"action": "confirm"},
        )
        bad_action = await client.post(
            f"{url}/{claim}/review", headers=headers, json={"action": "correct"}
        )
        still_pending = (await client.get(url, headers=headers)).json()["candidates"]

    assert seen.status_code == 403
    assert seen.json()["detail"]["code"] == "guardian_memory_review_unavailable"
    assert touched.status_code == 403
    assert anonymous.status_code == 401
    assert wrong_subject.status_code == 404
    assert wrong_subject.json()["detail"]["code"] == "memory_claim_not_found"
    assert unknown.status_code == 404
    assert bad_action.status_code == 422
    assert [item["claim_id"] for item in still_pending] == [claim]
