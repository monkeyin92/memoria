"""The agent's side of a minor's daily chat budget: the ledger client and the close report."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from services.agent.src.daily_chat import (
    DailyChatAccrual,
    DailyChatClient,
    DailyChatClientConfig,
    GovernedDailyChat,
    build_daily_chat_client,
    chat_usage_endpoint,
)
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.runtime_profile import parse_runtime_profile
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    open_bridge_connection,
)
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    canonical_wire_payload,
)
from services.common.daily_chat_limit import DailyChatBudget

_ENDPOINT = "http://control-api:8000/v1/internal/chat-usage"
_GOVERNED = GovernedDailyChat(subject_id="person/child 1", budget=DailyChatBudget(30))
_ACCRUAL = DailyChatAccrual(subject_id="person-child", accrual_id="a" * 32)

type Answer = Callable[[httpx.Request], httpx.Response]


def _ledger(answer: Answer, seen: list[httpx.Request]) -> DailyChatClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    return DailyChatClient(
        DailyChatClientConfig(
            endpoint=_ENDPOINT,
            internal_token="policy-token",
            read_timeout_s=0.4,
            retry_delay_s=0.0,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


def _used(seconds: object) -> Answer:
    return lambda _request: httpx.Response(200, json={"used_seconds": seconds})


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("control unreachable", request=request)


def _time_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("control too slow", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("used_seconds", [0, 1799])
async def test_a_wake_is_admitted_while_todays_use_is_below_the_budget(used_seconds: int) -> None:
    seen: list[httpx.Request] = []

    outcome = await _ledger(_used(used_seconds), seen).admit(_GOVERNED)

    assert isinstance(outcome, DailyChatAccrual)
    assert outcome.subject_id == "person/child 1"
    assert len(outcome.accrual_id) == 32 and set(outcome.accrual_id) <= set("0123456789abcdef")
    assert [(request.method, str(request.url)) for request in seen] == [
        ("GET", "http://control-api:8000/v1/internal/chat-usage/person%2Fchild%201/today")
    ]
    assert seen[0].headers["X-Memoria-Internal-Token"] == "policy-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("used_seconds", [1800, 2400])
async def test_a_wake_is_refused_once_todays_use_reaches_the_budget(used_seconds: int) -> None:
    assert await _ledger(_used(used_seconds), []).admit(_GOVERNED) == "exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        lambda _request: httpx.Response(503),
        lambda _request: httpx.Response(401, json={"detail": "token"}),
        lambda _request: httpx.Response(200, text="not json"),
        lambda _request: httpx.Response(200, json={}),
        _used(-1),
        _used("600"),
        _used(True),
        _used(1.5),
        _refuse_connection,
        _time_out,
    ],
    ids=[
        "unavailable",
        "unauthorized",
        "not-json",
        "no-total",
        "negative",
        "string",
        "bool",
        "fraction",
        "connection-refused",
        "timeout",
    ],
)
async def test_admission_is_unavailable_when_control_cannot_say(answer: Answer) -> None:
    assert await _ledger(answer, []).admit(_GOVERNED) == "unavailable"


@pytest.mark.asyncio
async def test_a_closed_session_is_reported_and_retried_once_after_a_failure() -> None:
    seen: list[httpx.Request] = []
    answers = iter([httpx.Response(503), httpx.Response(200, json={"status": "recorded"})])

    await _ledger(lambda _request: next(answers), seen).record(_ACCRUAL, elapsed_seconds=412)

    assert [(request.method, str(request.url)) for request in seen] == [
        ("POST", _ENDPOINT),
        ("POST", _ENDPOINT),
    ]
    assert [json.loads(request.content) for request in seen] == [
        {"accrual_id": "a" * 32, "subject_id": "person-child", "elapsed_seconds": 412}
    ] * 2
    assert {request.headers["X-Memoria-Internal-Token"] for request in seen} == {"policy-token"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        _refuse_connection,
        lambda _request: httpx.Response(409, json={"detail": {"code": "chat_usage_conflict"}}),
        lambda _request: httpx.Response(200, json={"status": "ignored"}),
    ],
    ids=["connection-refused", "conflict", "unexpected-body"],
)
async def test_a_report_that_keeps_failing_is_logged_without_the_child_and_never_raises(
    answer: Answer, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[httpx.Request] = []

    with caplog.at_level(logging.WARNING, logger="services.agent.src.daily_chat"):
        await _ledger(answer, seen).record(_ACCRUAL, elapsed_seconds=412)

    assert len(seen) == 2
    assert f"daily chat usage report lost accrual={'a' * 32}" in caplog.text
    assert "person-child" not in caplog.text


@pytest.mark.asyncio
async def test_a_report_after_shutdown_closed_the_connection_pool_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_used(0)))
    ledger = DailyChatClient(
        DailyChatClientConfig(
            endpoint=_ENDPOINT, internal_token="policy-token", read_timeout_s=0.4, retry_delay_s=0.0
        ),
        client=http,
    )
    await http.aclose()

    with caplog.at_level(logging.WARNING, logger="services.agent.src.daily_chat"):
        await ledger.record(_ACCRUAL, elapsed_seconds=412)

    assert f"daily chat usage report lost accrual={'a' * 32} error=RuntimeError" in caplog.text


@pytest.mark.parametrize(
    ("policy_url", "ledger_url"),
    [
        (
            "http://control-api:8000/v1/interaction/session-policy",
            "http://control-api:8000/v1/internal/chat-usage",
        ),
        (
            "https://control.example.cn/v1/interaction/session-policy",
            "https://control.example.cn/v1/internal/chat-usage",
        ),
    ],
)
def test_the_ledger_is_on_the_policy_endpoints_host(policy_url: str, ledger_url: str) -> None:
    assert chat_usage_endpoint(policy_url) == ledger_url


def _settings(token: str) -> SimpleNamespace:
    return SimpleNamespace(
        internal_token=lambda capability: token if capability == "interaction_policy" else "",
        interaction_policy_url="http://control-api:8000/v1/interaction/session-policy",
        interaction_policy_timeout_s=0.4,
    )


async def _close_minor_session(accrual: DailyChatAccrual | None) -> tuple[list[Any], list[int]]:
    """Close a minor's session after it ran 412.2 seconds. Returns the reports and,
    for each recap, how many reports Control had already received."""
    identity = SessionIdentity("daily-chat-close", account_id="parent")
    bridge = MediaBridgeGrpcServer()
    open_bridge_connection(bridge, identity)
    reports: list[Any] = []
    recaps: list[int] = []

    def ledger(request: httpx.Request) -> httpx.Response:
        reports.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "recorded"})

    minor = parse_runtime_profile(
        canonical_wire_payload(session_id=identity.session_id, session_epoch=1),
        verify_key=TEST_VERIFY_KEY,
    )
    assert minor is not None
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    runtime.set_mode_policy(ModePolicy.from_runtime_profile(minor))
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
        runtime_factory=lambda _session_id: runtime,
        session_recap_publisher=lambda **_recap: recaps.append(len(reports)),
        daily_chat_client=_ledger(ledger, []),
    )
    context = await registry.open_session(identity)
    context.daily_chat_accrual = accrual
    context.started_at -= 412.2

    await registry.finalize_session(identity.session_id)
    await registry.finalize_session(identity.session_id)
    return reports, recaps


@pytest.mark.asyncio
async def test_closing_an_admitted_minor_session_reports_its_time_once_before_the_recap() -> None:
    reports, recaps = await _close_minor_session(_ACCRUAL)

    assert reports == [
        {"accrual_id": "a" * 32, "subject_id": "person-child", "elapsed_seconds": 413}
    ]
    assert recaps == [1]


@pytest.mark.asyncio
async def test_closing_a_session_that_was_never_admitted_reports_nothing() -> None:
    reports, recaps = await _close_minor_session(None)

    assert (reports, recaps) == ([], [0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_quiet_hours", "gates"),
    [(True, ["quiet_hours"]), (False, ["quiet_hours", "daily_limit"])],
)
async def test_a_wake_in_quiet_hours_never_asks_the_ledger_and_a_refusal_skips_the_greeting(
    monkeypatch: pytest.MonkeyPatch, in_quiet_hours: bool, gates: list[str]
) -> None:
    seen: list[str] = []

    async def quiet_hours(_registry: Any, _context: Any) -> bool:
        seen.append("quiet_hours")
        return in_quiet_hours

    async def daily_limit(_registry: Any, _context: Any) -> bool:
        seen.append("daily_limit")
        return True

    async def greet(_registry: Any, _context: Any) -> None:
        seen.append("greeting")

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_refuse_minor_quiet_hours", quiet_hours)
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_refuse_minor_daily_limit", daily_limit)
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", greet)
    identity = SessionIdentity("daily-chat-gates")
    bridge = MediaBridgeGrpcServer()
    open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: FakeMediaProvider()
    )

    await registry.open_session(identity)
    await registry.finalize_session(identity.session_id)

    assert seen == gates


@pytest.mark.asyncio
async def test_no_ledger_client_is_built_without_the_interaction_policy_token() -> None:
    built = build_daily_chat_client(_settings("policy-token"))

    assert build_daily_chat_client(_settings("")) is None
    assert isinstance(built, DailyChatClient)
    await built.aclose()
