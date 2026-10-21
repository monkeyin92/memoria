from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from services.control_api.app import guardian_push, wechat_auth
from services.control_api.app.config import ControlSettings
from services.control_api.app.guardian_push import (
    WechatSubscribeSender,
    build_crisis_push_worker,
    crisis_alert_page,
    fit_keyword_value,
)
from services.control_api.app.main import create_app
from services.control_api.app.unconfigured_store import UnconfiguredStore
from services.control_api.tests.test_guardian_api import (
    _configure,
    _login,
    _mark_verified_adult,
    _register,
)
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.push import (
    CRISIS_PUSH_DEFAULT_CHILD_NAME,
    CRISIS_PUSH_TIP,
    CRISIS_PUSH_TITLE,
    CrisisPushContent,
    CrisisPushStorePort,
    PendingCrisisPush,
    crisis_push_content,
)
from services.guardian.subject_activity import SubjectActivityStore

TEMPLATE = "CrisisTemplate_01-x"
LOGIN_CODE = "dev-guardian-push-login"
NOTIFICATION_ID = "6f21b0c8-4a0e-4f57-9c3a-2d1e5b7a9c40"


def _unused_store() -> CrisisPushStorePort:
    # Building the worker never touches the store; any call would raise.
    return cast(CrisisPushStorePort, UnconfiguredStore("guardian", "MEMORIA_GUARDIAN_DATABASE_URL"))


def _push_settings(**overrides: Any) -> ControlSettings:
    values: dict[str, Any] = {
        "_env_file": None,
        "OFFLINE_MOCK": False,
        "MEMORIA_GUARDIAN_PUSH_ENABLED": True,
        "WECHAT_MINIPROGRAM_APPID": "wx-test",
        "WECHAT_MINIPROGRAM_APPSECRET": "wechat-secret",
        "MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID": TEMPLATE,
        "WECHAT_ACCESS_TOKEN_ENDPOINT": "https://wechat.example/token",
        "WECHAT_SUBSCRIBE_SEND_ENDPOINT": "https://wechat.example/subscribe",
    }
    values.update(overrides)
    return ControlSettings(**values)


def test_guardian_push_is_disabled_by_default() -> None:
    settings = ControlSettings(_env_file=None)

    assert settings.guardian_push_enabled is False
    assert settings.wechat_subscribe_crisis_template_id == ""
    assert (
        settings.wechat_subscribe_send_endpoint
        == "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
    )
    assert settings.wechat_subscribe_crisis_page == "pages/attention/index"
    assert settings.wechat_miniprogram_state == "formal"
    settings.validate_guardian_push()  # disabled: nothing to validate
    assert (
        build_crisis_push_worker(
            settings,
            _unused_store(),
        )
        is None
    )


def test_enabled_guardian_push_requires_wechat_credentials_and_template() -> None:
    with pytest.raises(ValueError, match="WECHAT_MINIPROGRAM_APPID"):
        _push_settings(WECHAT_MINIPROGRAM_APPSECRET="").validate_guardian_push()
    with pytest.raises(ValueError, match="TEMPLATE_ID"):
        _push_settings(MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID="").validate_guardian_push()
    with pytest.raises(ValueError, match="TEMPLATE_ID"):
        _push_settings(
            MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID="bad template"
        ).validate_guardian_push()
    with pytest.raises(ValueError, match="HTTPS"):
        _push_settings(
            WECHAT_SUBSCRIBE_SEND_ENDPOINT="http://wechat.example/subscribe"
        ).validate_guardian_push()
    with pytest.raises(ValueError, match="TEMPLATE_ID"):
        build_crisis_push_worker(
            _push_settings(MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID=""),
            _unused_store(),
        )
    for fields in ("title=thing1,title=thing2", "body=thing1", "tip=thing", "", "a"):
        with pytest.raises(ValidationError):
            _push_settings(MEMORIA_WECHAT_SUBSCRIBE_CRISIS_FIELDS=fields)
    with pytest.raises(ValidationError):
        _push_settings(MEMORIA_WECHAT_SUBSCRIBE_CRISIS_PAGE="/pages/guardian/index")
    with pytest.raises(ValidationError):
        _push_settings(WECHAT_MINIPROGRAM_STATE="beta")

    settings = _push_settings(
        MEMORIA_WECHAT_SUBSCRIBE_CRISIS_FIELDS="time=time2,title=thing1",
        WECHAT_MINIPROGRAM_STATE="trial",
    )
    settings.validate_guardian_push()
    assert settings.guardian_push_crisis_fields() == {"time": "time2", "title": "thing1"}
    worker = build_crisis_push_worker(
        settings,
        _unused_store(),
    )
    assert worker is not None


