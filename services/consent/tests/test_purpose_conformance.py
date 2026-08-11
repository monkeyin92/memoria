"""Consent purpose vocabulary is the generated canonical object-v2 vocabulary."""

from packages.contracts.generated.python.multi_subject_contracts import ALL_PURPOSE_VALUES
from services.consent.evidence import ALLOWED_PURPOSES


def test_consent_purposes_exactly_match_generated_v2() -> None:
    assert ALLOWED_PURPOSES == frozenset(item.value for item in ALL_PURPOSE_VALUES)
