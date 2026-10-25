"""A config reads only the keys it declares, so a release cannot drop a new one."""

from __future__ import annotations

from collections import UserDict

import pytest
from services.agent.src.providers.asr_failover import ASRFailoverConfig
from services.agent.src.providers.sensevoice import SenseVoiceRescueConfig
from services.agent.src.providers.volc_asr import VolcASRConfig


class _Recording(UserDict[str, str]):
    """Remembers every key ``from_env`` asks for, declared or not."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.read: set[str] = set()

    def get(self, key: str, default: str = "") -> str:  # type: ignore[override]
        self.read.add(key)
        return super().get(key, default)


def _assert_declared(config: type, env: _Recording) -> None:
    undeclared = env.read - config.ENV_KEYS
    assert not undeclared, f"{config.__name__} reads undeclared keys: {sorted(undeclared)}"
    assert env.read, f"{config.__name__}.from_env read nothing, so the guard is not wired"


def test_volc_asr_from_env_reads_only_declared_keys() -> None:
    env = _Recording({"VOLC_ASR_APP_ID": "app", "VOLC_ASR_ACCESS_TOKEN": "token"})
    VolcASRConfig.from_env(env)
    _assert_declared(VolcASRConfig, env)


def test_volc_asr_never_reads_doubao_tts_keys() -> None:
    """ASR and TTS keep separate keys even though they share one account."""
    env = _Recording(
        {
            "VOLC_ASR_APP_ID": "app",
            "VOLC_ASR_ACCESS_TOKEN": "token",
            "DOUBAO_TTS_APP_ID": "shared",
            "DOUBAO_TTS_ACCESS_TOKEN": "shared",
        }
    )
    VolcASRConfig.from_env(env)
    assert not any(key.startswith("DOUBAO_TTS_") for key in env.read)


def test_asr_failover_from_env_reads_only_declared_keys() -> None:
    env = _Recording({"ASR_BACKUP": "sensevoice"})
    ASRFailoverConfig.from_env(env)
    _assert_declared(ASRFailoverConfig, env)


def test_sensevoice_from_env_reads_only_declared_keys() -> None:
    env = _Recording({"SENSEVOICE_URL": "http://sensevoice.internal/v1"})
    SenseVoiceRescueConfig.from_env(env)
    _assert_declared(SenseVoiceRescueConfig, env)


def test_an_undeclared_read_fails() -> None:
    """The guard is what makes the tests above mean something."""
    from services.agent.src.providers.volc_asr import _DeclaredEnv

    with pytest.raises(KeyError, match="NOT_A_KEY"):
        _DeclaredEnv({}, VolcASRConfig.ENV_KEYS).get("NOT_A_KEY")
