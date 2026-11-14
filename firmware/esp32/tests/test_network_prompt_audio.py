"""Check the shipping audio and reject a tag-only replacement with matching hashes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from scripts.render_network_prompt_audio import IDS, MANIFEST, ROOT, check
from services.common.aigc_opus import audio_packets_digest


def test_shipping_network_prompts_decode_and_match_provenance() -> None:
    check()


def test_unchanged_audio_packets_are_rejected_even_with_matching_file_hash(tmp_path: Path) -> None:
    shutil.copytree(ROOT, tmp_path / "lines")
    root = tmp_path / "lines"
    path = root / MANIFEST.name
    manifest = json.loads(path.read_text())
    clip_id = IDS[0]
    blob = (root / f"{clip_id}.ogg").read_bytes()
    # Model a metadata-only replacement: the complete file and packet hashes
    # agree, but packets are identical to the pre-replacement audio.
    record = manifest["clips"][clip_id]
    record["sha256"] = hashlib.sha256(blob).hexdigest()
    record["previous_audio_packets_sha256"] = audio_packets_digest(blob)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="provenance mismatch"):
        check(root)


def test_changed_audio_file_is_rejected(tmp_path: Path) -> None:
    shutil.copytree(ROOT, tmp_path / "lines")
    root = tmp_path / "lines"
    path = root / f"{IDS[0]}.ogg"
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="provenance mismatch"):
        check(root)
