"""P0-04 D3: a minor's signed quiet hours and session limit are enforced."""

from __future__ import annotations

import json
import logging
import re
import time as monotonic_clock
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyObligation,
    PolicyObligationSpec,
)
from services.agent.src.daily_chat import (
    DailyChatClient,
    DailyChatClientConfig,
    GovernedDailyChat,
    governed_daily_chat,
)
from services.agent.src.minor_limit_bypass import (
    MINOR_DAILY_LIMIT_BYPASSED_REASON,
    MINOR_LIMIT_BYPASS_ENV,
    MINOR_SESSION_LIMIT_BYPASSED_REASON,
    MinorLimitBypass,
    subject_id_hash,
)
from services.agent.src.prompts import (
    CONNECTION_RETRY_PHRASE,
    MINOR_DAILY_LIMIT_PHRASE,
    MINOR_QUIET_HOURS_PHRASE,
    MINOR_SESSION_LIMIT_PHRASE,
    is_allowlisted_device_phrase,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile, parse_runtime_profile
from services.agent.src.voice_core import media_session_projection
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.media_session_connection import MediaSessionConnectionMixin
from services.agent.src.voice_core.media_session_output_stream import MediaOutputStreamMixin
from services.agent.src.voice_core.media_session_projection import MediaSessionProjectionMixin
from services.agent.src.voice_core.minor_session_limits import in_quiet_hours, minor_limits
from services.agent.tests.unit.media_session_support import FakeMediaProvider
from services.common.daily_chat_limit import DailyChatBudget
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.session_runtime.profile_service import (
    RuntimeProfile,
    runtime_profile_wire_payload,
    sign_runtime_profile_payload,
)

_KEY = "minor-session-limits-key"
_LEDGER = "http://control-api:8000/v1/internal/chat-usage"


def _obligation(code: PolicyObligation, **params: Any) -> PolicyObligationSpec:
    values: dict[str, Any] = {
        "max_session_seconds": None,
        "retention_ttl_seconds": None,
        "quiet_hours": None,
        "extras": (),
    }
    values.update(params)
    return PolicyObligationSpec(code=code, params=ObligationParams(**values))


def _signed_payload(
    category: str = "minor",
    *,
    daily_chat_seconds: str | None = None,
    max_session_seconds: int = 1800,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    minor = category == "minor"
    daily = () if daily_chat_seconds is None else (("daily_chat_seconds", daily_chat_seconds),)
    issued = RuntimeProfile(
        runtime_profile_id="rp-limits",
        device_id="dev-limits",
        session_id="ses-limits",
        actor_id="person-owner",
        binding_id="bind-limits",
        binding_version=1,
        active_subject_id="person-child" if minor else "person-owner",
        subject_revision=1,
        subject_category=category,  # type: ignore[arg-type]
        age_band="under_14" if minor else "adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        service_mode="student_minor" if minor else "adult_companion",
        persona_assignment_id="starlight:v1",
        persona_id="starlight",
        persona_version=1,
        relationship_stage="new",
        policy_bundle_version="policy-v2",
        capabilities=("chat",),
        obligations=(
            _obligation(
                PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
                max_session_seconds=max_session_seconds,
                extras=daily,
            ),
            _obligation(PolicyObligation.POLICY_OBLIGATION_QUIET_HOURS, quiet_hours=("21:30", "06:30")),
        ),
        policy_receipt_ids=("receipt-1",),
        session_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        signature="",
    )
    payload = runtime_profile_wire_payload(issued)
    payload["signature"] = sign_runtime_profile_payload(payload, signing_key=_KEY.encode())
    return payload


def _profile(
    category: str = "minor",
    *,
    daily_chat_seconds: str | None = None,
    max_session_seconds: int = 1800,
) -> VerifiedRuntimeProfile:
    payload = _signed_payload(
        category,
        daily_chat_seconds=daily_chat_seconds,
        max_session_seconds=max_session_seconds,
    )
    verified = parse_runtime_profile(payload, verify_key=_KEY)
    assert verified is not None
    return verified


@pytest.mark.parametrize(
    ("clock", "inside"),
    [
        (time(21, 29), False),
        (time(21, 30), True),
        (time(23, 59), True),
        (time(0, 0), True),
        (time(6, 29), True),
        (time(6, 30), False),
        (time(12, 0), False),
    ],
)
def test_quiet_hours_wrap_past_midnight(clock: time, inside: bool) -> None:
    assert in_quiet_hours(clock, ("21:30", "06:30")) is inside


def test_same_day_and_empty_windows() -> None:
    assert in_quiet_hours(time(13, 0), ("12:00", "14:00"))
    assert not in_quiet_hours(time(14, 0), ("12:00", "14:00"))
    assert not in_quiet_hours(time(10, 0), ("10:00", "10:00"))


def test_signed_limits_reach_the_agent_and_apply_to_minors_only() -> None:
    assert minor_limits(_profile()) == (1800, ("21:30", "06:30"))
    assert minor_limits(_profile("adult")) == (None, None)
    assert minor_limits(None) == (None, None)
    assert is_allowlisted_device_phrase(MINOR_QUIET_HOURS_PHRASE)
    assert is_allowlisted_device_phrase(MINOR_SESSION_LIMIT_PHRASE)


def test_the_signed_daily_budget_reaches_the_runtime_profile() -> None:
    assert _profile(daily_chat_seconds="2700").profile.daily_chat_budget == DailyChatBudget(45)
    assert _profile().profile.daily_chat_budget is None


def test_only_a_minors_signed_daily_budget_is_governed() -> None:
    assert governed_daily_chat(_profile(daily_chat_seconds="2700")) == GovernedDailyChat(
        subject_id="person-child", budget=DailyChatBudget(45)
    )
    assert governed_daily_chat(_profile()) is None
    assert governed_daily_chat(_profile("adult", daily_chat_seconds="2700")) is None
    assert governed_daily_chat(None) is None
    assert is_allowlisted_device_phrase(MINOR_DAILY_LIMIT_PHRASE)
    assert is_allowlisted_device_phrase(CONNECTION_RETRY_PHRASE)


@pytest.mark.parametrize("seconds", ["1200", "2000", "45m"])
def test_a_malformed_signed_daily_budget_rejects_the_whole_profile(seconds: str) -> None:
    payload = _signed_payload(daily_chat_seconds=seconds)

    assert parse_runtime_profile(payload, verify_key=_KEY) is None


class _Session(MediaSessionProjectionMixin, MediaOutputStreamMixin):
    def __init__(
        self,
        daily_chat_client: DailyChatClient | None = None,
        *,
        minor_limit_bypass: MinorLimitBypass | None = None,
    ) -> None:
        self.daily_chat_client = daily_chat_client
        # This fixture exercises the refusal copy, not the AIGC round markers.
        self.aigc_notice_config = AigcNoticeConfig(enabled=False)
        self.minor_limit_bypass = (
            minor_limit_bypass if minor_limit_bypass is not None else MinorLimitBypass()
        )
        self.spoken: list[str] = []
        self.standby: list[str] = []

    async def _speak_aigc_opening_marker(self, context: Any) -> None:
        return None

    async def _wait_aigc_opening_lead_in(self, context: Any) -> None:
        return None

    async def _play_aigc_closing_marker(self, context: Any) -> bool:
        return False

    def _finish_aigc_round(self, context: Any, *, close_reason: str) -> None:
        return None

    async def _speak_device_enrollment_phrase(self, context: Any, phrase: str) -> bool:
        self.spoken.append(phrase)
        return True

    async def _request_device_standby(self, context: Any, *, reason: str) -> None:
        self.standby.append(reason)
        context.standby_requested = True


def _context(
    profile: VerifiedRuntimeProfile,
    *,
    started_ago: float = 0.0,
    reply: str = "",
    client_type: str = "device",
) -> Any:
    runtime = SimpleNamespace(
        mode_policy=SimpleNamespace(runtime_profile=profile),
        fence=SimpleNamespace(matches=lambda _fence: True),
        fixed_safety_heard=False,
        fixed_safety_echo_text="",
    )

    def note_fixed_safety_spoken(text: str) -> None:
        runtime.fixed_safety_heard = True
        runtime.fixed_safety_echo_text = text

    runtime.note_fixed_safety_spoken = note_fixed_safety_spoken
    return SimpleNamespace(
        identity=SimpleNamespace(client_type=client_type, session_id="ses-limits"),
        runtime=runtime,
        device_wake_ack_pending=True,
        closed=False,
        standby_requested=False,
        crisis_reply_heard=False,
        output=SimpleNamespace(assistant_text=reply),
        started_at=monotonic_clock.monotonic() - started_ago,
        daily_chat_accrual=None,
    )


def _ledger(used: httpx.Response, asked: list[str]) -> DailyChatClient:
    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return used

    return DailyChatClient(
        DailyChatClientConfig(endpoint=_LEDGER, internal_token="policy-token", read_timeout_s=0.4),
        client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )


def _bypass(*subjects: str) -> MinorLimitBypass:
    return MinorLimitBypass.from_env({MINOR_LIMIT_BYPASS_ENV: ",".join(subjects)})


def _chat_ledger(
    used_seconds: int, posts: list[dict[str, Any]], asked: list[str]
) -> DailyChatClient:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            asked.append(str(request.url))
            return httpx.Response(200, json={"used_seconds": used_seconds})
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "recorded"})

    return DailyChatClient(
        DailyChatClientConfig(
            endpoint=_LEDGER,
            internal_token="policy-token",
            read_timeout_s=0.4,
            retry_delay_s=0.0,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("hour", "refused"), [(22, True), (15, False)])
async def test_a_minor_wake_in_quiet_hours_gets_a_fixed_goodnight(
    monkeypatch: pytest.MonkeyPatch, hour: int, refused: bool
) -> None:
    monkeypatch.setattr(
        media_session_projection,
        "current_local_time",
        lambda _tz: datetime(2026, 9, 26, hour, 0),
    )
    session = _Session()

    result = await session._refuse_minor_quiet_hours(_context(_profile()))

    assert result is refused
    assert session.spoken == ([MINOR_QUIET_HOURS_PHRASE] if refused else [])
    assert session.standby == (["minor_quiet_hours"] if refused else [])


@pytest.mark.asyncio
async def test_an_adult_wake_at_night_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media_session_projection, "current_local_time", lambda _tz: datetime(2026, 9, 26, 23, 0)
    )
    session = _Session()

    assert await session._refuse_minor_quiet_hours(_context(_profile("adult"))) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started_ago", "reply", "ends"),
    [
        (60.0, "好的。", False),  # inside the limit
        (1900.0, "好的。", True),  # past the limit: goodbye after the reply
        (1900.0, CRISIS_SELF_SCRIPT.display, False),
    ],
)
async def test_a_minor_session_past_its_limit_ends_after_the_reply(
    started_ago: float, reply: str, ends: bool
) -> None:
    session = _Session()
    context = _context(_profile(), started_ago=started_ago, reply=reply)

    await session._maybe_standby_after_session_limit(context, fence=object())  # type: ignore[arg-type]

    assert session.spoken == ([MINOR_SESSION_LIMIT_PHRASE] if ends else [])
    assert session.standby == (["minor_max_session"] if ends else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_seconds", [2700, 3600, 300])
async def test_an_out_of_range_stored_cap_is_issued_as_15_minutes_and_stops_the_session(
    stored_seconds: int,
) -> None:
    from services.policy.tests.fakes import FakeConsentParams
    from services.policy.tests.test_engine import _minor_session

    stored = FakeConsentParams(
        max_session_seconds=stored_seconds, quiet_hours=("21:30", "06:30")
    )
    decision = _minor_session("chat", stored)
    issued = next(
        item.params.max_session_seconds
        for item in decision.obligations
        if item.code.value == "MAX_SESSION_SECONDS"
    )

    assert stored.max_session_seconds == stored_seconds
    assert issued == 900
    profile = _profile(max_session_seconds=issued)
    session = _Session()
    await session._maybe_standby_after_session_limit(
        _context(profile, started_ago=900.0, reply="好的。"),
        fence=object(),  # type: ignore[arg-type]
    )
    assert session.spoken == [MINOR_SESSION_LIMIT_PHRASE]
    assert session.standby == ["minor_max_session"]

    earlier = _Session()
    await earlier._maybe_standby_after_session_limit(
        _context(profile, started_ago=899.0, reply="好的。"),
        fence=object(),  # type: ignore[arg-type]
    )
    assert earlier.spoken == []
    assert earlier.standby == []


@pytest.mark.asyncio
async def test_a_minor_wake_inside_the_daily_budget_is_admitted_and_kept_for_close() -> None:
    asked: list[str] = []
    session = _Session(_ledger(httpx.Response(200, json={"used_seconds": 2699}), asked))
    context = _context(_profile(daily_chat_seconds="2700"))

    assert await session._refuse_minor_daily_limit(context) is False
    assert (session.spoken, session.standby) == ([], [])
    assert asked == [f"{_LEDGER}/person-child/today"]
    assert context.daily_chat_accrual.subject_id == "person-child"
    assert context.device_wake_ack_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ledger", "phrase", "reason"),
    [
        (httpx.Response(200, json={"used_seconds": 2700}), MINOR_DAILY_LIMIT_PHRASE, "minor_daily_limit"),
        (httpx.Response(503), CONNECTION_RETRY_PHRASE, "minor_daily_usage_unavailable"),
        (None, CONNECTION_RETRY_PHRASE, "minor_daily_usage_unavailable"),
    ],
    ids=["spent", "ledger-down", "no-ledger-client"],
)
async def test_a_minor_wake_is_refused_when_today_is_spent_or_unknown(
    ledger: httpx.Response | None, phrase: str, reason: str
) -> None:
    session = _Session(None if ledger is None else _ledger(ledger, []))
    context = _context(_profile(daily_chat_seconds="2700"))

    assert await session._refuse_minor_daily_limit(context) is True
    assert session.spoken == [phrase]
    assert session.standby == [reason]
    assert context.daily_chat_accrual is None
    assert context.device_wake_ack_pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "daily_chat_seconds", "client_type"),
    [("adult", "2700", "device"), ("minor", None, "device"), ("minor", "2700", "app")],
    ids=["adult", "no-signed-budget", "not-the-device"],
)
async def test_only_a_minors_device_wake_with_a_signed_budget_asks_the_ledger(
    category: str, daily_chat_seconds: str | None, client_type: str
) -> None:
    asked: list[str] = []
    session = _Session(_ledger(httpx.Response(200, json={"used_seconds": 86_400}), asked))
    profile = _profile(category, daily_chat_seconds=daily_chat_seconds)

    assert await session._refuse_minor_daily_limit(_context(profile, client_type=client_type)) is False
    assert (asked, session.spoken, session.standby) == ([], [], [])


