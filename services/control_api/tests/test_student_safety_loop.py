"""P0-04 学生安全闭环：受控学生账号的危机路径到家长端只读列表。

范围边界（用户 2026-09-14 决定，本阶段不做微信发送链路）：

- 这里验证的是「入队 + 家长端列表可读 + 当前无投递通道」。
- 入队**不等于**家长已收到。发送 worker / 重试 / 模板 / 凭据都不在本阶段范围内。
- 真实设备上的固定安全话术（听感）仍待真机时段，不属于本文件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY
from services.common.crisis_policy import CRISIS_SCRIPT_VERSION
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.test_interaction_api import (
    _attach_signed_runtime_profile,
)

_RESPONSE_PLAN_TOKEN = "p0-04-student-safety-plan-token-long-enough"
_POLICY_TOKEN = "p0-04-student-safety-policy-token-long-enough"
_ARCHIVE_TOKEN = "p0-04-student-safety-archive-token-long-enough"

# 一个只带第一人称风险锚点、能被确定性策略直接命中的学生表达。
_CRISIS_QUERY = "我不会做题。我不想活了"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_SPEAKER_DB_PATH", str(tmp_path / "speaker.sqlite3"))
    monkeypatch.setenv("MEMORIA_EVOLUTION_DB_PATH", str(tmp_path / "evolution.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_ARCHIVE_OBJECT_STORE_PATH",
        str(tmp_path / "archive-objects"),
    )
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "p0-04-student-safety-auth-secret-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", _ARCHIVE_TOKEN)
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", _RESPONSE_PLAN_TOKEN)
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    monkeypatch.setenv("OFFLINE_MOCK", "true")


async def _register(client: AsyncClient, username: str) -> tuple[dict[str, Any], dict[str, str]]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    identity = response.json()
    return identity, {"Authorization": f"Bearer {identity['access_token']}"}


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _mark_verified_adult(app: Any, user_id: str) -> None:
    app.state.memory_store.update_subject_profile(
        user_id=user_id,
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )


async def _bind_family(
    client: AsyncClient,
    app: Any,
    *,
    parent_username: str,
    child_username: str,
    binding_key: str,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], dict[str, str], dict[str, Any]]:
    """Register a guardian + child pair and activate the link (child confirms)."""

    parent, _ = await _register(client, parent_username)
    child, child_headers = await _register(client, child_username)
    _mark_verified_adult(app, parent["user_id"])
    parent_headers = await _login(client, parent_username)
    app.state.memory_store.bind_external_identities(
        preferred_user_id=parent["user_id"],
        identities={"wechat_openid": f"{parent_username}-openid"},
        now=datetime.now(UTC).isoformat(),
    )
    created = await client.post(
        "/v1/guardian/links",
        headers={**parent_headers, "Idempotency-Key": f"{binding_key}-request-001"},
        json={"minor_user_id": child["user_id"], "relation": "parent"},
    )
    assert created.status_code == 201, created.text
    link = created.json()
    confirmed = await client.post(
        f"/v1/guardian/links/{link['link_id']}/confirm",
        headers=child_headers,
        json={"binding_code": link["binding_code"], "birth_year_band": "under_14"},
    )
    assert confirmed.status_code == 200, confirmed.text
    # Confirming the minor transition terminates the child's existing sessions,
    # so the pre-confirmation bearer token is no longer usable.
    child_headers = await _login(client, child_username)
    return parent, parent_headers, link, child, child_headers


async def _minor_session(
    client: AsyncClient,
    app: Any,
    *,
    child: dict[str, Any],
    child_headers: dict[str, str],
    link: dict[str, Any],
    parent_headers: dict[str, str],
    binding_key: str,
    consent: bool = True,
) -> dict[str, str]:
    if consent:
        granted = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": f"{binding_key}-voice-001"},
            json={
                "consent_kind": "minor_voice_session",
                "policy_version": "minor-voice-v1",
            },
        )
        assert granted.status_code == 201, granted.text
    session = await client.post(
        "/v1/sessions",
        headers=child_headers,
        json={"session_focus": "tutor_english"},
    )
    assert session.status_code == 200, session.text
    _attach_signed_runtime_profile(
        app,
        user_id=child["user_id"],
        session_id=session.json()["session_id"],
        subject_category="minor",
        age_band="under_14",
        service_mode="student_minor",
        capabilities=("chat", "tutor", "english_practice"),
    )
    return {"session_id": session.json()["session_id"]}


async def _response_plan(
    client: AsyncClient,
    *,
    session_id: str,
    query: str,
    turn_id: int,
    generation_id: int,
    speaker_classification: str = "owner",
) -> Any:
    return await client.post(
        "/v1/interaction/response-plan",
        headers={"X-Memoria-Internal-Token": _RESPONSE_PLAN_TOKEN},
        json={
            "session_id": session_id,
            "query": query,
            "utterance_intent": "chat",
            "fence": {
                "session_id": session_id,
                "turn_id": turn_id,
                "generation_id": generation_id,
                "tool_epoch": 0,
            },
            "speaker_decision": {
                "classification": speaker_classification,
                "reason_code": "test",
                "model_version": "test-speaker-v1",
                "profile_id": None,
                "template_version": None,
            },
        },
    )


async def _crisis_event_count(app: Any, *, minor_user_id: str) -> int:
    events = await app.state.life_archive.evidence_window(
        account_id=minor_user_id,
        occurred_after=datetime(1970, 1, 1, tzinfo=UTC),
        occurred_before=datetime.now(UTC),
        event_types=("guardian.crisis_event",),
    )
    return len(events)


@pytest.mark.asyncio
async def test_minor_crisis_queues_one_notification_and_fixed_text_reaches_the_device(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """学生危机 → 固定话术交付 + outbox 入队 + 家长端只读列表可见（无投递通道）。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-parent",
            child_username="safety-child",
            binding_key="safety-basic",
        )
        session = await _minor_session(
            client,
            app,
            child=child,
            child_headers=child_headers,
            link=link,
            parent_headers=parent_headers,
            binding_key="safety-basic",
        )

        crisis = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert crisis.status_code == 200, crisis.text
        body = crisis.json()

        # 设备端固定安全话术就是 response plan 的 direct_text，逐字相同。
        assert body["direct_text"] == CRISIS_SUPPORT_REPLY
        assert "急救或报警" in body["direct_text"]
        # 固定话术不得被导师话轮约束或普通人格上下文改写。
        assert "【导师话轮约束】" not in body["instructions"]

        notifications = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert notifications.status_code == 200, notifications.text
        items = notifications.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["minor_user_id"] == child["user_id"]
        assert item["channel"] == "wechat_subscription"
        # 边界：本阶段没有发送 worker，入队不等于家长已收到。
        assert item["delivery_status"] == "pending"
        assert item["contains_transcript"] is False
        assert item["contains_severity"] is False

        serialized = json.dumps(notifications.json(), ensure_ascii=False)
        assert _CRISIS_QUERY not in serialized
        assert "不想活" not in serialized

        events = await app.state.life_archive.evidence_window(
            account_id=child["user_id"],
            occurred_after=datetime(1970, 1, 1, tzinfo=UTC),
            occurred_before=datetime.now(UTC),
            event_types=("guardian.crisis_event",),
        )
        assert len(events) == 1
        payload = dict(events[0].payload)
        assert payload["script_version"] == CRISIS_SCRIPT_VERSION
        assert payload["contains_transcript"] is False
        assert payload["contains_severity"] is False
        assert _CRISIS_QUERY not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_replay_of_the_same_generation_does_not_duplicate_the_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同一 generation 重放同一危机表达只能产生一条入队记录。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-replay-parent",
            child_username="safety-replay-child",
            binding_key="safety-replay",
        )
        session = await _minor_session(
            client,
            app,
            child=child,
            child_headers=child_headers,
            link=link,
            parent_headers=parent_headers,
            binding_key="safety-replay",
        )

        # 同 fence 同 fingerprint 的重复请求走 response-plan 缓存，不得再次入队。
        first = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=1,
        )
        replay = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=1,
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json() == first.json()

        notifications = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert len(notifications.json()["items"]) == 1
        assert await _crisis_event_count(app, minor_user_id=child["user_id"]) == 1

        # 绕开 HTTP 缓存直接重放同一 fence：store 层幂等必须仍然是同一条事件。
        service = app.state.crisis_notification_service
        direct_first = await service.record_minor_crisis(
            minor_user_id=child["user_id"],
            session_id=session["session_id"],
            turn_id=2,
            generation_id=1,
            tool_epoch=0,
            script_version=CRISIS_SCRIPT_VERSION,
        )
        direct_replay = await service.record_minor_crisis(
            minor_user_id=child["user_id"],
            session_id=session["session_id"],
            turn_id=2,
            generation_id=1,
            tool_epoch=0,
            script_version=CRISIS_SCRIPT_VERSION,
        )
        assert direct_first == direct_replay
        assert direct_first.notification_count == 1
        assert await _crisis_event_count(app, minor_user_id=child["user_id"]) == 1
        assert len((await client.get(
            "/v1/guardian/notifications", headers=parent_headers
        )).json()["items"]) == 1


