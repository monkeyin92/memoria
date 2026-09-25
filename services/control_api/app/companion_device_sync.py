"""Make the account's companion pick reach the device(s) it administers.

One device serves one bound person, and the mini program's companion picker
(``profiles.companion_id``) is the authority for who that device speaks as.  A
device session, however, takes its persona from the signed Runtime Profile,
which resolves the binding's per-subject override first and the binding default
second.  So a pick is written as the per-subject override for each binding's
primary subject, through the same audited ``set_persona_assignment`` path the
persona-assignment API uses.

The binding default is deliberately left alone: changing it means superseding
the binding (a new ``binding_version``), which fences every live session and
re-runs activation -- far too heavy for a style choice.  The override is
binding+subject scoped, idempotent on replay, audited, and already re-read by
both Runtime Profile authorities without a binding version bump.

Nothing here may fail the caller's write: every step logs and continues.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import Request

from services.control_api.app.routes.device_control import (
    project_device_profile_change,
)
from services.identity.domain import BindingManifest
from services.identity.service import IdentityService

logger = logging.getLogger(__name__)


def _persona_id(assignment_id: str | None) -> str:
    return (assignment_id or "").partition(":v")[0]


async def project_persona_change(
    request: Request,
    *,
    device_id: str,
    actor_id: str,
    now: datetime,
) -> bool:
    """Rotate the device's control profile and project it for the next session.

    ``ensure_profile`` notices the persona write (it compares the issued
    profile against the assignment authority) and rotates while keeping the
    confirmed subject.  The projection then advances the device-visible profile
    version with ``next_session`` semantics: an online device is told, but a
    conversation in progress is never cut mid-reply -- the next session simply
    starts with the new persona.  Returns whether the Edge took the notice.
    """
    control = getattr(request.app.state, "multi_subject_runtime", None)
    if control is None:
        return False
    try:
        profile = await control.ensure_profile(
            device_id=device_id,
            session_id=None,
            actor_id=actor_id,
            now=now,
        )
        payload = control.serialize_profile(profile)
    except Exception as exc:
        logger.warning(
            "persona change projection deferred device_id=%s error=%s",
            device_id,
            type(exc).__name__,
        )
        return False
    return await project_device_profile_change(
        request, payload=payload, now=now, apply_at="next_session"
    )


async def _assign_primary_subject(
    identity: IdentityService,
    *,
    manifest: BindingManifest,
    subject_id: str,
    companion_id: str,
    actor_id: str,
    now: datetime,
) -> bool:
    """Pin ``companion_id`` to one subject; ``False`` when it already resolves so."""
    existing = await identity.get_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=subject_id,
        actor_person_id=actor_id,
    )
    if existing is not None:
        if existing.persona_id == companion_id:
            return False
    elif _persona_id(manifest.persona_assignment_id) == companion_id:
        # No override and the binding default already is this companion.
        return False
    await identity.set_persona_assignment(
        binding_id=manifest.binding_id,
        subject_id=subject_id,
        persona_selection=companion_id,
        actor_person_id=actor_id,
        now=now,
    )
    return True


async def apply_companion_to_bound_devices(
    request: Request,
    *,
    account_id: str,
    companion_id: str,
    now: datetime,
) -> tuple[str, ...]:
    """Apply the account's companion to every device it owns or administers.

    Only bindings where the account is the account owner or a device admin are
    touched -- the same narrowing the persona-assignment write route enforces.
    Returns the device ids whose persona actually changed.
    """
    identity = getattr(request.app.state, "identity_service", None)
    if not isinstance(identity, IdentityService):
        return ()
    try:
        manifests = await identity.list_active_manifests_for_person(
            account_id, now, actor_person_id=account_id
        )
    except Exception as exc:
        logger.warning(
            "companion device sync skipped: bindings unreadable error=%s",
            type(exc).__name__,
        )
        return ()
    changed_devices: list[str] = []
    for manifest in manifests:
        if account_id not in {manifest.account_owner_id, *manifest.device_admin_ids}:
            continue
        changed = False
        for subject_id in manifest.primary_subject_ids:
            try:
                changed = (
                    await _assign_primary_subject(
                        identity,
                        manifest=manifest,
                        subject_id=subject_id,
                        companion_id=companion_id,
                        actor_id=account_id,
                        now=now,
                    )
                    or changed
                )
            except Exception as exc:
                logger.warning(
                    "companion device sync failed device_id=%s error=%s",
                    manifest.device_id,
                    type(exc).__name__,
                )
        if not changed:
            continue
        changed_devices.append(manifest.device_id)
        await project_persona_change(
            request, device_id=manifest.device_id, actor_id=account_id, now=now
        )
        logger.info(
            "companion applied to device device_id=%s companion_id=%s",
            manifest.device_id,
            companion_id,
        )
    return tuple(changed_devices)


__all__ = ["apply_companion_to_bound_devices", "project_persona_change"]
