"""Signing secrets and internal service tokens."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class ServiceTokenFields(BaseModel):
    runtime_profile_signing_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
    )
    device_binding_token_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_BINDING_TOKEN_SECRET",
    )
    transfer_evidence_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_TRANSFER_EVIDENCE_SECRET",
    )
    memoria_archive_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_INTERNAL_TOKEN",
    )
    memoria_archive_write_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_WRITE_TOKEN",
    )
    memoria_agent_heartbeat_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_AGENT_HEARTBEAT_TOKEN",
    )
    memoria_voice_resolution_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_RESOLUTION_TOKEN",
    )
    memoria_voice_cleanup_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_CLEANUP_TOKEN",
    )
    memoria_interaction_policy_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_INTERACTION_POLICY_TOKEN",
    )
    memoria_response_plan_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RESPONSE_PLAN_TOKEN",
    )
    memoria_evolution_control_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_EVOLUTION_CONTROL_TOKEN",
    )
