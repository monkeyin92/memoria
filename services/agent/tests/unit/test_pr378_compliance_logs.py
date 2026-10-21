"""PR #378 compliance: safety logs carry hit/miss, category, latency, never text.

Set ``PR378_LOG_SAMPLE_DIR`` to dump the real captured records from this test
into that directory (``log-sample.txt``).  Nothing is written by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.control_api.app import companion_context, subject_session_recall

_LOCAL_HIT = "我不想活了"
_LOCAL_MISS = "我撑不下去了"
_ORDINARY = "今天天气不错"
_LOG_PREFIXES = (
    "local_crisis_classification",
    "late_crisis_plan",
    "recall_timeout",
)
_STANDARD_RECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


def _runtime(name: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name)
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=True,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=True,
    )
    bind_owner_speaker(runtime)
    return runtime


def _chat_plan(fence: GenerationFence) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions="按当前控制计划自然回答。",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v2",
            interaction_mode="companion",
            mode_policy_version="test-policy",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class="owner",
            speaker_reason_code="owner_match",
            speaker_profile_id="owner",
            speaker_model_version="test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _SlowRecallStore:
    async def list_recent(self, **_: Any) -> tuple[str, ...]:
        await asyncio.sleep(0.05)
        return ("不该被写进日志的旧话",)


def _record_extra(record: logging.LogRecord) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_KEYS
    }


def _record_args(record: logging.LogRecord) -> list[str]:
    if isinstance(record.args, tuple):
        return [str(value) for value in record.args]
    if isinstance(record.args, dict):
        return [f"{key}={value}" for key, value in record.args.items()]
    return [str(record.args)]


def _serialized_record(record: logging.LogRecord) -> str:
    return json.dumps(
        {
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "args": _record_args(record),
            "extra": _record_extra(record),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_pr378_safety_logs_have_no_transcript_and_can_be_dumped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    runtime = _runtime("pr378-log-compliance")
    pipeline = ReplyPipeline(instructions="系统提示", runtime=runtime)
    await pipeline.prepare_committed_turn(_LOCAL_HIT)
    await pipeline.prepare_committed_turn(_ORDINARY)

    task = asyncio.create_task(
        _late_plan(ResponsePlanFetch(replace(_chat_plan(runtime.fence), direct_text=CRISIS_SELF_SCRIPT.display), "ok"))
    )
    pipeline._watch_late_crisis_plan(task, text=_LOCAL_MISS, fence=runtime.fence)
    await task
    await asyncio.sleep(0)

    async def slow_context() -> str:
        await asyncio.sleep(0.05)
        return "不该被写进日志的上下文"

    monkeypatch.setattr(companion_context, "COMPANION_CONTEXT_BUDGET_S", 0.01)
    assert await companion_context.gather_companion_context(
        asyncio.create_task(slow_context()), None
    ) is None
    monkeypatch.setattr(subject_session_recall, "_RECALL_CONTEXT_BUDGET_S", 0.01)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(subject_session_recall_store=_SlowRecallStore())
        )
    )
    await subject_session_recall.authoritative_recall_context(
        request,  # type: ignore[arg-type]
        subject_id="subject-1",
        session_id="session-1",
        session_epoch=1,
        retention_allowed=True,
        client_items=(),
    )

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith(_LOG_PREFIXES)
    ]
    messages = [record.getMessage() for record in records]
    assert any(message.startswith("local_crisis_classification hit=true") for message in messages)
    assert any(message.startswith("local_crisis_classification hit=false") for message in messages)
    assert any(
        message.startswith("late_crisis_plan hit=true category=crisis-self-v1")
        for message in messages
    )
    assert any(message.startswith("recall_timeout hit=false category=session_recall") for message in messages)
    assert any(
        message.startswith("recall_timeout hit=false category=companion_context")
        for message in messages
    )

    forbidden = (
        _LOCAL_HIT,
        _LOCAL_MISS,
        _ORDINARY,
        "不该被写进日志的旧话",
        "不该被写进日志的上下文",
    )
    for record in records:
        serialized = _serialized_record(record)
        message = record.getMessage()
        assert "hit=" in message
        assert "category=" in message
        assert "latency_ms=" in message
        for text in forbidden:
            assert text not in message
            assert text not in serialized

    sample_dir = os.environ.get("PR378_LOG_SAMPLE_DIR", "").strip()
    if sample_dir:
        directory = Path(sample_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "log-sample.txt").write_text(
            "\n".join(_serialized_record(record) for record in records) + "\n",
            encoding="utf-8",
        )
    await runtime.close()


async def _late_plan(fetch: ResponsePlanFetch) -> ResponsePlanFetch:
    return fetch
