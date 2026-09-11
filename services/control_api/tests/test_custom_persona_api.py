"""T-B acceptance: three-stage custom persona API.

① ``POST /v1/personas/structuring`` writes nothing and is repeatable;
② an out-of-domain structuring result is rejected as ``422`` (never clamped);
③ confirming yields exactly one immutable ``v1`` record, idempotent by key;
④ ``fallback_designed_voice`` defaults to ``starlight``;
⑤ the six-per-owner limit is enforced;
⑥ a referenced persona needs ``?confirm=true`` to delete.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)

VALID_STRUCTURED = {
    "style_description": "温和的陪伴者",
    "warmth": "warm",
    "directness": "gentle",
    "response_length": "brief",
    "question_frequency": "rare",
    "interview_depth": "light",
    "welcome_text": "嗨，我在。",
    "conversation_instruction": "说话短一点，像朋友。",
    "voice_instruction": "轻松自然",
    "default_voice_emotion": "neutral",
    "default_voice_rate": 1.0,
}


class _StubStructurer:
    """Deterministic stand-in for the Qwen structurer."""

    version = "stub-structurer:v1"

    def __init__(self, payload: dict, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    async def structure(self, free_text: str):  # type: ignore[no-untyped-def]
        if self._error is not None:
            raise self._error
        from services.persona.custom_persona_fields import StructuredPersona

        return StructuredPersona.model_validate(self._payload)


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH", str(tmp_path / f"{name}-identity.sqlite3")
    )
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET", f"{name}-auth-secret-long-enough-0123456789"
    )
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET", f"{name}-transfer-evidence-secret-32-bytes"
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app, secret=app.state.settings.transfer_evidence_key()
    )
    return app


async def _register(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {user['access_token']}"}


@pytest.mark.asyncio
async def test_structuring_is_stateless_then_confirm_persists_one_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, "persona-three-stage")
    app.state.persona_structurer = _StubStructurer(VALID_STRUCTURED)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "persona-owner")
        headers = _auth(owner)

        first = await client.post(
            "/v1/personas/structuring",
            headers=headers,
            json={"free_text": "想要一个说话短一点、像朋友一样的伙伴"},
        )
        assert first.status_code == 200
        body = first.json()
        assert body["structured"] == VALID_STRUCTURED
        assert body["structurer_version"] == "stub-structurer:v1"
        draft_id = body["draft_id"]

        # Re-running is deterministic and still writes nothing.
        second = await client.post(
            "/v1/personas/structuring",
            headers=headers,
            json={"free_text": "想要一个说话短一点、像朋友一样的伙伴"},
        )
        assert second.json()["draft_id"] == draft_id
        empty = await client.get("/v1/personas", headers=headers)
        assert empty.json()["custom_personas"] == []

        created = await client.post(
            "/v1/personas",
            headers={**headers, "Idempotency-Key": "persona-key-1"},
            json={"display_name": "小北", "structured": VALID_STRUCTURED},
        )
        assert created.status_code == 201
        record = created.json()
        assert record["persona_id"].startswith("cu_")
        assert record["persona_version"] == 1
        assert record["source"] == "user_created"
        assert record["fallback_designed_voice"] == "starlight"

        # Same idempotency key -> the SAME row, never a second one.
        replayed = await client.post(
            "/v1/personas",
            headers={**headers, "Idempotency-Key": "persona-key-1"},
            json={"display_name": "小北", "structured": VALID_STRUCTURED},
        )
        assert replayed.status_code == 201
        assert replayed.json()["persona_id"] == record["persona_id"]

        listed = await client.get("/v1/personas", headers=headers)
        payload = listed.json()
        assert [item["persona_id"] for item in payload["custom_personas"]] == [
            record["persona_id"]
        ]
        assert {item["persona_id"] for item in payload["builtin"]} >= {"starlight"}


@pytest.mark.asyncio
async def test_out_of_domain_structuring_is_rejected_as_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from services.persona.custom_persona_structurer import PersonaStructuringError

    app = _app(monkeypatch, tmp_path, "persona-out-of-domain")
    app.state.persona_structurer = _StubStructurer(
        VALID_STRUCTURED,
        error=PersonaStructuringError("out of domain", field="warmth"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "persona-out-of-domain")
        response = await client.post(
            "/v1/personas/structuring",
            headers=_auth(owner),
            json={"free_text": "温柔到发甜"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "persona_structuring_invalid",
            "field": "warmth",
        }
        # Nothing was written by the failed stage.
        assert (
            await client.get("/v1/personas", headers=_auth(owner))
        ).json()["custom_personas"] == []


@pytest.mark.asyncio
async def test_confirm_revalidates_the_controlled_domain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, "persona-confirm-invalid")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "persona-confirm-invalid")
        response = await client.post(
            "/v1/personas",
            headers=_auth(owner),
            json={
                "display_name": "小北",
                "structured": {**VALID_STRUCTURED, "warmth": "甜"},
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "warmth"


@pytest.mark.asyncio
async def test_custom_persona_limit_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, "persona-limit")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "persona-limit-owner")
        headers = _auth(owner)
        created: set[str] = set()
        for index in range(5):
            response = await client.post(
                "/v1/personas",
                headers={**headers, "Idempotency-Key": f"limit-{index}"},
                json={"display_name": f"伙伴{index}", "structured": VALID_STRUCTURED},
            )
            assert response.status_code == 201
            created.add(response.json()["persona_id"])
        assert len(created) == 5

        overflow = await client.post(
            "/v1/personas",
            headers={**headers, "Idempotency-Key": "limit-overflow"},
            json={"display_name": "第六个", "structured": VALID_STRUCTURED},
        )
        assert overflow.status_code == 409
        assert overflow.json()["detail"] == {
            "code": "custom_persona_limit_reached",
            "limit": 5,
        }


@pytest.mark.asyncio
async def test_delete_needs_confirmation_when_referenced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _app(monkeypatch, tmp_path, "persona-delete")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "persona-delete-owner")
        headers = _auth(owner)
        created = await client.post(
            "/v1/personas",
            headers={**headers, "Idempotency-Key": "delete-key"},
            json={"display_name": "小北", "structured": VALID_STRUCTURED},
        )
        persona_id = created.json()["persona_id"]

        # A referenced persona demands an explicit second confirmation.
        identity = app.state.identity_service

        async def _two_references(*_args: object, **_kwargs: object) -> int:
            return 2

        monkeypatch.setattr(
            identity, "count_custom_persona_references", _two_references
        )
        blocked = await client.delete(f"/v1/personas/{persona_id}", headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == {"code": "persona_in_use", "in_use_count": 2}

        monkeypatch.undo()
        confirmed = await client.delete(
            f"/v1/personas/{persona_id}?confirm=true", headers=headers
        )
        assert confirmed.status_code == 200
        assert confirmed.json() == {"deleted": True, "drifted_subjects": 0}
        gone = await client.delete(f"/v1/personas/{persona_id}", headers=headers)
        assert gone.status_code == 404
