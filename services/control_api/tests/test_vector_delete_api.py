"""Vector cleanup reaches the HTTP exits: guardian retract, archive correction, unbind purge."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemorySearchQuery
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import (
    _bind,
    _configure,
    _owner,
    _RecordingConsent,
)
from services.control_api.tests.test_guardian_child_memories import _grant_memory
from services.guardian.child_memory import DialogueSummary
from services.memory_scope.child_layers import ChildLayerStore
from testing import app_store

CHILD_TEXT = "我喜欢蓝色。"


class _Embedder:
    model = "vector-delete-api-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0)


class _CorrectionEmbedder:
    """The replaced wording and the corrected wording land on opposite sides."""

    model = "vector-correction-api-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        if "蓝色" in text:
            return (1.0, 0.0)
        return (0.0, 1.0)


async def _bound_child(
    client: AsyncClient, app: Any, username: str, device_id: str
) -> tuple[str, str, dict[str, str]]:
    app.state.bound_subject_consent = _RecordingConsent()
    owner_id, headers = await _owner(client, app, username)
    created = await _bind(
        client,
        app,
        owner_id=owner_id,
        headers=headers,
        device_id=device_id,
        declared_mode="parent_for_child",
        relationship="guardian_of",
        age_band="under_14",
        offers=["offer_minor_voice_session_v1"],
    )
    assert created.status_code == 201, created.text
    return owner_id, created.json()["primary_subject_ids"][0], headers


async def _seed_child_vector(app: Any, owner_id: str, child_id: str) -> str:
    catalog = app.state.memory_catalog
    catalog._embedder = _Embedder()
    catalog._evidence_subject_category_resolver = lambda _event: "adult"
    event_id = "vector-delete-api-child-turn"
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=owner_id,
            subject_id=child_id,
            session_id="vector-delete-api-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="vector-delete-api-test",
            payload={
                "text": CHILD_TEXT,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    report = await catalog.compile_pending()
    assert report.failed_events == 0, report
    claims = await catalog.review_queue(account_id=owner_id)
    claim = next(item for item in claims if item.source_event_id == event_id)
    return claim.item_id


def _vector_count(owner_id: str, item_id: str) -> int:
    rows = app_store.fetch_all(
        "",
        "SELECT count(*) FROM memory_vector_documents WHERE account_id = ? AND item_id = ?",
        (owner_id, item_id),
    )
    return int(rows[0][0])


async def _seed_owner_claim(app: Any, owner_id: str, event_id: str, text: str) -> str:
    catalog = app.state.memory_catalog
    catalog._embedder = _CorrectionEmbedder()
    catalog._evidence_subject_category_resolver = lambda _event: "adult"
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=owner_id,
            subject_id=owner_id,
            session_id="vector-correction-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="vector-correction-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    report = await catalog.compile_pending()
    assert report.failed_events == 0, report
    queue = await catalog.review_queue(account_id=owner_id, subject_id=owner_id)
    matches = [item for item in queue if item.source_event_id == event_id]
    assert matches, "no claim compiled from the seeded turn"
    return matches[0].item_id


@pytest.mark.asyncio
async def test_guardian_retract_deletes_the_childs_vector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "vector-guardian-owner", "device-vector-guardian"
        )
        await _grant_memory(client, child_id, headers)
        claim_id = await _seed_child_vector(app, owner_id, child_id)
        assert _vector_count(owner_id, claim_id) == 1

        reviewed = await client.post(
            f"/v1/guardian/minors/{child_id}/memories/{claim_id}/review",
            headers=headers,
            json={"action": "retract"},
        )

        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "retracted"
        assert _vector_count(owner_id, claim_id) == 0
        events = app_store.fetch_all(
            "",
            "SELECT event_id FROM archive_evidence_events WHERE account_id = ? AND event_id = ?",
            (owner_id, "vector-delete-api-child-turn"),
        )
        assert events == [("vector-delete-api-child-turn",)]


@pytest.mark.asyncio
async def test_unbind_deletes_the_childs_vector_without_a_data_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "vector-keep-owner", "device-vector-keep"
        )
        claim_id = await _seed_child_vector(app, owner_id, child_id)

        # No purge_subject_data: unbind always erases now (#283).
        unbound = await client.post(
            "/v1/devices/device-vector-keep/binding/unbind",
            headers=headers,
            json={"reason": "unbind"},
        )

        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["subject_deletion"] == "completed"
        assert _vector_count(owner_id, claim_id) == 0


@pytest.mark.asyncio
async def test_unbind_purging_subject_data_deletes_the_childs_vector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "vector-purge-owner", "device-vector-purge"
        )
        claim_id = await _seed_child_vector(app, owner_id, child_id)
        settings = app.state.settings
        layers = ChildLayerStore(
            api_dsn=settings.memory_api_database_url.get_secret_value(),
            maintenance_dsn=settings.memory_maintenance_database_url.get_secret_value(),
        )
        await layers.initialize()
        app.state.child_layer_store = layers
        try:
            record = await layers.write_summary(
                DialogueSummary(
                    subject_id=child_id,
                    text="孩子聊了天气",
                    source_event_id="vector-delete-api-summary",
                    session_id="vector-delete-api-summary-session",
                ),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="vector-delete-api-consent",
            )
            app_store.execute(
                "",
                "UPDATE memory_records SET deletion_locked = true WHERE record_id = ?",
                (record.record_id,),
                bypass_triggers=True,
            )
            locked = app_store.fetch_all(
                "",
                "SELECT deletion_locked FROM memory_records WHERE record_id = ?",
                (record.record_id,),
            )
            assert locked == [(True,)]

            unbound = await client.post(
                "/v1/devices/device-vector-purge/binding/unbind",
                headers=headers,
                json={"reason": "unbind"},
            )

            assert unbound.status_code == 200, unbound.text
            assert unbound.json()["subject_deletion"] == "completed"
            assert _vector_count(owner_id, claim_id) == 0
            events = app_store.fetch_all(
                "",
                "SELECT event_id FROM archive_evidence_events"
                " WHERE account_id = ? AND event_id = ?",
                (owner_id, "vector-delete-api-child-turn"),
            )
            assert events == []
            records = app_store.fetch_all(
                "",
                "SELECT record_id FROM memory_records WHERE record_id = ?",
                (record.record_id,),
            )
            assert records == []
        finally:
            await layers.close()


@pytest.mark.asyncio
async def test_guardian_confirm_restores_the_childs_vector_for_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, child_id, headers = await _bound_child(
            client, app, "vector-guardian-confirm-owner", "device-vector-guardian-confirm"
        )
        await _grant_memory(client, child_id, headers)
        claim_id = await _seed_child_vector(app, owner_id, child_id)
        url = f"/v1/guardian/minors/{child_id}/memories/{claim_id}/review"

        retracted = await client.post(url, headers=headers, json={"action": "retract"})
        assert retracted.status_code == 200, retracted.text
        assert _vector_count(owner_id, claim_id) == 0

        started = time.monotonic()
        confirmed = await client.post(url, headers=headers, json={"action": "confirm"})
        recalled = await app.state.memory_catalog.context(
            MemorySearchQuery(
                account_id=owner_id,
                subject_id=child_id,
                speaker_class="owner",
                text="天空的颜色",
                kinds=("claim",),
                include_candidates=False,
            )
        )
        elapsed = time.monotonic() - started

        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"
        assert elapsed < 120, f"confirmation to recall took {elapsed:.3f}s"
        assert [item.item_id for item in recalled.items] == [claim_id]
        assert _vector_count(owner_id, claim_id) == 1
        vectors = app_store.fetch_all(
            "",
            "SELECT embedding_model, embedding_dimensions FROM memory_vector_documents"
            " WHERE account_id = ? AND item_id = ?",
            (owner_id, claim_id),
        )
        assert vectors == [("vector-delete-api-test-v1", 2)]


@pytest.mark.asyncio
async def test_archive_correction_reembeds_before_the_next_context_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_id, headers = await _owner(client, app, "vector-correction-owner")
        claim_id = await _seed_owner_claim(
            app, owner_id, "vector-correction-turn", "我喜欢蓝色。"
        )
        catalog = app.state.memory_catalog
        query = MemorySearchQuery(
            account_id=owner_id,
            subject_id=owner_id,
            speaker_class="owner",
            text="天边的火烧云",
            kinds=("claim",),
            include_candidates=False,
        )
        assert (await catalog.context(query)).items == ()

        started = time.monotonic()
        corrected = await client.post(
            f"/v1/archive/memories/{claim_id}/review",
            headers=headers,
            json={"action": "correct", "corrected_value": "晚霞是红色。"},
        )
        recalled = await catalog.context(query)
        elapsed = time.monotonic() - started

        dsn = app.state.settings.archive_database_url.get_secret_value()
        lexical_only = PostgresMemoryCatalog(
            dsn,
            extractor=RuleBasedMemoryExtractor(),
            embedder=None,
            evidence_subject_category_resolver=lambda _event: "adult",
        )
        await lexical_only.initialize()
        try:
            lexical = await lexical_only.context(query)
        finally:
            await lexical_only.close()

    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["value"] == "晚霞是红色。"
    assert elapsed < 120, f"correction to recall took {elapsed:.3f}s"
    assert [item.item_id for item in recalled.items] == [claim_id]
    assert recalled.items[0].snippet == "晚霞是红色。"
    # The same query has no lexical overlap with the corrected row, so only
    # the vector written by the correction can recall it.
    assert lexical.items == ()
