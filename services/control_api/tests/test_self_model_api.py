from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.control_api.app.main import create_app
from services.self_model.registry import SelfModelRegistry

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    return path


async def _register(
    client: AsyncClient,
    username: str,
    password: str = "safe-password",
) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201
    return response.json()


def _headers(identity: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {identity['access_token']}"}


async def _owner_evidence(
    path: Path,
    *,
    account_id: str,
    event_id: str,
    text: str,
    eligible: bool = True,
    speaker_class: str = "owner",
) -> None:
    await LifeArchive.sqlite(path).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=_NOW,
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="self-model-api-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "owner_projection_eligible": eligible,
            },
        )
    )


def _seed_relationship(
    path: Path,
    *,
    account_id: str,
    source_event_id: str,
) -> tuple[str, str]:
    person_id = "person-self-model-api"
    relationship_id = "relationship-self-model-api"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES (?, ?, 'friend:李梅', '李梅', 'friend', 'confirmed', ?, ?)
            """,
            (person_id, account_id, source_event_id, _NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES (?, ?, ?, 'friend', 'confirmed', ?, ?)
            """,
            (
                relationship_id,
                account_id,
                person_id,
                source_event_id,
                _NOW.isoformat(),
            ),
        )
    return person_id, relationship_id


def _seed_additional_relationship(
    path: Path,
    *,
    account_id: str,
    person_id: str,
    source_event_id: str,
) -> str:
    relationship_id = "relationship-self-model-api-second"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES (?, ?, ?, 'colleague', 'confirmed', ?, ?)
            """,
            (
                relationship_id,
                account_id,
                person_id,
                source_event_id,
                _NOW.isoformat(),
            ),
        )
    return relationship_id


@pytest.mark.asyncio
async def test_low_risk_claim_requires_owner_source_and_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-low-risk")
        headers = _headers(owner)
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="belief-source",
            text="我相信答应的事要做到。",
        )
        created = await client.post(
            "/v1/self-model/claims",
            headers=headers,
            json={
                "idempotency_key": "create-belief",
                "claim_type": "belief",
                "statement": "我相信答应的事要做到。",
                "confidence": 0.9,
                "sources": [{"source_event_id": "belief-source"}],
            },
        )
        candidate = created.json()
        confirmed = await client.post(
            f"/v1/self-model/claims/{candidate['claim_id']}/review",
            headers=headers,
            json={
                "status": "confirmed",
                "expected_version": candidate["version"],
                "idempotency_key": "confirm-belief",
            },
        )
        listed = await client.get("/v1/self-model", headers=headers)

    assert created.status_code == 201
    assert candidate["status"] == "candidate"
    assert candidate["effective"] is False
    assert candidate["effective_reasons"] == ["not_approved"]
    assert candidate["sources"][0]["excerpt"] == "我相信答应的事要做到。"
    assert confirmed.status_code == 200
    assert confirmed.json()["effective"] is True
    assert listed.json()["claims"] == [confirmed.json()]


@pytest.mark.asyncio
async def test_high_sensitivity_claim_requires_password_and_owner_counterexample(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    password = "safe-password"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-high-risk", password)
        headers = _headers(owner)
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="value-support",
            text="家庭安全高于短期收益。",
        )
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="value-boundary",
            text="如果家人已经安全，我也愿意冒险。",
        )
        created = (
            await client.post(
                "/v1/self-model/claims",
                headers=headers,
                json={
                    "idempotency_key": "create-value",
                    "claim_type": "value",
                    "statement": "家庭安全高于短期收益。",
                    "confidence": 0.95,
                    "sources": [{"source_event_id": "value-support"}],
                },
            )
        ).json()
        no_password = await client.post(
            f"/v1/self-model/claims/{created['claim_id']}/review",
            headers=headers,
            json={
                "status": "confirmed",
                "expected_version": created["version"],
                "idempotency_key": "confirm-without-password",
            },
        )
        no_boundary = await client.post(
            f"/v1/self-model/claims/{created['claim_id']}/review",
            headers=headers,
            json={
                "status": "confirmed",
                "expected_version": created["version"],
                "idempotency_key": "confirm-without-boundary",
                "password": password,
            },
        )
        with_boundary = await client.post(
            f"/v1/self-model/items/cognitive_claim/{created['claim_id']}/sources",
            headers=headers,
            json={
                "source_event_id": "value-boundary",
                "relation": "counterexample",
                "adopted": False,
                "negative": False,
                "expected_version": created["version"],
                "idempotency_key": "add-value-boundary",
            },
        )
        confirmed = await client.post(
            f"/v1/self-model/claims/{created['claim_id']}/review",
            headers=headers,
            json={
                "status": "confirmed",
                "expected_version": with_boundary.json()["version"],
                "idempotency_key": "confirm-value",
                "password": password,
            },
        )

    assert no_password.status_code == 403
    assert no_password.json()["detail"] == {"code": "step_up_failed"}
    assert no_boundary.status_code == 409
    assert no_boundary.json()["detail"] == {"code": "invalid_transition"}
    assert confirmed.json()["effective"] is True
    assert confirmed.json()["step_up_verified"] is True


@pytest.mark.asyncio
async def test_owner_can_record_an_idempotent_counterexample_from_the_review_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner = await _register(client, "self-model-counterexample")
        headers = _headers(owner)
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="counterexample-support",
            text="我通常会先守住家庭安全。",
        )
        created = (
            await client.post(
                "/v1/self-model/claims",
                headers=headers,
                json={
                    "idempotency_key": "create-counterexample-value",
                    "claim_type": "value",
                    "statement": "家庭安全高于短期收益。",
                    "confidence": 0.95,
                    "sources": [{"source_event_id": "counterexample-support"}],
                },
            )
        ).json()
        body = {
            "event_id": "counterexample-owner-action",
            "expected_version": created["version"],
            "text": "当家人已经安全且风险可控时，我也愿意尝试。",
        }
        first = await client.post(
            f"/v1/self-model/claims/{created['claim_id']}/counterexamples",
            headers=headers,
            json=body,
        )
        duplicate = await client.post(
            f"/v1/self-model/claims/{created['claim_id']}/counterexamples",
            headers=headers,
            json=body,
        )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert first.json()["version"] == created["version"] + 1
    counterexamples = [
        source
        for source in first.json()["sources"]
        if source["relation"] == "counterexample"
    ]
    assert len(counterexamples) == 1
    assert counterexamples[0]["source_event_id"] == "counterexample-owner-action"
    assert counterexamples[0]["adopted"] is False
    assert counterexamples[0]["negative"] is False
    assert counterexamples[0]["speaker_class"] == "owner"
    assert (
        counterexamples[0]["excerpt"]
        == "当家人已经安全且风险可控时，我也愿意尝试。"
    )


@pytest.mark.asyncio
async def test_untrusted_source_and_cross_account_source_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-owner")
        other = await _register(client, "self-model-other")
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="guest-source",
            text="访客内容",
            speaker_class="guest",
        )
        await _owner_evidence(
            path,
            account_id=other["user_id"],
            event_id="other-owner-source",
            text="其他账户内容",
        )
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="valid-owner-source",
            text="可信主人内容",
        )
        guest = await client.post(
            "/v1/self-model/claims",
            headers=_headers(owner),
            json={
                "idempotency_key": "guest-claim",
                "claim_type": "belief",
                "statement": "不可信候选",
                "confidence": 0.7,
                "sources": [
                    {"source_event_id": "valid-owner-source"},
                    {"source_event_id": "guest-source"},
                ],
            },
        )
        cross_account = await client.post(
            "/v1/self-model/claims",
            headers=_headers(owner),
            json={
                "idempotency_key": "cross-account-claim",
                "claim_type": "belief",
                "statement": "越权候选",
                "confidence": 0.7,
                "sources": [
                    {"source_event_id": "valid-owner-source"},
                    {"source_event_id": "other-owner-source"},
                ],
            },
        )
        listed = await client.get("/v1/self-model", headers=_headers(owner))

    assert guest.status_code == 422
    assert guest.json()["detail"] == {"code": "untrusted_owner_source"}
    assert cross_account.status_code == 422
    assert cross_account.json()["detail"] == {"code": "untrusted_owner_source"}
    assert listed.json()["claims"] == []
    exported = await app.state.self_model_registry.export_account(owner["user_id"])
    assert exported["self_model_sources"] == []
    assert exported["self_model_audit_events"] == []
    assert exported["self_model_command_receipts"] == []


@pytest.mark.asyncio
async def test_hypothetical_decision_stays_non_effective_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-decision")
        headers = _headers(owner)
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="decision-source",
            text="如果重来一次，我会先验证需求。",
        )
        created = (
            await client.post(
                "/v1/self-model/decision-cases",
                headers=headers,
                json={
                    "idempotency_key": "create-hypothetical",
                    "kind": "hypothetical",
                    "context": "如果重新选择",
                    "options": ["先开发", "先验证"],
                    "constraints": ["时间有限"],
                    "chosen_option": "先验证",
                    "rejected_options": ["先开发"],
                    "reflection": "这是情境推演，不是既有经历。",
                    "sources": [{"source_event_id": "decision-source"}],
                },
            )
        ).json()
        confirmed = await client.post(
            f"/v1/self-model/decision-cases/{created['case_id']}/review",
            headers=headers,
            json={
                "status": "confirmed",
                "expected_version": created["version"],
                "idempotency_key": "confirm-hypothetical",
            },
        )

    assert confirmed.json()["effective"] is False
    assert confirmed.json()["effective_reasons"] == ["hypothetical_decision"]


@pytest.mark.asyncio
async def test_growth_negative_feedback_deactivates_a_confirmed_self_model_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-negative-feedback")
        headers = _headers(owner)
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="negative-feedback-source",
            text="我喜欢先把事实确认清楚。",
        )
        created = (
            await client.post(
                "/v1/self-model/claims",
                headers=headers,
                json={
                    "idempotency_key": "create-negative-feedback-claim",
                    "claim_type": "belief",
                    "statement": "我喜欢先把事实确认清楚。",
                    "confidence": 0.9,
                    "sources": [{"source_event_id": "negative-feedback-source"}],
                },
            )
        ).json()
        confirmed = (
            await client.post(
                f"/v1/self-model/claims/{created['claim_id']}/review",
                headers=headers,
                json={
                    "status": "confirmed",
                    "expected_version": created["version"],
                    "idempotency_key": "confirm-negative-feedback-claim",
                },
            )
        ).json()
        feedback_body = {
            "event_id": "negative-feedback-action",
            "action": "not_me",
            "target_kind": "cognitive_claim",
            "target_id": confirmed["claim_id"],
        }
        first_feedback = await client.post(
            "/v1/growth/owner-actions",
            headers=headers,
            json=feedback_body,
        )
        duplicate_feedback = await client.post(
            "/v1/growth/owner-actions",
            headers=headers,
            json=feedback_body,
        )
        listed = await client.get("/v1/self-model", headers=headers)

    claim = listed.json()["claims"][0]
    assert first_feedback.status_code == 201
    assert duplicate_feedback.status_code == 200
    assert claim["effective"] is False
    assert claim["effective_reasons"] == ["negative_evidence"]
    assert sorted(source["negative"] for source in claim["sources"]) == [False, True]


@pytest.mark.asyncio
async def test_relationship_profile_is_step_up_versioned_and_not_an_acl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    password = "safe-password"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-relationship", password)
        headers = _headers(owner)
        registry = app.state.self_model_registry
        assert isinstance(registry, SelfModelRegistry)
        registry.initialize()
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="relationship-source",
            text="我和李梅聊天时更直接。",
        )
        person_id, relationship_id = _seed_relationship(
            path,
            account_id=owner["user_id"],
            source_event_id="relationship-source",
        )
        created = (
            await client.post(
                "/v1/self-model/relationship-profiles",
                headers=headers,
                json={
                    "idempotency_key": "create-relationship-profile",
                    "person_id": person_id,
                    "relationship_id": relationship_id,
                    "salutation": "梅姐",
                    "tone": "坦诚",
                    "advice_style": "先听再建议",
                    "sharing_scope": "family",
                    "boundaries": ["不谈财务细节"],
                    "sources": [{"source_event_id": "relationship-source"}],
                },
            )
        ).json()
        no_password = await client.post(
            f"/v1/self-model/relationship-profiles/{created['profile_id']}/versions/1/review",
            headers=headers,
            json={
                "status": "approved",
                "expected_status": "candidate",
                "idempotency_key": "approve-without-password",
            },
        )
        approved = await client.post(
            f"/v1/self-model/relationship-profiles/{created['profile_id']}/versions/1/review",
            headers=headers,
            json={
                "status": "approved",
                "expected_status": "candidate",
                "idempotency_key": "approve-profile",
                "password": password,
            },
        )
        revised = await client.post(
            f"/v1/self-model/relationship-profiles/{created['profile_id']}/revisions",
            headers=headers,
            json={
                "expected_version": 1,
                "idempotency_key": "revise-profile",
                "salutation": "李梅",
                "tone": "温和",
                "advice_style": "只在被询问时建议",
                "boundaries": ["不分享私人记忆"],
                "sharing_scope": "private",
                "password": password,
            },
        )
        listed = await client.get("/v1/self-model", headers=headers)

    assert no_password.status_code == 422
    assert approved.json()["effective"] is True
    assert "acl" not in approved.json()
    assert "grant" not in approved.json()
    assert revised.status_code == 201
    assert revised.json()["version_number"] == 2
    assert revised.json()["status"] == "candidate"
    assert [item["status"] for item in listed.json()["relationship_profiles"]] == [
        "superseded",
        "candidate",
    ]


@pytest.mark.asyncio
async def test_owner_can_create_two_profiles_for_different_relationships_to_one_person(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "self-model-multi-rel")
        headers = _headers(owner)
        registry = app.state.self_model_registry
        assert isinstance(registry, SelfModelRegistry)
        registry.initialize()
        await _owner_evidence(
            path,
            account_id=owner["user_id"],
            event_id="multiple-relationship-source",
            text="同一个人可以有不同关系语境。",
        )
        person_id, relationship_id = _seed_relationship(
            path,
            account_id=owner["user_id"],
            source_event_id="multiple-relationship-source",
        )
        second_relationship_id = _seed_additional_relationship(
            path,
            account_id=owner["user_id"],
            person_id=person_id,
            source_event_id="multiple-relationship-source",
        )
        responses = [
            await client.post(
                "/v1/self-model/relationship-profiles",
                headers=headers,
                json={
                    "idempotency_key": f"profile-{index}",
                    "person_id": person_id,
                    "relationship_id": current_relationship_id,
                    "salutation": salutation,
                    "tone": "坦诚",
                    "advice_style": "先听再建议",
                    "sources": [{"source_event_id": "multiple-relationship-source"}],
                },
            )
            for index, (current_relationship_id, salutation) in enumerate(
                (
                    (relationship_id, "朋友"),
                    (second_relationship_id, "同事"),
                )
            )
        ]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["profile_id"] != responses[1].json()["profile_id"]
