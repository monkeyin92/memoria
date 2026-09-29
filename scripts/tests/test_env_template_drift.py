"""Every setting a service reads is named in the production env template.

The template is hand-annotated and shared by all services, so it is checked
rather than generated: a new settings alias must be added to
``infra/memoria.env.production.example`` (a commented ``# KEY=default`` line
is enough) or listed below with the reason production never sets it.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.split_production_env import _RETIRED_KEYS, _aliases
from services.agent.src.config import AgentSettings
from services.control_api.app.config import ControlSettings

TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "memoria.env.production.example"

# Read by settings, deliberately absent from the production template.
NOT_IN_PRODUCTION = {
    # SQLite development twins; production sets the PostgreSQL DSNs.
    "MEMORIA_CONSENT_DB_PATH": "SQLite development store",
    "MEMORIA_EVOLUTION_DB_PATH": "SQLite development store",
    # Baked into the image by the release build (Dockerfile ARG).
    "MEMORIA_RELEASE_TAG": "set by the image build",
    # Test-only switch (production environments ignore it).
    "MEMORIA_EAGER_POSTGRES": "test harness only",
    # Legacy all-access token; split_production_env refuses it in production.
    "MEMORIA_ARCHIVE_INTERNAL_TOKEN": "forbidden in production",
    # Admin DSNs for schema bootstrap: the upgrade helper strips them from the
    # long-lived env (the template says so in words, not as a KEY= line).
    "MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL": "root-only upgrade workflow",
    "MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL": "root-only upgrade workflow",
}


def _named(template: str, key: str) -> bool:
    return re.search(rf"^#?\s*{re.escape(key)}=", template, re.MULTILINE) is not None


def test_every_settings_alias_is_in_the_production_template() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    aliases: set[str] = set()
    for settings in (ControlSettings, AgentSettings):
        aliases |= _aliases(settings)
    missing = sorted(
        key for key in aliases if key not in NOT_IN_PRODUCTION and not _named(template, key)
    )
    assert missing == [], f"add these to {TEMPLATE.name} (or NOT_IN_PRODUCTION): {missing}"


def test_the_exemption_list_names_only_real_settings() -> None:
    aliases = _aliases(ControlSettings) | _aliases(AgentSettings)
    assert set(NOT_IN_PRODUCTION) <= aliases


def test_retired_media_chain_keys_are_gone_from_both_templates() -> None:
    """LiveKit, its worker and the Python media gateways were retired.

    split_production_env still accepts these keys from old operator files and
    routes them nowhere, but the templates must not advertise them again.
    """

    aliases = _aliases(ControlSettings) | _aliases(AgentSettings)
    retired = {
        key
        for key in _RETIRED_KEYS
        if key.startswith(("LIVEKIT_", "MINIPROGRAM_GATEWAY_", "DEVICE_MEDIA_GATEWAY_"))
        or key
        in {
            "DEVICE_MEDIA_RUNTIME",
            "MEDIA_RUNTIME_DEFAULT",
            "MINIPROGRAM_MEDIA_GATEWAY_URL",
            "MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET",
            "MEMORIA_DEVICE_GATEWAY_TICKET_SECRET",
        }
    }
    assert retired
    # A retired key that a service still reads would be silently dropped.
    assert not set(_RETIRED_KEYS) & aliases
    for template_path in (TEMPLATE, TEMPLATE.parents[1] / ".env.example"):
        template = template_path.read_text(encoding="utf-8")
        named = sorted(key for key in retired if _named(template, key))
        assert named == [], f"{template_path.name} still names retired keys: {named}"
