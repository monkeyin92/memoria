"""Shared helpers for the split archive API test modules."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from services.common.companions import designed_voice_speaker_sha256
from services.digital_self.response_planner import PLANNER_POLICY_VERSION


class PersonaObservationStub:
    def __init__(self) -> None:
        self.observations = 0

    async def learning_allowed(self, *, account_id: str) -> bool:
        del account_id
        return True

    async def observe(self, evidence: object) -> None:
        del evidence
        self.observations += 1


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


async def _register_verified_adult(
    client: AsyncClient,
    app: Any,
    *,
    username: str,
    password: str = "safe-password",
) -> dict[str, object]:
    """Create the adult fixture, apply the profile ratchet, then re-login.

    Updating the subject profile revokes the registration session by design.
    Adult Archive/Raw Voice/Memory/Persona tests must therefore use the token
    minted after the ratchet, while unknown-safe tests continue to use the
    anonymous flow directly.
    """

    registered_response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert registered_response.status_code == 201
    registered = registered_response.json()
    app.state.memory_store.update_subject_profile(
        user_id=registered["user_id"],
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    logged_in_response = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert logged_in_response.status_code == 200
    logged_in = logged_in_response.json()
    assert logged_in["user_id"] == registered["user_id"]
    return logged_in


async def _register_minor(
    client: AsyncClient,
    app: Any,
    *,
    username: str,
    password: str = "safe-password",
) -> dict[str, object]:
    registered_response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert registered_response.status_code == 201
    registered = registered_response.json()
    app.state.memory_store.update_subject_profile(
        user_id=registered["user_id"],
        subject_category="minor",
        birth_year_band="14_17",
        age_evidence_status="unverified",
        now=datetime.now(UTC).isoformat(),
    )
    logged_in_response = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert logged_in_response.status_code == 200
    logged_in = logged_in_response.json()
    assert logged_in["user_id"] == registered["user_id"]
    return logged_in


def _response_provenance(
    *,
    session_id: str,
    turn_id: int,
    generation_id: int,
    source_refs: list[dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    return {
        "fence": {
            "session_id": session_id,
            "turn_id": turn_id,
            "generation_id": generation_id,
            "tool_epoch": 0,
        },
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "interaction_mode": "legacy",
        "mode_policy_version": "caller-forged",
        "digital_self_version_id": None,
        "manifest_sha256": None,
        "relationship_profile_id": None,
        "relationship_profile_version": None,
        "speaker_class": "guest",
        "speaker_reason_code": "caller-forged",
        "speaker_profile_id": "caller-forged",
        "speaker_model_version": "caller-forged",
        "speaker_template_version": 99,
        "source_refs": source_refs,
        "epistemic_status": "fact",
        "epistemic_reason_codes": ["caller-forged"],
        "disclosures": ["inference", "privacy_refusal"],
        "llm_provider": "qwen",
        "llm_model": "qwen-test",
        "tts_provider": "volcengine_doubao",
        "tts_model": "seed-tts-2.0",
        "actual_voice_profile_id": "warm_companion",
        "actual_voice_resource_id": "seed-tts-2.0",
        "actual_voice_speaker_sha256": designed_voice_speaker_sha256("warm_companion"),
        **overrides,
    }
