from __future__ import annotations

import json
from pathlib import Path

from services.agent.src.orchestration.prosody import cosyvoice_instruction
from services.agent.src.providers.cosyvoice_voice_catalog import (
    VOICE_DESIGN_CATALOG,
    catalog_by_id,
    resolve_voice_id,
    uses_freeform_instruct,
)


def test_catalog_has_default_warm_companion() -> None:
    by_id = catalog_by_id()
    assert "warm_companion" in by_id
    assert by_id["warm_companion"].recommended
    assert all(len(spec.prefix) <= 10 for spec in VOICE_DESIGN_CATALOG)
    assert all(len(spec.voice_prompt) <= 500 for spec in VOICE_DESIGN_CATALOG)


def test_freeform_vs_fixed_instructions() -> None:
    fixed = cosyvoice_instruction("happy", freeform=False)
    free = cosyvoice_instruction("happy", freeform=True)
    assert fixed == "你正在进行闲聊互动，你说话的情感是happy。"
    assert "轻松愉快" in free
    assert "你说话的情感是" not in free


def test_uses_freeform_auto_for_v3_5_and_designed_ids() -> None:
    assert uses_freeform_instruct(
        model="cosyvoice-v3.5-flash",
        voice="cosyvoice-v3.5-flash-vd-warmboy-x",
    )
    assert uses_freeform_instruct(
        model="cosyvoice-v3-flash",
        voice="cosyvoice-v3-flash-vd-clone-x",
    )
    assert not uses_freeform_instruct(
        model="cosyvoice-v3-flash",
        voice="longanyang",
    )


def test_resolve_voice_id_from_registry(tmp_path: Path) -> None:
    path = tmp_path / "designed_voice_ids.json"
    path.write_text(
        json.dumps(
            {
                "target_model": "cosyvoice-v3.5-flash",
                "voices": {
                    "warm_companion": {
                        "voice_id": "cosyvoice-v3.5-flash-vd-warmboy-xyz",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert (
        resolve_voice_id(profile_id="warm_companion", registry_path=path)
        == "cosyvoice-v3.5-flash-vd-warmboy-xyz"
    )
    assert resolve_voice_id(
        explicit_voice="cosyvoice-v3.5-flash-vd-other",
        registry_path=path,
    ) == "cosyvoice-v3.5-flash-vd-other"
