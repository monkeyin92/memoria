from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from services.control_api.app.config import ControlSettings
from services.control_api.app.main import _archive_object_store, _voice_profile_services


@pytest.mark.asyncio
async def test_archive_and_voice_factories_read_rotated_objects(tmp_path: Path) -> None:
    archive_old_key = Fernet.generate_key().decode("ascii")
    archive_active_key = Fernet.generate_key().decode("ascii")
    voice_old_key = Fernet.generate_key().decode("ascii")
    voice_active_key = Fernet.generate_key().decode("ascii")
    common = {
        "MEMORIA_ARCHIVE_OBJECT_STORE_PATH": str(tmp_path / "archive"),
        "MEMORIA_VOICE_SAMPLE_STORE_PATH": str(tmp_path / "voice"),
        "MEMORIA_DB_PATH": str(tmp_path / "memoria.sqlite3"),
    }
    old_settings = ControlSettings(
        _env_file=None,
        **common,
        MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY=archive_old_key,
        MEMORIA_ARCHIVE_OBJECT_KEY_VERSION="archive-v1",
        MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY=voice_old_key,
        MEMORIA_VOICE_SAMPLE_KEY_VERSION="voice-v1",
    )
    old_archive = _archive_object_store(old_settings)
    _, _, old_voice = _voice_profile_services(old_settings)
    archive_reference = await old_archive.put(
        account_id="owner-1",
        purpose="raw-voice-archive",
        data=b"old archive object",
        media_type="audio/wav",
    )
    voice_reference = await old_voice.put(
        account_id="owner-1",
        purpose="voice-clone-sample",
        data=b"old voice object",
        media_type="audio/wav",
    )

    rotated_settings = ControlSettings(
        _env_file=None,
        **common,
        MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY=archive_active_key,
        MEMORIA_ARCHIVE_OBJECT_KEY_VERSION="archive-v2",
        MEMORIA_ARCHIVE_OBJECT_READ_KEYS=json.dumps({"archive-v1": archive_old_key}),
        MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY=voice_active_key,
        MEMORIA_VOICE_SAMPLE_KEY_VERSION="voice-v2",
        MEMORIA_VOICE_SAMPLE_READ_KEYS=json.dumps({"voice-v1": voice_old_key}),
    )
    rotated_archive = _archive_object_store(rotated_settings)
    _, _, rotated_voice = _voice_profile_services(rotated_settings)

    assert await rotated_archive.get(archive_reference) == b"old archive object"
    assert await rotated_voice.get(voice_reference) == b"old voice object"