def test_the_bypass_is_off_unless_exactly_one_subject_is_configured() -> None:
    assert not MinorLimitBypass.from_env({}).enabled
    assert not MinorLimitBypass.from_env({MINOR_LIMIT_BYPASS_ENV: "  "}).enabled
    single = MinorLimitBypass.from_env({MINOR_LIMIT_BYPASS_ENV: " person-child "})
    duplicate = MinorLimitBypass.from_env({MINOR_LIMIT_BYPASS_ENV: "person-child, person-child"})

    assert single.subject_ids == frozenset({"person-child"})
    assert single.enabled
    assert duplicate.subject_ids == frozenset({"person-child"})
    assert duplicate.enabled


def test_more_than_one_configured_subject_disables_the_bypass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        bypass = _bypass("person-child", "person-other")

    assert not bypass.enabled
    assert bypass.subject_ids == frozenset()
    assert "reason=multiple_test_subjects" in caplog.text
    assert "person-child" not in caplog.text
    assert "person-other" not in caplog.text


def test_an_active_whitelist_logs_one_startup_warning_with_only_hashes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        _bypass("person-child")

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "minor limit bypass active" in message
    assert re.search(r"at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", message)
    assert f"subject_sha256_8={subject_id_hash('person-child')}" in message
    assert "person-child" not in message


