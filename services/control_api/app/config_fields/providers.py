"""Model providers: LLM, TTS, DashScope/Qwen and DeepSeek."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class ProviderFields(BaseModel):
    llm_provider: Literal["qwen", "bailian_deepseek", "deepseek"] = Field(
        default="qwen",
        alias="LLM_PROVIDER",
    )
    tts_provider: Literal["cosyvoice", "doubao"] = Field(
        default="doubao",
        alias="TTS_PROVIDER",
    )
    dashscope_api_key: SecretStr = Field(default=SecretStr(""), alias="DASHSCOPE_API_KEY")
    dashscope_ws_url: str = Field(default="", alias="DASHSCOPE_WS_URL")
    dashscope_workspace_id: str = Field(default="", alias="DASHSCOPE_WORKSPACE_ID")
    # P1-8: fixed Memoria persona voice on Omni (DashScope preset; not free-form clone).
    # Qwen3.5-Omni Realtime voice (not Qwen-TTS names like Cherry).
    # Liora Mira = 清欢，温柔女声（官方 Omni 音色表）.
    qwen_omni_voice: str = Field(default="Liora Mira", alias="QWEN_OMNI_VOICE")
    # Optional custom/cloned voice id from DashScope voice-clone product; wins over preset.
    qwen_omni_voice_clone_id: str = Field(default="", alias="QWEN_OMNI_VOICE_CLONE_ID")
    qwen_omni_persona_label: str = Field(
        default="Memoria 人设声",
        alias="QWEN_OMNI_PERSONA_LABEL",
    )
    qwen_omni_sdp_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="QWEN_OMNI_SDP_TIMEOUT_S",
    )
    # Shared semantic_vad knobs for Omni Flash A/B sweeps.
    qwen_omni_vad_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        alias="QWEN_OMNI_VAD_THRESHOLD",
    )
    qwen_omni_prefix_padding_ms: int = Field(
        default=500,
        ge=0,
        le=2000,
        alias="QWEN_OMNI_PREFIX_PADDING_MS",
    )
    qwen_omni_silence_duration_ms: int = Field(
        default=800,
        ge=200,
        le=2500,
        alias="QWEN_OMNI_SILENCE_DURATION_MS",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_summary_model: str = Field(
        default="qwen-flash",
        alias="DASHSCOPE_SUMMARY_MODEL",
    )
    dashscope_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DASHSCOPE_SUMMARY_TIMEOUT_S",
    )
    crisis_semantic_enabled: bool = Field(
        default=True,
        alias="CRISIS_SEMANTIC_ENABLED",
    )
    crisis_semantic_model: str = Field(
        default="qwen-flash",
        min_length=1,
        alias="CRISIS_SEMANTIC_MODEL",
    )
    crisis_semantic_timeout_s: float = Field(
        default=0.8,
        gt=0.0,
        le=2.0,
        alias="CRISIS_SEMANTIC_TIMEOUT_S",
    )
    memory_extraction_model: str = Field(
        default="qwen-flash",
        alias="MEMORIA_MEMORY_EXTRACTION_MODEL",
    )
    memory_extraction_timeout_s: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        alias="MEMORIA_MEMORY_EXTRACTION_TIMEOUT_S",
    )
    persona_structuring_model: str = Field(
        default="qwen-flash",
        min_length=1,
        alias="MEMORIA_PERSONA_STRUCTURING_MODEL",
    )
    persona_structuring_timeout_s: float = Field(
        default=8.0,
        gt=0.0,
        le=120.0,
        alias="MEMORIA_PERSONA_STRUCTURING_TIMEOUT_S",
    )

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_summary_model: str = Field(
        default="deepseek-v4-flash",
        alias="DEEPSEEK_SUMMARY_MODEL",
    )
    deepseek_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DEEPSEEK_SUMMARY_TIMEOUT_S",
    )
