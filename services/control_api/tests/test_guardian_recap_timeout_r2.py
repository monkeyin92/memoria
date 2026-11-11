"""Real recap read gate and parser with a timed-out model HTTP boundary."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.archive.domain import EvidenceEvent
from services.control_api.app import guardian_recap
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import _bind, _configure, _owner


@pytest.mark.asyncio
@pytest.mark.parametrize("age", ["under_14", "14_17"])
@pytest.mark.parametrize("retention", [True, False], ids=["allow", "deny"])
async def test_real_recap_timeout_and_empty_day_fail_closed(monkeypatch, tmp_path, age, retention):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    calls = []
    real_client = httpx.AsyncClient

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic recap deadline", request=request)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner, headers = await _owner(client, app, "r2-recap-owner")
        bound = await _bind(
            client,
            app,
            owner_id=owner,
            headers=headers,
            device_id="r2-recap",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band=age,
            offers=["offer_minor_voice_session_v1"],
        )
        assert bound.status_code == 201, bound.text
        child = bound.json()["primary_subject_ids"][0]
        words = "我刚才练习长除法遇到了困难想先休息一下"
        now = datetime.now(UTC)
        # A historical consented row is an allowed recap source. New minor turns
        # carry subject_category and withhold text at ingestion.
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="r2-recap-legacy",
                account_id=owner,
                subject_id=child,
                event_type="speech.utterance_finalized",
                occurred_at=now,
                speaker_class="owner",
                source="r2-synthetic-historical",
                payload={
                    "text": words,
                    "history_eligible": True,
                    "owner_projection_eligible": True,
                    "memory_retention": "retained",
                },
            )
        )
        if retention:
            grant = await client.post(
                f"/v1/guardian/minors/{child}/consents",
                headers={**headers, "Idempotency-Key": "r2-recap-consent"},
                json={"consent_kind": "memory_retention", "policy_version": "minor-retention-v1"},
            )
            assert grant.status_code == 201, grant.text
        settings = app.state.settings
        settings.llm_provider = "qwen"
        settings.dashscope_api_key = SecretStr("synthetic-key")
        settings.dashscope_base_url = "https://recap.invalid/v1"
        monkeypatch.setattr(
            guardian_recap.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(timeout), **kwargs),
        )
        day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        url = f"/v1/guardian/minors/{child}/days"
        recap = await client.post(f"{url}/{day}/recap", headers=headers)
        assert recap.status_code == (200 if retention else 403), recap.text
        assert words not in recap.text
        if retention:
            assert recap.json()["source"] == "fallback"
            assert recap.json()["message_count"] == 1
            assert recap.json()["summary"]["highlights"] == []
            assert len(calls) == 1
        else:
            assert calls == []
        empty = await client.post(f"{url}/2020-01-01/recap", headers=headers)
        assert empty.status_code == (404 if retention else 403), empty.text
        assert words not in empty.text
        assert len(calls) == (1 if retention else 0)
