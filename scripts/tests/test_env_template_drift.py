"""Every setting a service reads is named in the production env template.

The template is hand-annotated and shared by all services, so it is checked
rather than generated: a new settings alias must be added to
``infra/memoria.env.production.example`` (a commented ``# KEY=default`` line
is enough) or listed below with the reason production never sets it.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.split_production_env import _aliases
from services.agent.src.config import AgentSettings
from services.control_api.app.config import ControlSettings
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.miniprogram_gateway.config import MiniProgramGatewaySettings

TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "memoria.env.production.example"

# Read by settings, deliberately absent from the production template.
NOT_IN_PRODUCTION = {
    # SQLite development twins; production sets the PostgreSQL DSNs.
    "MEMORIA_CONSENT_DB_PATH": "SQLite development store",
    "MEMORIA_EVOLUTION_DB_PATH": "SQLite development store",
    "MEMORIA_IDENTITY_DB_PATH": "SQLite development store",
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
    for settings in (
        ControlSettings,
        AgentSettings,
        MiniProgramGatewaySettings,
        DeviceMediaGatewaySettings,
    ):
        aliases |= _aliases(settings)
    missing = sorted(
        key for key in aliases if key not in NOT_IN_PRODUCTION and not _named(template, key)
    )
    assert missing == [], f"add these to {TEMPLATE.name} (or NOT_IN_PRODUCTION): {missing}"


def test_the_exemption_list_names_only_real_settings() -> None:
    aliases = _aliases(ControlSettings) | _aliases(AgentSettings)
    assert set(NOT_IN_PRODUCTION) <= aliases
