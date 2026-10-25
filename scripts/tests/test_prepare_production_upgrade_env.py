"""prepare refuses a key whose meaning moved, before its silent filter drops it."""

from __future__ import annotations

import pytest
from scripts.prepare_production_upgrade_env import prepare


def test_prepare_rejects_a_stale_selector_before_filtering() -> None:
    """ASR_FAILOVER_ENABLED used to vanish in the known-keys filter. It must not."""
    legacy = {
        "TTS_PROVIDER": "doubao",
        "ASR_FAILOVER_ENABLED": "true",
        "DASHSCOPE_API_KEY": "dashscope",
        "DEEPSEEK_API_KEY": "deepseek",
    }

    with pytest.raises(ValueError, match="ASR_PROVIDER"):
        prepare(
            legacy=legacy,
            postgres={"DATABASE_URL": "postgresql://fake"},
            minio={"MINIO_URL": "http://fake"},
            release_tag="20261025-test-v1",
        )