class _FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _install_fake_wechat(
    monkeypatch: pytest.MonkeyPatch,
    send_responses: list[_FakeResponse],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    tokens = iter(f"token-{index}" for index in range(100))

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            _ = timeout

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            _ = args

        async def get(self, url: str, **kwargs: object) -> _FakeResponse:
            calls.append({"method": "GET", "url": url})
            return _FakeResponse({"access_token": next(tokens), "expires_in": 7200})

        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            calls.append(
                {
                    "method": "POST",
                    "url": url,
                    "token": kwargs["params"]["access_token"],
                    "json": kwargs["json"],
                }
            )
            return send_responses.pop(0)

    monkeypatch.setattr(guardian_push.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(wechat_auth.httpx, "AsyncClient", FakeClient)
    wechat_auth._ACCESS_TOKEN_CACHE.clear()
    return calls


def _content() -> CrisisPushContent:
    return crisis_push_content(
        occurred_at=datetime(2026, 9, 25, 2, 30, tzinfo=UTC),
        child_display_name="小明2号",
    )


@pytest.mark.asyncio
async def test_wechat_sender_posts_only_the_fixed_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_wechat(
        monkeypatch,
        [_FakeResponse({"errcode": 0, "errmsg": "ok"}), _FakeResponse({"errcode": 0})],
    )
    sender = WechatSubscribeSender(_push_settings())

    first = await sender.send_crisis_alert(
        openid="openid-a",
        template_id=TEMPLATE,
        content=_content(),
        notification_id=NOTIFICATION_ID,
    )
    second = await sender.send_crisis_alert(
        openid="openid-a",
        template_id=TEMPLATE,
        content=_content(),
        notification_id=NOTIFICATION_ID,
    )

    assert first.outcome == second.outcome == "delivered"
    gets = [call for call in calls if call["method"] == "GET"]
    posts = [call for call in calls if call["method"] == "POST"]
    assert len(gets) == 1  # the access token is cached across sends
    assert posts[0]["url"] == "https://wechat.example/subscribe"
    assert posts[0]["json"] == {
        "touser": "openid-a",
        "template_id": TEMPLATE,
        "page": f"pages/attention/index?id={NOTIFICATION_ID}",
        "miniprogram_state": "formal",
        "lang": "zh_CN",
        "data": {
            "thing1": {"value": "有一条需要你关注的对话"},
            "name2": {"value": CRISIS_PUSH_DEFAULT_CHILD_NAME},
            "time3": {"value": "2026-09-25 10:30"},
            "thing4": {"value": "有一条需要你关注的对话"},
        },
    }
    body = json.dumps(posts[0]["json"], ensure_ascii=False)
    for forbidden in ("transcript", "text", "utterance", "severity", "reply", "小明"):
        assert forbidden not in body
    wechat_auth._ACCESS_TOKEN_CACHE.clear()


def test_crisis_push_request_body_has_no_child_name_or_transcript() -> None:
    fake_name = "假名字小岸"
    transcript = "我不想活了，今天数学又考砸了"
    content = crisis_push_content(
        occurred_at=datetime(2026, 9, 25, 2, 30, tzinfo=UTC),
        child_display_name=fake_name,
    )

    body = WechatSubscribeSender(_push_settings()).request_body(
        openid="openid-a",
        template_id=TEMPLATE,
        content=content,
        notification_id=NOTIFICATION_ID,
    )

    assert body["data"] == {
        "thing1": {"value": CRISIS_PUSH_TITLE},
        "name2": {"value": CRISIS_PUSH_DEFAULT_CHILD_NAME},
        "time3": {"value": "2026-09-25 10:30"},
        "thing4": {"value": CRISIS_PUSH_TIP},
    }
    serialized = json.dumps(body["data"], ensure_ascii=False)
    assert fake_name not in serialized
    assert transcript not in serialized


def test_crisis_alert_page_deep_links_only_uuid_notifications() -> None:
    page = "pages/attention/index"

    assert crisis_alert_page(page, NOTIFICATION_ID) == f"{page}?id={NOTIFICATION_ID}"
    assert crisis_alert_page(f"{page}?from=push", NOTIFICATION_ID) == (
        f"{page}?from=push&id={NOTIFICATION_ID}"
    )
    assert crisis_alert_page(page, "not-a-uuid") == page
    assert crisis_alert_page(page, "") == page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome", "error_code"),
    [
        (_FakeResponse({"errcode": 43101, "errmsg": "user refuse"}), "no_subscription", "wechat_43101"),
        (_FakeResponse({"errcode": -1, "errmsg": "busy"}), "retry", "wechat_-1"),
        (_FakeResponse({"errcode": 45009}), "retry", "wechat_45009"),
        (_FakeResponse({"errcode": 40037, "errmsg": "bad template"}), "failed", "wechat_40037"),
        (_FakeResponse({"errcode": 47003}), "failed", "wechat_47003"),
        (_FakeResponse(ValueError("html")), "retry", "wechat_subscribe_invalid_response"),
        (_FakeResponse(ValueError("html"), status_code=502), "retry", "wechat_http_502"),
        (_FakeResponse(ValueError("html"), status_code=404), "failed", "wechat_http_404"),
    ],
)
async def test_wechat_sender_classifies_errcodes(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    outcome: str,
    error_code: str,
) -> None:
    _install_fake_wechat(monkeypatch, [response])

    result = await WechatSubscribeSender(_push_settings()).send_crisis_alert(
        openid="openid-a", template_id=TEMPLATE, content=_content()
    )

    assert (result.outcome, result.error_code) == (outcome, error_code)
    wechat_auth._ACCESS_TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_wechat_sender_refreshes_a_rejected_access_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_wechat(
        monkeypatch,
        [_FakeResponse({"errcode": 42001}), _FakeResponse({"errcode": 0})],
    )

    result = await WechatSubscribeSender(_push_settings()).send_crisis_alert(
        openid="openid-a", template_id=TEMPLATE, content=_content()
    )

    assert result.outcome == "delivered"
    posts = [call for call in calls if call["method"] == "POST"]
    assert [call["token"] for call in posts] == ["token-0", "token-1"]

    calls = _install_fake_wechat(
        monkeypatch,
        [_FakeResponse({"errcode": 40001}), _FakeResponse({"errcode": 40001})],
    )
    again = await WechatSubscribeSender(_push_settings()).send_crisis_alert(
        openid="openid-a", template_id=TEMPLATE, content=_content()
    )
    assert (again.outcome, again.error_code) == ("retry", "wechat_40001")
    assert len([call for call in calls if call["method"] == "POST"]) == 2
    wechat_auth._ACCESS_TOKEN_CACHE.clear()


def test_keyword_values_fit_wechat_limits() -> None:
    assert fit_keyword_value("thing1", "一" * 30) == "一" * 20
    assert fit_keyword_value("name2", "Tom 123") == "Tom"
    assert fit_keyword_value("name2", "123") == "TA"
    assert fit_keyword_value("phrase3", "安全提醒已送达") == "安全提醒已"
    assert fit_keyword_value("time4", "2026-09-25 10:30") == "2026-09-25 10:30"


def _enable_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORIA_GUARDIAN_PUSH_ENABLED", "true")
    monkeypatch.setenv("MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID", TEMPLATE)
    monkeypatch.setenv("WECHAT_MINIPROGRAM_APPID", "wx-test")
    monkeypatch.setenv("WECHAT_MINIPROGRAM_APPSECRET", "wechat-secret")


async def _wechat_guardian(client: AsyncClient, app: Any, username: str) -> tuple[str, dict[str, str]]:
    identity, _ = await _register(client, username)
    _mark_verified_adult(app, identity["user_id"])
    headers = await _login(client, username)
    app.state.memory_store.bind_external_identities(
        preferred_user_id=identity["user_id"],
        identities={
            "wechat_openid": wechat_auth.openid_hash(f"dev-openid-{LOGIN_CODE}")
        },
        now=datetime.now(UTC).isoformat(),
    )
    return str(identity["user_id"]), headers


@pytest.mark.asyncio
async def test_push_endpoints_never_offer_a_prompt_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.get("/v1/guardian/push-config")
        assert unauthenticated.status_code == 401
        _, headers = await _wechat_guardian(client, app, "push-disabled-parent")

        config = await client.get("/v1/guardian/push-config", headers=headers)
        assert config.status_code == 200
        assert config.json() == {"enabled": False, "template_ids": {}}
        recorded = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "accept", "login_code": LOGIN_CODE},
        )
        assert recorded.status_code == 409
        assert recorded.json()["detail"] == {"code": "guardian_push_disabled"}


