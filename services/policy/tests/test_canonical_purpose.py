from __future__ import annotations

import pytest
from services.policy.context import (
    CapabilityScope,
    canonical_purpose_for_capability,
    capability_scope_for,
    is_resource_scoped_action,
)
from services.policy.tests.fakes import make_context


@pytest.mark.parametrize(
    ("capability", "purpose"),
    [
        ("memory_capture", "memory_capture"),
        ("memory_promotion", "memory_promotion"),
        ("family_shared_memory_proposal", "family_shared_memory_proposal"),
        ("family_shared_memory_approval", "family_shared_memory_approval"),
        ("family_shared_memory_promotion", "family_shared_memory_promotion"),
    ],
)
def test_action_resource_capability_has_one_canonical_purpose(
    capability: str, purpose: str
) -> None:
    assert canonical_purpose_for_capability(capability) == purpose
    assert canonical_purpose_for_capability(
        capability, fallback_purpose=purpose
    ) == purpose
    with pytest.raises(ValueError, match="canonical"):
        canonical_purpose_for_capability(
            capability, fallback_purpose="runtime_profile_issue"
        )


@pytest.mark.parametrize(
    ("capability", "purpose"),
    [
        ("chat", "runtime_profile_issue"),
        ("voice_clone_use", "voice_clone"),
        ("device_ownership_transfer", "device_transfer"),
    ],
)
def test_non_action_capability_requires_explicit_valid_fallback(
    capability: str, purpose: str
) -> None:
    assert canonical_purpose_for_capability(
        capability, fallback_purpose=purpose
    ) == purpose
    with pytest.raises(ValueError, match="fallback_purpose"):
        canonical_purpose_for_capability(capability)


@pytest.mark.parametrize(
    ("capability", "purpose"),
    [
        ("unknown_capability", "user_request"),
        ("chat", "unknown_purpose"),
        ("chat", "memory_capture"),
    ],
)
def test_canonical_purpose_resolver_fails_closed_for_unknown_or_cross_action(
    capability: str, purpose: str
) -> None:
    with pytest.raises(ValueError):
        canonical_purpose_for_capability(capability, fallback_purpose=purpose)


@pytest.mark.parametrize(
    ("capability", "scope"),
    [
        ("chat", CapabilityScope.SESSION_LEVEL),
        ("memory_capture", CapabilityScope.SESSION_LEVEL),
        ("memory_promotion", CapabilityScope.RESOURCE_SCOPED_ACTION),
        (
            "family_shared_memory_proposal",
            CapabilityScope.RESOURCE_SCOPED_ACTION,
        ),
        (
            "family_shared_memory_approval",
            CapabilityScope.RESOURCE_SCOPED_ACTION,
        ),
        (
            "family_shared_memory_promotion",
            CapabilityScope.RESOURCE_SCOPED_ACTION,
        ),
    ],
)
def test_capability_scope_is_canonical_and_unknown_fails_closed(
    capability: str, scope: CapabilityScope
) -> None:
    assert capability_scope_for(capability) is scope
    assert is_resource_scoped_action(capability) is (
        scope is CapabilityScope.RESOURCE_SCOPED_ACTION
    )
    with pytest.raises(ValueError):
        capability_scope_for("unknown-capability")


@pytest.mark.parametrize(
    "capability",
    [
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
    ],
)
def test_resource_action_cannot_be_profile_time_or_use_default_fence(
    capability: str,
) -> None:
    with pytest.raises(ValueError, match="canonical action purpose"):
        make_context(capability=capability, purpose="runtime_profile_issue")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="explicit canonical action_resource_fence"):
        make_context(capability=capability, purpose=capability)  # type: ignore[arg-type]
