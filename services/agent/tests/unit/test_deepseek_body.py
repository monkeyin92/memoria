from __future__ import annotations

from services.agent.src.providers.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    validate_no_forbidden_fields,
)


def test_fast_body_uses_max_tokens_not_completion() -> None:
    client = DeepSeekClient(DeepSeekConfig(api_key="x"))
    body = client._fast_body([{"role": "user", "content": "hi"}], None)
    assert body["max_tokens"] == 240
    assert "max_completion_tokens" not in body
    assert "parallel_tool_calls" not in body
    assert body["thinking"] == {"type": "disabled"}
    validate_no_forbidden_fields(body)
