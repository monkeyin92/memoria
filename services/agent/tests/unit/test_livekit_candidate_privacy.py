"""P1-01 隐私/可观测性门：用 canary 证明内容不离开进程。

上游默认采集内容、默认允许 PII 外发（``allow_pii`` 未显式给 False 时按 True）。
所以本门不是"env 存在即绿"，而是用假姓名 / 私有文本 / tool 参数做 canary，检查
真实 exporter 实际收到的 span 属性与事件。
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("livekit.agents")

from livekit.agents.telemetry import gen_ai, pii, trace_types  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

# 假姓名 + 私有正文 + tool 参数，全部不得出现在第三方 exporter 的 span 里。
CANARY_NAME = "诸葛西柚-假名-canary-8f2c"
CANARY_PRIVATE_TEXT = "我儿子小周对猫毛过敏，家里地址是假地址-canary-8f2c"
CANARY_TOOL_ARGUMENT = '{"query": "诸葛西柚-假名-canary-8f2c", "limit": 3}'


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    yield memory
    memory.clear()


@pytest.fixture
def restore_livekit_tracer() -> Iterator[None]:
    """``set_tracer_provider`` mutates a process-global; put it back afterwards."""

    from livekit.agents.telemetry import traces

    original = traces.tracer
    try:
        yield
    finally:
        traces.tracer = original


def _third_party_provider(exporter: InMemorySpanExporter) -> TracerProvider:
    """A provider standing in for any non-LiveKit-Cloud destination."""

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _exported_json(exporter: InMemorySpanExporter) -> str:
    return json.dumps(
        [
            {
                "name": span.name,
                "attributes": {k: str(v) for k, v in (span.attributes or {}).items()},
                "events": [
                    {
                        "name": event.name,
                        "attributes": {k: str(v) for k, v in event.attributes.items()},
                    }
                    for event in (span.events or ())
                ],
            }
            for span in exporter.get_finished_spans()
        ],
        ensure_ascii=False,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    **kwargs: Any,
) -> TracerProvider:
    """Install ``set_tracer_provider`` exactly as a deployment bootstrap would."""

    from livekit.agents.telemetry import traces

    provider = _third_party_provider(exporter)
    traces.set_tracer_provider(provider, **kwargs)
    return provider


def test_upstream_default_would_leak_content_without_an_explicit_allow_pii_false(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    restore_livekit_tracer: None,
) -> None:
    """反证：不显式关闭时上游默认把内容交给第三方 exporter。

    这条保证我们不是因为"什么都没发生"而误判为安全。
    """

    monkeypatch.delenv("LIVEKIT_TELEMETRY_ALLOW_PII", raising=False)
    provider = _install(monkeypatch, exporter, allow_pii=None)

    with provider.get_tracer("memoria.pii.canary").start_as_current_span("gen_ai.chat") as span:
        gen_ai.set_content_attributes(
            span,
            input_messages=[
                {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
            ],
        )

    assert CANARY_PRIVATE_TEXT in _exported_json(exporter)


def test_allow_pii_false_keeps_conversation_content_out_of_the_exporter(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    restore_livekit_tracer: None,
) -> None:
    monkeypatch.delenv("LIVEKIT_TELEMETRY_ALLOW_PII", raising=False)
    monkeypatch.setattr(gen_ai, "_capture_content", True)
    provider = _install(monkeypatch, exporter, allow_pii=False)

    with provider.get_tracer("memoria.pii.canary").start_as_current_span("gen_ai.chat") as span:
        gen_ai.set_content_attributes(
            span,
            system_instructions=[{"type": "text", "content": CANARY_NAME}],
            input_messages=[
                {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
            ],
            output_messages=[
                {"role": "assistant", "parts": [{"type": "text", "content": CANARY_NAME}]}
            ],
            tool_definitions=[
                {"type": "function", "name": "lookup_person", "description": CANARY_NAME}
            ],
        )
        gen_ai.set_tool_attributes(
            span,
            name="lookup_person",
            call_id="canary-call",
            arguments=CANARY_TOOL_ARGUMENT,
        )

    exported = _exported_json(exporter)
    assert CANARY_NAME not in exported
    assert CANARY_PRIVATE_TEXT not in exported
    assert CANARY_TOOL_ARGUMENT not in exported
    # 非内容计量仍必须到达 exporter，否则"关掉内容"就等于关掉可观测性。
    attributes = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attributes[trace_types.ATTR_GEN_AI_TOOL_NAME] == "lookup_person"
    assert attributes[trace_types.ATTR_GEN_AI_OPERATION_NAME] == (
        trace_types.GenAIOperationName.EXECUTE_TOOL
    )


def test_livekit_telemetry_allow_pii_env_zero_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    restore_livekit_tracer: None,
) -> None:
    """部署 env 只写 `LIVEKIT_TELEMETRY_ALLOW_PII=0`（不给 allow_pii）也必须生效。"""

    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "0")
    monkeypatch.setattr(gen_ai, "_capture_content", True)
    provider = _install(monkeypatch, exporter, allow_pii=None)

    with provider.get_tracer("memoria.pii.canary").start_as_current_span("gen_ai.chat") as span:
        gen_ai.set_content_attributes(
            span,
            input_messages=[
                {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
            ],
        )

    assert CANARY_PRIVATE_TEXT not in _exported_json(exporter)


def test_capture_message_content_zero_omits_content_attributes_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0` 时内容属性根本不写入。"""

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "0")
    monkeypatch.setattr(gen_ai, "_capture_content", False)

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("memoria.genai.capture").start_as_current_span("gen_ai.chat") as span:
        gen_ai.set_content_attributes(
            span,
            input_messages=[
                {"role": "user", "parts": [{"type": "text", "content": CANARY_PRIVATE_TEXT}]}
            ],
        )
        gen_ai.set_tool_attributes(span, name="lookup_person", arguments=CANARY_TOOL_ARGUMENT)

    attributes = dict(memory.get_finished_spans()[0].attributes or {})
    assert trace_types.ATTR_GEN_AI_INPUT_MESSAGES not in attributes
    assert trace_types.ATTR_GEN_AI_TOOL_CALL_ARGUMENTS not in attributes
    assert attributes[trace_types.ATTR_GEN_AI_TOOL_NAME] == "lookup_person"


