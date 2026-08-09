from services.guardian.retention import (
    apply_memory_retention_ceiling,
    memory_retention_allowed,
)


def test_minor_retention_requires_explicit_consent_and_missing_category_fails_closed() -> None:
    assert memory_retention_allowed(subject_category="adult", active_consent=False) is True
    assert memory_retention_allowed(subject_category="minor", active_consent=True) is True
    assert memory_retention_allowed(subject_category="minor", active_consent=False) is False
    assert memory_retention_allowed(subject_category=None, active_consent=True) is False


def test_retention_ceiling_only_removes_persistent_capabilities() -> None:
    context = {
        "history_eligible": True,
        "owner_projection_eligible": True,
        "capabilities": {
            "conversation": True,
            "private_memory": True,
            "persona": True,
            "persona_low_sensitivity": True,
            "tools": True,
            "history": True,
            "learning": True,
        },
    }

    fenced = apply_memory_retention_ceiling(context, allowed=False)

    assert fenced["capabilities"]["conversation"] is True
    assert fenced["capabilities"]["tools"] is True
    assert fenced["capabilities"]["private_memory"] is False
    assert fenced["capabilities"]["learning"] is False
    assert fenced["history_eligible"] is False
    assert fenced["owner_projection_eligible"] is False
    assert fenced["memory_retention"] == "ephemeral_only"