@pytest.mark.asyncio
async def test_guardian_records_subscriptions_for_its_own_wechat_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    _enable_push(monkeypatch)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.post(
            "/v1/guardian/push-subscriptions",
            json={"template_id": TEMPLATE, "result": "reject"},
        )
        assert unauthenticated.status_code == 401
        guardian_id, headers = await _wechat_guardian(client, app, "push-parent")

        config = await client.get("/v1/guardian/push-config", headers=headers)
        assert config.json() == {"enabled": True, "template_ids": {"crisis": TEMPLATE}}

        empty = await client.get("/v1/guardian/push-subscriptions", headers=headers)
        assert empty.status_code == 200
        assert empty.json() == {
            "template_id": TEMPLATE,
            "remaining": 0,
            "last_result": None,
            "subscribed": False,
        }

        accepted = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "accept", "login_code": LOGIN_CODE},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"template_id": TEMPLATE, "result": "accept", "remaining": 1}
        assert "openid" not in accepted.text

        status = await client.get("/v1/guardian/push-subscriptions", headers=headers)
        assert status.json() == {
            "template_id": TEMPLATE,
            "remaining": 1,
            "last_result": "accept",
            "subscribed": True,
        }

        rejected = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "reject"},
        )
        assert rejected.json() == {"template_id": TEMPLATE, "result": "reject", "remaining": 1}

        missing_code = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "accept"},
        )
        assert missing_code.status_code == 422
        other_wechat = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={
                "template_id": TEMPLATE,
                "result": "accept",
                "login_code": "dev-someone-else",
            },
        )
        assert other_wechat.status_code == 403
        assert other_wechat.json()["detail"] == {"code": "guardian_push_identity_mismatch"}
        unknown_template = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": "another-template", "result": "reject"},
        )
        assert unknown_template.status_code == 422
        invalid_result = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "filter"},
        )
        assert invalid_result.status_code == 422

        opted_out = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=headers,
            json={"template_id": TEMPLATE, "result": "opt_out"},
        )
        assert opted_out.status_code == 200
        assert opted_out.json() == {
            "template_id": TEMPLATE,
            "result": "opt_out",
            "remaining": 0,
        }
        after_opt_out = await client.get("/v1/guardian/push-subscriptions", headers=headers)
        assert after_opt_out.json() == {
            "template_id": TEMPLATE,
            "remaining": 0,
            "last_result": "opt_out",
            "subscribed": False,
        }

        store = app.state.guardian_store
        subscription = await store.push_subscription(
            guardian_user_id=guardian_id,
            template_id=TEMPLATE,
        )
        assert subscription is not None and subscription.remaining == 0

        # An account without a bound WeChat identity is never prompted.
        _, plain_headers = await _register(client, "push-plain-account")
        plain = await client.get("/v1/guardian/push-config", headers=plain_headers)
        assert plain.status_code in {200, 403}
        if plain.status_code == 200:
            assert plain.json() == {"enabled": False, "template_ids": {}}
        plain_record = await client.post(
            "/v1/guardian/push-subscriptions",
            headers=plain_headers,
            json={"template_id": TEMPLATE, "result": "reject"},
        )
        assert plain_record.status_code == 403