@pytest.mark.asyncio
async def test_a_later_generation_is_a_separate_crisis_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新一轮真实风险必须重新入队，不能被幂等键吞掉。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-second-parent",
            child_username="safety-second-child",
            binding_key="safety-second",
        )
        session = await _minor_session(
            client,
            app,
            child=child,
            child_headers=child_headers,
            link=link,
            parent_headers=parent_headers,
            binding_key="safety-second",
        )

        first = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=3,
            generation_id=1,
        )
        second = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=4,
            generation_id=2,
        )
        assert first.status_code == 200
        assert second.status_code == 200

        notifications = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert len(notifications.json()["items"]) == 2
        assert await _crisis_event_count(app, minor_user_id=child["user_id"]) == 2


@pytest.mark.asyncio
async def test_adult_crisis_gets_the_fixed_reply_without_touching_the_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成人危机必须照常说固定安全话术，但不得产生家长通知。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        adult, adult_headers = await _register(client, "safety-adult")
        _mark_verified_adult(app, adult["user_id"])
        adult_headers = await _login(client, "safety-adult")
        session = await client.post("/v1/sessions", headers=adult_headers, json={})
        assert session.status_code == 200, session.text
        session_id = session.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=adult["user_id"],
            session_id=session_id,
            subject_category="adult",
            age_band="adult",
            service_mode="adult_companion",
            capabilities=("chat",),
        )

        crisis = await _response_plan(
            client,
            session_id=session_id,
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
        )

        assert crisis.status_code == 200, crisis.text
        assert crisis.json()["direct_text"] == CRISIS_SUPPORT_REPLY
        assert await _crisis_event_count(app, minor_user_id=adult["user_id"]) == 0