def test_capture_content_env_falsy_values_match_the_sdk_rule() -> None:
    """把 SDK 的 env 解析规则钉住：未设置 / 空串=采集，`0/false/no/off`=不采集。"""

    module_source = inspect.getsource(gen_ai)
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in module_source
    falsy = re.search(r"_FALSY = \(([^)]*)\)", module_source)
    assert falsy is not None
    assert all(value in falsy.group(1) for value in ("0", "false", "no", "off"))

    # 解析表达式本身：不是 _FALSY 就为 True（未设置/空串/未知值都按采集处理），
    # 所以部署必须真的把它设成 0，不能留空。
    assert re.search(
        r"_capture_content: bool = \(\s*os\.environ\.get\([^)]*\)\.strip\(\)\.lower\(\)"
        r"\s*not in _FALSY\s*\)",
        module_source,
    ), "SDK 的 capture-content 解析方式变了，本门的 env 前提需要重新评估"


def test_repo_media_otel_bridge_never_attaches_session_or_device_identity() -> None:
    """本仓自有 span 只挂低基数 fence，且明确丢弃 session/device 标识。"""

    from services.agent.src.observability.media_otel import MediaOtelBridge
    from services.agent.src.voice_core.telemetry import MEDIA_SPAN_NAMES, TraceContext

    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))

    class _TracerSource:
        def get_tracer(self, name: str) -> Any:
            return provider.get_tracer(name)

    bridge = MediaOtelBridge()
    bridge._tracer = _TracerSource().get_tracer("memoria.media")

    context = TraceContext(
        trace_id="trace-canary-8f2c",
        session_id="session-canary-8f2c",
        stream_epoch=7,
        turn_id=3,
        generation_id=4,
        tool_epoch=5,
        device_id="device-canary-8f2c",
    )
    span_name = "device.playback_ended"
    assert span_name in MEDIA_SPAN_NAMES
    with bridge.span(span_name, context):
        pass

    finished = memory.get_finished_spans()
    assert finished, "media span was not exported"
    attributes = dict(finished[0].attributes or {})
    assert "memoria.session_id" not in attributes
    assert "memoria.device_id" not in attributes
    assert attributes.get("memoria.trace_id") == "trace-canary-8f2c"
    assert attributes.get("memoria.turn_id") == 3
    assert attributes.get("memoria.generation_id") == 4
    assert attributes.get("memoria.tool_epoch") == 5


def test_pii_marker_helper_still_classifies_the_attributes_we_rely_on() -> None:
    """`pii.is_pii_attribute` 的名字变了就等于我们的门失效，必须显式钉住。"""

    for name in (
        "lk.pii.user_input",
        "lk.pii.response.text",
        "lk.pii.function_tool.arguments",
        "lk.pii.function_tool.output",
        "lk.pii.user_transcript",
        "gen_ai.input.messages",
        "gen_ai.tool.call.arguments",
    ):
        assert pii.is_pii_attribute(name) is True, name
    for name in ("lk.generation_id", "lk.response.ttft", "gen_ai.usage.input_tokens"):
        assert pii.is_pii_attribute(name) is False, name


def test_capture_content_flag_is_read_at_sdk_import_time() -> None:
    """运行时前提：这个开关必须在进程导入 SDK 前就存在，否则本门无效。

    SDK 在模块导入时求值 env，改 env 不会影响已经导入的进程。所以部署必须把
    `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0` 放在进程启动环境里。
    """

    assert gen_ai.capture_content_enabled() in {True, False}
    assert "os.environ.get" in inspect.getsource(gen_ai).split("set_capture_content")[0]
