"""Self-evolution control plane."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EvolutionFields(BaseModel):
    evolution_db_path: str = Field(
        default="",
        alias="MEMORIA_EVOLUTION_DB_PATH",
    )
    evolution_trusted_root_sha256: str = Field(
        default="",
        alias="MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256",
    )
    evolution_min_new_signals: int = Field(
        default=10,
        ge=1,
        le=10_000,
        alias="MEMORIA_EVOLUTION_MIN_NEW_SIGNALS",
    )
    evolution_min_failure_support: int = Field(
        default=2,
        ge=2,
        le=100,
        alias="MEMORIA_EVOLUTION_MIN_FAILURE_SUPPORT",
    )
    evolution_stale_after_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        alias="MEMORIA_EVOLUTION_STALE_AFTER_DAYS",
    )
    evolution_canary_percent: int = Field(
        default=0,
        ge=0,
        le=100,
        alias="MEMORIA_EVOLUTION_CANARY_PERCENT",
    )
    evolution_runtime_prompt_families: str = Field(
        default="weather",
        alias="MEMORIA_EVOLUTION_RUNTIME_PROMPT_FAMILIES",
    )
    evolution_sleep_interval_s: float = Field(
        default=3600.0,
        ge=0,
        le=86_400,
        alias="MEMORIA_EVOLUTION_SLEEP_INTERVAL_S",
    )
