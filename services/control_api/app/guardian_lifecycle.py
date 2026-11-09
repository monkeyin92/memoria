"""Irreversible child-data operations behind a proven guardian binding.

Binding a subject as a minor revokes their legacy grants, voice profiles and
speaker authority; clearing or deleting a child's data purges raw voice blobs
and erases a stored kind. These operations run only after the router has proven
the caller's binding, so the router keeps the guards and the effects live here.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException, Request

from services.archive.domain import LifeArchivePort, RawVoiceConsentRequiredError
from services.archive.object_store import ObjectStore
from services.guardian.child_memory import ChildMemoryKind
from services.legacy.domain import LegacyRegistryPort
from services.memory_scope.child_layers import ChildLayerStore
from services.speaker.domain import RevokeSpeakerProfile, SpeakerAuthorityPort
from services.voice_profile.domain import VoiceProfilePort

__all__ = [
    "deletion_confirmed",
    "disable_minor_restricted_assets",
    "erase_child_kind",
    "purge_minor_raw_voice",
]



def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _archive_objects(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.archive_object_store)


def _child_layers(request: Request) -> ChildLayerStore:
    store = getattr(request.app.state, "child_layer_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail={"code": "child_layers_unavailable"})
    return cast(ChildLayerStore, store)


def _now() -> datetime:
    return datetime.now(UTC)


_DELETE_CONFIRMATION = "永久删除孩子的全部数据"


async def disable_minor_restricted_assets(request: Request, account_id: str) -> None:
    now = _now()
    legacy = cast(LegacyRegistryPort, request.app.state.legacy_registry)
    for grant in await legacy.list_grants(actor_account_id=account_id, role="owner"):
        if grant.revoked_at is None:
            await legacy.revoke(
                actor_account_id=account_id,
                grant_id=grant.grant_id,
                expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,
                idempotency_key=f"minor-transition:{grant.grant_id}",
                now=now,
            )
    voice = cast(VoiceProfilePort, request.app.state.voice_profile_manager)
    for voice_profile in await voice.profiles(account_id=account_id):
        if voice_profile.status != "revoked":
            await voice.revoke_profile(
                account_id=account_id,
                profile_id=voice_profile.profile_id,
            )
    consent = await voice.consent(account_id=account_id)
    if consent is not None and consent.revoked_at is None:
        await voice.revoke_consent(account_id=account_id)
    speaker = cast(SpeakerAuthorityPort, request.app.state.speaker_authority)
    for speaker_profile in await speaker.profiles(account_id):
        if speaker_profile.status != "revoked":
            await speaker.revoke(
                RevokeSpeakerProfile(
                    account_id=account_id,
                    profile_id=speaker_profile.profile_id,
                    reason="subject_transition_to_minor",
                )
            )


def deletion_confirmed(confirmation: str) -> bool:
    # compare_digest rejects non-ASCII str, and the phrase is Chinese.
    return secrets.compare_digest(
        confirmation.encode("utf-8"), _DELETE_CONFIRMATION.encode("utf-8")
    )


async def purge_minor_raw_voice(request: Request, account_id: str) -> None:
    archive = _archive(request)
    references = await archive.raw_voice_blobs(account_id=account_id)
    active = await archive.active_raw_voice_consent(account_id=account_id)
    if active is not None:
        try:
            revocation = await archive.revoke_raw_voice_consent(
                account_id=account_id,
                revoked_at=_now(),
            )
            references = tuple({item.object_key: item for item in (*references, *revocation.references)}.values())
        except RawVoiceConsentRequiredError:
            pass
    if not references:
        return
    for reference in references:
        await _archive_objects(request).delete(reference)
    await archive.purge_raw_voice_blobs(
        account_id=account_id,
        object_keys=tuple(reference.object_key for reference in references),
    )


async def erase_child_kind(
    request: Request,
    *,
    subject_id: str,
    kind: ChildMemoryKind,
    incomplete_code: str,
) -> dict[str, Any]:
    layers = _child_layers(request)
    erased = await layers.erase_kind(subject_id=subject_id, kind=kind, only_expired=False)
    remaining = await layers.remaining_kind(
        subject_id=subject_id, kind=kind, only_expired=False
    )
    if int(remaining.get("memory_records", 0)):
        raise HTTPException(status_code=500, detail={"code": incomplete_code})
    return {"deleted": True, "kind": kind, "erased": dict(erased), "remaining": dict(remaining)}
