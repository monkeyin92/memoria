"""The guardian's retracted-memory list survives closing the page and can be restored.

The mini program's memory page reads this list on open; a retraction made on an
earlier visit must still be there, with the claim id the review endpoint needs
to put the vector back.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from services.control_api.tests.test_vector_delete_api import _Embedder, _vector_count

STUDY = "今天我练习了乘法口诀。"
COLOUR = "我最喜欢蓝色。"
PET = "我养了一只小猫。"
MUSIC = "我喜欢听儿歌。"


async def _grant_memory(
    client: AsyncClient, child_id: str, headers: dict[str, str], key: str
) -> None:
    granted = await client.post(
        f"/v1/guardian/minors/{child_id}/consents",
        headers={**headers, "Idempotency-Key": key},
        json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
    )
    assert granted.status_code == 201, granted.text


async def _bound_child(
    client: AsyncClient, app: Any, username: str, device_id: str
) -> tuple[str, str, dict[str, str]]:
    app.state.bound_subject_consent = _RecordingConsent()
    owner_id, headers = await _owner(client, app, username)
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id=device_id,
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    return owner_id, created.json()["primary_subject_ids"][0], headers


async def _second_child(
    client: AsyncClient, app: Any, *, owner_id: str, headers: dict[str, str], device_id: str
) -> str:
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id=device_id,
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    return created.json()["primary_subject_ids"][0]


async def _seed_claim(
    app: Any, owner_id: str, child_id: str, event_id: str, text: str
) -> str:
    catalog = app.state.memory_catalog
    catalog._embedder = _Embedder()
    catalog._evidence_subject_category_resolver = lambda _event: "adult"
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=owner_id,
            subject_id=child_id,
            session_id="guardian-retracted-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="guardian-retracted-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    report = await catalog.compile_pending()
    assert report.failed_events == 0, report
    queue = await catalog.review_queue(account_id=owner_id, subject_id=child_id)
    matches = [item for item in queue if item.source_event_id == event_id]
    assert matches, f"no claim compiled from {text}"
    return matches[0].item_id


async def _review(
    client: AsyncClient, child_id: str, claim_id: str, headers: dict[str, str], action: str
) -> None:
    reviewed = await client.post(
        f"/v1/guardian/minors/{child_id}/memories/{claim_id}/review",
        headers=headers,
        json={"action": action},
    )
    assert reviewed.status_code == 200, reviewed.text


def _retracted_url(child_id: str) -> str:
    return f"/v1/guardian/minors/{child_id}/memories/retracted"


def _retracted_ids(body: dict[str, Any]) -> list[str]:
    return [item["claim_id"] for item in body["items"]]


@pytest.mark.asyncio
async def test_retracted_memory_survives_reopening_and_restores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "retracted-reopen-owner", "device-retracted-reopen"
        )
        await _grant_memory(client, child_id, headers, "retracted-grant")
        claim_id = await _seed_claim(app, owner_id, child_id, "retracted-reopen-turn", COLOUR)
        initial = await client.get(_retracted_url(child_id), headers=headers)
        await _review(client, child_id, claim_id, headers, "retract")
        assert _vector_count(owner_id, claim_id) == 0

    # Closing and reopening the page is a new client against the same stored data.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as reopened:
        listed = await reopened.get(_retracted_url(child_id), headers=headers)
        body = listed.json()

        assert listed.status_code == 200, listed.text
        assert initial.status_code == 200, initial.text
        assert initial.json()["items"] == []
        assert body["subject_id"] == child_id
        assert _retracted_ids(body) == [claim_id]
        item = body["items"][0]
        assert item["value"]
        assert item["title"]
        assert item["domain_category"]
        stamp = datetime.fromisoformat(item["retracted_at"])
        assert stamp.tzinfo is not None

        await _review(reopened, child_id, claim_id, headers, "confirm")
        after = await reopened.get(_retracted_url(child_id), headers=headers)

    assert after.json()["items"] == []
    assert _vector_count(owner_id, claim_id) == 1


@pytest.mark.asyncio
async def test_another_guardian_gets_the_same_refusal_as_child_memories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "retracted-owner", "device-retracted-owner"
        )
        await _grant_memory(client, child_id, headers, "retracted-grant")
        claim_id = await _seed_claim(app, owner_id, child_id, "retracted-owner-turn", STUDY)
        await _review(client, child_id, claim_id, headers, "retract")

        _outsider_id, outsider_headers = await _owner(client, app, "retracted-outsider")
        refused_list = await client.get(_retracted_url(child_id), headers=outsider_headers)
        refused_memories = await client.get(
            f"/v1/guardian/minors/{child_id}/memories", headers=outsider_headers
        )

    assert refused_list.status_code == 403
    assert refused_memories.status_code == 403
    assert refused_list.json()["detail"]["code"] == "guardian_memory_review_unavailable"
    assert refused_list.json() == refused_memories.json()


@pytest.mark.asyncio
async def test_another_child_and_another_account_stay_out_of_the_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "retracted-scope-owner", "device-retracted-scope-1"
        )
        await _grant_memory(client, child_id, headers, "retracted-grant")
        first_claim = await _seed_claim(app, owner_id, child_id, "retracted-scope-turn-1", STUDY)
        await _review(client, child_id, first_claim, headers, "retract")

        sibling_id = await _second_child(
            client, app, owner_id=owner_id, headers=headers, device_id="device-retracted-scope-2"
        )
        await _grant_memory(client, sibling_id, headers, "retracted-grant-sibling")
        sibling_claim = await _seed_claim(
            app, owner_id, sibling_id, "retracted-scope-turn-2", PET
        )
        await _review(client, sibling_id, sibling_claim, headers, "retract")

        other_owner, other_child, other_headers = await _bound_child(
            client, app, "retracted-scope-other", "device-retracted-scope-other"
        )
        await _grant_memory(client, other_child, other_headers, "retracted-grant-other")
        other_claim = await _seed_claim(
            app, other_owner, other_child, "retracted-scope-turn-other", MUSIC
        )
        await _review(client, other_child, other_claim, other_headers, "retract")

        first = await client.get(_retracted_url(child_id), headers=headers)
        sibling = await client.get(_retracted_url(sibling_id), headers=headers)

    assert _retracted_ids(first.json()) == [first_claim]
    assert _retracted_ids(sibling.json()) == [sibling_claim]
    assert other_claim not in _retracted_ids(first.json())
    assert first.json()["subject_id"] == child_id
    assert sibling.json()["subject_id"] == sibling_id


@pytest.mark.asyncio
async def test_only_retracted_claims_are_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "retracted-status-owner", "device-retracted-status"
        )
        await _grant_memory(client, child_id, headers, "retracted-grant")
        candidate_id = await _seed_claim(
            app, owner_id, child_id, "retracted-status-candidate", STUDY
        )
        confirmed_id = await _seed_claim(
            app, owner_id, child_id, "retracted-status-confirmed", COLOUR
        )
        retracted_id = await _seed_claim(app, owner_id, child_id, "retracted-status-retracted", PET)
        await _review(client, child_id, confirmed_id, headers, "confirm")
        await _review(client, child_id, retracted_id, headers, "retract")

        listed = await client.get(_retracted_url(child_id), headers=headers)
        limited = await client.get(_retracted_url(child_id) + "?limit=1", headers=headers)

    body = listed.json()
    assert listed.status_code == 200, listed.text
    assert _retracted_ids(body) == [retracted_id]
    assert candidate_id not in _retracted_ids(body)
    assert confirmed_id not in _retracted_ids(body)
    assert limited.status_code == 200, limited.text
    assert len(limited.json()["items"]) == 1
