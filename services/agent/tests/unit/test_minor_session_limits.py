"""P0-04 D3: a minor's signed quiet hours and session limit are enforced."""

from __future__ import annotations

import time as monotonic_clock
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyObligation,
    PolicyObligationSpec,
)
from services.agent.src.prompts import (
    MINOR_QUIET_HOURS_PHRASE,
    MINOR_SESSION_LIMIT_PHRASE,
    is_allowlisted_device_phrase,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile, parse_runtime_profile
from services.agent.src.voice_core import media_session_projection
from services.agent.src.voice_core.media_session_output_stream import MediaOutputStreamMixin
from services.agent.src.voice_core.media_session_projection import MediaSessionProjectionMixin
from services.agent.src.voice_core.minor_session_limits import in_quiet_hours, minor_limits
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY
from services.session_runtime.profile_service import (
    RuntimeProfile,
    runtime_profile_wire_payload,
    sign_runtime_profile_payload,
)

_KEY = "minor-session-limits-key"


def _obligation(code: PolicyObligation, **params: Any) -> PolicyObligationSpec:
    values: dict[str, Any] = {
        "max_session_seconds": None,
        "retention_ttl_seconds": None,
        "quiet_hours": None,
        "extras": (),
    }
    values.update(params)
    return PolicyObligationSpec(code=code, params=ObligationParams(**values))


def _profile(category: str = "minor") -> VerifiedRuntimeProfile:
    now = datetime.now(UTC)
    minor = category == "minor"
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
            _obligation(PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS, max_session_seconds=1800),
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


class _Session(MediaSessionProjectionMixin, MediaOutputStreamMixin):
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.standby: list[str] = []

    async def _speak_device_enrollment_phrase(self, context: Any, phrase: str) -> bool:
        self.spoken.append(phrase)
        return True

    async def _request_device_standby(self, context: Any, *, reason: str) -> None:
        self.standby.append(reason)
        context.standby_requested = True


def _context(profile: VerifiedRuntimeProfile, *, started_ago: float = 0.0, reply: str = "") -> Any:
    return SimpleNamespace(
        identity=SimpleNamespace(client_type="device", session_id="ses-limits"),
        runtime=SimpleNamespace(
            mode_policy=SimpleNamespace(runtime_profile=profile),
            fence=SimpleNamespace(matches=lambda _fence: True),
        ),
        device_wake_ack_pending=True,
        closed=False,
        standby_requested=False,
        crisis_reply_heard=False,
        assistant_text=reply,
        started_at=monotonic_clock.monotonic() - started_ago,
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
        (1900.0, CRISIS_SUPPORT_REPLY, False),  # a crisis session is never cut short
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
