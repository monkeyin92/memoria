"""A child's "帮我记住…" gets a promise the archive will keep (2026-10-02 round 10).

The robot answered 「我记住啦」 to every request, while the write policy confirms only a closed set of
low-risk preferences and habits, and only when the profile grants long-term memory: the next session said
「这个我还真不知道呢」.  The local safe plan, which every device conversation runs on, now tells the model not
to promise what will not be kept.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.agent import build_local_safe_plan
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.runtime_profile import parse_runtime_profile
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    bind_owner_policy,
    canonical_wire_payload,
)
from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

NOTE = "记不下来"


def _policy(*, minor: bool, grant: bool) -> Any:
    runtime = DuplexRuntime.create(session_id=f"child-memory-promise-{minor}-{grant}")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=grant,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=grant,
    )
    policy = runtime.mode_policy
    if not minor:
        return policy
    child = parse_runtime_profile(
        canonical_wire_payload(
            session_id=runtime.session_id,
            runtime_profile_id="rp_child_promise",
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            session_epoch=1,
            capabilities=["chat", "memory_capture", *(["memory_recall_private"] if grant else [])],
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert child is not None
    return replace(policy, runtime_profile=child)


def _instructions(query: str, *, minor: bool = True, grant: bool = True) -> str:
    plan = build_local_safe_plan(
        policy=_policy(minor=minor, grant=grant),
        fence=GenerationFence("s", 1, 1, 0, 1),
        speaker=SimpleNamespace(
            classification="owner",
            reason_code=DEVICE_BOUND_SUBJECT_REASON,
            model_version="device-binding-v1",
            profile_id=None,
            template_version=None,
        ),
        reason="test",
        tts_model="seed-tts-2.0",
        query=query,
    )
    return plan.instructions


@pytest.mark.parametrize(
    "query",
    [
        "帮我记住我最喜欢蓝色。",
        "请帮我记住：我平时习惯早起。",
        # The wake greeting leaked back through the speaker and was glued to the first sentence.
        "晚上好，你在？ 帮我记住，我最喜欢蓝色。",
    ],
)
def test_a_request_the_archive_will_keep_is_answered_as_before(query: str) -> None:
    assert NOTE not in _instructions(query)


@pytest.mark.parametrize(
    "query",
    [
        "帮我记住我喜欢恐龙。",
        "帮我记住明天要交作业。",
        "你要记住我的小狗叫旺财",
        "别忘了我喜欢画画",
        "帮我记住我爸爸喜欢蓝色。",
    ],
)
def test_a_request_the_archive_will_not_keep_must_not_be_promised(query: str) -> None:
    instructions = _instructions(query)

    assert NOTE in instructions
    assert "不要说“我记住了”" in instructions


def test_without_the_profiles_long_term_memory_grant_nothing_is_kept_at_all() -> None:
    assert NOTE in _instructions("帮我记住我最喜欢蓝色。", grant=False)


@pytest.mark.parametrize(
    "query",
    [
        "我最喜欢蓝色。",
        "你记住了吗",
        "我记住了",
        "你还记得我喜欢什么颜色吗",
        "今天天气怎么样",
    ],
)
def test_a_sentence_that_asks_for_nothing_to_be_remembered_gets_no_note(query: str) -> None:
    assert NOTE not in _instructions(query)


def test_an_adult_listener_is_not_touched() -> None:
    assert NOTE not in _instructions("帮我记住我喜欢恐龙。", minor=False)