@pytest.mark.asyncio
async def test_lifespan_starts_the_push_worker_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dev_database_url: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    # Archive-family data is PostgreSQL-only: live startup on a local-dev database.
    monkeypatch.setenv("MEMORIA_ARCHIVE_DATABASE_URL", dev_database_url)
    # Live startup requires a guardian DSN; the store never connects here and
    # has no pending alert, so the worker's batches claim nothing.
    monkeypatch.setenv(
        "MEMORIA_GUARDIAN_DATABASE_URL", "postgresql://guardian:unused@127.0.0.1:1/guardian"
    )

    async def noop(_store: object) -> None:
        return None

    async def no_pending(_store: object, **_kwargs: object) -> tuple[PendingCrisisPush, ...]:
        return ()

    monkeypatch.setattr(PostgresGuardianStore, "initialize", noop)
    monkeypatch.setattr(PostgresGuardianStore, "close", noop)
    monkeypatch.setattr(PostgresGuardianStore, "claim_crisis_pushes", no_pending)
    monkeypatch.setattr(SubjectActivityStore, "initialize", noop)
    monkeypatch.setattr(SubjectActivityStore, "close", noop)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.crisis_push_worker is None

    _enable_push(monkeypatch)
    enabled_app = create_app()
    async with enabled_app.router.lifespan_context(enabled_app):
        worker = enabled_app.state.crisis_push_worker
        assert worker is not None
        assert worker._task is not None  # started; no pending alert, so no send
    assert worker._task is None
