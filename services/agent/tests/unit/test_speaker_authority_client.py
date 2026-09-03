from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
from services.agent.src.speaker_authority_client import (
    SpeakerAuthorityClient,
    SpeakerAuthorityClientConfig,
    SpeakerEnrollmentSample,
)


def _guest_payload() -> dict[str, object]:
    return {
        "classification": "guest",
        "score": 0.1,
        "quality_score": 0.92,
        "reason_code": "owner_mismatch",
        "model_version": "campplus-v1",
        "template_version": 3,
        "profile_id": "profile-003",
        "permissions": {
            "normal_conversation": True,
            "read_private_memory": False,
            "write_long_term_memory": False,
            "sensitive_actions": False,
        },
    }


@pytest.mark.asyncio
async def test_client_classifies_session_audio_without_sending_account_id() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["token"] = request.headers.get("X-Memoria-Speaker-Token")
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json={**_guest_payload(), "reject_non_owner_voice": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
                timeout_s=0.4,
            ),
            client=http_client,
        )
        decision = await client.classify(
            session_id="session-001",
            pcm=b"\x00\x01\x02\x03",
            sample_rate=16000,
        )

    assert observed == {
        "token": "speaker-internal-token",
        "body": {
            "audio_base64": base64.b64encode(b"\x00\x01\x02\x03").decode("ascii"),
            "sample_rate": 16000,
            "session_id": "session-001",
        },
    }
    assert decision.classification == "guest"
    assert decision.permissions.normal_conversation is True
    assert decision.permissions.read_private_memory is False
    assert client.reject_non_owner_voice is False


@pytest.mark.asyncio
async def test_client_enrolls_device_samples_without_sending_account_id() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"profile_id": "profile-shadow", "status": "shadow", "sample_count": 3},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        result = await client.enroll(
            session_id="session-001",
            intent_id="intent-001",
            samples=[
                SpeakerEnrollmentSample(pcm=value, sample_rate=16000)
                for value in (b"one-01", b"two-02", b"three-03")
            ],
        )

    assert observed["path"] == "/v1/speakers/enrollments/internal"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["session_id"] == "session-001"
    assert body["intent_id"] == "intent-001"
    assert "account_id" not in body
    assert len(body["samples"]) == 3
    assert result["profile_id"] == "profile-shadow"


@pytest.mark.asyncio
async def test_client_retries_enrollment_once_on_503() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                503,
                request=request,
                json={"detail": "speaker embedding service unavailable"},
            )
        return httpx.Response(
            201,
            request=request,
            json={"profile_id": "profile-shadow", "status": "shadow", "sample_count": 3},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        result = await client.enroll(
            session_id="session-001",
            intent_id="intent-001",
            samples=[
                SpeakerEnrollmentSample(pcm=value, sample_rate=16000)
                for value in (b"one-01", b"two-02", b"three-03")
            ],
        )

    assert calls["count"] == 2
    assert result["profile_id"] == "profile-shadow"


@pytest.mark.asyncio
async def test_client_reads_session_scoped_enrollment_status() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["session_id"] = request.url.params["session_id"]
        return httpx.Response(200, json={"enrollment": {"state": "required"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        status = await client.enrollment_status(session_id="session-001")

    assert observed == {
        "path": "/v1/speakers/status/internal",
        "session_id": "session-001",
    }
    assert status["enrollment"] == {"state": "required"}


@pytest.mark.asyncio
async def test_enrollment_status_uses_enrollment_timeout_not_classify_timeout() -> None:
    observed: dict[str, object] = {}

    class _Recorder:
        async def get(self, url: object, **kwargs: object) -> httpx.Response:
            observed["timeout"] = kwargs.get("timeout")
            observed["path"] = httpx.URL(str(url)).path
            return httpx.Response(
                200,
                request=httpx.Request("GET", str(url)),
                json={"enrollment": {"state": "requested"}},
            )

        async def aclose(self) -> None:
            return None

    client = SpeakerAuthorityClient(
        SpeakerAuthorityClientConfig(
            endpoint="https://control.test/v1/speakers/classify",
            internal_token="speaker-internal-token",
            timeout_s=0.4,
            enrollment_timeout_s=10.0,
        ),
        client=_Recorder(),  # type: ignore[arg-type]
    )
    status = await client.enrollment_status(session_id="session-001")

    assert observed["timeout"] == 10.0
    assert observed["path"] == "/v1/speakers/status/internal"
    assert status["enrollment"] == {"state": "requested"}


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, "false", 0])
async def test_client_defaults_missing_or_unparseable_policy_to_strict(policy: object) -> None:
    payload = _guest_payload()
    if policy is not None:
        payload["reject_non_owner_voice"] = policy
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        await client.classify(
            session_id="session-001",
            pcm=b"\x00\x01",
            sample_rate=16000,
        )

    assert client.reject_non_owner_voice is True


@pytest.mark.asyncio
async def test_client_rejects_malformed_authority_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"reject_non_owner_voice": False},
            )
        )
    ) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        with pytest.raises(ValueError, match="invalid speaker authority response"):
            await client.classify(
                session_id="session-001",
                pcm=b"\x00\x01",
                sample_rate=16000,
            )
        assert client.reject_non_owner_voice is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "minor_forbidden",
        "subject_capability_forbidden",
        "subject_category_unavailable",
    ],
)
async def test_client_maps_stable_policy_denials_to_unprivileged_uncertain_decision(
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="services.agent.src.speaker_authority_client",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                403,
                json={
                    "detail": {
                        "code": code,
                        "capability": "speaker_enrollment",
                    }
                },
            )
        )
    ) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        decision = await client.classify(
            session_id="session-001",
            pcm=b"\x00\x01",
            sample_rate=16000,
        )

    assert decision.classification == "uncertain"
    assert decision.reason_code == "authority_policy_denied"
    assert decision.permissions.normal_conversation is True
    assert decision.permissions.read_private_memory is False
    assert decision.permissions.write_long_term_memory is False
    assert decision.permissions.sensitive_actions is False
    assert client.reject_non_owner_voice is True
    assert f"speaker authority policy denied code={code}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (403, {"detail": {"code": "unknown_policy_denial"}}),
        (503, {"detail": {"code": "service_unavailable"}}),
    ],
)
async def test_client_keeps_unknown_policy_and_server_errors_as_transport_failures(
    status_code: int,
    payload: dict[str, object],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json=payload)
        )
    ) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.classify(
                session_id="session-001",
                pcm=b"\x00\x01",
                sample_rate=16000,
            )

    assert client.reject_non_owner_voice is True


@pytest.mark.asyncio
async def test_client_keeps_network_errors_as_transport_failures() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        with pytest.raises(httpx.ConnectError):
            await client.classify(
                session_id="session-001",
                pcm=b"\x00\x01",
                sample_rate=16000,
            )

    assert client.reject_non_owner_voice is True