@pytest.mark.asyncio
async def test_unknown_safe_subject_gets_the_fixed_reply_without_any_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """unknown/guest 主体拿匿名公开面，危机话术仍固定，但绝不进私有链。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner, owner_headers = await _register(client, "safety-owner")
        _mark_verified_adult(app, owner["user_id"])
        owner_headers = await _login(client, "safety-owner")
        session = await client.post("/v1/sessions", headers=owner_headers, json={})
        assert session.status_code == 200, session.text
        session_id = session.json()["session_id"]
        _attach_signed_runtime_profile(
            app,
            user_id=owner["user_id"],
            session_id=session_id,
            unknown_subject=True,
        )

        crisis = await _response_plan(
            client,
            session_id=session_id,
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
            speaker_classification="guest",
        )

        assert crisis.status_code == 200, crisis.text
        assert crisis.json()["direct_text"] == CRISIS_SUPPORT_REPLY
        # 匿名公开面不得因为危机话术而变成主人私有链。
        notifications = await client.get("/v1/guardian/notifications", headers=owner_headers)
        assert notifications.status_code in {403, 200}
        if notifications.status_code == 200:
            assert notifications.json()["items"] == []
        assert await _crisis_event_count(app, minor_user_id=owner["user_id"]) == 0


@pytest.mark.asyncio
async def test_minor_crisis_still_delivers_the_fixed_reply_when_the_outbox_is_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """通知入队不可用时，固定安全话术不得被压掉（安全优先于记账）。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-down-parent",
            child_username="safety-down-child",
            binding_key="safety-down",
        )
        session = await _minor_session(
            client,
            app,
            child=child,
            child_headers=child_headers,
            link=link,
            parent_headers=parent_headers,
            binding_key="safety-down",
        )

        class BrokenOutbox:
            async def record_minor_crisis(self, **_: object) -> None:
                raise RuntimeError("outbox unavailable")

        app.state.crisis_notification_service = BrokenOutbox()

        crisis = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
        )

        assert crisis.status_code == 200, crisis.text
        assert crisis.json()["direct_text"] == CRISIS_SUPPORT_REPLY


