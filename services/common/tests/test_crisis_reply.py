from __future__ import annotations

import asyncio

import pytest
from services.common.crisis_reply import (
    CRISIS_REPLY_BUDGET_S,
    resolve_crisis_reply,
    rule_failures,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
)

_PASSED = (
    "你说不想活了，小助手听到了，先别一个人扛。现在马上去找身边的大人，"
    "请大人拨幺二三五六，已经受伤请大人打幺二零。"
)


class _Model:
    def __init__(
        self,
        drafts: list[str | None],
        *,
        accept: bool | Exception = True,
    ) -> None:
        self._drafts = list(drafts)
        self._accept = accept
        self.failures: list[tuple[str, ...]] = []
        self.replies: list[str] = []

    async def generate(
        self,
        *,
        query: str,
        tier: str,
        failures: tuple[str, ...],
    ) -> str | None:
        del query, tier
        self.failures.append(failures)
        if not self._drafts:
            return None
        return self._drafts.pop(0)

    async def accept(self, *, query: str, tier: str, reply: str) -> bool:
        del query, tier
        self.replies.append(reply)
        if isinstance(self._accept, Exception):
            raise self._accept
        return self._accept


def test_short_fallback_lines_meet_their_own_rules() -> None:
    assert rule_failures("crisis_self", CRISIS_SELF_SCRIPT.display) == ()
    assert rule_failures("support_victim", SUPPORT_VICTIM_SCRIPT.display) == ()
    assert rule_failures("support_relay", SUPPORT_RELAY_SCRIPT.display) == ()


def test_rules_name_the_missing_hotline_and_a_secrecy_promise() -> None:
    assert rule_failures("crisis_self", "去找身边的大人。") == (
        "hotline_12356",
        "hotline_120",
    )
    assert "confidentiality" in rule_failures(
        "support_victim",
        "去找身边的大人，请大人打幺幺零。这是我们的秘密。",
    )
    assert "method" in rule_failures(
        "support_relay",
        "去告诉老师。可以割腕。",
    )


@pytest.mark.asyncio
async def test_no_model_speaks_the_short_fallback() -> None:
    spoken = await resolve_crisis_reply(
        query="我不想活了",
        script=CRISIS_SELF_SCRIPT,
        model=None,
    )

    assert spoken.kind == "fallback"
    assert spoken.text == CRISIS_SELF_SCRIPT.display
    assert spoken.template_id == "crisis-self-v1"


@pytest.mark.asyncio
async def test_a_passing_draft_is_what_gets_spoken() -> None:
    model = _Model([_PASSED])

    spoken = await resolve_crisis_reply(
        query="我不想活了",
        script=CRISIS_SELF_SCRIPT,
        model=model,
    )

    assert spoken.kind == "dynamic"
    assert spoken.text == _PASSED
    assert model.failures == [()]
    assert model.replies == [_PASSED]


@pytest.mark.asyncio
async def test_ascii_hotlines_are_spoken_as_approved_readings() -> None:
    model = _Model(
        ["你说不想活了。小助手在。去找身边的大人，请大人拨12356，受伤打120。"]
    )

    spoken = await resolve_crisis_reply(
        query="我不想活了",
        script=CRISIS_SELF_SCRIPT,
        model=model,
    )

    assert spoken.kind == "dynamic"
    assert "12356" not in spoken.text
    assert "120" not in spoken.text
    assert "幺二三五六" in spoken.text
    assert "幺二零" in spoken.text


@pytest.mark.asyncio
async def test_one_correction_then_the_short_script() -> None:
    model = _Model(["去找妈妈。", "还是不行。"])

    spoken = await resolve_crisis_reply(
        query="有人打我",
        script=SUPPORT_VICTIM_SCRIPT,
        model=model,
    )

    assert spoken.kind == "fallback"
    assert spoken.text == SUPPORT_VICTIM_SCRIPT.display
    assert model.failures[0] == ()
    assert "hotline_110" in model.failures[1]


@pytest.mark.asyncio
async def test_a_failed_review_gets_one_correction_and_can_pass() -> None:
    model = _Model(
        [
            "去找身边的大人。",
            "有人打你，小助手在。现在去找身边的大人，请大人打幺幺零。",
        ]
    )

    spoken = await resolve_crisis_reply(
        query="有人打我",
        script=SUPPORT_VICTIM_SCRIPT,
        model=model,
    )

    assert spoken.kind == "dynamic"
    assert spoken.text.endswith("请大人打幺幺零。")
    assert len(model.failures) == 2


@pytest.mark.asyncio
async def test_a_missing_reviewer_does_not_speak_the_draft() -> None:
    model = _Model([_PASSED], accept=RuntimeError("down"))

    spoken = await resolve_crisis_reply(
        query="我不想活了",
        script=CRISIS_SELF_SCRIPT,
        model=model,
    )

    assert spoken.kind == "fallback"
    assert spoken.text == CRISIS_SELF_SCRIPT.display
    assert model.failures == [()]


@pytest.mark.asyncio
async def test_the_budget_returns_the_fallback_script() -> None:
    class _Slow:
        async def generate(self, *, query: str, tier: str, failures: tuple[str, ...]) -> str:
            del query, tier, failures
            await asyncio.sleep(0.2)
            return _PASSED

        async def accept(self, *, query: str, tier: str, reply: str) -> bool:
            del query, tier, reply
            return True

    spoken = await resolve_crisis_reply(
        query="我不想活了",
        script=CRISIS_SELF_SCRIPT,
        model=_Slow(),
        budget_s=0.01,
    )

    assert spoken.kind == "fallback"
    assert spoken.text == CRISIS_SELF_SCRIPT.display
    assert CRISIS_REPLY_BUDGET_S > 1
