"""The guardian's daily view of a bound child: which days they talked, and a recap that is
never in the child's words.

Until 2026-10-01 a parent who bound a device for a child saw an empty 回顾 tab for every
conversation: the day list read a legacy table production no longer writes, and the turn list
is (rightly) keyed to the signed-in account's own subject. The weekly report that was meant to
cover this counts events nothing emits. These tests pin the replacement: counts from the
child's own consented turns, and an on-demand recap that cannot quote them.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.control_api.app import guardian_recap
from services.control_api.app.guardian_recap import (
    ChildUtterance,
    child_utterances,
    day_activity,
    fallback_recap,
    parse_recap,
)
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import (
    _bind,
    _configure,
    _owner,
    _RecordingConsent,
)

ZONE = ZoneInfo("Asia/Shanghai")
CHILD_WORDS = ("今天老师说我画的小狗像一只会飞的猫", "我想要一只叫豆豆的小狗陪我玩")


def _event(event_id: str, subject: str | None, text: str, at: datetime, **payload: Any) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=event_id,
        account_id="owner",
        subject_id=subject,
        event_type="speech.utterance_finalized",
        occurred_at=at,
        speaker_class=payload.pop("speaker_class", "owner"),
        source="test",
        payload={
            "text": text,
            "history_eligible": True,
            "owner_projection_eligible": True,
            "memory_retention": "retained",
            **payload,
        },
    )


# --- pure pieces -------------------------------------------------------------------


def test_only_the_childs_own_consented_turns_count() -> None:
    at = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)  # 09:00 in Shanghai
    events = [
        _event("a", "child", "你好呀", at),
        _event("dup", "child", "重复", at),
        _event("dup", "child", "重复", at),
        _event("parent", "owner-person", "家长的话", at),
        _event("uncertain", "child", "不确定的人", at, speaker_class="uncertain"),
        _event("hidden", "child", "不让看", at, history_eligible=False),
        _event("ephemeral", "child", "只聊一次", at, memory_retention="ephemeral_only"),
        _event("blank", "child", "   ", at),
    ]
    found = child_utterances(events, subject_id="child", zone=ZONE)
    assert [item.text for item in found] == ["你好呀", "重复"]
    assert all(item.day == date(2026, 10, 1) for item in found)


def test_days_are_shanghai_days_newest_first_and_carry_no_text() -> None:
    late_evening_utc = datetime(2026, 9, 30, 17, 30, tzinfo=UTC)  # already 10-01 in Shanghai
    utterances = child_utterances(
        [
            _event("1", "child", "夜里说的话", late_evening_utc),
            _event("2", "child", "早上说的话", late_evening_utc + timedelta(hours=8)),
            _event("3", "child", "前一天说的话", late_evening_utc - timedelta(days=1)),
        ],
        subject_id="child",
        zone=ZONE,
    )
    days = day_activity(utterances, limit=30)
    assert days == [
        {"day": "2026-10-01", "message_count": 2},
        {"day": "2026-09-30", "message_count": 1},
    ]
    assert "话" not in json.dumps(days, ensure_ascii=False)
    assert day_activity(utterances, limit=1) == days[:1]


def _model_reply(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "title": "今天聊了画画和小狗",
        "overview": "孩子聊了学校里画画的事，也说到想养一只宠物，整体心情不错。",
        "highlights": ["画画", "想养宠物"],
        "mood": "positive",
        "suggestion": "可以找个轻松的时间，和孩子一起看看他的画。",
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


def _turns() -> list[ChildUtterance]:
    return [ChildUtterance(day=date(2026, 10, 1), text=text) for text in CHILD_WORDS]


def test_a_recap_in_the_models_own_words_is_accepted() -> None:
    recap = parse_recap(_model_reply(), _turns())
    assert recap is not None
    assert recap["mood"] == "positive" and recap["highlights"] == ["画画", "想养宠物"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"overview": "孩子说今天老师说我画的小狗像一只会飞的猫，很开心。"},  # a 10+ character quote
        {"highlights": ["我想要一只叫豆豆的小狗陪我玩"]},
        {"mood": "ecstatic"},
        {"title": ""},
        {"overview": ""},
    ],
)
def test_a_recap_that_quotes_or_breaks_the_shape_is_rejected(overrides: dict[str, Any]) -> None:
    assert parse_recap(_model_reply(**overrides), _turns()) is None


def test_short_common_phrases_are_not_quotes_and_identifiers_are_redacted() -> None:
    turns = [ChildUtterance(day=date(2026, 10, 1), text="我喜欢画画，好开心呀")]  # < 10 chars of overlap room
    recap = parse_recap(_model_reply(overview="孩子喜欢画画，电话13800138000可联系。"), turns)
    assert recap is not None
    assert "13800138000" not in str(recap["overview"])


def test_fallback_is_counts_only() -> None:
    recap = fallback_recap(date(2026, 10, 1), 7)
    assert "7 次" in str(recap["overview"]) and recap["highlights"] == []
    assert not any(word in json.dumps(recap, ensure_ascii=False) for word in CHILD_WORDS)


class _FakeClient:
    """Stands in for httpx.AsyncClient: returns one canned chat completion."""

    reply: object = None
    seen: list[dict[str, Any]] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> httpx.Response:
        type(self).seen.append({"url": url, "payload": json})
        if isinstance(type(self).reply, Exception):
            raise type(self).reply  # type: ignore[misc]
        body = {"choices": [{"message": {"content": type(self).reply}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


class _Settings:
    llm_provider = "qwen"

    class _Key:
        def __init__(self, value: str) -> None:
            self._value = value

        def get_secret_value(self) -> str:
            return self._value

    def __init__(self, key: str = "test-key") -> None:
        self.dashscope_api_key = self._Key(key)
        self.dashscope_base_url = "https://example.invalid/v1"
        self.dashscope_summary_model = "summary-model"
        self.dashscope_summary_timeout_s = 5.0
        self.deepseek_api_key = self._Key("")
        self.deepseek_base_url = ""
        self.deepseek_summary_model = ""
        self.deepseek_summary_timeout_s = 5.0


@pytest.mark.asyncio
async def test_recap_for_day_uses_the_model_but_never_returns_a_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guardian_recap.httpx, "AsyncClient", _FakeClient)
    _FakeClient.seen = []
    _FakeClient.reply = _model_reply()
    recap, source = await guardian_recap.recap_for_day(
        _Settings(), day=date(2026, 10, 1), utterances=_turns()  # type: ignore[arg-type]
    )
    assert source == "qwen" and recap["title"] == "今天聊了画画和小狗"
    # The child's words go to the summary model (that is its input), with the no-quote rule.
    prompt = _FakeClient.seen[0]["payload"]["messages"][1]["content"]
    assert "不得引用或复述孩子的原话" in prompt and CHILD_WORDS[0] in prompt
    assert _FakeClient.seen[0]["payload"]["enable_thinking"] is False

    _FakeClient.reply = _model_reply(overview="孩子说：" + CHILD_WORDS[0])
    quoted, quoted_source = await guardian_recap.recap_for_day(
        _Settings(), day=date(2026, 10, 1), utterances=_turns()  # type: ignore[arg-type]
    )
    assert quoted_source == "fallback" and "2 次" in str(quoted["overview"])

    _FakeClient.reply = RuntimeError("upstream down")
    down, down_source = await guardian_recap.recap_for_day(
        _Settings(), day=date(2026, 10, 1), utterances=_turns()  # type: ignore[arg-type]
    )
    assert down_source == "fallback"

    unconfigured, unconfigured_source = await guardian_recap.recap_for_day(
        _Settings(key=""), day=date(2026, 10, 1), utterances=_turns()  # type: ignore[arg-type]
    )
    assert unconfigured_source == "fallback" and unconfigured["highlights"] == []


# --- the endpoints ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardian_sees_day_counts_and_a_recap_but_never_the_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    recorder = _RecordingConsent()
    now = datetime.now(UTC)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.bound_subject_consent = recorder
        owner_id, headers = await _owner(client, app, "recap-owner")
        created = await _bind(
            client,
            app,
            owner_id=owner_id,
            headers=headers,
            device_id="device-recap",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child_id = created.json()["primary_subject_ids"][0]
        for index, (subject, text) in enumerate(
            [(child_id, CHILD_WORDS[0]), (child_id, CHILD_WORDS[1]), (owner_id, "这是家长自己说的话")]
        ):
            await app.state.life_archive.record(
                EvidenceEvent(
                    event_id=f"recap-{index}",
                    account_id=owner_id,
                    subject_id=subject,
                    event_type="speech.utterance_finalized",
                    occurred_at=now - timedelta(minutes=index),
                    speaker_class="owner",
                    source="test",
                    payload={
                        "text": text,
                        "history_eligible": True,
                        "owner_projection_eligible": True,
                        "memory_retention": "retained",
                    },
                )
            )
        today = now.astimezone(ZONE).date().isoformat()
        days_url = f"/v1/guardian/minors/{child_id}/days"
        recap_url = f"/v1/guardian/minors/{child_id}/days/{today}/recap"

        # Closed until the guardian ticks long-term memory for the child.
        closed = await client.get(days_url, headers=headers)
        closed_recap = await client.post(recap_url, headers=headers)
        granted = await client.post(
            f"/v1/guardian/minors/{child_id}/consents",
            headers={**headers, "Idempotency-Key": "recap-grant-0001"},
            json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
        )
        assert granted.status_code == 201, granted.text

        days = await client.get(days_url, headers=headers)

        seen: list[list[str]] = []

        async def stub(settings: object, *, day: date, utterances: list[ChildUtterance]) -> Any:
            seen.append([item.text for item in utterances])
            return _json_recap(), "qwen"

        monkeypatch.setattr("services.control_api.app.routes.guardian.recap_for_day", stub)
        recap = await client.post(recap_url, headers=headers)
        nothing = await client.post(
            f"/v1/guardian/minors/{child_id}/days/2020-01-01/recap", headers=headers
        )
        stranger_id, stranger_headers = await _owner(client, app, "recap-stranger")
        stranger = await client.get(days_url, headers=stranger_headers)

    assert closed.status_code == 403 and closed.json()["detail"]["code"] == "guardian_consent_required"
    assert closed_recap.status_code == 403
    assert days.status_code == 200, days.text
    assert days.json()["items"] == [{"day": today, "message_count": 2}], "the parent's own turn is not the child's"
    assert not any(word in days.text for word in CHILD_WORDS)

    assert recap.status_code == 200, recap.text
    body = recap.json()
    assert body["message_count"] == 2 and body["source"] == "qwen"
    assert body["privacy"] == {"contains_transcript": False}
    assert sorted(seen[0]) == sorted(CHILD_WORDS), "only the child's turns reach the model"
    assert not any(word in recap.text for word in CHILD_WORDS)
    assert nothing.status_code == 404 and nothing.json()["detail"]["code"] == "no_conversation_that_day"
    assert stranger.status_code in {403, 404}, "another account is not this child's guardian"
    assert stranger_id != owner_id


def _json_recap() -> dict[str, object]:
    return json.loads(_model_reply())