@pytest.mark.asyncio
async def test_guardian_notification_list_is_guardian_scoped_and_needs_an_active_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """家长端列表只对已绑定微信的 guardian 开放，且不返回别人的孩子。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-scope-parent",
            child_username="safety-scope-child",
            binding_key="safety-scope",
        )
        session = await _minor_session(
            client,
            app,
            child=child,
            child_headers=child_headers,
            link=link,
            parent_headers=parent_headers,
            binding_key="safety-scope",
        )
        crisis = await _response_plan(
            client,
            session_id=session["session_id"],
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert crisis.status_code == 200

        # 已在绑定的家长可以看到。
        bound = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert bound.status_code == 200
        assert len(bound.json()["items"]) == 1

        # 另一个孩子账号即使有微信身份，也看不到不属于自己的通知。
        other, _ = await _register(client, "safety-scope-bystander")
        _mark_verified_adult(app, other["user_id"])
        other_headers = await _login(client, "safety-scope-bystander")
        app.state.memory_store.bind_external_identities(
            preferred_user_id=other["user_id"],
            identities={"wechat_openid": "bystander-openid"},
            now=datetime.now(UTC).isoformat(),
        )
        bystander = await client.get("/v1/guardian/notifications", headers=other_headers)
        assert bystander.status_code == 200
        assert bystander.json()["items"] == []

        # 孩子本人没有 guardian_manage，不得读家长端列表。
        child_view = await client.get("/v1/guardian/notifications", headers=child_headers)
        assert child_view.status_code == 403
        assert child_view.json()["detail"]["code"] in {
            "subject_capability_forbidden",
            "minor_forbidden",
        }


@pytest.mark.asyncio
async def test_revoked_voice_consent_stops_the_next_minor_turn_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """撤销学生语音同意后立即生效：新会话被拒，不留旧会话可用。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-revoke-parent",
            child_username="safety-revoke-child",
            binding_key="safety-revoke",
        )
        granted = await client.post(
            f"/v1/guardian/links/{link['link_id']}/consents",
            headers={**parent_headers, "Idempotency-Key": "safety-revoke-voice-001"},
            json={
                "consent_kind": "minor_voice_session",
                "policy_version": "minor-voice-v1",
            },
        )
        assert granted.status_code == 201
        consent_id = granted.json()["consent_id"]

        allowed = await client.post(
            "/v1/sessions",
            headers=child_headers,
            json={"session_focus": "tutor_english"},
        )
        assert allowed.status_code == 200
        first_session_id = allowed.json()["session_id"]

        revoked = await client.delete(
            f"/v1/guardian/links/{link['link_id']}/consents/{consent_id}",
            headers={**parent_headers, "Idempotency-Key": "safety-revoke-voice-002"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False

        blocked = await client.post(
            "/v1/sessions",
            headers=child_headers,
            json={"session_focus": "tutor_homework"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == {
            "code": "guardian_consent_required",
            "capability": "minor_voice_session",
        }
        # 撤销同时终止已登记的语音会话，不留下旧载体。
        assert app.state.memory_store.get_voice_session_by_id(session_id=first_session_id) is None


@pytest.mark.asyncio
async def test_inviting_family_does_not_elevate_the_child_to_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """学生账号即使被邀请进家庭，也不得提升为 owner 能力。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, parent_headers, link, child, child_headers = await _bind_family(
            client,
            app,
            parent_username="safety-elevate-parent",
            child_username="safety-elevate-child",
            binding_key="safety-elevate",
        )
        assert link["actor_role"] == "guardian"

        # 学生自己的链接视角是 minor，不是 guardian。
        child_links = await client.get("/v1/guardian/links", headers=child_headers)
        assert child_links.status_code == 200
        roles = {item["actor_role"] for item in child_links.json()["items"]}
        assert roles == {"minor"}

        profile = app.state.memory_store.get_subject_profile(user_id=child["user_id"])
        assert profile is not None
        assert profile["subject_category"] == "minor"

        # adult-only 能力必须继续被拒。
        digital_self = await client.get("/v1/digital-self/versions", headers=child_headers)
        assert digital_self.status_code == 403
        assert digital_self.json()["detail"]["code"] == "minor_forbidden"

        speakers = await client.get("/v1/speakers", headers=child_headers)
        assert speakers.status_code == 403
        assert speakers.json()["detail"]["code"] == "minor_forbidden"


@pytest.mark.asyncio
async def test_adult_account_manages_independent_under_14_subject_without_child_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """管理账号为 adult，独立设置 under_14 使用人（无需孩子注册账号），启用学生危机固定话术并正确入队家长通知。"""

    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 成人注册并登录
        parent, parent_headers = await _register(client, "adult-manager-001")
        _mark_verified_adult(app, parent["user_id"])
        await app.state.identity_service.register_person(
            person_id=parent["user_id"],
            display_name="成人管理者",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=datetime.now(UTC),
        )
        parent_headers = await _login(client, "adult-manager-001")
        app.state.memory_store.bind_external_identities(
            preferred_user_id=parent["user_id"],
            identities={"wechat_openid": "adult-manager-openid"},
            now=datetime.now(UTC).isoformat(),
        )

        # 2. 成人绑定设备，设置独立的使用人小明（under_14，未成年），无需孩子建账号
        now = datetime.now(UTC)
        token = mint_device_binding_token(
            device_id="dev-child-no-account-1",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=10),
            nonce="test-nonce-1234",
        )
        binding_res = await client.post(
            "/v1/device-bindings",
            headers={**parent_headers, "Idempotency-Key": "bind-child-independent-001"},
            json={
                "device_claim_token": token,
                "declared_mode": "parent_for_child",
                "account_owner_person_id": parent["user_id"],
                "primary_subject": {
                    "person_id": "new",
                    "relationship": "guardian_of",
                    "subject_draft": {
                        "display_name": "独立小明",
                        "age_band": "under_14",
                    },
                },
                "persona_selection": "starlight",
                "consent_offer_ids": [
                    "offer_minor_voice_session_v1",
                ],
            },
        )
        assert binding_res.status_code == 201, binding_res.text
        binding_manifest = binding_res.json()
        child_person_id = binding_manifest["primary_subject_ids"][0]
        assert child_person_id != parent["user_id"]

        # 3. 开启会话
        session_res = await client.post(
            "/v1/sessions",
            headers=parent_headers,
            json={"session_focus": "tutor_english"},
        )
        assert session_res.status_code == 200
        session_id = session_res.json()["session_id"]

        # 挂载当前使用人（孩子小明）的签名 RuntimeProfile
        _attach_signed_runtime_profile(
            app,
            user_id=parent["user_id"],
            session_id=session_id,
            active_subject_id=child_person_id,
            subject_category="minor",
            age_band="under_14",
            service_mode="student_minor",
            capabilities=("chat", "tutor", "english_practice"),
        )

        # 4. 触发危机语句
        response = await _response_plan(
            client,
            session_id=session_id,
            query=_CRISIS_QUERY,
            turn_id=1,
            generation_id=1,
            speaker_classification="owner",
        )
        assert response.status_code == 200, response.text
        plan = response.json()

        # 断言设备端下发的固定危机安全话术逐字一致
        assert plan["direct_text"] == CRISIS_SUPPORT_REPLY

        # 断言家长通知入队，且归属于孩子的 person_id
        notifications = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert notifications.status_code == 200
        items = notifications.json()["items"]
        assert len(items) == 1
        assert items[0]["minor_user_id"] == child_person_id
        assert items[0]["minor_display_name"] == "独立小明"
        assert items[0]["delivery_status"] == "pending"

        # 5. 对照验证：若切换为成人使用人（例如家长本人），危机语句虽然给出通用固定话术，但绝不入队未成年危机通知
        _attach_signed_runtime_profile(
            app,
            user_id=parent["user_id"],
            session_id=session_id,
            subject_category="adult",
            age_band="adult",
            service_mode="adult_companion",
            capabilities=("chat",),
        )
        adult_response = await _response_plan(
            client,
            session_id=session_id,
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=2,
            speaker_classification="owner",
        )
        assert adult_response.status_code == 200
        assert adult_response.json()["direct_text"] == CRISIS_SUPPORT_REPLY

        # 通知队列数量依然为 1（成人危机不碰 minor outbox）
        notifications_after = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert len(notifications_after.json()["items"]) == 1

        # 6. 对照验证：若使用人为 unknown / guest，固定安全回复保留，但不进入主人通知链
        _attach_signed_runtime_profile(
            app,
            user_id=parent["user_id"],
            session_id=session_id,
            unknown_subject=True,
            service_mode="unknown_safe",
        )
        guest_response = await _response_plan(
            client,
            session_id=session_id,
            query=_CRISIS_QUERY,
            turn_id=3,
            generation_id=3,
            speaker_classification="guest",
        )
        assert guest_response.status_code == 200
        assert guest_response.json()["direct_text"] == CRISIS_SUPPORT_REPLY

        # 通知队列依然为 1
        notifications_final = await client.get("/v1/guardian/notifications", headers=parent_headers)
        assert len(notifications_final.json()["items"]) == 1
