"""Unseen deterministic task summaries through Control and real PG child layers.

These fixtures test persisted task continuity and permissions, not model wording.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from services.companionship.evaluation import (
    CompanionshipCase,
    OfflineControlAdapter,
    _grant_retention,
    _owner_headers,
)
from services.memory_scope.tests.test_child_layers_postgres import _close_layers, _layers
from services.memory_scope.tests.test_postgres_store import pg_env  # noqa: F401

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("MEMORIA_TEST_POSTGRES_DSN"), reason="requires isolated local PG"
    ),
]

UNSEEN = (
    "孩子因分数题受挫，约定下次先画图，再练习两道同类题。",
    "孩子练习拼读时感到困难，未完计划是下次先复习三个音节。",
)


@pytest.mark.parametrize("age", ["under_14", "14_17"])
@pytest.mark.parametrize("retention", [True, False], ids=["allow", "deny"])
@pytest.mark.parametrize("summary", UNSEEN, ids=["fractions", "phonics"])
async def test_unseen_task_summary_survives_next_session_only_with_retention(
    pg_env,  # noqa: F811
    monkeypatch,
    tmp_path,
    age,
    retention,
    summary,  # noqa: F811
):
    saved = dict(os.environ)
    layers, bootstrap, dsn = await _layers(pg_env)
    adapter = OfflineControlAdapter()
    harness = await adapter._build(
        tmp_path,
        case=CompanionshipCase(
            case_id=f"r2-{age}-{retention}",
            scenario="review_scope",
            category=age,
            expect_service_mode="student_minor",
            expect_subject_category="minor",
            probes=("profile_fence", "review_allow"),
        ),
    )
    harness.app.state.child_layer_store = layers
    from services.control_api.tests.multi_subject_test_support import _establish_relationship

    await _establish_relationship(
        harness.app.state.identity_service,
        relation_type="guardian_of",
        source=harness.owner["user_id"],
        target=harness.child_id,
        now=datetime.now(UTC),
    )
    client = harness.client
    headers = _owner_headers(harness)
    url = f"/v1/guardian/minors/{harness.child_id}/session-summaries"
    try:
        first = await adapter._confirm_subject(harness)
        assert first.envelope["service_mode"] == "student_minor"
        if retention:
            failures, consent_id = await _grant_retention(harness)
            assert failures == []
        else:
            # The binding itself grants retention, so explicitly revoke it.
            consent = await harness.app.state.guardian_store.active_consent(
                minor_user_id=harness.child_id, consent_kind="memory_retention"
            )
            if consent is not None:
                revoked = await client.delete(
                    f"/v1/guardian/minors/{harness.child_id}/consents/{consent.consent_id}",
                    headers={**headers, "Idempotency-Key": "r2-deny-retention"},
                )
                assert revoked.status_code == 200, revoked.text
        ingest = await client.post(
            "/v1/internal/child-memory/session-recap",
            headers={"X-Memoria-Internal-Token": "companionship-eval-archive-token"},
            json={
                "subject_id": harness.child_id,
                "resource_owner_id": harness.owner["user_id"],
                "session_id": first.session_id,
                "source_event_id": "r2-unseen-summary",
                "summary_text": summary,
                "archive_facts": [],
            },
        )
        assert ingest.status_code == (200 if retention else 403), ingest.text
        second = await adapter._confirm_subject(harness)
        assert second.session_id != first.session_id
        assert second.envelope["active_subject_id"] == first.envelope["active_subject_id"]
        tutor = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": second.envelope["runtime_profile_id"],
                "capability": "tutor",
                "data_classification": "ephemeral",
            },
        )
        assert tutor.status_code == 403, tutor.text
        assert tutor.json()["detail"]["code"] == "policy_actor_mismatch"
        reviewed = await client.get(url, headers=headers)
        assert reviewed.status_code == (200 if retention else 403), reviewed.text
        if retention:
            assert [row["text"] for row in reviewed.json()["items"]] == [summary]
            assert reviewed.json()["items"][0]["session_id"] == first.session_id
            own = await layers.list(
                subject_id=harness.child_id,
                kind="dialogue_summary",
                viewer_subject_id=harness.child_id,
            )
            assert [item.text for item in own] == [summary]
            other_subject = await layers.list(
                subject_id=harness.child_id,
                kind="dialogue_summary",
                viewer_subject_id="r2-other-child",
            )
            assert other_subject == []
            denied_quote = await client.post(
                url,
                headers=headers,
                json={
                    "text": '孩子说"我数学太差了"',
                    "session_id": second.session_id,
                    "source_event_id": "quoted",
                },
            )
            assert denied_quote.status_code == 422
            stranger = await adapter._register_adult(client, harness.app, username="r2-stranger")
            other = await client.get(
                url, headers={"Authorization": f"Bearer {stranger['access_token']}"}
            )
            assert other.status_code in (403, 404)
            assert summary not in other.text
            revoked = await client.delete(
                f"/v1/guardian/minors/{harness.child_id}/consents/{consent_id}",
                headers={**headers, "Idempotency-Key": "r2-revoke"},
            )
            assert revoked.status_code == 200, revoked.text
            closed = await client.get(url, headers=headers)
            assert closed.status_code == 403, closed.text
            assert summary not in closed.text
        else:
            stored = await layers.list(
                subject_id=harness.child_id,
                kind="dialogue_summary",
                viewer_subject_id=harness.child_id,
            )
            assert stored == []
    finally:
        await client.__aexit__(None, None, None)
        await _close_layers(layers, bootstrap, dsn)
        os.environ.clear()
        os.environ.update(saved)
