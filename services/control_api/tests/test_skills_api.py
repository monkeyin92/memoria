from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import ContextQuery, EvidenceEvent
from services.archive.skill_domain import SkillRunRequest
from services.control_api.app.main import create_app


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-skills")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


async def _identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    payload = response.json()
    return str(payload["user_id"]), {
        "Authorization": f"Bearer {payload['access_token']}"
    }


def _proposal(source_event_id: str) -> dict[str, object]:
    return {
        "name": "睡前流程",
        "description": "先调暗灯光，再播放一个睡前故事。",
        "trigger_phrases": ["开始睡前流程"],
        "input_schema": {
            "type": "object",
            "properties": {"device": {"type": "string"}},
            "required": ["device"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"story_id": {"type": "string"}},
            "required": ["story_id"],
            "additionalProperties": False,
        },
        "output_template": {"story_id": "$steps.story.output.story_id"},
        "allowed_tools": ["set_light", "play_story"],
        "steps": [
            {
                "step_id": "dim",
                "tool_name": "set_light",
                "arguments": {"device": "$input.device", "brightness": 20},
            },
            {
                "step_id": "story",
                "tool_name": "play_story",
                "arguments": {"minutes": 5},
            },
        ],
        "source_kind": "explicit_instruction",
        "source_event_ids": [source_event_id],
        "domain_category": "daily_life",
        "sensitivity": "personal",
        "salience": 0.8,
    }


@pytest.mark.asyncio
async def test_owner_can_propose_approve_and_create_single_use_run_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        user_id, headers = await _identity(client)
        other_id, other_headers = await _identity(client)
        assert user_id != other_id
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="api-skill-instruction",
                account_id=user_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="skill-api-test",
                payload={
                    "text": "以后我说开始睡前流程，就先调暗灯光再讲故事。"
                },
            )
        )

        proposed = await client.post(
            "/v1/skills/proposals",
            headers=headers,
            json=_proposal("api-skill-instruction"),
        )
        assert proposed.status_code == 201
        candidate = proposed.json()
        assert candidate["status"] == "candidate"

        approved = await client.post(
            f"/v1/skills/{candidate['skill_id']}/versions/1/approve",
            headers=headers,
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        confirmation = await client.post(
            f"/v1/skills/{candidate['skill_id']}/versions/1/confirm-run",
            headers=headers,
            json={"inputs": {"device": "bedroom"}},
        )
        assert confirmation.status_code == 201
        confirmation_payload = confirmation.json()
        assert confirmation_payload["single_use"] is True

        run = await app.state.skill_catalog.start_run(
            SkillRunRequest(
                account_id=user_id,
                skill_id=candidate["skill_id"],
                version=1,
                confirmation_event_id=confirmation_payload["confirmation_event_id"],
                inputs={"device": "bedroom"},
            )
        )
        listed = await client.get("/v1/skills", headers=headers)
        isolated = await client.get("/v1/skills", headers=other_headers)
        cross_account_approval = await client.post(
            f"/v1/skills/{candidate['skill_id']}/versions/1/approve",
            headers=other_headers,
        )
        other_evidence = await app.state.life_archive.context(
            ContextQuery(
                account_id=other_id,
                speaker_class="owner",
                limit=100,
            )
        )

    assert run.status == "running"
    assert [item["status"] for item in listed.json()["items"]] == ["approved"]
    assert isolated.json() == {"items": []}
    assert cross_account_approval.status_code == 404
    assert all(
        event.event_type != "skill.approved" for event in other_evidence.evidence
    )


@pytest.mark.asyncio
async def test_skill_proposal_rejects_cross_account_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner_id, owner_headers = await _identity(client)
        other_id, other_headers = await _identity(client)
        del owner_headers
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="other-owner-instruction",
                account_id=owner_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="skill-api-test",
                payload={"text": "这是另一个账户的技能来源。"},
            )
        )

        response = await client.post(
            "/v1/skills/proposals",
            headers=other_headers,
            json=_proposal("other-owner-instruction"),
        )

    assert other_id != owner_id
    assert response.status_code == 404