@pytest.mark.parametrize(
    "configured",
    ["", "   ", " , "],
    ids=["empty", "whitespace-only", "separator-only"],
)
def test_a_blank_whitelist_logs_no_startup_warning(
    configured: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        bypass = MinorLimitBypass.from_env({MINOR_LIMIT_BYPASS_ENV: configured})

    assert not bypass.enabled
    assert caplog.records == []


def test_the_media_registry_reads_the_whitelist_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MINOR_LIMIT_BYPASS_ENV, "person-child")

    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: FakeMediaProvider(),
    )

    assert registry.minor_limit_bypass.subject_ids == frozenset({"person-child"})


@pytest.mark.asyncio
async def test_a_whitelisted_minor_keeps_chatting_past_the_daily_budget_and_still_records_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asked: list[str] = []
    posts: list[dict[str, Any]] = []
    session = _Session(
        _chat_ledger(2700, posts, asked),
        minor_limit_bypass=_bypass("person-child"),
    )
    context = _context(_profile(daily_chat_seconds="2700"))

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        assert await session._refuse_minor_daily_limit(context) is False

    assert (session.spoken, session.standby) == ([], [])
    assert asked == [f"{_LEDGER}/person-child/today"]
    assert context.device_wake_ack_pending is True
    accrual = context.daily_chat_accrual
    assert accrual is not None
    assert accrual.subject_id == "person-child"
    context.started_at -= 412.2

    await MediaSessionConnectionMixin._record_daily_chat_usage(session, context)

    assert len(posts) == 1
    assert posts[0]["accrual_id"] == accrual.accrual_id
    assert posts[0]["subject_id"] == "person-child"
    assert posts[0]["elapsed_seconds"] == 413
    assert f"reason={MINOR_DAILY_LIMIT_BYPASSED_REASON}" in caplog.text
    assert "at=" in caplog.text
    assert f"subject_sha256_8={subject_id_hash('person-child')}" in caplog.text
    assert "person-child" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bypass",
    [None, _bypass("person-other"), _bypass("Person-Child")],
    ids=["unset", "other-subject", "case-mismatch"],
)
async def test_the_daily_limit_still_refuses_a_subject_not_exactly_whitelisted(
    bypass: MinorLimitBypass | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session(
        _ledger(httpx.Response(200, json={"used_seconds": 2700}), []),
        minor_limit_bypass=bypass,
    )
    context = _context(_profile(daily_chat_seconds="2700"))

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        assert await session._refuse_minor_daily_limit(context) is True

    assert session.spoken == [MINOR_DAILY_LIMIT_PHRASE]
    assert session.standby == ["minor_daily_limit"]
    assert context.daily_chat_accrual is None
    assert context.device_wake_ack_pending is False
    assert MINOR_DAILY_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
async def test_more_than_one_subject_leaves_the_daily_limit_blocking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        bypass = _bypass("person-child", "person-other")

    session = _Session(
        _ledger(httpx.Response(200, json={"used_seconds": 2700}), []),
        minor_limit_bypass=bypass,
    )
    context = _context(_profile(daily_chat_seconds="2700"))

    assert await session._refuse_minor_daily_limit(context) is True
    assert session.standby == ["minor_daily_limit"]
    assert context.daily_chat_accrual is None
    assert MINOR_DAILY_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
async def test_an_unavailable_ledger_is_never_bypassed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session(
        _ledger(httpx.Response(503), []),
        minor_limit_bypass=_bypass("person-child"),
    )
    context = _context(_profile(daily_chat_seconds="2700"))

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        assert await session._refuse_minor_daily_limit(context) is True

    assert session.spoken == [CONNECTION_RETRY_PHRASE]
    assert session.standby == ["minor_daily_usage_unavailable"]
    assert context.daily_chat_accrual is None
    assert MINOR_DAILY_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
async def test_a_whitelisted_minor_session_past_its_limit_keeps_going_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session(minor_limit_bypass=_bypass("person-child"))
    context = _context(_profile(max_session_seconds=900), started_ago=901.0, reply="好的。")

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        await session._maybe_standby_after_session_limit(context, fence=object())  # type: ignore[arg-type]

    assert (session.spoken, session.standby) == ([], [])
    assert f"reason={MINOR_SESSION_LIMIT_BYPASSED_REASON}" in caplog.text
    assert "at=" in caplog.text
    assert f"subject_sha256_8={subject_id_hash('person-child')}" in caplog.text
    assert "person-child" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bypass",
    [None, _bypass("person-other"), _bypass("Person-Child")],
    ids=["unset", "other-subject", "case-mismatch"],
)
async def test_the_session_limit_still_ends_a_subject_not_exactly_whitelisted(
    bypass: MinorLimitBypass | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session(minor_limit_bypass=bypass)
    context = _context(_profile(max_session_seconds=900), started_ago=901.0, reply="好的。")

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        await session._maybe_standby_after_session_limit(context, fence=object())  # type: ignore[arg-type]

    assert session.spoken == [MINOR_SESSION_LIMIT_PHRASE]
    assert session.standby == ["minor_max_session"]
    assert MINOR_SESSION_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
async def test_more_than_one_subject_leaves_the_session_limit_ending_the_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        bypass = _bypass("person-child", "person-other")
    session = _Session(minor_limit_bypass=bypass)
    context = _context(_profile(max_session_seconds=900), started_ago=901.0, reply="好的。")

    await session._maybe_standby_after_session_limit(context, fence=object())  # type: ignore[arg-type]

    assert session.spoken == [MINOR_SESSION_LIMIT_PHRASE]
    assert session.standby == ["minor_max_session"]
    assert MINOR_SESSION_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured",
    ["", "   ", " , "],
    ids=["empty", "whitespace-only", "separator-only"],
)
async def test_an_empty_or_separator_only_whitelist_still_blocks_the_daily_limit(
    configured: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(MINOR_LIMIT_BYPASS_ENV, configured)
    bypass = MinorLimitBypass.from_env()
    session = _Session(
        _ledger(httpx.Response(200, json={"used_seconds": 2700}), []),
        minor_limit_bypass=bypass,
    )
    context = _context(_profile(daily_chat_seconds="2700"))

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        refused = await session._refuse_minor_daily_limit(context)

    assert bypass.subject_ids == frozenset()
    assert refused is True
    assert session.spoken == [MINOR_DAILY_LIMIT_PHRASE]
    assert session.standby == ["minor_daily_limit"]
    assert context.daily_chat_accrual is None
    assert context.device_wake_ack_pending is False
    assert MINOR_DAILY_LIMIT_BYPASSED_REASON not in caplog.text
    assert MINOR_SESSION_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured",
    ["", "   ", " , "],
    ids=["empty", "whitespace-only", "separator-only"],
)
async def test_an_empty_or_separator_only_whitelist_still_ends_the_session_at_its_limit(
    configured: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(MINOR_LIMIT_BYPASS_ENV, configured)
    bypass = MinorLimitBypass.from_env()
    session = _Session(minor_limit_bypass=bypass)
    context = _context(_profile(max_session_seconds=900), started_ago=901.0, reply="好的。")

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        await session._maybe_standby_after_session_limit(context, fence=object())  # type: ignore[arg-type]

    assert bypass.subject_ids == frozenset()
    assert session.spoken == [MINOR_SESSION_LIMIT_PHRASE]
    assert session.standby == ["minor_max_session"]
    assert MINOR_DAILY_LIMIT_BYPASSED_REASON not in caplog.text
    assert MINOR_SESSION_LIMIT_BYPASSED_REASON not in caplog.text


@pytest.mark.asyncio
async def test_the_bypass_never_opens_quiet_hours(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        media_session_projection,
        "current_local_time",
        lambda _tz: datetime(2026, 9, 26, 22, 0),
    )
    session = _Session(minor_limit_bypass=_bypass("person-child"))

    with caplog.at_level(logging.WARNING, logger="services.agent.src.minor_limit_bypass"):
        refused = await session._refuse_minor_quiet_hours(_context(_profile()))

    assert refused is True
    assert session.spoken == [MINOR_QUIET_HOURS_PHRASE]
    assert session.standby == ["minor_quiet_hours"]
    assert MINOR_SESSION_LIMIT_BYPASSED_REASON not in caplog.text
